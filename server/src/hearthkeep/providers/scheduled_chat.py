"""The chat client Hearthkeep uses when it runs the chat model itself: every stream holds a GPU
lease on the chat model, so an image render can't unload it mid-reply. `hold()` keeps one
lease over several streams (read_pages reads every page of a call under one)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from contextvars import ContextVar

from hearthkeep.providers.chat import Event, OpenAIChatClient, ProviderError
from hearthkeep.runtime.gpu import GpuScheduler, ModelLoadError
from hearthkeep.runtime.supervisor import ProcessError

_NOT_STARTED = "The chat model couldn't be started. Check <data>/logs/llama-server.log."
# The clients whose lease the current task holds through `hold()`.
_HELD: ContextVar[frozenset[int]] = ContextVar("hearthkeep_chat_held", default=frozenset())


class ScheduledChatClient:
    def __init__(self, inner: OpenAIChatClient, gpu: GpuScheduler, model_id: str) -> None:
        self._inner = inner
        self._gpu = gpu
        self._model_id = model_id

    async def _lease(self, stack: AsyncExitStack) -> None:
        try:
            await stack.enter_async_context(self._gpu.lease(self._model_id))
        except ProcessError as exc:
            # The chat model's own server didn't start: its log says why. Anything else
            # (memory that couldn't be freed, a model too big) is said as it is.
            own = isinstance(exc, ModelLoadError) and exc.model_id == self._model_id
            raise ProviderError(_NOT_STARTED if own else exc.user_message, str(exc)) from exc

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """One lease on the chat model for every stream this task opens inside the block
        (those take none of their own). Raises ProviderError like `stream`."""
        if id(self) in _HELD.get():
            yield
            return
        async with AsyncExitStack() as stack:
            await self._lease(stack)
            token = _HELD.set(_HELD.get() | {id(self)})
            try:
                yield
            finally:
                _HELD.reset(token)

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        think: bool,
        temperature: float | None = None,
    ) -> AsyncIterator[Event]:
        """`OpenAIChatClient.stream` under a lease that lasts until the stream ends, fails or
        is closed early by the consumer (or under the `hold()` lease of this task). Errors from
        the inner client pass through as they are."""
        extra = {} if temperature is None else {"temperature": temperature}
        async with AsyncExitStack() as stack:
            if id(self) not in _HELD.get():
                await self._lease(stack)
            events = await stack.enter_async_context(
                aclosing(self._inner.stream(messages, tools, think, **extra))
            )
            async for event in events:
                yield event
