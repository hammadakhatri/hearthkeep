"""Enrollment codes: short single-use codes that authorise registering a passkey.

Only a SHA-256 hash of a code is stored, and the code never appears in the audit log.
"""

import hashlib
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from hearthkeep import audit, db

Purpose = Literal["first_device", "add_device"]
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def first_device_allowed(conn: sqlite3.Connection) -> bool:
    """True only when no unrevoked credential exists."""
    row = conn.execute("SELECT 1 FROM credentials WHERE revoked_at IS NULL LIMIT 1").fetchone()
    return row is None


class EnrollmentError(Exception):
    """An enrollment code cannot be issued in the current state."""


class EnrollmentCodes:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = _utcnow):
        self._conn = conn
        self._clock = clock

    def first_device_allowed(self) -> bool:
        return first_device_allowed(self._conn)

    def issue(self, purpose: Purpose, ttl_minutes: int) -> str:
        if purpose == "first_device" and not first_device_allowed(self._conn):
            raise EnrollmentError("a passkey already exists; first-device enrollment is closed")
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        expires = self._clock() + timedelta(minutes=ttl_minutes)
        with db.transaction(self._conn):
            self._conn.execute(
                "INSERT INTO enrollment_codes (code_hash, purpose, expires_at, used_at)"
                " VALUES (?, ?, ?, NULL)",
                (_hash(code), purpose, _iso(expires)),
            )
            audit.record(self._conn, "owner", "enrollment.code_issued", {"purpose": purpose})
        return code

    def purpose_of(self, code: str) -> Purpose | None:
        """The purpose of a currently redeemable code, without consuming it."""
        if not isinstance(code, str):
            return None
        row = self._conn.execute(
            "SELECT purpose FROM enrollment_codes"
            " WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
            (_hash(code), _iso(self._clock())),
        ).fetchone()
        if row is None or row["purpose"] not in ("first_device", "add_device"):
            return None
        if row["purpose"] == "first_device" and not first_device_allowed(self._conn):
            return None
        return row["purpose"]

    def consume(self, code: str, purpose: Purpose) -> bool:
        """Atomically redeem a code. False if unknown, used, expired, or the wrong purpose."""
        with db.transaction(self._conn):
            return self.consume_in(self._conn, code, purpose)

    def consume_in(self, conn: sqlite3.Connection, code: str, purpose: Purpose) -> bool:
        """`consume` for a caller that already holds an open `db.transaction` on `conn`, so the
        redemption commits (or rolls back) together with the caller's other writes."""
        if not isinstance(code, str):
            return False
        now = _iso(self._clock())
        if purpose == "first_device" and not first_device_allowed(conn):
            return False
        cursor = conn.execute(
            "UPDATE enrollment_codes SET used_at = ?"
            " WHERE code_hash = ? AND purpose = ? AND used_at IS NULL AND expires_at > ?",
            (now, _hash(code), purpose, now),
        )
        if cursor.rowcount != 1:
            return False
        audit.record(conn, "owner", "enrollment.code_used", {"purpose": purpose})
        return True
