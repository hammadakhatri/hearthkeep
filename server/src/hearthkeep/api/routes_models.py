"""The model cards, whether each one is loaded on the GPU, and the GPU's state."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from hearthkeep.api.common import current_session, services
from hearthkeep.auth.sessions import Session

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


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
