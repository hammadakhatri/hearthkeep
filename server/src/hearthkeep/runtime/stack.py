"""The model stack `hearthkeep serve` runs: the cards, the model servers Hearthkeep owns, and
the GPU scheduler, chat client and image tool built from them when the app starts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from hearthkeep.images import ImageStore
from hearthkeep.models.cards import CardError, ModelCard
from hearthkeep.providers.scheduled_chat import ScheduledChatClient
from hearthkeep.runtime.gpu import Backend, GpuScheduler
from hearthkeep.tools.images import GenerateImageTool

FALLBACK_VRAM_GB = 16.0
STOP_TIMEOUT_SECONDS = 10.0

log = logging.getLogger(__name__)


class Stoppable(Protocol):
    name: str

    async def stop(self, timeout: float = ...) -> None: ...


@dataclass
class ModelStack:
    """Everything model-related the app needs. The default (empty) stack manages nothing:
    chat goes straight to `chat_client` and there is no image tool.

    `managed` are the cards the GPU scheduler handles (each provider needs a backend);
    `chat_model_id` is set when Hearthkeep runs the chat model itself, so every chat stream
    takes a lease on it. `processes` are stopped when the app shuts down.
    """

    cards: dict[str, ModelCard] = field(default_factory=dict)
    card_errors: list[CardError] = field(default_factory=list)
    managed: frozenset[str] = frozenset()
    backends: dict[str, Backend] = field(default_factory=dict)
    processes: list[Stoppable] = field(default_factory=list)
    chat_model_id: str | None = None
    comfy: object | None = None  # ComfyUIClient (render/upload) when image models are managed
    default_image_model: str | None = None
    gpu_vram_gb: float | None = None
    detect_vram: Callable[[], float | None] | None = None

    async def build_gpu(self) -> GpuScheduler:
        """The scheduler over the managed cards. GPU memory: the configured value, else what
        nvidia-smi reports (run off the event loop), else FALLBACK_VRAM_GB with a warning."""
        total = self.gpu_vram_gb
        if total is None and self.detect_vram is not None:
            total = await asyncio.to_thread(self.detect_vram)
            if total is None:
                log.warning(
                    "Could not detect the GPU memory; assuming %.0f GB. Set gpu_vram_gb in "
                    "<data>/models.toml if that is wrong.",
                    FALLBACK_VRAM_GB,
                )
                total = FALLBACK_VRAM_GB
        cards = {cid: self.cards[cid] for cid in sorted(self.managed)}
        return GpuScheduler(cards, self.backends, total or 0.0)

    def chat_client(self, inner, gpu: GpuScheduler):
        if self.chat_model_id is None:
            return inner
        return ScheduledChatClient(inner, gpu, self.chat_model_id)

    def image_tool(self, gpu: GpuScheduler, store: ImageStore) -> GenerateImageTool | None:
        """The image tool over the managed image cards, or None when there are none."""
        image_cards = [self.cards[c] for c in sorted(self.managed) if self.cards[c].kind == "image"]
        if self.comfy is None or not image_cards:
            return None
        default = self.default_image_model
        if default not in {c.id for c in image_cards}:
            log.warning(
                "default_image_model %s has no image card; using %s instead.",
                default,
                image_cards[0].id,
            )
            default = image_cards[0].id
        return GenerateImageTool(image_cards, default, gpu, self.comfy, store)

    async def stop(self, timeout: float = STOP_TIMEOUT_SECONDS) -> None:
        """Stop every model server Hearthkeep started (each within `timeout`)."""
        for process in self.processes:
            try:
                await process.stop(timeout)
            except Exception as exc:  # keep stopping the others
                log.error("could not stop %s: %s", process.name, type(exc).__name__)
