"""Owner sessions and short-lived step-up tokens.

The session cookie value is the raw session id; the database only holds its SHA-256 hash.
Step-up tokens live in memory (single process) and are bound to a session, an optional
approval, and a purpose.
"""

import hashlib
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from hearthkeep import audit, db

STEP_UP_TTL = timedelta(seconds=60)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    id: str  # the raw id the caller presented
    credential_id: str
    created_at: str
    last_seen_at: str


class SessionStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        idle_days: int,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self._conn = conn
        self._idle = timedelta(days=idle_days)
        self._clock = clock

    def create(self, credential_id: str) -> str:
        session_id = secrets.token_urlsafe(32)
        now = _iso(self._clock())
        with db.transaction(self._conn):
            self._conn.execute(
                "INSERT INTO sessions (id, credential_id, created_at, last_seen_at, revoked_at)"
                " VALUES (?, ?, ?, ?, NULL)",
                (_hash(session_id), credential_id, now, now),
            )
            audit.record(self._conn, "owner", "session.created", {"credential_id": credential_id})
        return session_id

    def validate(self, session_id: str) -> Session | None:
        """Return the live session and refresh `last_seen_at`, or None."""
        now = self._clock()
        with db.transaction(self._conn):
            row = self._conn.execute(
                "SELECT s.credential_id, s.created_at, s.last_seen_at, s.revoked_at,"
                " c.revoked_at AS credential_revoked_at"
                " FROM sessions s JOIN credentials c ON c.id = s.credential_id"
                " WHERE s.id = ?",
                (_hash(session_id),),
            ).fetchone()
            if row is None or row["revoked_at"] or row["credential_revoked_at"]:
                return None
            if datetime.fromisoformat(row["last_seen_at"]) + self._idle < now:
                return None
            self._conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
                (_iso(now), _hash(session_id)),
            )
        return Session(
            id=session_id,
            credential_id=row["credential_id"],
            created_at=row["created_at"],
            last_seen_at=_iso(now),
        )

    def revoke(self, session_id: str) -> None:
        with db.transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_iso(self._clock()), _hash(session_id)),
            )
            if cursor.rowcount:
                audit.record(self._conn, "owner", "session.revoked", {})

    def revoke_credential(self, credential_id: str) -> None:
        """Revoke a passkey and every session it created."""
        now = _iso(self._clock())
        with db.transaction(self._conn):
            self._conn.execute(
                "UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now, credential_id),
            )
            cursor = self._conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE credential_id = ? AND revoked_at IS NULL",
                (now, credential_id),
            )
            audit.record(
                self._conn, "owner", "credential.revoked", {"credential_id": credential_id}
            )
            for _ in range(cursor.rowcount):
                audit.record(
                    self._conn, "owner", "session.revoked", {"reason": "credential_revoked"}
                )


class StepUp:
    """Single-use, 60 second tokens proving a fresh passkey assertion."""

    def __init__(self, clock: Callable[[], datetime] = _utcnow):
        self._clock = clock
        self._tokens: dict[str, tuple[datetime, str, str | None, str]] = {}
        self._lock = threading.Lock()

    def issue(self, session_id: str, approval_id: str | None, purpose: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._tokens = {k: v for k, v in self._tokens.items() if v[0] > now}
            self._tokens[token] = (now + STEP_UP_TTL, session_id, approval_id, purpose)
        return token

    def check(self, token: str, session_id: str, approval_id: str | None, purpose: str) -> bool:
        """Consume the token (even on mismatch) and report whether it matched its binding."""
        with self._lock:
            entry = self._tokens.pop(token, None)
        if entry is None:
            return False
        expires, bound_session, bound_approval, bound_purpose = entry
        return (
            expires > self._clock()
            and secrets.compare_digest(bound_session, session_id)
            and bound_approval == approval_id
            and bound_purpose == purpose
        )
