"""GPU scheduler: leases on models so the chat model and an image model never share the GPU
when they don't both fit.

Every chat stream holds a lease on the chat model and every image render holds one on its image
model. Leases on a resident model are shared. A lease on a model that isn't resident waits until
the models it must evict are idle, unloads just enough of them (least recently used first), then
loads its model. Requests are served first come, first served: once a request that needs an
eviction is queued, later requests queue behind it, so new chat streams can't starve an image.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from hearthkeep.models.cards import ModelCard
from hearthkeep.runtime.supervisor import ManagedProcess, ProcessError

RESERVED_GB = 1.5  # kept free for the CUDA contexts, the desktop and fragmentation
_TOO_BIG = "This model needs more GPU memory than this PC has."
_NVIDIA_SMI_ARGS = ["--query-gpu=memory.total", "--format=csv,noheader,nounits"]

log = logging.getLogger(__name__)


def _nvidia_smi_path() -> Path | None:
    """nvidia-smi as an absolute path: from PATH (a match in the current folder or through a
    relative PATH entry is ignored), else the driver's usual install places; None if absent."""
    found = shutil.which("nvidia-smi")
    if found:
        path = Path(found)
        if path.is_absolute() and path.parent.resolve() != Path.cwd().resolve():
            return path
    candidates = []
    if root := os.environ.get("SystemRoot"):
        candidates.append(Path(root) / "System32" / "nvidia-smi.exe")
    if program_files := os.environ.get("ProgramFiles"):
        candidates.append(Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe")
    for candidate in candidates:
        if candidate.is_absolute() and candidate.is_file():
            return candidate
    return None


def detect_vram_gb() -> float | None:
    """Total memory of the first NVIDIA GPU in GB (nvidia-smi reports MiB); None if unknown."""
    exe = _nvidia_smi_path()
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [str(exe), *_NVIDIA_SMI_ARGS],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return float(result.stdout.strip().splitlines()[0]) / 1024
    except ValueError:
        return None


class Backend(Protocol):
    async def load(self, card: ModelCard) -> None: ...
    async def unload(self, card: ModelCard) -> None: ...
    async def is_ready(self, card: ModelCard) -> bool: ...


class LlamaBackend:
    """Chat models: one llama-server child per card. load = start, unload = stop."""

    def __init__(self, process_for: Callable[[ModelCard], ManagedProcess]) -> None:
        self._process_for = process_for
        self._procs: dict[str, ManagedProcess] = {}

    async def load(self, card: ModelCard) -> None:
        proc = self._procs.get(card.id)
        if proc is None:
            proc = self._procs[card.id] = self._process_for(card)
        await proc.start()

    async def unload(self, card: ModelCard) -> None:
        proc = self._procs.get(card.id)
        if proc is not None:
            await proc.stop()

    async def is_ready(self, card: ModelCard) -> bool:
        proc = self._procs.get(card.id)
        return proc is not None and proc.running and await proc.is_healthy()


class ComfyBackend:
    """Image models: ComfyUI loads them on demand. load = make sure ComfyUI runs; unload = ask
    it to free its models (`free`, e.g. POST /free); ComfyUI itself keeps running."""

    def __init__(self, process: ManagedProcess, free: Callable[[], Awaitable[None]]) -> None:
        self._process = process
        self._free = free

    async def load(self, card: ModelCard) -> None:
        await self._process.start()

    async def unload(self, card: ModelCard) -> None:
        if self._process.running:
            await self._free()

    async def is_ready(self, card: ModelCard) -> bool:
        return self._process.running and await self._process.is_healthy()


_WAITING, _LOADING = "waiting", "loading"


@dataclass(eq=False)
class _Waiter:
    card: ModelCard
    future: asyncio.Future
    state: str = _WAITING
    victims: list[str] = field(default_factory=list)  # models this load evicts


async def _run_to_end(awaitable: Awaitable[None]) -> None:
    """Run cleanup to the end even if we are cancelled meanwhile (re-raised afterwards).
    Errors from the cleanup itself are swallowed: the caller is already failing."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = cancelled or not task.done()
        except Exception:
            pass
    if not task.cancelled() and task.exception() is not None:
        log.warning("cleanup after a failed load failed: %s", type(task.exception()).__name__)
    if cancelled:
        raise asyncio.CancelledError


class GpuScheduler:
    """Hands out leases on models. `backends` is keyed by card provider ("llama_server",
    "comfyui"). All bookkeeping happens on the event loop between awaits; only one load (with
    its evictions) runs at a time."""

    def __init__(
        self, cards: dict[str, ModelCard], backends: dict[str, Backend], total_vram_gb: float
    ) -> None:
        missing = sorted({c.provider for c in cards.values()} - set(backends))
        if missing:
            raise ValueError(f"no GPU backend for provider {', '.join(missing)}")
        self._cards = dict(cards)
        self._backends = dict(backends)
        self._total = float(total_vram_gb)
        self._budget = self._total - RESERVED_GB
        self._resident: OrderedDict[str, None] = OrderedDict()  # least recently used first
        self._active: dict[str, int] = {}  # model id -> leases held
        self._queue: list[_Waiter] = []  # FIFO of requests not yet granted
        self._evicting: list[str] = []  # being unloaded by the current load
        self._loading: str | None = None  # the one model being loaded

    # -- public -------------------------------------------------------------------------------

    @asynccontextmanager
    async def lease(self, model_id: str) -> AsyncIterator[None]:
        card = self._card(model_id)
        await self._acquire(card)
        try:
            yield
        finally:
            self._release(card.id)

    def resident(self) -> list[str]:
        return list(self._resident)

    def status(self) -> dict:
        used = sum(self._cards[m].vram_gb for m in self._resident)
        return {
            "total_gb": self._total,
            "used_gb": round(used, 1),
            "resident": self.resident(),
            "waiting": len(self._queue),
        }

    # -- acquire / release --------------------------------------------------------------------

    def _card(self, model_id: str) -> ModelCard:
        card = self._cards.get(model_id)
        if card is None:
            raise ProcessError(f"There is no model card for {model_id}.")
        if card.vram_gb > self._budget:
            raise ProcessError(_TOO_BIG, f"{card.id} needs {card.vram_gb} GB of {self._budget}")
        return card

    async def _acquire(self, card: ModelCard) -> None:
        waiter = _Waiter(card, asyncio.get_running_loop().create_future())
        self._queue.append(waiter)
        while True:
            self._dispatch()
            try:
                await waiter.future
            except asyncio.CancelledError:
                self._abandon(waiter)
                raise
            if waiter.state == _LOADING:
                await self._load(waiter)
                return
            # Granted on a model marked resident: make sure its server is still alive.
            try:
                ready = await self._is_ready(card)
            except asyncio.CancelledError:
                self._release(card.id)
                raise
            if ready:
                return
            # Crashed: forget it was resident and queue again at the front to reload it.
            self._active[card.id] -= 1
            self._resident.pop(card.id, None)
            waiter.future = asyncio.get_running_loop().create_future()
            waiter.state = _WAITING
            self._queue.insert(0, waiter)

    def _abandon(self, waiter: _Waiter) -> None:
        """A cancelled request: leave the queue, giving back a grant that raced the cancel."""
        if waiter.future.done() and not waiter.future.cancelled():
            if waiter.state == _LOADING:
                self._end_load(waiter)  # it never started: victims are still resident
                return
            self._release(waiter.card.id)  # granted a shared lease it will never use
            return
        if waiter in self._queue:
            self._queue.remove(waiter)
        self._dispatch()

    def _release(self, model_id: str) -> None:
        self._active[model_id] -= 1
        if model_id in self._resident:
            self._resident.move_to_end(model_id)
        self._dispatch()

    async def _is_ready(self, card: ModelCard) -> bool:
        try:
            return await self._backend(card).is_ready(card)
        except Exception as exc:
            log.warning("readiness check for %s failed: %s", card.id, type(exc).__name__)
            return False

    def _backend(self, card: ModelCard) -> Backend:
        return self._backends[card.provider]

    # -- the queue ----------------------------------------------------------------------------

    def _used(self) -> float:
        models = [*self._resident, *self._evicting]
        if self._loading is not None:
            models.append(self._loading)
        return sum(self._cards[m].vram_gb for m in models)

    def _fits(self, card: ModelCard, freed: float = 0.0) -> bool:
        return self._used() - freed + card.vram_gb <= self._budget

    def _pick_victims(self, card: ModelCard) -> list[str] | None:
        """Idle residents to unload, least recently used first, just enough for `card`;
        None if the idle ones are not enough (yet)."""
        victims: list[str] = []
        freed = 0.0
        for model_id in self._resident:
            if self._fits(card, freed):
                break
            if self._active.get(model_id, 0) == 0:
                victims.append(model_id)
                freed += self._cards[model_id].vram_gb
        return victims if self._fits(card, freed) else None

    def _dispatch(self) -> None:
        """Grant what can be granted, in arrival order. Called after every state change."""
        blocked = False  # a request that needs an eviction is queued: later ones wait behind it
        for waiter in list(self._queue):
            if waiter.state == _LOADING:
                blocked = blocked or bool(waiter.victims)
                continue
            if waiter.future.done():
                continue
            model_id = waiter.card.id
            if model_id in self._resident:
                if not blocked:
                    self._queue.remove(waiter)
                    self._active[model_id] = self._active.get(model_id, 0) + 1
                    self._resident.move_to_end(model_id)
                    waiter.future.set_result(None)
                continue
            if blocked or self._loading == model_id:
                continue  # behind an eviction, or its model is being loaded right now
            if self._loading is not None:
                blocked = not self._fits(waiter.card)  # one load at a time
                continue
            victims = self._pick_victims(waiter.card)
            if victims is None:
                blocked = True  # wait until enough models are idle
                continue
            waiter.state, waiter.victims = _LOADING, victims
            self._loading = model_id
            for victim in victims:
                del self._resident[victim]
            self._evicting = list(victims)
            blocked = bool(victims)
            waiter.future.set_result(None)

    # -- loading ------------------------------------------------------------------------------

    async def _load(self, waiter: _Waiter) -> None:
        """Evict the victims, then load the waiter's model. Runs in the requester's task."""
        card = waiter.card
        try:
            for victim in waiter.victims:
                victim_card = self._cards[victim]
                try:
                    await self._backend(victim_card).unload(victim_card)
                except ProcessError:
                    raise
                except Exception as exc:
                    raise ProcessError(
                        f"Hearthkeep couldn't free GPU memory used by {victim_card.name}.",
                        f"unload {victim} failed: {type(exc).__name__}: {exc}",
                    ) from exc
                self._evicting.remove(victim)
                self._active.pop(victim, None)
            try:
                await self._backend(card).load(card)
            except BaseException as exc:
                # Don't leave a half-started server behind (start() cleans up after itself,
                # this covers backends that don't) unless others still hold leases on it.
                if self._active.get(card.id, 0) == 0:
                    await _run_to_end(self._backend(card).unload(card))
                if isinstance(exc, Exception) and not isinstance(exc, ProcessError):
                    raise ProcessError(
                        f"{card.name} couldn't be started.", f"{type(exc).__name__}: {exc}"
                    ) from exc
                raise
        except BaseException:
            self._end_load(waiter)
            raise
        self._resident[card.id] = None
        self._active[card.id] = self._active.get(card.id, 0) + 1
        self._end_load(waiter, loaded=True)

    def _end_load(self, waiter: _Waiter, loaded: bool = False) -> None:
        """Finish the current load. Victims not unloaded yet stay resident (their memory may
        still be in use); a model that failed to load is not resident."""
        for victim in reversed(self._evicting):
            self._resident[victim] = None
            self._resident.move_to_end(victim, last=False)
        self._evicting = []
        self._loading = None
        if not loaded:
            self._resident.pop(waiter.card.id, None)
        if waiter in self._queue:
            self._queue.remove(waiter)
        self._dispatch()
