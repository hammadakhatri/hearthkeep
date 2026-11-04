"""Setup mode's endpoints, for the setup page `hearthkeep setup` opens.

The page checks the secret, fetches the first-device enrolment code it passes to the existing
passkey enrolment, and before that can show this PC, move the data folder setup just made,
download models and choose the chat model.
The setup secret stands in for the passkey step-up those need in Settings: it proves the person
is at this PC. What the model routes do is `models.service`, the same code the session routes
call; audit rows use the actor "setup".

Every route is a POST behind `SetupDep` (or `SetupPollDep` for progress polling), which checks,
in this order: setup is active and no passkey exists (404 otherwise, even with the right secret),
the TCP peer is this PC, the rate limit, then the secret in the body. The secret and the code are
never logged or audited."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

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
from hearthkeep.hardware import free_bytes
from hearthkeep.host import DATA_MARKERS, NOT_EMPTY, Host, current_host
from hearthkeep.lifecycle import EXIT_RELOCATE
from hearthkeep.models import service
from hearthkeep.models.service import ExternalChat, ItemId, LocalChat

router = APIRouter(prefix="/api/setup")

LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1"})
ACTOR = "setup"
RATE_GROUP = "setup"
POLL_RATE_GROUP = "setup_poll"  # progress polling, every second while a job runs


class SetupBody(BaseModel):
    """Every setup request's body: the secret, and whatever the route needs besides."""

    secret: str = Field(min_length=1, max_length=128)


def setup_open(hk: Services) -> bool:
    """Whether setup mode is on and no passkey exists yet."""
    return hk.setup is not None and hk.setup.active and first_device_allowed(hk.conn)


def _check(request: Request, body: SetupBody, group: str) -> None:
    hk = services(request)
    if not setup_open(hk):
        raise ApiError(404, "not_found")
    ip = client_ip(request)
    if ip not in LOOPBACK_PEERS:
        raise ApiError(403, "loopback_only")
    rate_limit(request, group)
    if not hk.setup.check(body.secret):
        record(hk, "anonymous", "setup.denied", {"reason": "bad_secret", "client": ip})
        raise ApiError(403, "setup_secret")


# Each route names its own body `body` (a SetupBody subclass); FastAPI reads the one request
# body for both, so the gate sees the secret whatever else the route takes.
async def setup_gate(request: Request, body: SetupBody) -> None:
    _check(request, body, RATE_GROUP)


async def setup_poll_gate(request: Request, body: SetupBody) -> None:
    _check(request, body, POLL_RATE_GROUP)


SetupDep = Annotated[None, Depends(setup_gate)]
SetupPollDep = Annotated[None, Depends(setup_poll_gate)]


@router.post("/status")
async def setup_status(_: SetupDep, body: SetupBody) -> dict:
    return {"state": "needs_passkey"}


@router.post("/enrollment-code")
async def setup_enrollment_code(_: SetupDep, body: SetupBody, request: Request) -> JSONResponse:
    hk = services(request)
    try:
        # Audited by `issue` as enrollment.code_issued with the purpose only, never the code.
        code = hk.codes.issue("first_device", ENROLLMENT_CODE_TTL_MINUTES)
    except EnrollmentError:  # a passkey was made between the gate and here
        raise ApiError(404, "not_found") from None
    return JSONResponse(
        {"code": code, "expires_in_minutes": ENROLLMENT_CODE_TTL_MINUTES},
        headers={"Cache-Control": "no-store"},
    )


# ---- this PC --------------------------------------------------------------------------------


def _is_default(data: Path) -> bool:
    default = current_host().default_data_dir()
    if default is None:
        return False
    return os.path.normcase(str(data)) == os.path.normcase(str(Path(default).resolve()))


def _download_started(hk: Services) -> bool:
    return hk.installer is not None and hk.installer.current() is not None


@router.post("/pc")
async def setup_pc(_: SetupDep, body: SetupBody, request: Request) -> dict:
    """This PC, and the data folder: the one place a path is shown, the folder the owner chose
    or the default. It can still move while this run made it and no download has started."""
    hk = services(request)
    if hk.pc is None:
        raise ApiError(503, "unavailable")
    pc = await hk.pc.get()
    data = Path(hk.settings.data_dir).resolve()
    return {
        "pc": pc.to_json(),
        "data_dir": str(data),
        "data_dir_default": await asyncio.to_thread(_is_default, data),
        "can_move_data": bool(hk.setup.created_folder) and not _download_started(hk),
    }


# ---- the data folder ------------------------------------------------------------------------

MIN_FREE_GB = 5
_GB = 1024**3
INSIDE_CURRENT = "Choose a folder outside the one Hearthkeep uses now."


class SetupDataFolderBody(SetupBody):
    path: str = Field(min_length=1, max_length=260)


def folder_move_problem(host: Host, target: Path, current: Path) -> str | None:
    """Why `target` can't take the data, as the sentence to show; None when it can. The 2a
    folder rules, and a move is always to a new folder: never the current one or one inside
    it, and never another Hearthkeep data folder (check_data_dir lets those through, as
    existing installs are kept as they are)."""
    problem = host.check_data_dir(target)
    if problem:
        return problem
    resolved, current = target.resolve(), current.resolve()
    if resolved == current or current in resolved.parents:
        return INSIDE_CURRENT
    if any((resolved / name).exists() for name in DATA_MARKERS):
        return NOT_EMPTY
    return None


@router.post("/data-folder", status_code=202)
async def setup_data_folder(
    _: SetupDep, body: SetupDataFolderBody, request: Request
) -> JSONResponse:
    """Move Hearthkeep's data to another folder: allowed only for a folder this run made and
    before any download has started (checked here, whatever the page was told). Setup's server
    stops once this answers, and `cmd_setup` sets up again in the new folder."""
    hk = services(request)
    host = current_host()
    if not host.supported:
        raise ApiError(400, "unsupported")
    if not hk.setup.created_folder or _download_started(hk):
        raise ApiError(409, "too_late")
    target = Path(body.path)
    current = Path(hk.settings.data_dir).resolve()
    problem = await asyncio.to_thread(folder_move_problem, host, target, current)
    if problem:
        raise ApiError(400, "bad_folder", message=problem)
    target = target.resolve()
    free = await asyncio.to_thread(free_bytes, target)
    if free < MIN_FREE_GB * _GB:
        raise ApiError(400, "not_enough_space", free_gb=round(free / _GB, 1), needed_gb=MIN_FREE_GB)
    hk.setup.next_data_dir = target
    # As the 2a hand-over: the server stops only after this answer has gone out.
    return JSONResponse(
        {"ok": True},
        status_code=202,
        background=BackgroundTask(hk.control.request, EXIT_RELOCATE),
    )


# ---- the catalog and downloads --------------------------------------------------------------


class SetupInstallBody(SetupBody):
    items: list[ItemId] = Field(min_length=1, max_length=10)
    make_default: bool = True


@router.post("/catalog")
async def setup_catalog(_: SetupDep, body: SetupBody, request: Request) -> dict:
    return await service.catalog_view(services(request))


@router.post("/install", status_code=202)
async def setup_install(_: SetupDep, body: SetupInstallBody, request: Request) -> JSONResponse:
    hk = services(request)
    job = await service.start_install(hk, list(body.items), body.make_default, ACTOR)
    return JSONResponse({"job": job.to_json()}, status_code=202)


@router.post("/install/status")
async def setup_install_status(_: SetupPollDep, body: SetupBody, request: Request) -> dict:
    return {"job": service.current_job(services(request))}


@router.post("/install/cancel")
async def setup_install_cancel(_: SetupDep, body: SetupBody, request: Request) -> dict:
    return {"job": service.cancel_install(services(request), ACTOR)}


@router.post("/install/retry", status_code=202)
async def setup_install_retry(_: SetupDep, body: SetupBody, request: Request) -> JSONResponse:
    job = service.retry_install(services(request), ACTOR)
    return JSONResponse({"job": job.to_json()}, status_code=202)


# ---- the chat model -------------------------------------------------------------------------


class SetupLocalChat(SetupBody, LocalChat):
    source: Literal["local"]


class SetupExternalChat(SetupBody, ExternalChat):
    source: Literal["external"]


SetupChatBody = Annotated[SetupLocalChat | SetupExternalChat, Field(discriminator="source")]


@router.post("/chat")
async def setup_chat(_: SetupDep, body: SetupChatBody, request: Request) -> dict:
    return await service.switch_chat(services(request), body, ACTOR)
