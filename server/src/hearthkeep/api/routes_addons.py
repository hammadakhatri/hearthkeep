"""The add-on registry over HTTP: every add-on's state, from the cache; and "Check again"."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from hearthkeep.api.common import ApiError, current_session, rate_limit, services
from hearthkeep.auth.sessions import Session

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
