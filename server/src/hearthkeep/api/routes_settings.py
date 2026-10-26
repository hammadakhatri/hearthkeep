"""Per-tool permission levels. Widening needs a passkey step-up; narrowing does not."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from hearthkeep import db
from hearthkeep.api.common import ApiError, Services, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.permissions import (
    DEFAULT_LEVELS,
    ToolLevel,
    effective_level,
    get_level,
    is_widening,
    set_level,
)

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


class LevelBody(BaseModel):
    level: str = Field(max_length=32)
    step_up_token: str | None = Field(default=None, max_length=256)


def _level_keys(hk: Services) -> list[str]:
    keys = list(DEFAULT_LEVELS)
    for tool in hk.registry.all():
        if tool.level_key not in keys:
            keys.append(tool.level_key)
    return keys


def _resolve_key(hk: Services, name: str) -> str:
    """A tool name or a level key -> the level key its permission is stored under."""
    tool = hk.registry.get(name)
    if tool is not None:
        return tool.level_key
    if name in _level_keys(hk):
        return name
    raise ApiError(404, "not_found")


def _item(hk: Services, key: str) -> dict:
    tools = [t for t in hk.registry.all() if t.level_key == key]
    level = get_level(hk.conn, key)
    return {
        "tool": key,
        "level": level.value,
        "effective_level": effective_level(key, level, hk.docker_available).value,
        "description": " ".join(t.description for t in tools),
        "tools": [t.name for t in tools],
    }


@router.get("/settings/tools")
async def list_tool_levels(session: SessionDep, request: Request) -> list[dict]:
    hk = services(request)
    return [_item(hk, key) for key in _level_keys(hk)]


@router.put("/settings/tools/{tool}")
async def set_tool_level(tool: str, body: LevelBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    try:
        new = ToolLevel(body.level)  # validated before anything compares levels
    except ValueError:
        raise ApiError(422, "invalid_level") from None
    key = _resolve_key(hk, tool)
    old = get_level(hk.conn, key)
    widening = is_widening(old, new)
    if widening and not (
        body.step_up_token and hk.step_up.check(body.step_up_token, session.id, None, "settings")
    ):
        record(
            hk,
            actor(session),
            "settings.tool_level_denied",
            {"tool": key, "from": old.value, "to": new.value, "reason": "step_up_required"},
        )
        raise ApiError(403, "step_up_required")
    with db.transaction(hk.conn):
        set_level(hk.conn, key, new)
        record(
            hk,
            actor(session),
            "settings.tool_level_changed",
            {"tool": key, "from": old.value, "to": new.value, "step_up": widening},
        )
    return _item(hk, key)
