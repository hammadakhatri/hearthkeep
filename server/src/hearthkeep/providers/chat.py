"""Streaming client for the local chat model (OpenAI-compatible, e.g. llama-server)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

_UNREACHABLE = "The chat model isn't reachable. Make sure it's running on this PC."
_REJECTED = (
    "The chat model rejected Hearthkeep's key. "
    "Store the right one with: hearthkeep secrets set chat_api_key"
)
_UNREADABLE = "The chat model sent a reply Hearthkeep couldn't read."
_TOO_LONG = "This chat is too long for the model. Start a new chat."
# Wording servers use when a request does not fit the context (llama-server, OpenAI-style).
_OVERFLOW_HINTS = (
    "context size",
    "context length",
    "context window",
    "context_length",
    "exceed_context",
    "too many tokens",
    "too long",
)
_ERROR_BODY_LIMIT = 4096


def _is_context_overflow(status: int, body: bytes) -> bool:
    """A 400/413 whose error body names the context limit (anything else stays generic)."""
    if status not in (400, 413):
        return False
    text = body[:_ERROR_BODY_LIMIT].decode("utf-8", errors="replace").lower()
    return any(hint in text for hint in _OVERFLOW_HINTS)


_TIMEOUT = httpx.Timeout(300.0, connect=5.0)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class StreamDone:
    finish_reason: str


Event = TextDelta | ReasoningDelta | ToolCallRequest | StreamDone


class ProviderError(Exception):
    """Chat provider failure. `user_message` is plain language for the owner;
    str(exc) holds technical detail (never the API key)."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message


class OpenAIChatClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._transport = transport

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        think: bool,
        temperature: float | None = None,
    ) -> AsyncIterator[Event]:
        """`temperature`: sent only when given (the server's own default otherwise)."""
        body: dict = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": think},
        }
        if tools:
            body["tools"] = tools
        if temperature is not None:
            body["temperature"] = temperature
        # No key configured (a local server without --api-key): send no Authorization at all.
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        done = False

        try:
            # trust_env=False: never route chat history or the key through a system proxy.
            async with httpx.AsyncClient(
                transport=self._transport, timeout=_TIMEOUT, trust_env=False
            ) as http:
                async with http.stream("POST", self._url, json=body, headers=headers) as resp:
                    if resp.status_code in (401, 403):
                        raise ProviderError(_REJECTED, f"HTTP {resp.status_code} from chat model")
                    if resp.status_code >= 400:
                        body = b""
                        async for chunk in resp.aiter_bytes():
                            body += chunk
                            if len(body) >= _ERROR_BODY_LIMIT:
                                break
                        if _is_context_overflow(resp.status_code, body):
                            raise ProviderError(
                                _TOO_LONG, f"HTTP {resp.status_code}: context overflow"
                            )
                        raise ProviderError(
                            f"The chat model returned an error (HTTP {resp.status_code}).",
                            f"HTTP {resp.status_code} from chat model",
                        )
                    async for raw in resp.aiter_lines():
                        line = raw.rstrip("\r")
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        data = line[5:].removeprefix(" ")
                        if data == "[DONE]":
                            done = True
                            break
                        try:
                            choices = json.loads(data)["choices"]
                            if not choices:  # usage/timings chunk
                                continue
                            choice = choices[0]
                            delta = choice.get("delta") or {}
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            reasoning = delta.get("reasoning_content")
                            content = delta.get("content")
                            fragments = delta.get("tool_calls") or []
                            for frag in fragments:
                                slot = calls.setdefault(
                                    int(frag.get("index", 0)),
                                    {"id": "", "name": "", "arguments": ""},
                                )
                                fn = frag.get("function") or {}
                                slot["id"] = frag.get("id") or slot["id"]
                                slot["name"] = fn.get("name") or slot["name"]
                                slot["arguments"] += fn.get("arguments") or ""
                        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
                            raise ProviderError(_UNREADABLE, f"bad stream chunk: {e!r}") from e
                        if reasoning:
                            yield ReasoningDelta(reasoning)
                        if content:
                            yield TextDelta(content)
        except httpx.TransportError as e:
            raise ProviderError(_UNREACHABLE, f"{type(e).__name__}: {e}") from e
        except (httpx.HTTPError, httpx.StreamError) as e:
            raise ProviderError(_UNREADABLE, f"{type(e).__name__}: {e}") from e

        if not done and finish_reason is None:
            raise ProviderError(_UNREADABLE, "stream ended without finish_reason or [DONE]")
        for index in sorted(calls):
            c = calls[index]
            yield ToolCallRequest(c["id"], c["name"], c["arguments"])
        yield StreamDone(finish_reason or "stop")
