"""The chat client Hearthkeep uses when it runs the chat model itself: every stream holds a GPU
lease on the chat model, so an image render can't unload it mid-reply."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing

from hearthkeep.providers.chat import Event, OpenAIChatClient, ProviderError
from hearthkeep.runtime.gpu import GpuScheduler
from hearthkeep.runtime.supervisor import ProcessError

_NOT_STARTED = "The chat model couldn't be started. Check <data>/logs/llama-server.log."


class ScheduledChatClient:
    def __init__(self, inner: OpenAIChatClient, gpu: GpuScheduler, model_id: str) -> None:
        self._inner = inner
        self._gpu = gpu
        self._model_id = model_id

    async def stream(
        self, messages: list[dict], tools: list[dict], think: bool
    ) -> AsyncIterator[Event]:
        """`OpenAIChatClient.stream` under a lease that lasts until the stream ends, fails or
        is closed early by the consumer. Errors from the inner client pass through as they are."""
        async with AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(self._gpu.lease(self._model_id))
            except ProcessError as exc:
                raise ProviderError(_NOT_STARTED, str(exc)) from exc
            events = await stack.enter_async_context(
                aclosing(self._inner.stream(messages, tools, think))
            )
            async for event in events:
                yield event
