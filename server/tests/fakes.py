"""Test doubles shared across test modules."""

import copy
import secrets

from hearthkeep.auth.webauthn import (
    NewCredential,
    StoredCredential,
    VerificationError,
    b64url_decode,
    b64url_encode,
)


def _as_bytes(value) -> bytes | None:
    """Responses sent over HTTP carry bytes as base64url strings."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        try:
            return b64url_decode(value)
        except VerificationError:
            return None
    return None


class FakeVerifier:
    """Deterministic Verifier: a response is valid iff it echoes the expected challenge.

    Registration response: {"id": str, "public_key": bytes, "challenge": bytes}.
    Authentication response: {"id": str, "challenge": bytes, "sign_count": int}.
    Over HTTP the bytes values are base64url strings.
    """

    def registration_options(
        self, user_id: bytes, exclude_credential_ids: list[str] | None = None
    ) -> dict:
        return {
            "challenge": b64url_encode(secrets.token_bytes(32)),
            "user": b64url_encode(user_id),
            "excludeCredentials": list(exclude_credential_ids or []),
        }

    def verify_registration(self, response: dict, challenge: bytes) -> NewCredential:
        if _as_bytes(response.get("challenge")) != challenge:
            raise VerificationError("challenge mismatch")
        public_key = _as_bytes(response.get("public_key"))
        if public_key is None or not isinstance(response.get("id"), str):
            raise VerificationError("malformed registration response")
        return NewCredential(id=response["id"], public_key=public_key, sign_count=0)

    def authentication_options(self, credential_ids: list[str]) -> dict:
        return {
            "challenge": b64url_encode(secrets.token_bytes(32)),
            "allowCredentials": list(credential_ids),
        }

    def verify_authentication(
        self, response: dict, challenge: bytes, credential: StoredCredential
    ) -> int:
        if _as_bytes(response.get("challenge")) != challenge or response.get("id") != credential.id:
            raise VerificationError("authentication failed")
        count = response["sign_count"]
        # Real passkeys often report 0 forever; only a decrease (or a repeat of a non-zero
        # count) signals a cloned authenticator.
        if count < credential.sign_count or (count == credential.sign_count and count != 0):
            raise VerificationError("sign count did not increase")
        return response["sign_count"]


class FakeChatClient:
    """Scripted chat client: each `stream` call plays the next script entry.

    A script entry is a list of provider events; an Exception instance in the list is raised
    at that point (after the events before it were yielded). Every call records a deep copy
    of the messages and tools it was given.
    """

    def __init__(self, scripts: list[list]):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    async def stream(self, messages, tools, think):
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools), "think": think}
        )
        if not self.scripts:
            raise AssertionError("FakeChatClient ran out of scripted responses")
        for item in self.scripts.pop(0):
            if isinstance(item, BaseException):
                raise item
            yield item
