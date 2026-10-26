"""Test doubles shared across test modules."""

import secrets

from hearthkeep.auth.webauthn import (
    NewCredential,
    StoredCredential,
    VerificationError,
    b64url_encode,
)


class FakeVerifier:
    """Deterministic Verifier: a response is valid iff it echoes the expected challenge.

    Registration response: {"id": str, "public_key": bytes, "challenge": bytes}.
    Authentication response: {"id": str, "challenge": bytes, "sign_count": int}.
    """

    def registration_options(self, user_id: bytes) -> dict:
        return {"challenge": b64url_encode(secrets.token_bytes(32)), "user": b64url_encode(user_id)}

    def verify_registration(self, response: dict, challenge: bytes) -> NewCredential:
        if response.get("challenge") != challenge:
            raise VerificationError("challenge mismatch")
        return NewCredential(id=response["id"], public_key=response["public_key"], sign_count=0)

    def authentication_options(self, credential_ids: list[str]) -> dict:
        return {
            "challenge": b64url_encode(secrets.token_bytes(32)),
            "allowCredentials": list(credential_ids),
        }

    def verify_authentication(
        self, response: dict, challenge: bytes, credential: StoredCredential
    ) -> int:
        if response.get("challenge") != challenge or response.get("id") != credential.id:
            raise VerificationError("authentication failed")
        count = response["sign_count"]
        # Real passkeys often report 0 forever; only a decrease (or a repeat of a non-zero
        # count) signals a cloned authenticator.
        if count < credential.sign_count or (count == credential.sign_count and count != 0):
            raise VerificationError("sign count did not increase")
        return response["sign_count"]
