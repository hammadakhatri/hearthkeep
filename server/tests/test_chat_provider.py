import json
from pathlib import Path

import httpx
import pytest

from hearthkeep.providers.chat import (
    OpenAIChatClient,
    ProviderError,
    ReasoningDelta,
    StreamDone,
    TextDelta,
    ToolCallRequest,
)

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "sk-test-secret-key"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_client(handler) -> OpenAIChatClient:
    return OpenAIChatClient(
        base_url="http://127.0.0.1:8080/v1",
        api_key=SECRET,
        model="test-model",
        transport=httpx.MockTransport(handler),
    )


def sse_handler(name: str, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=fixture_bytes(name)
        )

    return handler


async def collect(client, think=False, tools=None):
    return [e async for e in client.stream([{"role": "user", "content": "hi"}], tools or [], think)]


async def test_streams_text_deltas():
    events = await collect(make_client(sse_handler("sse_text.txt")))
    assert events == [TextDelta("Hello"), TextDelta(", world"), StreamDone("stop")]


async def test_streams_text_with_crlf():
    lf = fixture_bytes("sse_text.txt").replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    assert b"\r\n" in crlf
    client = make_client(lambda r: httpx.Response(200, content=crlf))
    events = await collect(client)
    texts = [e.text for e in events if isinstance(e, TextDelta)]
    assert "".join(texts) == "Hello, world"
    assert events[-1] == StreamDone("stop")


async def test_streams_reasoning_separately():
    events = await collect(make_client(sse_handler("sse_reasoning.txt")), think=True)
    assert [e for e in events if isinstance(e, ReasoningDelta)] == [
        ReasoningDelta("Let me think"),
        ReasoningDelta(" about it."),
    ]
    assert [e for e in events if isinstance(e, TextDelta)] == [TextDelta("The answer is 4.")]


async def test_assembles_fragmented_tool_call():
    events = await collect(make_client(sse_handler("sse_tool_call.txt")))
    assert events[-2:] == [
        ToolCallRequest("call_1", "get_weather", '{"city": "Oslo"}'),
        StreamDone("tool_calls"),
    ]
    assert json.loads(events[-2].arguments_json) == {"city": "Oslo"}


async def test_emits_tool_calls_when_stream_ends_without_done():
    events = await collect(make_client(sse_handler("sse_no_done.txt")))
    assert events == [ToolCallRequest("call_9", "ping", "{}"), StreamDone("tool_calls")]


async def test_sends_think_flag_and_auth():
    seen: list[httpx.Request] = []
    client = make_client(sse_handler("sse_text.txt", seen))
    tools = [{"type": "function", "function": {"name": "x"}}]
    await collect(client, think=True, tools=tools)
    body = json.loads(seen[0].content)
    assert body["stream"] is True
    assert body["model"] == "test-model"
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert body["tools"] == tools
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert seen[0].url.path == "/v1/chat/completions"


async def test_no_api_key_sends_no_authorization_header():
    seen: list[httpx.Request] = []
    client = OpenAIChatClient(
        base_url="http://127.0.0.1:8080/v1",
        api_key="",
        model="test-model",
        transport=httpx.MockTransport(sse_handler("sse_text.txt", seen)),
    )
    assert [e for e in await collect(client) if isinstance(e, TextDelta)]
    assert "authorization" not in seen[0].headers


async def test_omits_tools_when_empty():
    seen: list[httpx.Request] = []
    await collect(make_client(sse_handler("sse_text.txt", seen)), think=False)
    body = json.loads(seen[0].content)
    assert "tools" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("status", [401, 403])
async def test_http_auth_failure_raises_provider_error(status):
    client = make_client(lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(ProviderError) as exc:
        await collect(client)
    assert "rejected" in exc.value.user_message
    assert SECRET not in exc.value.user_message
    assert SECRET not in str(exc.value)


async def test_http_401_raises_provider_error():
    client = make_client(lambda r: httpx.Response(401, text="unauthorized"))
    with pytest.raises(ProviderError) as exc:
        await collect(client)
    assert exc.value.user_message == (
        "The chat model rejected Hearthkeep's key. "
        "Store the right one with: hearthkeep secrets set chat_api_key"
    )


async def test_other_http_error_mentions_status():
    client = make_client(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(ProviderError) as exc:
        await collect(client)
    assert exc.value.user_message == "The chat model returned an error (HTTP 500)."


async def test_stream_connection_error_raises_provider_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as exc:
        await collect(make_client(handler))
    assert exc.value.user_message == (
        "The chat model isn't reachable. Make sure it's running on this PC."
    )
    assert SECRET not in str(exc.value)


async def test_timeout_raises_provider_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ProviderError) as exc:
        await collect(make_client(handler))
    assert "isn't reachable" in exc.value.user_message


async def test_malformed_stream_raises_provider_error():
    with pytest.raises(ProviderError) as exc:
        await collect(make_client(sse_handler("sse_malformed.txt")))
    assert exc.value.user_message == "The chat model sent a reply Hearthkeep couldn't read."


async def test_interleaved_out_of_order_tool_calls_emit_in_index_order():
    events = await collect(make_client(sse_handler("sse_tool_calls_interleaved.txt")))
    assert events == [
        ToolCallRequest("call_a", "alpha", '{"x": 1}'),
        ToolCallRequest("call_b", "beta", '{"y": 2}'),
        StreamDone("tool_calls"),
    ]


async def test_truncated_stream_raises_provider_error():
    with pytest.raises(ProviderError) as exc:
        await collect(make_client(sse_handler("sse_truncated.txt")))
    assert exc.value.user_message == "The chat model sent a reply Hearthkeep couldn't read."


async def test_empty_choices_chunk_is_skipped():
    events = await collect(make_client(sse_handler("sse_usage_chunk.txt")))
    assert events == [TextDelta("ok"), StreamDone("stop")]


async def test_decoding_error_maps_to_unreadable():
    def handler(request):
        raise httpx.DecodingError("bad gzip", request=request)

    with pytest.raises(ProviderError) as exc:
        await collect(make_client(handler))
    assert exc.value.user_message == "The chat model sent a reply Hearthkeep couldn't read."


TOO_LONG = "This chat is too long for the model. Start a new chat."


@pytest.mark.parametrize(
    "body",
    [
        # llama-server
        {
            "error": {
                "code": 400,
                "message": "the request exceeds the available context size, try increasing it",
                "type": "exceed_context_size_error",
            }
        },
        # OpenAI-style servers (vLLM, LM Studio, ...)
        {
            "error": {
                "message": "This model's maximum context length is 8192 tokens.",
                "code": "context_length_exceeded",
            }
        },
        {"detail": "prompt is too long: 9000 tokens > 8192"},
    ],
)
async def test_context_overflow_says_chat_is_too_long(body):
    client = make_client(lambda r: httpx.Response(400, json=body))
    with pytest.raises(ProviderError) as exc:
        await collect(client)
    assert exc.value.user_message == TOO_LONG


async def test_other_400_is_not_reported_as_too_long():
    body = {"error": {"message": "invalid 'tools': unknown field"}}
    client = make_client(lambda r: httpx.Response(400, json=body))
    with pytest.raises(ProviderError) as exc:
        await collect(client)
    assert exc.value.user_message == "The chat model returned an error (HTTP 400)."
