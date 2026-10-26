import asyncio

import pytest
from fakes import CHAT, IMAGE, FakeBackend, Holder, settle

from hearthkeep.providers.chat import ProviderError, StreamDone, TextDelta
from hearthkeep.providers.scheduled_chat import ScheduledChatClient
from hearthkeep.runtime.gpu import GpuScheduler

LOG_HINT = "The chat model couldn't be started. Check <data>/logs/llama-server.log."


class FakeInner:
    """Stands in for OpenAIChatClient: yields two deltas, pausing on `gate` between them."""

    def __init__(self, error: ProviderError | None = None) -> None:
        self.gate: asyncio.Event | None = None
        self.error = error
        self.calls: list[tuple] = []
        self.closed = 0

    async def stream(self, messages, tools, think):
        self.calls.append((messages, tools, think))
        try:
            yield TextDelta("Hel")
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            yield TextDelta("lo")
            yield StreamDone("stop")
        finally:
            self.closed += 1


def make(inner: FakeInner | None = None):
    backend = FakeBackend()
    gpu = GpuScheduler(
        {"chat": CHAT, "image": IMAGE}, {"llama_server": backend, "comfyui": backend}, 16.0
    )
    inner = inner or FakeInner()
    return ScheduledChatClient(inner, gpu, "chat"), gpu, backend, inner


async def collect(client: ScheduledChatClient) -> list:
    return [event async for event in client.stream([{"role": "user", "content": "hi"}], [], False)]


async def test_stream_holds_lease_for_whole_stream():
    client, gpu, backend, inner = make()
    inner.gate = asyncio.Event()
    stream = client.stream([{"role": "user", "content": "hi"}], [{"type": "function"}], True)
    assert await anext(stream) == TextDelta("Hel")
    image = Holder(gpu, "image")  # an image request arrives mid-reply
    await settle()
    assert not image.entered.is_set() and ("unload", "chat") not in backend.calls
    inner.gate.set()
    assert [e async for e in stream] == [TextDelta("lo"), StreamDone("stop")]
    await asyncio.wait_for(image.entered.wait(), 1)  # released once the stream ended
    assert backend.ops() == [("load", "chat"), ("unload", "chat"), ("load", "image")]
    assert inner.calls == [([{"role": "user", "content": "hi"}], [{"type": "function"}], True)]
    await image.release()


async def test_lease_released_when_consumer_stops_early():
    client, gpu, backend, inner = make()
    stream = client.stream([], [], False)
    assert await anext(stream) == TextDelta("Hel")
    image = Holder(gpu, "image")
    await settle()
    assert not image.entered.is_set()
    await stream.aclose()
    assert inner.closed == 1  # the inner stream is closed too
    await asyncio.wait_for(image.entered.wait(), 1)
    await image.release()


async def test_chat_restart_failure_is_reported_and_retried():
    client, gpu, backend, inner = make()
    backend.failures["chat"] = 1
    with pytest.raises(ProviderError) as exc:
        await collect(client)
    assert exc.value.user_message == LOG_HINT
    assert inner.calls == [] and gpu.resident() == []
    events = await collect(client)  # the backend has recovered
    assert events == [TextDelta("Hel"), TextDelta("lo"), StreamDone("stop")]
    assert backend.ops("load") == [("load", "chat"), ("load", "chat")]


async def test_inner_provider_error_passes_through_and_releases():
    error = ProviderError("The chat model isn't reachable.", "ConnectError")
    client, gpu, backend, inner = make(FakeInner(error))
    with pytest.raises(ProviderError) as exc:
        await collect(client)
    assert exc.value is error
    async with gpu.lease("image"):  # the chat lease was released
        pass
