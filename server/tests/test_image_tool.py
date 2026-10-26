import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from fakes import CHAT, FakeBackend, card

from hearthkeep.cli import build_registry
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.images import ASPECTS, PNG_SIGNATURE, ImageStore
from hearthkeep.permissions import ActionClass
from hearthkeep.providers.comfyui import RenderError
from hearthkeep.runtime.gpu import GpuScheduler
from hearthkeep.runtime.supervisor import ProcessError
from hearthkeep.tools.base import ToolContext
from hearthkeep.tools.images import GenerateImageTool

PNG = PNG_SIGNATURE + b"fake image data"
FILES = {"unet": "u.safetensors", "text_encoder": "t.safetensors", "vae": "v.safetensors"}


def image_card(model_id, name, workflow, *, vram=10.0, steps=8, prefer="", guidance=""):
    return replace(
        card(model_id, vram, kind="image"),
        name=name,
        files=dict(FILES),
        workflow=workflow,
        default_steps=steps,
        prefer_when=prefer,
        guidance=guidance,
    )


ZIMAGE = image_card(
    "z-image-turbo",
    "Z-Image Turbo",
    "z-image-turbo.json",
    steps=8,
    prefer="the image contains words",
    guidance="\nWrite plain, descriptive English.\nPut exact text in double quotes.\n",
)
FLUX = image_card(
    "flux2-klein-4b",
    "FLUX.2 Klein 4B",
    "flux2-klein-4b.json",
    steps=4,
    prefer="artistic styles",
    guidance="Lead with the subject, then the style.",
)


class FakeGpu:
    """Records lease enter/exit; `held` is the model currently leased (or None)."""

    def __init__(self, fail_with: Exception | None = None):
        self.log: list[tuple[str, str]] = []
        self.held: str | None = None
        self.fail_with = fail_with

    @asynccontextmanager
    async def lease(self, model_id):
        if self.fail_with is not None:
            raise self.fail_with
        self.log.append(("enter", model_id))
        self.held = model_id
        try:
            yield
        finally:
            self.held = None
            self.log.append(("exit", model_id))


class FakeComfy:
    """Returns one PNG per render; `fail_on[i]` raises for the i-th render (0-based)."""

    def __init__(self, gpu=None, fail_on=None):
        self.graphs: list[dict] = []
        self.held_during: list[str | None] = []
        self.gpu = gpu
        self.fail_on = fail_on or {}

    async def render(self, graph):
        index = len(self.graphs)
        self.graphs.append(graph)
        self.held_during.append(self.gpu.held if self.gpu else None)
        await asyncio.sleep(0)
        if index in self.fail_on:
            raise self.fail_on[index]
        return [PNG]


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "hk.db")


@pytest.fixture
def store(conn, tmp_path):
    return ImageStore(conn, tmp_path)


@pytest.fixture
def ctx(conn, tmp_path):
    return ToolContext("chat1", "turn-1", Settings(data_dir=tmp_path), conn)


def make_tool(store, gpu=None, comfy=None, default="z-image-turbo", cards=(FLUX, ZIMAGE)):
    gpu = gpu or FakeGpu()
    comfy = comfy or FakeComfy(gpu)
    return GenerateImageTool(list(cards), default, gpu, comfy, store), gpu, comfy


def image_ids(result):
    return [a["id"] for a in result.data["attachments"]]


def test_description_lists_each_image_card_guidance(store):
    tool, _, _ = make_tool(store, cards=(ZIMAGE, CHAT, FLUX))
    assert tool.name == "generate_image"
    assert tool.level_key == "images"
    assert tool.classify({"prompt": "x"}) == ActionClass.LOOK
    desc = tool.description
    for c in (FLUX, ZIMAGE):
        for text in (c.id, c.name, c.prefer_when, c.guidance.strip()):
            assert text in desc
    assert f"{CHAT.id} ({CHAT.name})" not in desc  # only image cards
    assert desc.index(f"{FLUX.id} (") < desc.index(f"{ZIMAGE.id} (")  # card id order
    assert desc.endswith(
        "Write the prompt in that model's style. Put any on-image text in double quotes."
    )
    props = tool.parameters["properties"]
    assert props["model"]["enum"] == ["flux2-klein-4b", "z-image-turbo"]
    assert props["aspect"]["enum"] == list(ASPECTS)
    assert tool.parameters["required"] == ["prompt"]
    assert tool.summarize({"prompt": "a red fox " * 20, "count": 2, "model": FLUX.id}) == (
        "Create 2 image(s) with FLUX.2 Klein 4B: " + ("a red fox " * 20)[:80]
    )
    assert tool.summarize({"prompt": "a fox"}) == "Create 4 image(s) with Z-Image Turbo: a fox"


def test_needs_an_image_card_and_a_known_default(store):
    with pytest.raises(ValueError):
        GenerateImageTool([CHAT], "chat", FakeGpu(), FakeComfy(), store)
    with pytest.raises(ValueError):
        GenerateImageTool([ZIMAGE], "flux2-klein-4b", FakeGpu(), FakeComfy(), store)


async def test_defaults_square_four_images(store, ctx, conn):
    tool, gpu, comfy = make_tool(store)
    result = await tool.run({"prompt": 'A sign saying "OPEN"'}, ctx)

    assert result.ok is True
    assert result.content == (
        'Created 4 image(s) with Z-Image Turbo at 1024x1024. Prompt used: A sign saying "OPEN"'
    )
    ids = image_ids(result)
    assert len(ids) == 4 and len(set(ids)) == 4
    assert result.data == {"attachments": [{"kind": "image", "id": i} for i in ids]}
    for image_id in ids:
        assert image_id not in result.content
    assert "png" not in result.content.lower()
    # the graph got every value filled in from the card and the call
    ksampler = comfy.graphs[0]["3"]["inputs"]
    assert ksampler["steps"] == 8
    assert comfy.graphs[0]["13"]["inputs"]["width"] == 1024
    assert comfy.graphs[0]["28"]["inputs"]["unet_name"] == "u.safetensors"
    assert comfy.graphs[0]["30"]["inputs"]["clip_name"] == "t.safetensors"
    assert comfy.graphs[0]["29"]["inputs"]["vae_name"] == "v.safetensors"
    assert comfy.graphs[0]["27"]["inputs"]["text"] == 'A sign saying "OPEN"'
    assert comfy.graphs[0]["9"]["inputs"]["filename_prefix"] == "hearthkeep/turn-1"
    seeds = [g["3"]["inputs"]["seed"] for g in comfy.graphs]
    assert all(0 <= s < 2**31 for s in seeds)
    # stored for this chat and turn, with the seed each was made with
    records = [store.get(i) for i in ids]
    assert [r.seed for r in records] == seeds
    assert {(r.chat_id, r.turn_id, r.model_id, r.width, r.height) for r in records} == {
        ("chat1", "turn-1", "z-image-turbo", 1024, 1024)
    }
    assert all(store.path(i).read_bytes() == PNG for i in ids)


async def test_aspect_and_explicit_size(store, ctx):
    tool, _, comfy = make_tool(store)
    result = await tool.run(
        {"prompt": "fox", "model": FLUX.id, "aspect": "landscape", "count": 1, "seed": 7}, ctx
    )
    assert result.ok and result.content.startswith(
        "Created 1 image(s) with FLUX.2 Klein 4B at 1536x864."
    )
    graph = comfy.graphs[0]
    assert graph["66"]["inputs"] == {"width": 1536, "height": 864, "batch_size": 1}
    assert graph["62"]["inputs"]["steps"] == 4
    assert graph["73"]["inputs"]["noise_seed"] == 7

    result = await tool.run(
        {"prompt": "fox", "aspect": "portrait", "width": 768, "height": 1280, "count": 2,
         "seed": 100},
        ctx,
    )  # fmt: skip
    assert "at 768x1280" in result.content
    assert [g["13"]["inputs"]["width"] for g in comfy.graphs[1:]] == [768, 768]
    assert [g["3"]["inputs"]["seed"] for g in comfy.graphs[1:]] == [100, 101]
    assert [store.get(i).height for i in image_ids(result)] == [1280, 1280]

    result = await tool.run({"prompt": "fox", "aspect": "banner", "width": 1024, "count": 1}, ctx)
    assert "at 1024x640" in result.content  # one side given: the other comes from the aspect


async def test_invalid_size_is_error_result_not_raise(store, ctx):
    tool, gpu, comfy = make_tool(store)
    result = await tool.run({"prompt": "fox", "width": 1000, "height": 1024}, ctx)
    assert result.ok is False
    assert result.content.startswith("That image size isn't supported: ")
    assert "width 1000" in result.content
    assert comfy.graphs == [] and gpu.log == []


async def test_render_failure_restores_chat_model(store, ctx):
    image = replace(ZIMAGE, vram_gb=10.0)
    backend = FakeBackend()
    sched = GpuScheduler(
        {CHAT.id: CHAT, image.id: image}, {"llama_server": backend, "comfyui": backend}, 16.0
    )
    async with sched.lease(CHAT.id):
        pass
    assert sched.resident() == ["chat"]
    comfy = FakeComfy(fail_on={0: RenderError("Image generation failed.", "boom detail")})
    tool = GenerateImageTool([image], image.id, sched, comfy, store)

    result = await tool.run({"prompt": "fox"}, ctx)
    assert result.ok is False
    assert result.content == "Image generation failed."
    assert result.data == {"attachments": []}
    assert len(comfy.graphs) == 1  # stopped after the failure
    assert sched.status()["waiting"] == 0

    async with sched.lease(CHAT.id):
        assert sched.resident() == ["chat"]
    assert backend.ops() == [
        ("load", "chat"),
        ("unload", "chat"),
        ("load", "z-image-turbo"),
        ("unload", "z-image-turbo"),
        ("load", "chat"),
    ]


async def test_lease_failure_is_error_result(store, ctx):
    gpu = FakeGpu(fail_with=ProcessError("This model needs more GPU memory than this PC has."))
    tool, _, comfy = make_tool(store, gpu=gpu)
    result = await tool.run({"prompt": "fox"}, ctx)
    assert result.ok is False
    assert result.content == "This model needs more GPU memory than this PC has."
    assert comfy.graphs == []


async def test_partial_success_keeps_saved_images(store, ctx):
    gpu = FakeGpu()
    comfy = FakeComfy(gpu, fail_on={2: RenderError("Image generation took too long.", "x")})
    tool, _, _ = make_tool(store, gpu=gpu, comfy=comfy)
    result = await tool.run({"prompt": "fox", "seed": 5}, ctx)
    assert result.ok is False
    assert result.content == "Image generation took too long."
    ids = image_ids(result)
    assert len(ids) == 2
    assert [store.get(i).seed for i in ids] == [5, 6]
    assert len(comfy.graphs) == 3


async def test_each_render_holds_an_image_lease(store, ctx):
    tool, gpu, comfy = make_tool(store)
    await tool.run({"prompt": "fox", "count": 3}, ctx)
    assert comfy.held_during == ["z-image-turbo"] * 3
    assert gpu.log == [("enter", "z-image-turbo"), ("exit", "z-image-turbo")] * 3


async def test_prefix_is_sanitised(store, conn, tmp_path):
    tool, _, comfy = make_tool(store)
    ctx = ToolContext("chat1", 'a b/../"c', Settings(data_dir=tmp_path), conn)
    await tool.run({"prompt": "fox", "count": 1}, ctx)
    prefix = comfy.graphs[0]["9"]["inputs"]["filename_prefix"]
    assert prefix.startswith("hearthkeep/")
    assert set(prefix) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/-")


def test_build_registry_registers_the_image_tool_only_when_given(store):
    names = [t.name for t in build_registry().all()]
    assert names == ["clock", "note"]
    tool, _, _ = make_tool(store)
    registry = build_registry(image_tool=tool)
    assert [t.name for t in registry.all()] == ["clock", "note", "generate_image"]
    schema = registry.schemas([registry.get("generate_image")])[0]["function"]
    json.dumps(schema)  # serialisable for the chat request
