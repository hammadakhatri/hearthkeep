"""Client for the owner's local ComfyUI: renders images from the shipped workflow graphs."""

from __future__ import annotations

import asyncio
import copy
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from hearthkeep.config import LOOPBACK_HOSTS

_UNREACHABLE = "Image generation isn't available right now: ComfyUI isn't reachable."
_FAILED = "Image generation failed."
_REJECTED = "Image generation failed: the workflow was rejected."
_TIMED_OUT = "Image generation took too long and was stopped."
_NOT_AN_IMAGE = "Image generation returned something that isn't an image."

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_REQUEST_TIMEOUT = 30.0
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_UPLOAD_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_NODE_TYPE = re.compile(r"[A-Za-z0-9_ .-]{1,64}")
_UNSAFE_PATH_CHARS = ("/", "\\", ":", "\x00")


class RenderError(Exception):
    """Image generation failure. `user_message` is plain language for the owner and never
    carries ComfyUI's own text (tracebacks, paths); str(exc) holds technical detail."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message


def fill_workflow(graph: dict, values: dict[str, Any]) -> dict:
    """Deep-copy `graph` and fill its `{{key}}` placeholders from `values`.

    A string that is exactly one placeholder becomes the value itself (ints stay ints); a
    placeholder inside longer text is replaced with the value as text. A placeholder with no
    value raises KeyError naming it. Substituted text is never re-scanned, so a prompt that
    happens to contain braces is left alone. Unused keys in `values` are ignored.
    """

    def fill(node: Any) -> Any:
        if isinstance(node, str):
            whole = _PLACEHOLDER.fullmatch(node)
            if whole:
                return copy.deepcopy(values[_lookup(whole.group(1))])
            return _PLACEHOLDER.sub(lambda m: str(values[_lookup(m.group(1))]), node)
        if isinstance(node, dict):
            return {key: fill(item) for key, item in node.items()}
        if isinstance(node, list):
            return [fill(item) for item in node]
        return node

    def _lookup(key: str) -> str:
        if key not in values:
            raise KeyError(f"unreplaced workflow placeholder: {{{{{key}}}}}")
        return key

    return fill(graph)


def _safe_image_ref(image: Any) -> tuple[str, str] | None:
    """(filename, subfolder) for an output image we may fetch, else None."""
    if not isinstance(image, dict) or image.get("type") != "output":
        return None
    filename = image.get("filename")
    subfolder = image.get("subfolder") or ""
    if not isinstance(filename, str) or not isinstance(subfolder, str) or not filename:
        return None
    for part in (filename, subfolder):
        if ".." in part or any(c in part for c in _UNSAFE_PATH_CHARS):
            return None
    return filename, subfolder


def _error_message(status: dict) -> str:
    """'Image generation failed: <node type> reported an error.' from ComfyUI's status
    messages, or the generic message. Only a short plain node-type name is ever echoed."""
    for entry in status.get("messages") or []:
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == "execution_error":
            data = entry[1]
            node_type = data.get("node_type") if isinstance(data, dict) else None
            if isinstance(node_type, str) and _NODE_TYPE.fullmatch(node_type):
                return f"Image generation failed: {node_type} reported an error."
    return _FAILED


class ComfyUIClient:
    def __init__(
        self,
        base_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval: float = 0.5,
        timeout: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in LOOPBACK_HOSTS:
            raise ValueError(
                "ComfyUI must be on a loopback address (127.0.0.1, ::1, localhost): "
                "Hearthkeep only talks to local models."
            )
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._clock = clock

    def _http(self) -> httpx.AsyncClient:
        # trust_env=False: never route anything through a system or environment proxy.
        return httpx.AsyncClient(
            transport=self._transport,
            base_url=self._base,
            timeout=_REQUEST_TIMEOUT,
            trust_env=False,
        )

    async def render(self, graph: dict) -> list[bytes]:
        try:
            async with self._http() as http:
                prompt_id = await self._submit(http, graph)
                outputs = await self._wait(http, prompt_id)
                return await self._fetch_images(http, outputs)
        except httpx.TransportError as e:
            raise RenderError(_UNREACHABLE, f"{type(e).__name__}: {e}") from e
        except httpx.HTTPError as e:
            raise RenderError(_FAILED, f"{type(e).__name__}: {e}") from e

    async def upload(self, png: bytes, name: str) -> str:
        if not _UPLOAD_NAME.fullmatch(name):
            raise ValueError("upload name must be 1-64 characters of letters, digits, _ . -")
        try:
            async with self._http() as http:
                resp = await http.post(
                    "/upload/image",
                    files={"image": (name, png, "image/png")},
                    data={"overwrite": "true", "type": "input"},
                )
                if resp.status_code >= 400:
                    raise RenderError(_FAILED, f"HTTP {resp.status_code} from /upload/image")
                try:
                    stored = resp.json()["name"]
                except (ValueError, KeyError, TypeError) as e:
                    raise RenderError(_FAILED, f"bad upload response: {e!r}") from e
                if not isinstance(stored, str) or not stored:
                    raise RenderError(_FAILED, "upload response had no name")
                return stored
        except httpx.TransportError as e:
            raise RenderError(_UNREACHABLE, f"{type(e).__name__}: {e}") from e
        except httpx.HTTPError as e:
            raise RenderError(_FAILED, f"{type(e).__name__}: {e}") from e

    async def free(self) -> None:
        """Ask ComfyUI to unload its models and release GPU memory."""
        try:
            async with self._http() as http:
                resp = await http.post("/free", json={"unload_models": True, "free_memory": True})
                if resp.status_code >= 400:
                    raise RenderError(_FAILED, f"HTTP {resp.status_code} from /free")
        except httpx.TransportError as e:
            raise RenderError(_UNREACHABLE, f"{type(e).__name__}: {e}") from e
        except httpx.HTTPError as e:
            raise RenderError(_FAILED, f"{type(e).__name__}: {e}") from e

    async def _submit(self, http: httpx.AsyncClient, graph: dict) -> str:
        resp = await http.post("/prompt", json={"prompt": graph})
        if resp.status_code == 400:
            raise RenderError(_REJECTED, "HTTP 400 from /prompt (workflow validation)")
        if resp.status_code >= 400:
            raise RenderError(_FAILED, f"HTTP {resp.status_code} from /prompt")
        try:
            prompt_id = resp.json()["prompt_id"]
        except (ValueError, KeyError, TypeError) as e:
            raise RenderError(_FAILED, f"bad /prompt response: {e!r}") from e
        if not isinstance(prompt_id, str) or not prompt_id:
            raise RenderError(_FAILED, "/prompt returned no prompt_id")
        return prompt_id

    async def _wait(self, http: httpx.AsyncClient, prompt_id: str) -> dict:
        started = self._clock()
        while True:
            resp = await http.get(f"/history/{prompt_id}")
            if resp.status_code >= 400:
                raise RenderError(_FAILED, f"HTTP {resp.status_code} from /history")
            try:
                entry = resp.json().get(prompt_id)
            except (ValueError, AttributeError) as e:
                raise RenderError(_FAILED, f"bad /history response: {e!r}") from e
            if isinstance(entry, dict):
                status = entry.get("status")
                status = status if isinstance(status, dict) else {}
                if status.get("status_str") == "error":
                    raise RenderError(_error_message(status), "ComfyUI reported an error")
                if status.get("completed"):
                    outputs = entry.get("outputs")
                    return outputs if isinstance(outputs, dict) else {}
            if self._clock() - started > self._timeout:
                await self._interrupt(http)
                raise RenderError(_TIMED_OUT, f"no result after {self._timeout}s")
            await asyncio.sleep(self._poll_interval)

    async def _interrupt(self, http: httpx.AsyncClient) -> None:
        try:  # best effort: stop the run so it doesn't keep the GPU busy
            await http.post("/interrupt")
        except httpx.HTTPError:
            pass

    async def _fetch_images(self, http: httpx.AsyncClient, outputs: dict) -> list[bytes]:
        refs: list[tuple[str, str]] = []
        for node_id in sorted(outputs, key=_node_order):
            node = outputs[node_id]
            images = node.get("images") if isinstance(node, dict) else None
            if not isinstance(images, list):
                continue
            for image in images:
                ref = _safe_image_ref(image)
                if ref is None:
                    raise RenderError(_FAILED, "ComfyUI listed an image Hearthkeep won't fetch")
                refs.append(ref)
        if not refs:
            raise RenderError(_FAILED, "ComfyUI finished without producing an image")

        pngs: list[bytes] = []
        for filename, subfolder in refs:
            resp = await http.get(
                "/view", params={"filename": filename, "subfolder": subfolder, "type": "output"}
            )
            if resp.status_code >= 400:
                raise RenderError(_FAILED, f"HTTP {resp.status_code} from /view")
            if not resp.content.startswith(_PNG_SIGNATURE):
                raise RenderError(_NOT_AN_IMAGE, "/view response is not a PNG")
            pngs.append(resp.content)
        return pngs


def _node_order(node_id: str) -> tuple[int, int, str]:
    """Numeric node ids in numeric order, anything else after them alphabetically."""
    return (0, int(node_id), "") if str(node_id).isdigit() else (1, 0, str(node_id))
