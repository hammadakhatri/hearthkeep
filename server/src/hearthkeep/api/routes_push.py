"""Push notifications for this device: the server's public key and the device's endpoint.

"This device" is the session's passkey, so each device has at most one subscription. The
audit log records only the credential id, never the endpoint (it carries the device token).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from hearthkeep.api.common import ApiError, Services, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.push import PushEndpointError, PushSender, PushSubscriptions

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


class SubscriptionBody(BaseModel):
    # Longer than any allowed endpoint, so check_endpoint gives the length answer.
    endpoint: str = Field(max_length=4096)


def _push(hk: Services) -> tuple[PushSender, PushSubscriptions]:
    if hk.push is None or hk.subscriptions is None:
        raise ApiError(503, "push_unavailable")
    return hk.push, hk.subscriptions


@router.get("/push")
async def push_state(session: SessionDep, request: Request) -> dict:
    sender, subscriptions = _push(services(request))
    # This device's own endpoint, so the page can re-save one the browser has since changed.
    endpoint = subscriptions.endpoint_for(session.credential_id)
    return {"public_key": sender.public_key, "enabled": endpoint is not None, "endpoint": endpoint}


@router.put("/push/subscription")
async def subscribe(body: SubscriptionBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    _, subscriptions = _push(hk)
    try:
        subscriptions.set(session.credential_id, body.endpoint)
    except PushEndpointError as exc:
        raise ApiError(422, "push_host_not_allowed") from exc
    record(hk, actor(session), "push.enabled", {"credential_id": session.credential_id})
    return {"enabled": True}


@router.delete("/push/subscription")
async def unsubscribe(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    _, subscriptions = _push(hk)
    if subscriptions.remove(session.credential_id):
        record(hk, actor(session), "push.disabled", {"credential_id": session.credential_id})
    return {"enabled": False}
