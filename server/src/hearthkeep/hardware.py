"""What this PC has, and which chat model and llama.cpp build suit it."""

from __future__ import annotations

import asyncio
import ctypes
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from hearthkeep.config import ConfigError
from hearthkeep.models.cards import ModelCard, load_shipped_cards
from hearthkeep.models.catalog import CatalogItem
from hearthkeep.models.runtime import load_runtime
from hearthkeep.runtime.gpu import nvidia_smi_query

_GB = 1024**3


@dataclass(frozen=True)
class PcInfo:
    gpu_name: str | None
    vram_gb: float | None
    nvidia: bool
    ram_gb: float | None
    free_disk_gb: float

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Recommendation:
    chat: str
    runtime: str
    reason: Literal["fits_gpu", "no_nvidia", "small_gpu"]


def total_memory_gb() -> float | None:
    """Installed RAM in GB (Windows only; None elsewhere or when the call fails)."""
    if sys.platform != "win32":
        return None

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
            return None
        return status.ullTotalPhys / _GB
    except (OSError, AttributeError):
        return None


def read_pc(
    data_dir: Path,
    *,
    smi=nvidia_smi_query,
    memory=total_memory_gb,
    disk=shutil.disk_usage,
) -> PcInfo:
    """VRAM comes from `gpu_vram_gb` in models.toml when set, else from nvidia-smi. A broken
    models.toml counts as not set."""
    found = smi()
    try:
        configured = load_runtime(data_dir).gpu_vram_gb
    except (ConfigError, OSError):
        configured = None
    vram = configured if configured is not None else (found[1] if found else None)
    root = Path(data_dir)
    while not root.exists() and root != root.parent:
        root = root.parent  # the data folder may not exist yet: ask about its drive
    try:
        free_gb = disk(root).free / _GB
    except OSError:
        free_gb = 0.0
    return PcInfo(
        gpu_name=found[0] if found else None,
        vram_gb=vram,
        nvidia=found is not None,
        ram_gb=memory(),
        free_disk_gb=free_gb,
    )


PC_CACHE_SECONDS = 60.0
# Before the first read: no GPU known, so the CPU build (never CUDA on a guess).
_UNKNOWN_PC = PcInfo(gpu_name=None, vram_gb=None, nvidia=False, ram_gb=None, free_disk_gb=0.0)


class PcCache:
    """`read` (which runs nvidia-smi) off the event loop, its answer kept for `ttl` seconds.
    `get()` refreshes when the answer is stale; `last()` never reads, so it can be called on
    the loop (the installer's `pc`)."""

    def __init__(
        self,
        read: Callable[[], PcInfo],
        *,
        ttl: float = PC_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._ttl = ttl
        self._clock = clock
        self._value: PcInfo | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> PcInfo:
        async with self._lock:
            if self._value is None or self._clock() - self._at >= self._ttl:
                self._value = await asyncio.to_thread(self._read)
                self._at = self._clock()
            return self._value

    def last(self) -> PcInfo:
        return self._value if self._value is not None else _UNKNOWN_PC


@lru_cache(maxsize=1)
def _shipped_cards() -> dict[str, ModelCard]:
    return load_shipped_cards()


def _smallest_chat(catalog: dict[str, CatalogItem]) -> CatalogItem | None:
    """The catalog's smallest chat model, sized by the shipped cards: an owner's own cards
    never change which model always fits."""
    shipped = _shipped_cards()
    chats = [i for i in catalog.values() if i.kind == "chat" and i.card in shipped]
    return min(chats, key=lambda i: shipped[i.card].vram_gb, default=None)


def fits(
    item: CatalogItem,
    pc: PcInfo,
    cards: dict[str, ModelCard],
    catalog: dict[str, CatalogItem],
) -> bool:
    """Whether this PC can use the item. Chat models fit when their card's memory need is
    within the PC's VRAM, and the smallest catalog chat model always fits (it runs on the CPU)."""
    if item.kind in ("image", "comfyui"):
        return pc.nvidia
    if item.kind == "runtime":
        return pc.nvidia if item.variant == "cuda" else True
    smallest = _smallest_chat(catalog)
    if smallest is not None and item.id == smallest.id:
        return True
    card = cards.get(item.card)
    return card is not None and pc.vram_gb is not None and card.vram_gb <= pc.vram_gb


def recommend(
    pc: PcInfo, catalog: dict[str, CatalogItem], cards: dict[str, ModelCard]
) -> Recommendation:
    """The largest chat model whose card fits the VRAM; with no NVIDIA GPU the smallest on the
    CPU build. The runtime follows `pc.nvidia` only: an owner's VRAM setting sizes the model
    but never picks CUDA without an NVIDIA GPU."""
    chats = sorted(
        (i for i in catalog.values() if i.kind == "chat" and i.card in cards),
        key=lambda i: cards[i.card].vram_gb,
    )
    smallest = _smallest_chat(catalog)
    if not chats or smallest is None:
        raise ValueError("no chat model in the catalog")
    if not pc.nvidia:
        return Recommendation(smallest.id, "llama-cpu", "no_nvidia")
    fitting = [i for i in chats if pc.vram_gb is not None and cards[i.card].vram_gb <= pc.vram_gb]
    if not fitting:
        return Recommendation(smallest.id, "llama-cuda", "small_gpu")
    return Recommendation(fitting[-1].id, "llama-cuda", "fits_gpu")
