"""What this PC has, and which chat model and llama.cpp build suit it."""

from __future__ import annotations

import ctypes
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from hearthkeep.config import ConfigError
from hearthkeep.models.cards import ModelCard
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
    except ConfigError:
        configured = None
    vram = configured if configured is not None else (found[1] if found else None)
    root = Path(data_dir)
    while not root.exists() and root != root.parent:
        root = root.parent  # the data folder may not exist yet: ask about its drive
    return PcInfo(
        gpu_name=found[0] if found else None,
        vram_gb=vram,
        nvidia=found is not None,
        ram_gb=memory(),
        free_disk_gb=disk(root).free / _GB,
    )


def _smallest_chat_gb(cards: dict[str, ModelCard]) -> float | None:
    sizes = [c.vram_gb for c in cards.values() if c.kind == "chat"]
    return min(sizes) if sizes else None


def fits(item: CatalogItem, pc: PcInfo, cards: dict[str, ModelCard]) -> bool:
    """Whether this PC can use the item. Chat models fit when their card's memory need is
    within the PC's VRAM, and the smallest chat model always fits (it runs on the CPU)."""
    if item.kind in ("image", "comfyui"):
        return pc.nvidia
    if item.kind == "runtime":
        return pc.nvidia if item.variant == "cuda" else True
    card = cards.get(item.card)
    if card is None:
        return False
    if pc.vram_gb is not None and card.vram_gb <= pc.vram_gb:
        return True
    return card.vram_gb == _smallest_chat_gb(cards)


def recommend(
    pc: PcInfo, catalog: dict[str, CatalogItem], cards: dict[str, ModelCard]
) -> Recommendation:
    """The largest chat model whose card fits the VRAM; with no NVIDIA GPU the smallest on the
    CPU build."""
    chats = sorted(
        (i for i in catalog.values() if i.kind == "chat" and i.card in cards),
        key=lambda i: cards[i.card].vram_gb,
    )
    smallest = chats[0].id
    if not pc.nvidia:
        return Recommendation(smallest, "llama-cpu", "no_nvidia")
    vram = pc.vram_gb
    fitting = [i for i in chats if vram is not None and cards[i.card].vram_gb <= vram]
    if not fitting:
        return Recommendation(smallest, "llama-cuda", "small_gpu")
    return Recommendation(fitting[-1].id, "llama-cuda", "fits_gpu")
