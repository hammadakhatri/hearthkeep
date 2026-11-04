"""The model cards, whether each one is loaded on the GPU, and the GPU's state.

Also the download catalog, install jobs and choosing the chat model. Requests name catalog item
ids only: every URL comes from the shipped catalog. Starting a download takes a passkey step-up
(purpose `download`) and pointing chat at another server one with purpose `settings`; cancelling,
retrying an approved job and switching between installed models need only the session. What
each route does is in `models.service`, which the setup routes share."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from hearthkeep.api.common import (
    ApiError,
    actor,
    current_session,
    rate_limit,
    record,
    services,
)
from hearthkeep.auth.sessions import Session
from hearthkeep.models import service
from hearthkeep.models.service import ExternalChat, ItemId, LocalChat

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]
RATE_GROUP = "models"


@router.get("/models")
async def list_models(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    gpu = hk.gpu.status()
    resident = set(gpu["resident"])
    models = [
        {
            "id": card.id,
            "name": card.name,
            "kind": card.kind,
            "capabilities": list(card.capabilities),
            "vram_gb": card.vram_gb,
            "source": card.source,
            "resident": card.id in resident,
            "managed": card.id in hk.managed,
        }
        for card in sorted(hk.cards.values(), key=lambda c: c.id)
    ]
    errors = [{"id_or_file": e.id_or_file, "message": e.message} for e in hk.card_errors]
    return {"models": models, "errors": errors, "gpu": gpu}


# ---- the catalog ----------------------------------------------------------------------------


@router.get("/models/catalog")
async def get_catalog(session: SessionDep, request: Request) -> dict:
    return await service.catalog_view(services(request))


# ---- install jobs ---------------------------------------------------------------------------


class InstallBody(BaseModel):
    items: list[ItemId] = Field(min_length=1, max_length=10)
    make_default: bool = True
    # A missing token is a refusal like a wrong one (403, audited), not a 422.
    step_up_token: str | None = Field(default="", max_length=256)


@router.get("/models/install")
async def install_status(session: SessionDep, request: Request) -> dict:
    return {"job": service.current_job(services(request))}


@router.post("/models/install", status_code=202)
async def install(body: InstallBody, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    if not hk.step_up.check(body.step_up_token or "", session.id, None, "download"):
        record(hk, actor(session), "models.install_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    job = await service.start_install(hk, list(body.items), body.make_default, actor(session))
    return JSONResponse({"job": job.to_json()}, status_code=202)


@router.post("/models/install/cancel")
async def cancel_install(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    return {"job": service.cancel_install(hk, actor(session))}


@router.post("/models/install/retry", status_code=202)
async def retry_install(session: SessionDep, request: Request) -> JSONResponse:
    """The items were approved with a passkey when the job started: the session is enough."""
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    job = service.retry_install(hk, actor(session))
    return JSONResponse({"job": job.to_json()}, status_code=202)


# ---- choosing the chat model ----------------------------------------------------------------


class ExternalChatBody(ExternalChat):
    step_up_token: str | None = Field(default="", max_length=256)


ChatBody = Annotated[LocalChat | ExternalChatBody, Field(discriminator="source")]


@router.post("/models/chat")
async def choose_chat(body: ChatBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    if isinstance(body, ExternalChat) and not hk.step_up.check(
        body.step_up_token or "", session.id, None, "settings"
    ):
        record(hk, actor(session), "models.chat_external_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    return await service.switch_chat(hk, body, actor(session))
