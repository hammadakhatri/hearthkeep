"""Per-tool permission levels. Widening needs a passkey step-up; narrowing does not."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from hearthkeep import db, startup
from hearthkeep.addons import ABOUT
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
from hearthkeep.host import HostUnsupported, current_host
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
    """The default keys, then the registered tools' own. A key that is only a stored row (a
    removed tool's) is never listed."""
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


def _item(hk: Services, key: str, docker: bool) -> dict:
    tools = [t for t in hk.registry.all() if t.level_key == key]
    level = get_level(hk.conn, key)
    addon = hk.addons.level_addon(key) if hk.addons is not None else None
    description = " ".join(t.description for t in tools)
    if not tools and addon is not None:
        description = ABOUT[addon.id][1]
    return {
        "tool": key,
        "level": level.value,
        "effective_level": effective_level(key, level, docker).value,
        "description": description,
        "tools": [t.name for t in tools],
        "addon": None
        if addon is None
        else {
            "id": addon.id,
            "state": addon.state,
            "ready": addon.ready,
            "short": addon.short,
            "message": addon.message,
        },
    }


@router.get("/settings/tools")
async def list_tool_levels(session: SessionDep, request: Request) -> list[dict]:
    hk = services(request)
    docker = await hk.docker_available()
    return [_item(hk, key, docker) for key in _level_keys(hk)]


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
    return _item(hk, key, await hk.docker_available())


log = logging.getLogger(__name__)

STARTUP_FAILED = (
    "Start at login couldn't be changed. Try again; if it keeps failing, Windows may be "
    "blocking scheduled tasks for this account."
)


class StartupBody(BaseModel):
    enabled: bool
    step_up_token: str = Field(default="", max_length=256)


async def _startup_view(hk: Services, *, fresh: bool = False) -> dict:
    """Whether Hearthkeep starts at login; `fresh` looks at the task again, not at the cache."""
    state = await asyncio.to_thread(startup.cached_status, hk.monotonic(), fresh=fresh)
    return {
        "supported": state.supported,
        "enabled": state.enabled,
        # On, but for another data folder: turning it on here points it at this one.
        "other_data_folder": startup.on_for_another_folder(state, hk.settings.data_dir),
    }


@router.get("/settings/startup")
async def startup_settings(session: SessionDep, request: Request) -> dict:
    """Whether Hearthkeep starts at login."""
    return await _startup_view(services(request))


@router.post("/settings/startup")
async def set_startup(body: StartupBody, session: SessionDep, request: Request) -> dict:
    """Turn start at login on (a passkey step-up, as it makes Hearthkeep run unasked) or off
    (the session is enough). A failure is a plain message; what Windows said stays out of it."""
    rate_limit(request, "startup")
    hk = services(request)
    host = current_host()
    if not host.supported:
        raise ApiError(400, "unsupported")
    if body.enabled:
        if not (
            body.step_up_token
            and hk.step_up.check(body.step_up_token, session.id, None, "settings")
        ):
            record(hk, actor(session), "startup.enable_denied", {"reason": "step_up_required"})
            raise ApiError(403, "step_up_required")
    try:
        if body.enabled:
            await asyncio.to_thread(host.enable_startup, hk.settings.data_dir)
        else:
            removed = await asyncio.to_thread(host.disable_startup)
    except HostUnsupported:
        raise ApiError(400, "unsupported") from None
    except (startup.StartupError, OSError) as exc:
        # The type only: what Windows said can name paths and accounts.
        log.warning("start at login couldn't be changed: %s", type(exc).__name__)
        raise ApiError(502, "startup_failed", message=STARTUP_FAILED) from None
    view = await _startup_view(hk, fresh=True)
    if not body.enabled and not removed and view["enabled"]:
        raise ApiError(502, "startup_failed", message=STARTUP_FAILED)  # the task is still there
    record(hk, actor(session), "startup.enabled" if body.enabled else "startup.disabled", {})
    if hk.addons is not None:
        await hk.addons.refresh("startup", fresh=True)
    return view
