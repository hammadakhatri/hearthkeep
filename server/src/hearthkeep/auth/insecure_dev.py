"""INSECURE development verifier: accepts every passkey response without checking it.

It exists only for `hearthkeep serve --insecure-dev-auth` (local development and the end-to-end
smoke test) and is refused unless `public_origin` is a localhost origin. Never use it otherwise.
"""

import secrets
import sqlite3
from urllib.parse import urlparse

from hearthkeep.auth.webauthn import NewCredential, StoredCredential, b64url_encode
from hearthkeep.config import LOOPBACK_HOSTS, Settings

WARNING = (
    "!!! INSECURE DEV AUTH IS ON: every passkey response is accepted without verification. !!!\n"
    "!!! Anyone who can reach this server with an enrollment code or a known device id can   !!!\n"
    "!!! sign in. Use it only on this computer, for development and the smoke test.         !!!"
)


DEV_PUBLIC_KEY = b"insecure-dev"


class InsecureDevAuthError(Exception):
    """--insecure-dev-auth was requested for a non-localhost origin."""


def check_allowed(settings: Settings, conn: sqlite3.Connection) -> None:
    """Refuse dev auth for a non-localhost origin, or for a data folder that already holds a
    real passkey (anyone could otherwise sign in as that device by sending its id)."""
    host = urlparse(settings.public_origin).hostname
    if host not in LOOPBACK_HOSTS:
        raise InsecureDevAuthError(
            "--insecure-dev-auth is only allowed when public_origin is a localhost origin"
        )
    real = conn.execute(
        "SELECT 1 FROM credentials WHERE revoked_at IS NULL"
        " AND (public_key IS NULL OR public_key != ?) LIMIT 1",
        (DEV_PUBLIC_KEY,),
    ).fetchone()
    if real is not None:
        raise InsecureDevAuthError(
            "--insecure-dev-auth refuses to run on a data folder with a real passkey enrolled; "
            "use a separate data folder for development"
        )


class InsecureDevVerifier:
    def registration_options(
        self, user_id: bytes, exclude_credential_ids: list[str] | None = None
    ) -> dict:
        return {
            "challenge": b64url_encode(secrets.token_bytes(32)),
            "user": {"id": b64url_encode(user_id), "name": "owner", "displayName": "Owner"},
            "excludeCredentials": [
                {"id": c, "type": "public-key"} for c in exclude_credential_ids or []
            ],
            "insecureDevAuth": True,
        }

    def verify_registration(self, response: dict, challenge: bytes) -> NewCredential:
        cred_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(cred_id, str) or not cred_id:
            cred_id = b64url_encode(secrets.token_bytes(16))
        return NewCredential(id=cred_id, public_key=DEV_PUBLIC_KEY, sign_count=0)

    def authentication_options(self, credential_ids: list[str]) -> dict:
        return {
            "challenge": b64url_encode(secrets.token_bytes(32)),
            "allowCredentials": [{"id": c, "type": "public-key"} for c in credential_ids],
            "insecureDevAuth": True,
        }

    def verify_authentication(
        self, response: dict, challenge: bytes, credential: StoredCredential
    ) -> int:
        return credential.sign_count
