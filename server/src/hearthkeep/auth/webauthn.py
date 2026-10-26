"""WebAuthn (passkey) verification behind a small protocol, plus server-side challenges.

`PyWebAuthnVerifier` wraps the py_webauthn library. Everything else in Hearthkeep talks to the
`Verifier` protocol so behaviour can be tested with a fake.
"""

import base64
import binascii
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import webauthn
from webauthn.helpers import options_to_json_dict
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from hearthkeep.config import Settings

RP_NAME = "Hearthkeep"
CHALLENGE_TTL = timedelta(minutes=5)

Clock = Callable[[], datetime]
Ceremony = Literal["register", "login", "step_up"]
Binding = tuple[str, str | None, str | None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class VerificationError(Exception):
    """A passkey registration or authentication response did not verify."""


@dataclass(frozen=True)
class NewCredential:
    id: str  # base64url credential id
    public_key: bytes
    sign_count: int


@dataclass(frozen=True)
class StoredCredential:
    id: str
    public_key: bytes
    sign_count: int


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise VerificationError("invalid base64url value") from exc


class Verifier(Protocol):
    def registration_options(self, user_id: bytes) -> dict: ...

    def verify_registration(self, response: dict, challenge: bytes) -> NewCredential: ...

    def authentication_options(self, credential_ids: list[str]) -> dict: ...

    def verify_authentication(
        self, response: dict, challenge: bytes, credential: StoredCredential
    ) -> int: ...


class PyWebAuthnVerifier(Verifier):
    """Verifier backed by py_webauthn. User verification is always required."""

    def __init__(self, settings: Settings):
        self._rp_id = settings.rp_id
        self._origin = settings.public_origin

    def registration_options(self, user_id: bytes) -> dict:
        options = webauthn.generate_registration_options(
            rp_id=self._rp_id,
            rp_name=RP_NAME,
            user_name="owner",
            user_display_name="Owner",
            user_id=user_id,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return options_to_json_dict(options)

    def verify_registration(self, response: dict, challenge: bytes) -> NewCredential:
        try:
            verified = webauthn.verify_registration_response(
                credential=json.dumps(response),
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
            raise VerificationError(f"registration failed: {exc}") from exc
        return NewCredential(
            id=b64url_encode(verified.credential_id),
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
        )

    def authentication_options(self, credential_ids: list[str]) -> dict:
        options = webauthn.generate_authentication_options(
            rp_id=self._rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=b64url_decode(cid)) for cid in credential_ids
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return options_to_json_dict(options)

    def verify_authentication(
        self, response: dict, challenge: bytes, credential: StoredCredential
    ) -> int:
        try:
            verified = webauthn.verify_authentication_response(
                credential=json.dumps(response),
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                credential_public_key=credential.public_key,
                credential_current_sign_count=credential.sign_count,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
            raise VerificationError(f"authentication failed: {exc}") from exc
        return verified.new_sign_count


class ChallengeStore:
    """In-memory, single-use challenges keyed by a random id (single process)."""

    def __init__(self, clock: Clock = _utcnow):
        self._clock = clock
        self._items: dict[str, tuple[bytes, datetime, Binding]] = {}
        self._lock = threading.Lock()

    def put(
        self,
        challenge: bytes,
        *,
        ceremony: Ceremony,
        session_id: str | None = None,
        approval_id: str | None = None,
    ) -> str:
        now = self._clock()
        challenge_id = secrets.token_urlsafe(16)
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v[1] > now}
            self._items[challenge_id] = (
                challenge,
                now + CHALLENGE_TTL,
                (ceremony, session_id, approval_id),
            )
        return challenge_id

    def take(
        self,
        challenge_id: str,
        *,
        ceremony: Ceremony,
        session_id: str | None = None,
        approval_id: str | None = None,
    ) -> bytes | None:
        """Consume a challenge; None if unknown, expired, or bound to something else."""
        with self._lock:
            item = self._items.pop(challenge_id, None)
        if item is None or item[1] <= self._clock():
            return None
        if item[2] != (ceremony, session_id, approval_id):
            return None
        return item[0]
