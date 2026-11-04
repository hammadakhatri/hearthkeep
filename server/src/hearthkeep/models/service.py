"""What the model routes do, shared by the session routes (`/api/models/*`, after their own
passkey step-up) and the setup routes (`/api/setup/*`, after the setup secret): the catalog as
the web app shows it, starting, cancelling and retrying a download job, and choosing the chat
model. Each takes the audit actor from its caller; nothing here checks who is asking.

Requests name catalog item ids only: every URL comes from the shipped catalog. Every change to
models.toml or config.toml keeps the owner's other lines, and asks for a restart."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StringConstraints

from hearthkeep.api.common import ApiError, Services, record
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

_log = logging.getLogger(__name__)
SECRETS_UNREADABLE = "Hearthkeep couldn't read its stored secrets. Check the server."
MODEL_NAME_MAX = 128
ItemId = Annotated[str, StringConstraints(max_length=64)]  # a catalog item id


def installer_for(hk: Services) -> Installer:
    """The installer, or 503 `unavailable` when it (or the PC reader) couldn't be made."""
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


def restart_needed(hk: Services) -> bool:
    return hk.control is not None and bool(hk.control.restart_needed)


# ---- the catalog ----------------------------------------------------------------------------


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


async def catalog_view(hk: Services) -> dict:
    """The catalog with this PC, the recommendation, what is installed and fits, and where
    chat comes from. No paths."""
    installer = installer_for(hk)
    pc = await hk.pc.get()
    items, recommended = await asyncio.to_thread(_items, hk, installer, pc)
    return {
        "pc": pc.to_json(),
        "recommended": recommended,
        "items": items,
        "chat": await asyncio.to_thread(_chat_state, hk),
        # The installer's own check, so the web app lists the engine only when a download adds it.
        "runtime_ready": await asyncio.to_thread(installer.runtime_ready),
        "restart_needed": restart_needed(hk),
        "supervised": hk.control is not None and hk.control.supervised,
    }


# ---- install jobs ---------------------------------------------------------------------------


def job_json(job: Job | None) -> dict | None:
    return job.to_json() if job is not None else None


def current_job(hk: Services) -> dict | None:
    """The current (or last) job, None with no installer."""
    installer = hk.installer
    return job_json(installer.current() if installer is not None else None)


def _job_error(exc: NotEnoughSpace | NothingToDo) -> ApiError:
    if isinstance(exc, NotEnoughSpace):
        return ApiError(409, "no_space", needed_bytes=exc.needed, free_bytes=exc.free)
    return ApiError(409, "nothing_to_do")


async def start_install(
    hk: Services,
    items: list[str],
    make_default: bool,
    actor: str,
    *,
    guard: Callable[[], None] | None = None,
) -> Job:
    """Start a job for these catalog ids (already approved by the caller). Raises `ApiError`
    `unknown_item`, `doesnt_fit`, `busy`, `no_space` or `nothing_to_do`. `guard`, when given,
    is called right before the job starts, with no await in between: it raises `ApiError` to
    refuse (setup's "the data folder is moving")."""
    installer = installer_for(hk)
    catalog = installer.catalog
    if any(i not in catalog for i in items):
        raise ApiError(400, "unknown_item")
    # nvidia-smi runs in a thread here; plan() inside start() reads the cached answer.
    pc = await hk.pc.get()
    cards = (await asyncio.to_thread(load_cards, Path(hk.settings.data_dir)))[0]
    for item_id in items:
        if not fits(catalog[item_id], pc, cards, catalog):
            raise ApiError(400, "doesnt_fit", item=item_id)
    if guard is not None:
        guard()
    try:
        job = installer.start(list(items), make_default=make_default)
    except InstallBusy as exc:
        raise ApiError(409, "busy", job=exc.job.to_json()) from None
    except KeyError:
        raise ApiError(400, "unknown_item") from None
    except (NotEnoughSpace, NothingToDo) as exc:
        raise _job_error(exc) from None
    record(hk, actor, "models.install_approved", {"items": list(items)})
    return job


def cancel_install(hk: Services, actor: str) -> dict | None:
    """Cancel the running job, if any; the current job after."""
    installer = installer_for(hk)
    job = installer.current()
    if job is not None and job.state == "running":
        installer.cancel()
        record(hk, actor, "models.install_cancelled", {"job": job.id})
    return job_json(installer.current())


def retry_install(hk: Services, actor: str) -> Job:
    """Retry the last job's items (approved when it started). Raises `ApiError` `no_job`,
    `busy`, `no_space` or `nothing_to_do`."""
    installer = installer_for(hk)
    if installer.current() is None:
        raise ApiError(404, "no_job")
    try:
        job = installer.retry()
    except InstallBusy as exc:
        raise ApiError(409, "busy", job=exc.job.to_json()) from None
    except (NotEnoughSpace, NothingToDo) as exc:
        raise _job_error(exc) from None
    record(hk, actor, "models.install_retried", {"job": job.id})
    return job


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
    return {**await asyncio.to_thread(_chat_state, hk), "restart_needed": restart_needed(hk)}


async def switch_chat(hk: Services, body: LocalChat | ExternalChat, actor: str) -> dict:
    """Point chat at an installed local model or at another server (already approved by the
    caller); the chat state after, with `restart_needed`."""
    data = Path(hk.settings.data_dir)
    lock = installer_for(hk).config_lock
    if isinstance(body, LocalChat):
        if not await asyncio.to_thread(_local_installed, data, body.card):
            raise ApiError(400, "not_installed")
        values = {"default_chat_model": body.card, "chat_server": "local"}
        async with lock:
            await _edit(data / "models.toml", values, data / BACKUP_NAME)
        record(hk, actor, "models.chat_switched", {"card": body.card})
        return await _switched(hk)

    await _switch_external(hk, data, body, lock)
    host = urlparse(body.base_url).hostname
    record(hk, actor, "models.chat_external_set", {"host": host})
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


# ---- a download that outlives setup ---------------------------------------------------------

SETUP_RESUME_KEY = "setup_resume"
_UNFINISHED_SKIP = ("done", "cancelled")


def remember_unfinished_download(hk: Services) -> None:
    """At setup's hand-over: setup's server stops once the first passkey exists, and its download
    with it. Keep what hadn't arrived, so the server that starts next carries on. Items the owner
    stopped are left stopped."""
    installer = hk.installer
    job = installer.current() if installer is not None else None
    if job is None or job.state == "done":
        return
    ids = [i.id for i in job.items if i.state not in _UNFINISHED_SKIP]
    if not ids:
        return
    hk.conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETUP_RESUME_KEY, json.dumps({"items": ids, "make_default": job.make_default})),
    )


def resume_setup_download(hk: Services) -> None:
    """On a normal start: start once more the download setup left unfinished, if any. The record
    goes first, whatever happens next, so it is never tried twice. A start that can't happen is
    logged by type only and never stops Hearthkeep from starting."""
    row = hk.conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (SETUP_RESUME_KEY,)
    ).fetchone()
    if row is None:
        return
    hk.conn.execute("DELETE FROM app_state WHERE key = ?", (SETUP_RESUME_KEY,))
    try:
        saved = json.loads(row[0])
        ids = saved["items"]
        make_default = saved["make_default"]
        if not (
            isinstance(ids, list)
            and ids
            and all(isinstance(i, str) for i in ids)
            and isinstance(make_default, bool)
        ):
            raise ValueError("bad record")
    except (ValueError, KeyError, TypeError) as exc:
        _log.warning("could not resume the setup download: %s", type(exc).__name__)
        return
    if hk.installer is None:
        _log.warning("could not resume the setup download: %s", "unavailable")
        return
    try:
        hk.installer.start(ids, make_default=make_default)
    except (NothingToDo, NotEnoughSpace, InstallBusy, KeyError) as exc:
        _log.warning("could not resume the setup download: %s", type(exc).__name__)
        return
    record(hk, "setup", "models.install_resumed", {"items": ids})
