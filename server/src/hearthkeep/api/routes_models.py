"""The model cards, whether each one is loaded on the GPU, and the GPU's state.

Also the download catalog, install jobs and choosing the chat model. Requests name catalog item
ids only: every URL comes from the shipped catalog. Starting a download takes a passkey step-up
(purpose `download`) and pointing chat at another server one with purpose `settings`; cancelling,
retrying an approved job and switching between installed models need only the session. Every
change to models.toml or config.toml keeps the owner's other lines, and asks for a restart."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

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
from hearthkeep.config import ConfigError, Settings, check_chat_base_url
from hearthkeep.hardware import PcInfo, fits, recommend
from hearthkeep.installer import (
    BACKUP_NAME,
    CONFIG_NOT_UPDATED,
    InstallBusy,
    Installer,
    Job,
    NotEnoughSpace,
    NothingToDo,
)
from hearthkeep.models.cards import load_cards
from hearthkeep.models.chatstate import chat_state, read_settings
from hearthkeep.models.runtime import RuntimeConfig, card_file_paths, load_runtime
from hearthkeep.secrets import SecretStoreError
from hearthkeep.tomledit import TomlEditError, edit_toml

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]
ItemId = Annotated[str, StringConstraints(max_length=64)]
RATE_GROUP = "models"
_log = logging.getLogger(__name__)
SECRETS_UNREADABLE = "Hearthkeep couldn't read its stored secrets. Check the server."


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


def _installer(hk: Services) -> Installer:
    if hk.installer is None or hk.pc is None:
        raise ApiError(503, "unavailable")
    return hk.installer


def _read_runtime(data: Path) -> RuntimeConfig:
    try:
        return load_runtime(data)
    except (ConfigError, OSError):
        return RuntimeConfig()


def _read_settings(hk: Services) -> Settings:
    """config.toml as it is now (it may have changed since the server started)."""
    return read_settings(Path(hk.settings.data_dir), hk.settings)


def _chat_state(hk: Services) -> dict:
    """Where chat comes from once the server (re)starts (`chat_state`, which the installer
    shares)."""
    return chat_state(Path(hk.settings.data_dir), _read_settings(hk))


def _items(hk: Services, installer: Installer, pc: PcInfo) -> tuple[list[dict], dict | None]:
    cards = load_cards(Path(hk.settings.data_dir))[0]
    catalog = installer.catalog
    try:
        recommended: dict | None = asdict(recommend(pc, catalog, cards))
    except ValueError:  # no chat model in the catalog
        recommended = None
    items = [
        {
            "id": item.id,
            "kind": item.kind,
            "name": item.name,
            "note": item.note,
            "licence": item.licence,
            "licence_url": item.licence_url,
            "size_bytes": item.size_bytes,
            "needs_nvidia": item.needs_nvidia,
            "variant": item.variant,
            "card": item.card,
            "installed": installer.installed(item),
            "fits": fits(item, pc, cards, catalog),
        }
        for item in catalog.values()
    ]
    return items, recommended


def _restart_needed(hk: Services) -> bool:
    return hk.control is not None and bool(hk.control.restart_needed)


@router.get("/models/catalog")
async def get_catalog(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    installer = _installer(hk)
    pc = await hk.pc.get()
    items, recommended = await asyncio.to_thread(_items, hk, installer, pc)
    return {
        "pc": pc.to_json(),
        "recommended": recommended,
        "items": items,
        "chat": await asyncio.to_thread(_chat_state, hk),
        # The installer's own check, so the web app lists the engine only when a download adds it.
        "runtime_ready": await asyncio.to_thread(installer.runtime_ready),
        "restart_needed": _restart_needed(hk),
        "supervised": hk.control is not None and hk.control.supervised,
    }


# ---- install jobs ---------------------------------------------------------------------------


class InstallBody(BaseModel):
    items: list[ItemId] = Field(min_length=1, max_length=10)
    make_default: bool = True
    # A missing token is a refusal like a wrong one (403, audited), not a 422.
    step_up_token: str | None = Field(default="", max_length=256)


def _job(job: Job | None) -> dict | None:
    return job.to_json() if job is not None else None


def _job_error(exc: NotEnoughSpace | NothingToDo) -> ApiError:
    if isinstance(exc, NotEnoughSpace):
        return ApiError(409, "no_space", needed_bytes=exc.needed, free_bytes=exc.free)
    return ApiError(409, "nothing_to_do")


@router.get("/models/install")
async def install_status(session: SessionDep, request: Request) -> dict:
    installer = services(request).installer
    return {"job": _job(installer.current() if installer is not None else None)}


@router.post("/models/install", status_code=202)
async def install(body: InstallBody, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    if not hk.step_up.check(body.step_up_token or "", session.id, None, "download"):
        record(hk, actor(session), "models.install_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    installer = _installer(hk)
    catalog = installer.catalog
    if any(i not in catalog for i in body.items):
        raise ApiError(400, "unknown_item")
    # nvidia-smi runs in a thread here; plan() inside start() reads the cached answer.
    pc = await hk.pc.get()
    cards = (await asyncio.to_thread(load_cards, Path(hk.settings.data_dir)))[0]
    for item_id in body.items:
        if not fits(catalog[item_id], pc, cards, catalog):
            raise ApiError(400, "doesnt_fit", item=item_id)
    try:
        job = installer.start(list(body.items), make_default=body.make_default)
    except InstallBusy as exc:
        raise ApiError(409, "busy", job=exc.job.to_json()) from None
    except KeyError:
        raise ApiError(400, "unknown_item") from None
    except (NotEnoughSpace, NothingToDo) as exc:
        raise _job_error(exc) from None
    record(hk, actor(session), "models.install_approved", {"items": list(body.items)})
    return JSONResponse({"job": job.to_json()}, status_code=202)


@router.post("/models/install/cancel")
async def cancel_install(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    installer = _installer(hk)
    job = installer.current()
    if job is not None and job.state == "running":
        installer.cancel()
        record(hk, actor(session), "models.install_cancelled", {"job": job.id})
    return {"job": _job(installer.current())}


@router.post("/models/install/retry", status_code=202)
async def retry_install(session: SessionDep, request: Request) -> JSONResponse:
    """The items were approved with a passkey when the job started: the session is enough."""
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    installer = _installer(hk)
    if installer.current() is None:
        raise ApiError(404, "no_job")
    try:
        job = installer.retry()
    except InstallBusy as exc:
        raise ApiError(409, "busy", job=exc.job.to_json()) from None
    except (NotEnoughSpace, NothingToDo) as exc:
        raise _job_error(exc) from None
    record(hk, actor(session), "models.install_retried", {"job": job.id})
    return JSONResponse({"job": job.to_json()}, status_code=202)


# ---- choosing the chat model ----------------------------------------------------------------


class LocalChat(BaseModel):
    source: Literal["local"]
    card: str = Field(max_length=64)


class ExternalChat(BaseModel):
    source: Literal["external"]
    base_url: str = Field(max_length=200)
    # None keeps the stored key (when chat already uses an external server; otherwise a key
    # left from before is deleted), "" deletes it.
    api_key: str | None = Field(default=None, max_length=512)
    # The model's name on that server, for servers that ask for one. None keeps config.toml's
    # chat_model. Checked by `_model_ok` (400 bad_model), so only absurd input is a 422.
    model: str | None = Field(default=None, max_length=1024)
    step_up_token: str | None = Field(default="", max_length=256)


ChatBody = Annotated[LocalChat | ExternalChat, Field(discriminator="source")]


def _local_installed(data: Path, card_id: str) -> bool:
    """The card is a chat card whose files exist, and llama-server is there to run it."""
    rt = _read_runtime(data)
    card = load_cards(data)[0].get(card_id)
    if card is None or card.kind != "chat":
        return False
    paths = card_file_paths(card, rt)
    exe = rt.llama_server_exe
    return (
        paths is not None and all(p.is_file() for p in paths) and exe is not None and exe.is_file()
    )


async def _edit(path: Path, values: dict[str, str], backup: Path | None = None) -> None:
    try:
        await asyncio.to_thread(edit_toml, path, set_top=values, backup=backup)
    except (TomlEditError, OSError):
        raise ApiError(409, CONFIG_NOT_UPDATED) from None


def _read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _put_back(files: dict[Path, bytes | None]) -> None:
    """Put each file back as it was read (None: it didn't exist). Best effort: a failure here
    is logged by type, and the next start reads whatever is on disk."""
    for path, data in files.items():
        try:
            if data is None:
                path.unlink(missing_ok=True)
                continue
            tmp = path.with_name(path.name + ".restore")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError as exc:
            _log.warning("could not put %s back: %s", path.name, type(exc).__name__)


MODEL_NAME_MAX = 128


def _model_ok(model: str) -> bool:
    """A plain name: 1 to 128 printable characters, no quotes, no line breaks, no spaces at
    either end (`isprintable` already refuses line breaks, tabs and other control characters)."""
    return (
        0 < len(model) <= MODEL_NAME_MAX
        and model.isprintable()
        and model == model.strip()
        and not any(c in model for c in "\"'`")
    )


def _url_parts_ok(url: str) -> bool:
    """No userinfo, query or fragment: a chat server is named by scheme, host, port and path."""
    try:
        parts = urlparse(url)
        parts.port  # noqa: B018 (raises ValueError on a bad port)
    except ValueError:
        return False
    return "@" not in parts.netloc and "?" not in url and "#" not in url


async def _switched(hk: Services) -> dict:
    if hk.control is not None:
        hk.control.mark_restart_needed("models")
    return {**await asyncio.to_thread(_chat_state, hk), "restart_needed": _restart_needed(hk)}


@router.post("/models/chat")
async def choose_chat(body: ChatBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    rate_limit(request, RATE_GROUP)
    data = Path(hk.settings.data_dir)
    if isinstance(body, LocalChat):
        lock = _installer(hk).config_lock
        if not await asyncio.to_thread(_local_installed, data, body.card):
            raise ApiError(400, "not_installed")
        values = {"default_chat_model": body.card, "chat_server": "local"}
        async with lock:
            await _edit(data / "models.toml", values, data / BACKUP_NAME)
        record(hk, actor(session), "models.chat_switched", {"card": body.card})
        return await _switched(hk)

    if not hk.step_up.check(body.step_up_token or "", session.id, None, "settings"):
        record(hk, actor(session), "models.chat_external_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    lock = _installer(hk).config_lock
    await _switch_external(hk, data, body, lock)
    host = urlparse(body.base_url).hostname
    record(hk, actor(session), "models.chat_external_set", {"host": host})
    return await _switched(hk)


async def _switch_external(
    hk: Services, data: Path, body: ExternalChat, lock: asyncio.Lock
) -> None:
    """All or nothing: every check first, then models.toml, config.toml (the address and, when
    given, the model name) and the key last. A step that fails puts back what the earlier steps
    changed."""
    try:
        check_chat_base_url(body.base_url)
    except ConfigError:
        raise ApiError(400, "bad_url") from None
    if not _url_parts_ok(body.base_url):
        raise ApiError(400, "bad_url")
    if body.model is not None and not _model_ok(body.model):
        raise ApiError(400, "bad_model")
    secrets = hk.secrets
    name = hk.settings.chat_api_key_secret
    models, config, backup = data / "models.toml", data / "config.toml", data / BACKUP_NAME
    values = {"chat_base_url": body.base_url}
    if body.model is not None:
        values["chat_model"] = body.model
    async with lock:
        # A key stored while chat wasn't external (an old generated one) must never be sent to
        # this server: with no key given, it is deleted.
        was_external = (await asyncio.to_thread(_chat_state, hk))["source"] == "external"
        key = body.api_key if body.api_key is not None or was_external else ""
        stored = False
        if key is not None and secrets is None:
            if body.api_key is not None:
                raise ApiError(503, "secrets_unreadable", message=SECRETS_UNREADABLE)
            key = None  # no store: nothing stale to delete
        if key is not None and secrets is not None:
            try:  # readable now, before anything changes
                stored = name in await asyncio.to_thread(secrets.names)
            except SecretStoreError:
                raise ApiError(503, "secrets_unreadable", message=SECRETS_UNREADABLE) from None
        before = {
            path: await asyncio.to_thread(_read_bytes, path) for path in (models, backup, config)
        }
        await _edit(models, {"chat_server": "external"}, backup)
        try:
            await _edit(config, values)
        except ApiError:
            await asyncio.to_thread(_put_back, {models: before[models], backup: before[backup]})
            raise
        if key is None or secrets is None or (not key and not stored):
            return
        try:
            if key:
                await asyncio.to_thread(secrets.put, name, key)
            else:
                await asyncio.to_thread(secrets.delete, name)
        except SecretStoreError:
            await asyncio.to_thread(_put_back, before)
            raise ApiError(503, "secrets_unreadable", message=SECRETS_UNREADABLE) from None
