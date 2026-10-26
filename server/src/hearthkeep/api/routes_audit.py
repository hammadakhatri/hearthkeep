"""Read-only audit log."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from hearthkeep import audit
from hearthkeep.api.common import current_session, services
from hearthkeep.auth.sessions import Session

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


@router.get("/audit")
async def list_audit(
    session: SessionDep,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[dict]:
    return audit.list_events(services(request).conn, limit=limit, query=q or None)
