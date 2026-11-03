"""The running server: what it is (version, supervised, restart needed) and stopping or
restarting it from the app. Both actions take a passkey step-up (purpose `server`) and are
audited; the answer leaves before the server stops. Restart needs the launcher (a supervised
server): without one, nothing would start the server again.

Also the "Hearthkeep was updated" note the upgrade leaves, and dismissing it."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

import hearthkeep
from hearthkeep.api.common import ApiError, Services, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.lifecycle import EXIT_RESTART, EXIT_STOPPED, ServerControl
from hearthkeep.upgrade import dismiss_update_note, update_note

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


class StepUpTokenBody(BaseModel):
    # A missing token is a refusal like a wrong one (403, audited), not a 422.
    step_up_token: str | None = Field(default="", max_length=256)


def _control(hk: Services) -> ServerControl:
    if hk.control is None:
        raise ApiError(503, "unavailable")
    return hk.control


def _require_step_up(hk: Services, session: Session, body: StepUpTokenBody, event: str) -> None:
    if not hk.step_up.check(body.step_up_token or "", session.id, None, "server"):
        record(hk, actor(session), f"{event}_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")


@router.get("/server")
async def server_info(session: SessionDep, request: Request) -> dict:
    control = _control(services(request))
    return {
        "version": hearthkeep.__version__,
        "supervised": control.supervised,
        "setup_mode": control.setup_mode,
        "started_at": control.started_at.isoformat(),
        "restart_needed": bool(control.restart_needed),
        "restart_reasons": sorted(control.restart_needed),
    }


@router.post("/server/restart", status_code=202)
async def restart(body: StepUpTokenBody, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    control = _control(hk)
    if not control.supervised:  # before the token, which stays unused
        raise ApiError(409, "not_supervised")
    _require_step_up(hk, session, body, "server.restart")
    record(hk, actor(session), "server.restart", {"reasons": sorted(control.restart_needed)})
    return JSONResponse(
        {"ok": True}, status_code=202, background=BackgroundTask(control.request, EXIT_RESTART)
    )


@router.post("/server/stop", status_code=202)
async def stop(body: StepUpTokenBody, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    control = _control(hk)
    _require_step_up(hk, session, body, "server.stop")
    record(hk, actor(session), "server.stop", {})
    return JSONResponse(
        {"ok": True}, status_code=202, background=BackgroundTask(control.request, EXIT_STOPPED)
    )


@router.get("/update-note")
async def get_update_note(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    return {"note": update_note(hk.conn, hk.addons)}


@router.post("/update-note/dismiss")
async def dismiss_note(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    if dismiss_update_note(hk.conn):
        record(hk, actor(session), "update_note.dismissed", {})
    return {"ok": True}
