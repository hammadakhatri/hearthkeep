"""Setup mode's two endpoints: the setup page checks the secret, then fetches the first-device
enrolment code it passes to the existing passkey enrolment.

Both answer only while setup is active and no passkey exists (404 otherwise, even with the
right secret), only to a TCP peer on this PC, rate-limited, and only with the secret in the
body. The secret and the code are never logged or audited."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hearthkeep.api.common import (
    ENROLLMENT_CODE_TTL_MINUTES,
    ApiError,
    Services,
    client_ip,
    rate_limit,
    record,
    services,
)
from hearthkeep.auth.enrollment import EnrollmentError, first_device_allowed

router = APIRouter(prefix="/api/setup")

LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1"})


class SetupBody(BaseModel):
    secret: str = Field(min_length=1, max_length=128)


def setup_open(hk: Services) -> bool:
    """Whether setup mode is on and no passkey exists yet."""
    return hk.setup is not None and hk.setup.active and first_device_allowed(hk.conn)


def _gate(request: Request, body: SetupBody) -> Services:
    hk = services(request)
    if not setup_open(hk):
        raise ApiError(404, "not_found")
    ip = client_ip(request)
    if ip not in LOOPBACK_PEERS:
        raise ApiError(403, "loopback_only")
    rate_limit(request, "setup")
    if not hk.setup.check(body.secret):
        record(hk, "anonymous", "setup.denied", {"reason": "bad_secret", "client": ip})
        raise ApiError(403, "setup_secret")
    return hk


@router.post("/status")
async def setup_status(body: SetupBody, request: Request) -> dict:
    _gate(request, body)
    return {"state": "needs_passkey"}


@router.post("/enrollment-code")
async def setup_enrollment_code(body: SetupBody, request: Request) -> JSONResponse:
    hk = _gate(request, body)
    try:
        # Audited by `issue` as enrollment.code_issued with the purpose only, never the code.
        code = hk.codes.issue("first_device", ENROLLMENT_CODE_TTL_MINUTES)
    except EnrollmentError:  # a passkey was made between the gate and here
        raise ApiError(404, "not_found") from None
    return JSONResponse(
        {"code": code, "expires_in_minutes": ENROLLMENT_CODE_TTL_MINUTES},
        headers={"Cache-Control": "no-store"},
    )
