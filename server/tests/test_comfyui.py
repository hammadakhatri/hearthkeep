"""ComfyUI client: renders through a (mocked) local ComfyUI; never leaks its error text."""

import json

import httpx
import pytest

from hearthkeep.providers.comfyui import ComfyUIClient, RenderError, fill_workflow

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-body"
BASE = "http://127.0.0.1:8188"
GRAPH = {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}


def _history(pid="p1", status=None, outputs=None):
    return {
        pid: {
            "status": status or {"status_str": "success", "completed": True, "messages": []},
            "outputs": outputs if outputs is not None else {},
        }
    }


def _image(filename="a.png", subfolder="", type_="output"):
    return {"filename": filename, "subfolder": subfolder, "type": type_}


class Fake:
    """A scripted ComfyUI. `history` is a list of responses served in order (last repeats)."""

    def __init__(self, history, view=None, prompt_status=200):
        self.history = list(history)
        self.view = view or (lambda request: httpx.Response(200, content=PNG))
        self.prompt_status = prompt_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/prompt":
            if self.prompt_status != 200:
                return httpx.Response(self.prompt_status, json={"error": "/secret/path trace"})
            return httpx.Response(200, json={"prompt_id": "p1"})
        if path == "/history/p1":
            item = self.history.pop(0) if len(self.history) > 1 else self.history[0]
            return httpx.Response(200, json=item)
        if path == "/view":
            return self.view(request)
        if path == "/interrupt":
            return httpx.Response(200)
        if path == "/free":
            return httpx.Response(200)
        if path == "/upload/image":
            return httpx.Response(
                200, json={"name": "stored.png", "subfolder": "", "type": "input"}
            )
        return httpx.Response(404)

    def client(self, **kwargs):
        kwargs.setdefault("poll_interval", 0)
        return ComfyUIClient(BASE, transport=httpx.MockTransport(self), **kwargs)


async def test_render_returns_png_bytes():
    outputs = {"9": {"images": [_image("a.png"), _image("b.png", "sub")]}}
    fake = Fake([{}, _history(outputs=outputs)])
    images = await fake.client().render(GRAPH)
    assert images == [PNG, PNG]
    views = [r for r in fake.requests if r.url.path == "/view"]
    assert [dict(v.url.params) for v in views] == [
        {"filename": "a.png", "subfolder": "", "type": "output"},
        {"filename": "b.png", "subfolder": "sub", "type": "output"},
    ]
    posted = json.loads(fake.requests[0].content)
    assert posted == {"prompt": GRAPH}


async def test_render_collects_in_node_id_order_and_skips_nodes_without_images():
    outputs = {
        "9": {"images": [_image("nine.png")]},
        "10": {"text": ["hello"]},
        "2": {"images": [_image("two.png")]},
    }
    fake = Fake([_history(outputs=outputs)])
    await fake.client().render(GRAPH)
    names = [r.url.params["filename"] for r in fake.requests if r.url.path == "/view"]
    assert names == ["two.png", "nine.png"]


async def test_render_error_status_maps_to_render_error():
    status = {
        "status_str": "error",
        "completed": False,
        "messages": [
            ["execution_start", {}],
            [
                "execution_error",
                {
                    "node_type": "KSampler",
                    "traceback": ["File C:\\secret\\x.py"],
                    "exception_message": "boom",
                },
            ],
        ],
    }
    fake = Fake([_history(status=status)])
    with pytest.raises(RenderError) as exc:
        await fake.client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed: KSampler reported an error."
    assert "secret" not in exc.value.user_message and "boom" not in exc.value.user_message


async def test_render_error_without_node_type_is_generic():
    status = {"status_str": "error", "completed": False, "messages": []}
    with pytest.raises(RenderError) as exc:
        await Fake([_history(status=status)]).client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed."


async def test_render_error_node_type_is_sanitised():
    status = {
        "status_str": "error",
        "messages": [["execution_error", {"node_type": "Bad\nType C:\\x"}]],
    }
    with pytest.raises(RenderError) as exc:
        await Fake([_history(status=status)]).client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed."


async def test_prompt_rejected_maps_to_workflow_rejected():
    with pytest.raises(RenderError) as exc:
        await Fake([{}], prompt_status=400).client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed: the workflow was rejected."
    assert "secret" not in str(exc.value)


async def test_prompt_server_error_is_generic_failure():
    with pytest.raises(RenderError) as exc:
        await Fake([{}], prompt_status=500).client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed."


async def test_unreachable_maps_to_render_error():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    client = ComfyUIClient(BASE, transport=httpx.MockTransport(refuse))
    expected = "Image generation isn't available right now: ComfyUI isn't reachable."
    with pytest.raises(RenderError) as exc:
        await client.render(GRAPH)
    assert exc.value.user_message == expected
    with pytest.raises(RenderError) as exc:
        await client.free()
    assert exc.value.user_message == expected
    with pytest.raises(RenderError) as exc:
        await client.upload(PNG, "a.png")
    assert exc.value.user_message == expected


async def test_timeout_maps_to_render_error_and_interrupts():
    ticks = iter(range(0, 1000, 100))
    fake = Fake([{}])  # never completes
    client = fake.client(timeout=250, clock=lambda: next(ticks))
    with pytest.raises(RenderError) as exc:
        await client.render(GRAPH)
    assert exc.value.user_message == "Image generation took too long and was stopped."
    assert fake.requests[-1].url.path == "/interrupt"
    assert fake.requests[-1].method == "POST"


async def test_timeout_survives_a_failing_interrupt():
    ticks = iter(range(0, 1000, 100))

    def handler(request):
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "p1"})
        if request.url.path == "/interrupt":
            raise httpx.ConnectError("gone")
        return httpx.Response(200, json={})

    client = ComfyUIClient(
        BASE,
        transport=httpx.MockTransport(handler),
        poll_interval=0,
        timeout=150,
        clock=lambda: next(ticks),
    )
    with pytest.raises(RenderError) as exc:
        await client.render(GRAPH)
    assert exc.value.user_message == "Image generation took too long and was stopped."


@pytest.mark.parametrize(
    "image",
    [
        _image("a.png", type_="temp"),
        _image("a.png", type_="input"),
        _image("../a.png"),
        _image("a/b.png"),
        _image("a\\b.png"),
        _image("a.png", "../etc"),
        _image("a.png", "a/b"),
        _image("a.png", "..\\x"),
        _image("a.png", "/abs"),
        _image("C:\\a.png"),
        _image(""),
        {"filename": "a.png"},
    ],
)
async def test_render_rejects_unsafe_images_without_fetching(image):
    fake = Fake([_history(outputs={"9": {"images": [image]}})])
    with pytest.raises(RenderError) as exc:
        await fake.client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed."
    assert not [r for r in fake.requests if r.url.path == "/view"]


async def test_render_rejects_non_png_view_response():
    fake = Fake(
        [_history(outputs={"9": {"images": [_image()]}})],
        view=lambda request: httpx.Response(200, content=b"<html>nope</html>"),
    )
    with pytest.raises(RenderError) as exc:
        await fake.client().render(GRAPH)
    assert exc.value.user_message == "Image generation returned something that isn't an image."


async def test_render_with_no_images_is_an_error():
    with pytest.raises(RenderError) as exc:
        await Fake([_history(outputs={"9": {}})]).client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed."


async def test_view_http_error_is_generic_failure():
    fake = Fake(
        [_history(outputs={"9": {"images": [_image()]}})],
        view=lambda request: httpx.Response(404, text="C:\\secret"),
    )
    with pytest.raises(RenderError) as exc:
        await fake.client().render(GRAPH)
    assert exc.value.user_message == "Image generation failed."


def test_fill_workflow_replaces_and_types():
    graph = {
        "1": {"inputs": {"text": "{{prompt}}", "width": "{{width}}", "seed": "{{seed}}"}},
        "2": {"inputs": {"name": "hk_{{prefix}}_out", "link": ["1", 0], "n": 1}},
        "3": {"inputs": {"list": ["{{prompt}}", 3]}},
    }
    values = {"prompt": "a cat", "width": 1024, "seed": 7, "prefix": "abc", "unused": "x"}
    filled = fill_workflow(graph, values)
    assert filled["1"]["inputs"] == {"text": "a cat", "width": 1024, "seed": 7}
    assert isinstance(filled["1"]["inputs"]["width"], int)
    assert filled["2"]["inputs"] == {"name": "hk_abc_out", "link": ["1", 0], "n": 1}
    assert filled["3"]["inputs"]["list"] == ["a cat", 3]
    assert graph["1"]["inputs"]["text"] == "{{prompt}}"  # the template is not mutated


def test_fill_workflow_rejects_unreplaced():
    with pytest.raises(KeyError, match="seed"):
        fill_workflow({"1": {"inputs": {"seed": "{{seed}}"}}}, {"prompt": "x"})
    with pytest.raises(KeyError, match="prefix"):
        fill_workflow({"1": {"inputs": {"name": "a_{{prefix}}"}}}, {})


def test_fill_workflow_does_not_reinterpret_substituted_text():
    # A prompt that itself contains braces is data, not a placeholder.
    filled = fill_workflow({"1": {"t": "{{prompt}}"}}, {"prompt": "write {{seed}} on a sign"})
    assert filled["1"]["t"] == "write {{seed}} on a sign"


def test_fill_workflow_fills_shipped_workflows():
    from pathlib import Path

    import hearthkeep.models as models

    folder = Path(models.__file__).parent / "workflows"
    for path in folder.glob("*.json"):
        graph = json.loads(path.read_text(encoding="utf-8"))
        keys = {
            "unet",
            "text_encoder",
            "vae",
            "prompt",
            "width",
            "height",
            "seed",
            "steps",
            "prefix",
        }
        fill_workflow(graph, dict.fromkeys(keys, 1))  # raises KeyError if a placeholder is unknown


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8188",
        "http://192.168.1.5:8188",
        "http://0.0.0.0:8188",
        "ftp://127.0.0.1",
        "nonsense",
    ],
)
def test_non_loopback_base_url_rejected(url):
    with pytest.raises(ValueError):
        ComfyUIClient(url)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8188", "http://localhost:8188/", "http://[::1]:8188"]
)
def test_loopback_base_urls_accepted(url):
    ComfyUIClient(url)


async def test_upload_returns_name():
    fake = Fake([{}])
    name = await fake.client().upload(PNG, "my_pic-1.png")
    assert name == "stored.png"
    req = fake.requests[0]
    assert req.url.path == "/upload/image"
    body = req.content
    assert b'name="image"' in body and b'filename="my_pic-1.png"' in body
    assert PNG in body
    assert b'name="overwrite"' in body and b"true" in body
    assert b'name="type"' in body and b"input" in body


@pytest.mark.parametrize("name", ["", "a b.png", "../x.png", "a/b.png", "x" * 65, "é.png"])
async def test_upload_rejects_bad_names(name):
    fake = Fake([{}])
    with pytest.raises(ValueError):
        await fake.client().upload(PNG, name)
    assert fake.requests == []


async def test_free_posts_unload_flags():
    fake = Fake([{}])
    await fake.client().free()
    req = fake.requests[0]
    assert (req.method, req.url.path) == ("POST", "/free")
    assert json.loads(req.content) == {"unload_models": True, "free_memory": True}
