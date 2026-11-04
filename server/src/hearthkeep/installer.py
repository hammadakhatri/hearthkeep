"""Install jobs: download catalog items, check them, put them in place and point models.toml at
them. One job at a time, held in memory; a restart forgets the job but keeps the `.part` files,
so starting the same items again resumes them.

The job runs as one asyncio task on the server's loop. Hashing, moving and unpacking run in
threads, so the loop never waits on the disk. Request handlers read the job's plain attributes on
the same loop, so no locking is needed.

Stopping: a download and the tar.exe run (a child process, killed when the job is cancelled or
the server stops) stop at once. A hash or a zip extract runs in a thread that can't be killed:
`shutdown()` doesn't wait for it (the task is cancelled and returns), so it only finishes on its
own in the background; the next run clears the unpack folder first, and a `.part` that was being
hashed is simply hashed again. `cancel()` lets such a step finish, then stops before the next one.

A job that ends stopped or failed still points models.toml at the items that finished (ruling
Q13), so a model that is verified and in place can be used after the restart.

A model file already at its destination with the catalog size counts as in place: only this
installer writes there, and only after the SHA-256 check, so it isn't hashed again.

An unexpected exception inside the job (a bug, not the owner's network) is logged and reported
as `network`, so the job ends `failed` and the server keeps running."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import math
import ntpath
import os
import shutil
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Hashable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from hearthkeep.config import ConfigError, Settings
from hearthkeep.fetching import (
    FetchError,
    Progress,
    download_file,
    new_client,
    safe_extract_zip,
)
from hearthkeep.hardware import PcInfo, free_bytes, read_pc, recommend
from hearthkeep.lifecycle import ServerControl
from hearthkeep.models.cards import ModelCard, load_cards, load_shipped_cards
from hearthkeep.models.catalog import CatalogFile, CatalogItem, load_catalog
from hearthkeep.models.chatstate import chat_is_external
from hearthkeep.models.runtime import (
    IMAGE_FILE_FOLDERS,
    RuntimeConfig,
    card_file_paths,
    load_runtime,
)
from hearthkeep.procutil import NO_WINDOW, system32_exe
from hearthkeep.tomledit import TomlEditError, edit_toml

ItemState = Literal[
    "queued", "downloading", "verifying", "unpacking", "done", "failed", "cancelled"
]
JobState = Literal["running", "done", "failed", "cancelled"]

ZIP_MAX_FILES = 5000
ZIP_MAX_BYTES = 4 * 1024**3
TAR_TIMEOUT_SECONDS = 30 * 60
TAR_POLL_SECONDS = 0.1
# Room to leave for unpacking, as a multiple of the archive's size.
UNPACK_FACTOR = {"zip": 3, "7z": 4}
# Pauses before the three automatic retries of a file that failed on the network.
RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 10.0)
# Pauses before the three retries of a rename that Windows refused because another program (a
# virus scanner, the search indexer) had the file or folder open for a moment.
RENAME_RETRY_DELAYS: tuple[float, ...] = (1.0, 1.0, 1.0)
# Windows' ERROR_NOT_SAME_DEVICE: a rename can't cross drives.
_NOT_SAME_DEVICE = 17
SPACE_MARGIN = 1.1
SPEED_WINDOW_SECONDS = 2.0
BACKUP_NAME = "models.toml.before-hearthkeep"
CONFIG_NOT_UPDATED = "config_not_updated"
_KIND_ORDER = {"runtime": 0, "comfyui": 1, "chat": 2, "image": 3}
_log = logging.getLogger(__name__)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass
class ItemProgress:
    id: str
    name: str
    state: ItemState = "queued"
    done_bytes: int = 0
    total_bytes: int = 0
    speed_bps: float = 0.0
    error: str | None = None  # a FetchError reason, or "config_not_updated"

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class Job:
    id: str
    items: list[ItemProgress]
    state: JobState
    make_default: bool
    restart_needed: bool = field(default=False)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "items": [i.to_json() for i in self.items],
            "restart_needed": self.restart_needed,
        }


class InstallBusy(Exception):
    """A job is already running."""

    def __init__(self, job: Job) -> None:
        super().__init__("an install job is already running")
        self.job = job


class NotEnoughSpace(Exception):
    def __init__(self, needed: int, free: int) -> None:
        super().__init__(f"needs {needed} bytes, {free} free")
        self.needed = needed
        self.free = free


class NothingToDo(Exception):
    """Everything asked for is already installed (or nothing is left to retry)."""


def _long_path(path: Path | str, *, windows: bool = os.name == "nt") -> str:
    """`path` with Windows' `\\\\?\\` prefix, so a folder deeper than 260 characters (ComfyUI's
    Python packages go deep) can still be walked and removed. Unchanged off Windows or when it
    already has the prefix. `path` is absolute."""
    text = str(path)
    if not windows or text.startswith("\\\\?\\"):
        return text
    text = ntpath.normpath(text)
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _is_link(path: Path | str) -> bool:
    path = Path(_long_path(path))
    if path.is_symlink() or path.is_junction():
        return True
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & _REPARSE_POINT)


def _walk_refused(exc: OSError) -> None:
    raise FetchError("unpack", f"could not check the unpacked files: {type(exc).__name__}")


def _refuse_links(root: Path) -> None:
    """Raise `unpack` when anything under `root` is a symlink, junction or other reparse point,
    or a file with another hard link. Each folder's entries are checked before the walk could go
    into them.

    Names outside the staging folder are refused by bsdtar (Windows' tar.exe) itself: by default
    it strips a leading `/` or drive and refuses entries with `..` (no `-P`). This walk catches
    what bsdtar does create: links. A folder the walk can't read refuses the install too."""
    for folder, dirs, files in os.walk(_long_path(root), followlinks=False, onerror=_walk_refused):
        for name in (*dirs, *files):
            path = Path(folder) / name
            if _is_link(path):
                raise FetchError("unpack", f"refused a link in the archive: {name!r}")
        for name in files:
            try:
                links = os.lstat(Path(folder) / name).st_nlink
            except OSError:
                links = 2
            if links > 1:
                raise FetchError("unpack", f"refused a hard link in the archive: {name!r}")


def _remove_tree(path: Path) -> None:
    path = Path(_long_path(path))
    if path.is_symlink() or path.is_junction():
        with contextlib.suppress(OSError):
            path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _retry_busy(op: Callable[[], object], sleep: Callable[[float], None]) -> None:
    """Run a rename; when Windows refuses it with PermissionError (another program has the file
    open for a moment), wait and try again, up to three more times."""
    for delay in RENAME_RETRY_DELAYS:
        try:
            op()
            return
        except PermissionError:
            sleep(delay)
    op()


def _swap_in(new: Path, target: Path, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Replace `target` with `new` by renames (`target` -> `target.old`, `new` -> `target`), then
    remove `target.old`. If the second rename fails, the old `target` is put back. A busy file
    is retried."""
    target.parent.mkdir(parents=True, exist_ok=True)
    old = target.with_name(target.name + ".old")
    _remove_tree(old)
    had_old = target.exists()
    if had_old:
        _retry_busy(lambda: target.rename(old), sleep)
    try:
        _retry_busy(lambda: new.rename(target), sleep)
    except OSError:
        if had_old:
            _retry_busy(lambda: old.rename(target), sleep)
        raise
    _remove_tree(old)


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc.kill()
        proc.wait(timeout=5)


def _other_drive(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == _NOT_SAME_DEVICE


def _move_into_place(
    part: Path, dest: Path, *, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Rename the checked `.part` to `dest`. When `dest` is on another drive (an owner's
    `comfyui_model_root`), copy it to `dest.tmp` beside `dest` and rename that, so `dest` is
    never half-written, then remove the `.part`. A busy file is retried. Runs in a thread."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _retry_busy(lambda: os.replace(part, dest), sleep)
        return
    except OSError as exc:
        if not _other_drive(exc):
            raise
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        shutil.copyfile(part, tmp)
        _retry_busy(lambda: os.replace(tmp, dest), sleep)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        part.unlink()


def _volume_of(path: Path) -> Hashable:
    """Which drive `path` is on (or would be, once created)."""
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return os.stat(path).st_dev
    except OSError:
        return path.anchor


def _in_place(path: Path, size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def _place_error(exc: OSError) -> FetchError:
    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
        return FetchError("no_space", f"{type(exc).__name__}: {exc}")
    return FetchError("unpack", f"could not put the files in place: {type(exc).__name__}")


class Installer:
    def __init__(
        self,
        data_dir: Path,
        control: ServerControl,
        *,
        catalog: dict[str, CatalogItem] | None = None,
        client_factory: Callable[[], httpx.AsyncClient] = new_client,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        disk: Callable = shutil.disk_usage,
        clock: Callable[[], float] = time.monotonic,
        record: Callable[[str, dict], None] = lambda event, detail: None,
        pc: Callable[[], PcInfo] | None = None,
        chat_is_external: Callable[[], bool] | None = None,
        volume: Callable[[Path], Hashable] = _volume_of,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.data = Path(data_dir).resolve()
        self._control = control
        self._catalog = catalog if catalog is not None else load_catalog()
        self._client_factory = client_factory
        self._popen = popen
        self._disk = disk
        self._clock = clock
        self._record = record
        self._pc = pc if pc is not None else (lambda: read_pc(self.data))
        # Whether chat uses an external server now, by the models API's rule (`chat_state`).
        self._chat_is_external = chat_is_external or (lambda: chat_is_external_default(self.data))
        self._volume = volume
        self._sleep = sleep
        self._shipped = load_shipped_cards()
        self._job: Job | None = None
        self._task: asyncio.Task | None = None
        self._cancel = False
        # Only a download or the tar run may be interrupted by cancelling the task.
        self._interruptible = False
        # Held around every edit of models.toml and config.toml, here and by the API's chat
        # switches, so two edits never read and write the files at the same time.
        self.config_lock = asyncio.Lock()

    @property
    def catalog(self) -> dict[str, CatalogItem]:
        return self._catalog

    # ---- paths ----------------------------------------

    @property
    def _downloads(self) -> Path:
        return self.data / "runtime" / "downloads"

    def _part(self, f: CatalogFile) -> Path:
        return self._downloads / f"{f.sha256}.part"

    def _image_root(self, rt: RuntimeConfig | None = None) -> Path:
        """Where image files go: the owner's `comfyui_model_root` when set, else ours."""
        rt = rt if rt is not None else self._runtime_config()
        return rt.comfyui_model_root or self.data / "models" / "image"

    def _dest(self, item: CatalogItem, f: CatalogFile, image_root: Path) -> Path | None:
        """Where a model file goes; None for an archive, which is unpacked instead."""
        if item.kind == "chat":
            card = self._shipped[item.card]
            return self.data / "models" / "chat" / card.id / card.files[f.save_as]
        if item.kind == "image":
            card = self._shipped[item.card]
            return image_root / IMAGE_FILE_FOLDERS[f.save_as] / card.files[f.save_as]
        return None

    def _staging(self, item: CatalogItem) -> Path:
        """Where an archive is unpacked: beside its folder, on the same drive, so putting it in
        place is a rename (`tools/.comfyui.new` for `tools/comfyui`)."""
        target = self.data / item.install_dir
        return target.with_name(f".{target.name}.new")

    def _required(self, item: CatalogItem) -> tuple[str, ...]:
        """The files an unpacked runtime or ComfyUI must have, relative to its install_dir."""
        return (item.exe,) if item.kind == "runtime" else (item.python, item.main)

    def _unpacked_here(self, item: CatalogItem) -> bool:
        root = self.data / item.install_dir
        return all((root / rel).is_file() for rel in self._required(item))

    # ---- what's installed ----------------------------------------

    def _runtime_config(self) -> RuntimeConfig:
        try:
            return load_runtime(self.data)
        except (ConfigError, OSError):
            return RuntimeConfig()

    def _cards(self) -> dict[str, ModelCard]:
        return load_cards(self.data)[0]

    def installed(self, item: CatalogItem) -> bool:
        rt = self._runtime_config()
        if item.kind == "runtime":
            exe = rt.llama_server_exe
            root = (self.data / item.install_dir).resolve()
            if exe is not None and exe.is_file() and exe.resolve().is_relative_to(root):
                return True
            return self._unpacked_here(item)
        if item.kind == "comfyui":
            if self._unpacked_here(item):
                return True
            py, main = rt.comfyui_python, rt.comfyui_main
            return py is not None and main is not None and py.is_file() and main.is_file()
        card = self._cards().get(item.card)
        if card is None:
            return False
        paths = card_file_paths(card, rt)
        return paths is not None and all(p.is_file() for p in paths)

    def runtime_ready(self) -> bool:
        """Whether a chat engine is there: the one models.toml names (a catalog build or the
        owner's own), or a catalog build already unpacked. `plan()` adds none when this is true."""
        return self._runtime_installed()

    def _runtime_installed(self) -> bool:
        exe = self._runtime_config().llama_server_exe
        if exe is not None and exe.is_file():
            return True
        return any(self.installed(i) for i in self._catalog.values() if i.kind == "runtime")

    def plan(self, ids: list[str]) -> list[CatalogItem]:
        """The items to install for `ids`, dependencies first, without what's installed. Unknown
        ids raise KeyError."""
        asked = [self._catalog[i] for i in ids]
        wanted: dict[str, CatalogItem] = {}
        for item in asked:
            if item.id not in wanted and not self.installed(item):
                wanted[item.id] = item
        kinds = {i.kind for i in wanted.values()}
        if (
            "chat" in kinds
            and not any(i.kind == "runtime" for i in asked)
            and not self._runtime_installed()
        ):
            runtime = recommend(self._pc(), self._catalog, self._cards()).runtime
            wanted.setdefault(runtime, self._catalog[runtime])
        if "image" in kinds:
            for comfy in (i for i in self._catalog.values() if i.kind == "comfyui"):
                if not self.installed(comfy):
                    wanted.setdefault(comfy.id, comfy)
        return sorted(wanted.values(), key=lambda i: _KIND_ORDER[i.kind])

    def _needs(self, items: list[CatalogItem]) -> dict[Hashable, tuple[Path, int]]:
        """Bytes needed on each drive, as (a folder on it, bytes): what's still to download (a
        shared file once, less what's already in a `.part`) and room to unpack each archive
        (`UNPACK_FACTOR` times its size) on the data folder's drive, plus each model file again
        on the drive it goes to when that is another one (it is copied there)."""
        needs: dict[Hashable, tuple[Path, int]] = {}

        def add(folder: Path, size: int) -> None:
            key = self._volume(folder)
            first, total = needs.get(key, (folder, 0))
            needs[key] = (first, total + size)

        seen: set[str] = set()
        image_root = self._image_root()
        downloads = self._volume(self._downloads)
        for item in items:
            if item.kind in ("runtime", "comfyui") and self._unpacked_here(item):
                continue  # a retry will skip it
            for f in item.files:
                if f.sha256 in seen:
                    continue
                seen.add(f.sha256)
                if f.unpack:
                    add(self._staging(item), f.size * UNPACK_FACTOR.get(f.unpack, 0))
                dest = self._dest(item, f, image_root)
                if dest is not None and _in_place(dest, f.size):
                    continue
                part = self._part(f)
                try:
                    have = part.stat().st_size if part.is_file() else 0
                except OSError:
                    have = 0
                add(self._downloads, f.size - min(have, f.size))
                if dest is not None and self._volume(dest) != downloads:
                    add(dest.parent, f.size)
        return needs

    def needed_bytes(self, items: list[CatalogItem]) -> int:
        """Bytes needed on all drives together (see `_needs`)."""
        return sum(size for _, size in self._needs(items).values())

    def _free_bytes(self, folder: Path) -> int:
        return free_bytes(folder, disk=self._disk)

    def _check_space(self, items: list[CatalogItem]) -> int:
        """Refuse when any drive the job writes to is short: the error names that drive's
        bytes needed and free. Returns the bytes needed on all of them."""
        needs = self._needs(items)
        for folder, needed in needs.values():
            free = self._free_bytes(folder)
            if needed * SPACE_MARGIN > free:
                raise NotEnoughSpace(needed, free)
        return sum(size for _, size in needs.values())

    # ---- the job ----------------------------------------

    def current(self) -> Job | None:
        return self._job

    def _running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, ids: list[str], *, make_default: bool) -> Job:
        if self._running() and self._job is not None:
            raise InstallBusy(self._job)
        items = self.plan(ids)
        if not items:
            raise NothingToDo()
        needed = self._check_space(items)
        job = Job(
            id=uuid.uuid4().hex,
            items=[ItemProgress(i.id, i.name, total_bytes=i.size_bytes) for i in items],
            state="running",
            make_default=make_default,
        )
        self._job = job
        self._launch(job, job.items, needed)
        return job

    def retry(self) -> Job:
        """Run the failed and cancelled items of the last job again."""
        job = self._job
        if job is not None and self._running():
            raise InstallBusy(job)
        todo = [i for i in (job.items if job else []) if i.state in ("failed", "cancelled")]
        if job is None or not todo:
            raise NothingToDo()
        needed = self._check_space([self._catalog[i.id] for i in todo])
        for item in todo:
            item.state, item.error, item.done_bytes, item.speed_bps = "queued", None, 0, 0.0
        job.state = "running"
        self._launch(job, todo, needed)
        return job

    def _launch(self, job: Job, todo: list[ItemProgress], needed: int) -> None:
        self._cancel = False
        self._record("models.install_started", {"items": [i.id for i in todo], "bytes": needed})
        self._task = asyncio.get_running_loop().create_task(self._work(job, todo))

    def cancel(self) -> None:
        """Stop the running job. A download stops at once; unpacking finishes its step first."""
        if not self._running():
            return
        self._cancel = True
        if self._interruptible and self._task is not None:
            self._task.cancel()

    async def wait(self) -> Job | None:
        """Wait for the running job (if any) to end."""
        if self._task is not None:
            # asyncio.wait never raises the task's own cancellation, but a cancellation aimed
            # at the caller still reaches it.
            await asyncio.wait({self._task})
        return self._job

    async def shutdown(self) -> None:
        """Stop the job for a server stop: `.part` files stay for the next start."""
        if self._running() and self._task is not None:
            self._cancel = True
            self._task.cancel()
        await self.wait()

    async def _work(self, job: Job, todo: list[ItemProgress]) -> None:
        current: ItemProgress | None = None
        try:
            async with self._client_factory() as client:
                for current in todo:
                    if self._cancel:
                        raise FetchError("cancelled")
                    await self._install(self._catalog[current.id], current, client)
                    current.state, current.speed_bps = "done", 0.0
                current = None
            await self._update_config(job)
        except FetchError as exc:
            if exc.reason == "cancelled":
                self._end(job, todo, current, "cancelled", None)
            else:
                self._end(job, todo, current, "failed", exc.reason)
            await self._config_after_stop(job)
        except asyncio.CancelledError:
            self._end(job, todo, current, "cancelled", None)
            # The cancellation was delivered; the edit below runs unless another one comes.
            await self._config_after_stop(job)
            raise
        except Exception:  # a bug, not the owner's network: keep the server running
            _log.exception("the install job stopped unexpectedly")
            self._end(job, todo, current, "failed", "network")
            await self._config_after_stop(job)
        finally:
            self._interruptible = False
            self._record(
                "models.install_finished",
                {"items": [i.id for i in job.items], "state": job.state},
            )

    def _end(
        self,
        job: Job,
        todo: list[ItemProgress],
        current: ItemProgress | None,
        state: Literal["failed", "cancelled"],
        error: str | None,
    ) -> None:
        """End the job: the item that stopped it gets `state`; the ones not reached yet are
        cancelled, so a retry picks them all up."""
        if current is not None:
            current.state, current.error, current.speed_bps = state, error, 0.0
        for item in todo:
            if item.state not in ("done", "failed", "cancelled"):
                item.state, item.speed_bps = "cancelled", 0.0
        job.state = state

    # ---- one item ----------------------------------------

    async def _install(
        self, item: CatalogItem, progress: ItemProgress, client: httpx.AsyncClient
    ) -> None:
        if item.kind in ("runtime", "comfyui") and self._unpacked_here(item):
            progress.done_bytes = progress.total_bytes  # already unpacked by an earlier run
            return
        progress.done_bytes = 0
        archives: list[tuple[CatalogFile, Path]] = []
        image_root = self._image_root()
        for f in item.files:
            if self._cancel:
                raise FetchError("cancelled")
            dest = self._dest(item, f, image_root)
            if dest is not None and _in_place(dest, f.size):
                progress.done_bytes += f.size  # an earlier item or run put it there
                continue
            part = self._part(f)
            before = progress.done_bytes
            await self._download(f, part, progress, client)
            progress.done_bytes = before + f.size
            if dest is None:
                archives.append((f, part))
                continue
            try:
                await asyncio.to_thread(_move_into_place, part, dest, sleep=self._sleep)
            except OSError as exc:
                raise _place_error(exc) from exc
        if archives:
            if self._cancel:
                raise FetchError("cancelled")
            await self._unpack(item, archives, progress)

    async def _download(
        self, f: CatalogFile, part: Path, progress: ItemProgress, client: httpx.AsyncClient
    ) -> None:
        base = progress.done_bytes
        last: dict = {"t": self._clock(), "done": None}
        progress.speed_bps = 0.0

        def on_progress(p: Progress) -> None:
            now = self._clock()
            dt = now - last["t"]
            if last["done"] is None:  # the first report after a (re)start may include a resume
                last["t"], last["done"] = now, p.done
            elif dt > 0:
                rate = (p.done - last["done"]) / dt
                weight = 1 - math.exp(-dt / SPEED_WINDOW_SECONDS)
                progress.speed_bps = max(0.0, weight * rate + (1 - weight) * progress.speed_bps)
                last["t"], last["done"] = now, p.done
            progress.done_bytes = base + p.done
            progress.state = "verifying" if p.done >= p.total else "downloading"

        for attempt in range(len(RETRY_DELAYS) + 1):
            progress.state = "downloading"
            self._interruptible = True
            try:
                await download_file(
                    f.url,
                    part,
                    size=f.size,
                    sha256=f.sha256,
                    client=client,
                    on_progress=on_progress,
                    cancelled=lambda: self._cancel,
                )
                return
            except FetchError as exc:
                if exc.reason != "network" or attempt == len(RETRY_DELAYS):
                    raise
                await asyncio.sleep(RETRY_DELAYS[attempt])
                last["t"], last["done"] = self._clock(), None
            finally:
                self._interruptible = False
            progress.done_bytes = base

    async def _unpack(
        self, item: CatalogItem, archives: list[tuple[CatalogFile, Path]], progress: ItemProgress
    ) -> None:
        progress.state = "unpacking"
        progress.speed_bps = 0.0
        staging = self._staging(item)
        await asyncio.to_thread(_remove_tree, staging)  # what a stopped run left behind
        try:
            try:
                await self._unpack_into(item, archives, staging)
            except OSError as exc:  # a full disk reads no_space; anything else, unpack
                raise _place_error(exc) from exc
        except asyncio.CancelledError:
            raise  # stopping: a thread may still be writing there; the next run clears it
        except BaseException:
            await asyncio.to_thread(_remove_tree, staging)
            with contextlib.suppress(OSError):
                staging.parent.rmdir()  # only when nothing else is there
            raise
        for _, part in archives:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)

    async def _unpack_into(
        self, item: CatalogItem, archives: list[tuple[CatalogFile, Path]], staging: Path
    ) -> None:
        for f, part in archives:
            if f.unpack == "zip":
                await asyncio.to_thread(
                    safe_extract_zip,
                    part,
                    staging,
                    max_files=ZIP_MAX_FILES,
                    max_bytes=ZIP_MAX_BYTES,
                )
            elif f.unpack == "7z":
                await self._untar(part, staging)
            else:
                raise FetchError("unpack", "a program file that isn't an archive")
        await asyncio.to_thread(_refuse_links, staging)
        for rel in self._required(item):
            if not (staging / rel).is_file():
                raise FetchError("unpack", f"the archive has no {rel}")
        if self._cancel:
            raise FetchError("cancelled")
        await asyncio.to_thread(_swap_in, staging, self.data / item.install_dir, sleep=self._sleep)

    async def _untar(self, archive: Path, staging: Path) -> None:
        tar = system32_exe("tar.exe")
        if tar is None:
            raise FetchError("unpack", "tar.exe was not found")
        staging.mkdir(parents=True, exist_ok=True)
        try:
            proc = self._popen(
                [str(tar), "-xf", str(archive), "-C", str(staging)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FetchError("unpack", f"tar failed: {type(exc).__name__}") from exc
        started = self._clock()
        self._interruptible = True
        try:
            while (code := proc.poll()) is None:
                if self._cancel:
                    raise FetchError("cancelled")
                if self._clock() - started > TAR_TIMEOUT_SECONDS:
                    raise FetchError("unpack", "tar took too long")
                await asyncio.sleep(TAR_POLL_SECONDS)
        except BaseException:
            _kill(proc)
            raise
        finally:
            self._interruptible = False
        if code != 0:
            raise FetchError("unpack", f"tar exited with {code}")

    # ---- models.toml ----------------------------------------

    def _config_changes(self, job: Job) -> tuple[dict, dict]:
        """What models.toml gets for the job's finished items (all of them, when it ends
        well). The local chat default is written only if chat isn't external at this moment
        (ruling Q14): an owner who chose their own server meanwhile keeps it."""
        rt = load_runtime(self.data)  # ConfigError: the file can't be read, so can't be edited
        items = [self._catalog[i.id] for i in job.items if i.state == "done"]
        kinds = {i.kind for i in items}
        set_top: dict[str, str] = {}
        model_dirs: dict[str, str] = {}

        runtimes = [i for i in items if i.kind == "runtime"]
        if not runtimes and "chat" in kinds:
            exe = rt.llama_server_exe
            if exe is None or not exe.is_file():
                # A runtime unpacked by an earlier job that never reached models.toml.
                runtimes = [
                    i
                    for i in self._catalog.values()
                    if i.kind == "runtime" and self._unpacked_here(i)
                ][:1]
        for runtime in runtimes:
            set_top["llama_server_exe"] = str(self.data / runtime.install_dir / runtime.exe)

        chats = [i for i in items if i.kind == "chat"]
        for chat in chats:
            model_dirs[chat.card] = str(self.data / "models" / "chat" / chat.card)
        if chats and self._chat_is_external():
            # The owner's own server stays in use: without this, a downloaded card that is
            # also the default (Gemma) would make the local engine win after the restart.
            set_top["chat_server"] = "external"
        elif chats and job.make_default:
            set_top["default_chat_model"] = chats[-1].card
            set_top["chat_server"] = "local"

        comfys = [i for i in items if i.kind == "comfyui"]
        if not comfys and "image" in kinds:
            if rt.comfyui_python is None or rt.comfyui_main is None:
                comfys = [
                    i
                    for i in self._catalog.values()
                    if i.kind == "comfyui" and self._unpacked_here(i)
                ][:1]
        for comfy in comfys:
            set_top["comfyui_python"] = str(self.data / comfy.install_dir / comfy.python)
            set_top["comfyui_main"] = str(self.data / comfy.install_dir / comfy.main)
        root = self._image_root(rt)
        if rt.comfyui_model_root is None and (comfys or "image" in kinds):
            set_top["comfyui_model_root"] = str(root)  # never replaces the owner's

        images = [i for i in items if i.kind == "image"]
        if images:
            default = self._cards().get(rt.default_image_model)
            after = RuntimeConfig(comfyui_model_root=root)
            paths = card_file_paths(default, after) if default is not None else None
            if paths is None or not all(p.is_file() for p in paths):
                set_top["default_image_model"] = images[0].card

        return set_top, ({"model_dirs": model_dirs} if model_dirs else {})

    async def _write_config(self, job: Job) -> bool:
        """Point models.toml at the job's finished items, under `config_lock`; False when the
        file can't be read or edited (it is then left as it was)."""
        try:
            async with self.config_lock:
                set_top, set_table = self._config_changes(job)
                await asyncio.to_thread(
                    edit_toml,
                    self.data / "models.toml",
                    set_top=set_top,
                    set_table=set_table,
                    backup=self.data / BACKUP_NAME,
                )
        except (TomlEditError, ConfigError, OSError):
            return False
        self._control.mark_restart_needed("models")
        job.restart_needed = True
        return True

    async def _update_config(self, job: Job) -> None:
        if not await self._write_config(job):
            last = job.items[-1]
            last.state, last.error = "failed", CONFIG_NOT_UPDATED
            job.state = "failed"
            return
        job.state = "done"

    async def _config_after_stop(self, job: Job) -> None:
        """A job that was stopped or failed still sets up what finished (ruling Q13): a model
        that is verified and in place is usable. When models.toml can't be edited, the finished
        items are marked so, and a retry does the edit. Never raises but for a cancellation."""
        done = [i for i in job.items if i.state == "done"]
        if not done:
            return
        self._interruptible = False
        try:
            written = await self._write_config(job)
        except Exception:  # a bug: the job has ended already, so say so and keep the server
            _log.exception("the install job's models.toml edit failed")
            written = False
        if not written:
            for item in done:
                item.state, item.error = "failed", CONFIG_NOT_UPDATED


def chat_is_external_default(data: Path) -> bool:
    """`chat_is_external` read from the data folder alone (no running server's settings)."""
    return chat_is_external(data, Settings(data_dir=data))
