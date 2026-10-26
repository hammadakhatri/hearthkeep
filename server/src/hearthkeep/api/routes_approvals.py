"""Pending approvals and their resolution by the owner's session."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from hearthkeep.api.common import ApiError, current_session, services
from hearthkeep.approvals import ApprovalError
from hearthkeep.auth.sessions import Session, public_id

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

_STATUS_FOR_REASON = {
    "not_found": 404,
    "already_resolved": 409,
    "expired": 409,
    "step_up_required": 403,
}


class ResolveBody(BaseModel):
    approve: bool
    step_up_token: str | None = Field(default=None, max_length=256)


@router.get("/approvals")
async def list_approvals(
    session: SessionDep,
    request: Request,
    status: Annotated[Literal["pending"], Query()] = "pending",
) -> list[dict]:
    return [asdict(a) for a in services(request).approvals.pending()]


@router.post("/approvals/{approval_id}")
async def resolve_approval(
    approval_id: str, body: ResolveBody, session: SessionDep, request: Request
) -> dict:
    hk = services(request)
    try:
        approval = hk.approvals.get(approval_id)
    except ApprovalError as exc:
        raise ApiError(_STATUS_FOR_REASON[exc.reason], exc.reason) from None
    step_up_verified = False
    if body.approve and approval.requires_step_up and body.step_up_token:
        step_up_verified = hk.step_up.check(body.step_up_token, session.id, approval_id, "approval")
    try:
        # The resolver is always the calling session (by its non-secret reference).
        resolved = hk.approvals.resolve(
            approval_id, public_id(session.id), body.approve, step_up_verified
        )
    except ApprovalError as exc:
        raise ApiError(_STATUS_FOR_REASON.get(exc.reason, 409), exc.reason) from None
    return asdict(resolved)
