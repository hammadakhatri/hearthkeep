"""Passkey enrollment, login, logout, step-up and device management."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hearthkeep.api.common import (
    ENROLLMENT_CODE_TTL_MINUTES,
    SESSION_COOKIE,
    ApiError,
    Services,
    actor,
    clear_session_cookie,
    client_ip,
    current_session,
    rate_limit,
    record,
    services,
    set_session_cookie,
)
from hearthkeep.approvals import ApprovalError
from hearthkeep.auth.credentials import Credential, complete_enrollment
from hearthkeep.auth.sessions import Session
from hearthkeep.auth.webauthn import VerificationError, b64url_decode

router = APIRouter(prefix="/api")

OWNER_USER_ID = b"hearthkeep-owner"
StepUpPurpose = Literal["approval", "settings", "add_device"]

SessionDep = Annotated[Session, Depends(current_session)]


class EnrollOptionsBody(BaseModel):
    code: str = Field(max_length=64)


class EnrollVerifyBody(BaseModel):
    code: str = Field(max_length=64)
    challenge_id: str = Field(max_length=128)
    response: dict
    device_name: str = Field(default="", max_length=200)


class LoginVerifyBody(BaseModel):
    challenge_id: str = Field(max_length=128)
    response: dict


class StepUpOptionsBody(BaseModel):
    purpose: StepUpPurpose
    approval_id: str | None = Field(default=None, max_length=128)


class StepUpVerifyBody(StepUpOptionsBody):
    challenge_id: str = Field(max_length=128)
    response: dict


class AddCodeBody(BaseModel):
    step_up_token: str = Field(max_length=256)


def _challenge(options: dict) -> bytes:
    return b64url_decode(options["challenge"])


def _device(cred: Credential, current_id: str | None = None) -> dict:
    return {
        "id": cred.id,
        "device_name": cred.device_name,
        "created_at": cred.created_at,
        "current": cred.id == current_id,
    }


def _enroll_failed(hk: Services, request: Request, reason: str, stage: str) -> ApiError:
    # Never the code itself.
    record(
        hk,
        "anonymous",
        "enrollment.failed",
        {"reason": reason, "stage": stage, "client": client_ip(request)},
    )
    return ApiError(400, reason)


def _signed_in(hk: Services, request: Request, cred: Credential, status: int = 200) -> JSONResponse:
    # Whatever session this browser presented is replaced, never left alive alongside.
    previous = request.cookies.get(SESSION_COOKIE)
    if previous:
        hk.sessions.revoke(previous)
    session_id = hk.sessions.create(cred.id)
    response = JSONResponse({"device": _device(cred, cred.id)}, status_code=status)
    set_session_cookie(response, hk, session_id)
    return response


# ---- enrollment -------------------------------------------------------------------------


@router.post("/auth/enroll/options")
async def enroll_options(body: EnrollOptionsBody, request: Request) -> dict:
    hk = services(request)
    rate_limit(request, "enroll")
    purpose = hk.codes.purpose_of(body.code)
    if purpose is None:
        raise _enroll_failed(hk, request, "invalid_code", "options")
    existing = [c.id for c in hk.credentials.unrevoked()]
    options = hk.verifier.registration_options(OWNER_USER_ID, existing)
    challenge_id = hk.challenges.put(_challenge(options), ceremony="register", purpose=purpose)
    return {"challenge_id": challenge_id, "options": options}


@router.post("/auth/enroll/verify")
async def enroll_verify(body: EnrollVerifyBody, request: Request) -> JSONResponse:
    hk = services(request)
    rate_limit(request, "enroll")
    purpose = hk.codes.purpose_of(body.code)
    # Always consume the challenge, whatever happens next.
    challenge = hk.challenges.take(body.challenge_id, ceremony="register", purpose=purpose)
    if purpose is None:
        raise _enroll_failed(hk, request, "invalid_code", "verify")
    if challenge is None:
        raise ApiError(400, "challenge_invalid")
    try:
        new = hk.verifier.verify_registration(body.response, challenge)
    except VerificationError:
        raise _enroll_failed(hk, request, "verification_failed", "verify") from None
    try:
        cred = complete_enrollment(
            hk.conn, hk.codes, hk.credentials, body.code, purpose, new, body.device_name
        )
    except sqlite3.IntegrityError:
        raise ApiError(409, "credential_exists") from None
    if cred is None:
        raise _enroll_failed(hk, request, "invalid_code", "verify")
    return _signed_in(hk, request, cred, status=201)


# ---- login / logout ---------------------------------------------------------------------


@router.post("/auth/login/options")
async def login_options(request: Request) -> dict:
    hk = services(request)
    rate_limit(request, "login")
    creds = hk.credentials.unrevoked()
    if not creds:
        raise ApiError(409, "no_devices")
    options = hk.verifier.authentication_options([c.id for c in creds])
    challenge_id = hk.challenges.put(_challenge(options), ceremony="login")
    return {"challenge_id": challenge_id, "options": options}


@router.post("/auth/login/verify")
async def login_verify(body: LoginVerifyBody, request: Request) -> JSONResponse:
    hk = services(request)
    rate_limit(request, "login")
    challenge = hk.challenges.take(body.challenge_id, ceremony="login")
    if challenge is None:
        raise ApiError(400, "challenge_invalid")
    cred = hk.credentials.get(body.response.get("id"))
    try:
        if cred is None:
            raise VerificationError("unknown or revoked credential")
        count = hk.verifier.verify_authentication(body.response, challenge, cred.stored())
    except VerificationError:
        record(hk, "anonymous", "login.failed", {"client": client_ip(request)})
        raise ApiError(400, "verification_failed") from None
    hk.credentials.update_sign_count(cred.id, count)
    return _signed_in(hk, request, cred)


@router.post("/auth/logout")
async def logout(session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    hk.sessions.revoke(session.id)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response, hk)
    return response


@router.get("/auth/session")
async def whoami(session: SessionDep, request: Request) -> dict:
    cred = services(request).credentials.get(session.credential_id)
    return {"device": _device(cred, cred.id) if cred else None}


# ---- step-up ----------------------------------------------------------------------------


def _check_step_up_target(hk: Services, purpose: str, approval_id: str | None) -> None:
    if purpose != "approval":
        if approval_id is not None:
            raise ApiError(422, "approval_id_not_allowed")
        return
    if approval_id is None:
        raise ApiError(422, "approval_id_required")
    try:
        approval = hk.approvals.get(approval_id)
    except ApprovalError:
        raise ApiError(404, "not_found") from None
    if approval.status == "expired":
        raise ApiError(409, "expired")
    if approval.status != "pending":
        raise ApiError(409, "already_resolved")


def _session_credential(hk: Services, session: Session) -> Credential:
    cred = hk.credentials.get(session.credential_id)
    if cred is None:
        raise ApiError(401, "unauthenticated")
    return cred


@router.post("/auth/stepup/options")
async def step_up_options(body: StepUpOptionsBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    _check_step_up_target(hk, body.purpose, body.approval_id)
    cred = _session_credential(hk, session)
    # Only the passkey this session signed in with can step it up.
    options = hk.verifier.authentication_options([cred.id])
    challenge_id = hk.challenges.put(
        _challenge(options),
        ceremony="step_up",
        session_id=session.id,
        approval_id=body.approval_id,
        purpose=body.purpose,
    )
    return {"challenge_id": challenge_id, "options": options}


@router.post("/auth/stepup/verify")
async def step_up_verify(body: StepUpVerifyBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    challenge = hk.challenges.take(
        body.challenge_id,
        ceremony="step_up",
        session_id=session.id,
        approval_id=body.approval_id,
        purpose=body.purpose,
    )
    if challenge is None:
        raise ApiError(400, "challenge_invalid")
    _check_step_up_target(hk, body.purpose, body.approval_id)
    cred = _session_credential(hk, session)
    try:
        if body.response.get("id") != cred.id:
            raise VerificationError("step-up must use the session's own passkey")
        count = hk.verifier.verify_authentication(body.response, challenge, cred.stored())
    except VerificationError:
        record(hk, actor(session), "step_up.failed", {"purpose": body.purpose})
        raise ApiError(400, "verification_failed") from None
    hk.credentials.update_sign_count(cred.id, count)
    token = hk.step_up.issue(session.id, body.approval_id, body.purpose)
    record(
        hk,
        actor(session),
        "step_up.verified",
        {"purpose": body.purpose, "approval_id": body.approval_id},
    )
    return {"step_up_token": token}


# ---- devices ----------------------------------------------------------------------------


@router.post("/devices/add-code", status_code=201)
async def add_device_code(body: AddCodeBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    if not hk.step_up.check(body.step_up_token, session.id, None, "add_device"):
        record(hk, actor(session), "device.add_code_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    code = hk.codes.issue("add_device", ENROLLMENT_CODE_TTL_MINUTES)
    return {"code": code, "expires_in_minutes": ENROLLMENT_CODE_TTL_MINUTES}


@router.get("/devices")
async def list_devices(session: SessionDep, request: Request) -> list[dict]:
    hk = services(request)
    return [_device(c, session.credential_id) for c in hk.credentials.unrevoked()]


@router.delete("/devices/{credential_id}")
async def remove_device(credential_id: str, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    outcome = hk.credentials.revoke(credential_id, hk.sessions, actor(session))
    if outcome == "not_found":
        raise ApiError(404, "not_found")
    if outcome == "last_device":
        raise ApiError(409, "last_device")
    response = JSONResponse({"ok": True})
    if credential_id == session.credential_id:
        clear_session_cookie(response, hk)
    return response
