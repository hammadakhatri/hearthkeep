"""Passkey credential repository: enrolled devices, their public keys and sign counts."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from hearthkeep import audit, db
from hearthkeep.auth.enrollment import EnrollmentCodes, Purpose
from hearthkeep.auth.sessions import SessionStore
from hearthkeep.auth.webauthn import NewCredential, StoredCredential

DEVICE_NAME_MAX = 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Credential:
    id: str
    public_key: bytes
    sign_count: int
    device_name: str
    created_at: str

    def stored(self) -> StoredCredential:
        return StoredCredential(id=self.id, public_key=self.public_key, sign_count=self.sign_count)


def _from_row(row: sqlite3.Row) -> Credential:
    return Credential(
        id=row["id"],
        public_key=bytes(row["public_key"]),
        sign_count=int(row["sign_count"] or 0),
        device_name=row["device_name"] or "",
        created_at=row["created_at"],
    )


def clean_device_name(name: object) -> str:
    text = " ".join(str(name or "").split())[:DEVICE_NAME_MAX]
    return text or "Unnamed device"


class CredentialStore:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], datetime] = _utcnow):
        self._conn = conn
        self._clock = clock

    def add_in(
        self, conn: sqlite3.Connection, credential: NewCredential, device_name: str
    ) -> Credential:
        """Insert a credential; the caller holds an open `db.transaction` on `conn`.
        Raises sqlite3.IntegrityError if the credential id already exists."""
        now = _iso(self._clock())
        name = clean_device_name(device_name)
        conn.execute(
            "INSERT INTO credentials (id, public_key, sign_count, device_name, created_at,"
            " revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
            (credential.id, credential.public_key, credential.sign_count, name, now),
        )
        audit.record(
            conn, "owner", "credential.added", {"credential_id": credential.id, "device": name}
        )
        return Credential(credential.id, credential.public_key, credential.sign_count, name, now)

    def unrevoked(self) -> list[Credential]:
        rows = self._conn.execute(
            "SELECT id, public_key, sign_count, device_name, created_at FROM credentials"
            " WHERE revoked_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [_from_row(r) for r in rows]

    def get(self, credential_id: object) -> Credential | None:
        """The credential if it exists and is not revoked."""
        if not isinstance(credential_id, str):
            return None
        row = self._conn.execute(
            "SELECT id, public_key, sign_count, device_name, created_at FROM credentials"
            " WHERE id = ? AND revoked_at IS NULL",
            (credential_id,),
        ).fetchone()
        return _from_row(row) if row is not None else None

    def update_sign_count(self, credential_id: str, sign_count: int) -> None:
        self._conn.execute(
            "UPDATE credentials SET sign_count = ? WHERE id = ? AND revoked_at IS NULL",
            (int(sign_count), credential_id),
        )

    def revoke(self, credential_id: str, sessions: SessionStore) -> bool:
        """Revoke an unrevoked credential and all its sessions. False if there was none."""
        if self.get(credential_id) is None:
            return False
        sessions.revoke_credential(credential_id)
        return True


def complete_enrollment(
    conn: sqlite3.Connection,
    codes: EnrollmentCodes,
    credentials: CredentialStore,
    code: str,
    purpose: Purpose,
    credential: NewCredential,
    device_name: str,
) -> Credential | None:
    """Redeem the code and store the credential in ONE transaction on `conn`.

    For a first-device code the "no credential exists yet" rule is re-checked inside the same
    IMMEDIATE transaction, so two racing first-device enrollments cannot both succeed.
    Returns None (and writes nothing) if the code cannot be redeemed.
    """
    with db.transaction(conn):
        if not codes.consume_in(conn, code, purpose):
            return None
        return credentials.add_in(conn, credential, device_name)
