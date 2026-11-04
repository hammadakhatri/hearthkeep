"""The add-on registry over HTTP: every add-on's state, from the cache; and "Check again"."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hearthkeep.api.common import (
    ApiError,
    Services,
    actor,
    current_session,
    rate_limit,
    record,
    services,
)
from hearthkeep.auth.sessions import Session
from hearthkeep.sandbox.builder import BuildBusy

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


def _body(request: Request) -> dict:
    addons = services(request).addons
    if addons is None:
        raise ApiError(503, "unavailable")
    checked_at = addons.checked_at
    return {
        "addons": [status.to_json() for status in addons.all()],
        "checked_at": checked_at.isoformat() if checked_at is not None else None,
    }


@router.get("/addons")
async def list_addons(request: Request, session: SessionDep) -> dict:
    return _body(request)


@router.post("/addons/refresh")
async def refresh_addons(request: Request, session: SessionDep) -> dict:
    rate_limit(request, "addons")
    addons = services(request).addons
    if addons is None:
        raise ApiError(503, "unavailable")
    await addons.refresh(fresh=True)
    return _body(request)


# ---- building the analysis sandbox ----------------------------------------------------------


class BuildBody(BaseModel):
    step_up_token: str = Field(default="", max_length=256)


def get_builder(hk: Services):
    if hk.sandbox_builder is None:
        raise ApiError(503, "unavailable")
    return hk.sandbox_builder


async def start_build(hk: Services, who: str) -> dict:
    """Start building the sandbox images (the one function the Settings and the setup routes
    call). 409 `busy` while a build runs; the state says if Docker is missing or stopped."""
    try:
        return await get_builder(hk).start(who)
    except BuildBusy:
        raise ApiError(409, "busy") from None


@router.post("/addons/analysis/build", status_code=202)
async def build_sandbox(body: BuildBody, session: SessionDep, request: Request) -> JSONResponse:
    """Building the sandbox downloads about 1 GB and runs Docker, so it takes a passkey
    step-up (`settings`), as other things that widen what Hearthkeep does."""
    hk = services(request)
    if not (
        body.step_up_token and hk.step_up.check(body.step_up_token, session.id, None, "settings")
    ):
        record(hk, actor(session), "sandbox.build_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    return JSONResponse({"build": await start_build(hk, actor(session))}, status_code=202)


@router.get("/addons/analysis/build")
async def sandbox_build_state(request: Request, session: SessionDep) -> dict:
    return {"build": get_builder(services(request)).state()}
