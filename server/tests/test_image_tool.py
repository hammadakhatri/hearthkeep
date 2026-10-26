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

    def __init__(self, gpu=None, fail_on=None, per_render=1):
        self.graphs: list[dict] = []
        self.held_during: list[str | None] = []
        self.gpu = gpu
        self.fail_on = fail_on or {}
        self.per_render = per_render

    async def render(self, graph):
        index = len(self.graphs)
        self.graphs.append(graph)
        self.held_during.append(self.gpu.held if self.gpu else None)
        await asyncio.sleep(0)
        if index in self.fail_on:
            raise self.fail_on[index]
        return [PNG + bytes([index, n]) for n in range(self.per_render)]


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
    assert all(store.path(i).read_bytes().startswith(PNG) for i in ids)


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
    assert result.content == (
        "Image generation took too long. 2 of 4 images were made before it stopped."
    )
    ids = image_ids(result)
    for image_id in ids:
        assert image_id not in result.content
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
    assert comfy.graphs[0]["9"]["inputs"]["filename_prefix"] == "hearthkeep/a_b/__/_c"


def test_build_registry_registers_the_image_tool_only_when_given(store):
    names = [t.name for t in build_registry().all()]
    assert names == ["clock", "note"]
    tool, _, _ = make_tool(store)
    registry = build_registry(image_tool=tool)
    assert [t.name for t in registry.all()] == ["clock", "note", "generate_image"]
    schema = registry.schemas([registry.get("generate_image")])[0]["function"]
    json.dumps(schema)  # serialisable for the chat request


def test_seed_above_the_random_range_is_rejected_by_the_schema(store):
    import jsonschema

    tool, _, _ = make_tool(store)
    jsonschema.validate({"prompt": "fox", "seed": 2**31 - 1}, tool.parameters)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"prompt": "fox", "seed": 2**31}, tool.parameters)


@pytest.mark.parametrize("error", [OSError("disk full"), ValueError("not a PNG"),
                                   OverflowError("too big")])  # fmt: skip
async def test_save_failure_keeps_earlier_images(store, ctx, error):
    real_save = store.save
    calls = []

    def flaky_save(png, **kwargs):
        calls.append(png)
        if len(calls) == 2:
            raise error
        return real_save(png, **kwargs)

    store.save = flaky_save
    tool, _, comfy = make_tool(store)
    result = await tool.run({"prompt": "fox", "count": 3}, ctx)
    assert result.ok is False
    assert result.content == (
        "Hearthkeep couldn't save the image. 1 of 3 images were made before it stopped."
    )
    assert len(image_ids(result)) == 1
    assert store.get(image_ids(result)[0]) is not None
    assert len(comfy.graphs) == 2


async def test_save_failure_on_first_image_says_only_the_message(store, ctx):
    import sqlite3

    def broken_save(png, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    store.save = broken_save
    tool, _, _ = make_tool(store)
    result = await tool.run({"prompt": "fox", "count": 2}, ctx)
    assert (result.ok, result.content) == (False, "Hearthkeep couldn't save the image.")
    assert result.data == {"attachments": []}


async def test_only_the_first_png_of_a_render_is_kept(store, ctx):
    gpu = FakeGpu()
    tool, _, _ = make_tool(store, gpu=gpu, comfy=FakeComfy(gpu, per_render=3))
    result = await tool.run({"prompt": "fox", "count": 2}, ctx)
    assert result.ok and result.content.startswith("Created 2 image(s)")
    ids = image_ids(result)
    assert len(ids) == 2
    assert [store.path(i).read_bytes()[-2:] for i in ids] == [bytes([0, 0]), bytes([1, 0])]


# ---- editing the owner's photo ----------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"owner photo bytes"
FLUX_EDIT = replace(FLUX, capabilities=("photoreal", "image-edit"),
                    edit_workflow="flux2-klein-4b-edit.json")  # fmt: skip
EDIT_SENTENCE = (
    "To edit or reuse the owner's photo, pass its id as input_image and use flux2-klein-4b."
)
NOT_FOUND = "I can't find that photo. Ask the owner to attach it again."


class UploadingComfy(FakeComfy):
    """FakeComfy that also takes uploads; ComfyUI answers with its own stored name."""

    def __init__(self, gpu=None, fail_upload=None):
        super().__init__(gpu)
        self.uploads: list[tuple[bytes, str, str | None]] = []
        self.fail_upload = fail_upload

    async def upload(self, data, name):
        self.uploads.append((data, name, self.gpu.held if self.gpu else None))
        if self.fail_upload is not None:
            raise self.fail_upload
        return f"stored-{name}"


def save_photo(store, data=JPEG, mime="image/jpeg", ext="jpg", width=1000, height=1000):
    return store.save_upload(data, mime=mime, ext=ext, width=width, height=height).id


def test_description_says_how_to_edit_the_owners_photo(store):
    tool, _, _ = make_tool(store, cards=(FLUX_EDIT, ZIMAGE))
    assert EDIT_SENTENCE in tool.description
    prop = tool.parameters["properties"]["input_image"]
    assert prop["type"] == "string" and "upload id" in prop["description"]
    assert prop["pattern"] == "^[A-Za-z0-9_-]{1,32}$" and "maxLength" not in prop
    assert tool.parameters["required"] == ["prompt"]
    plain, _, _ = make_tool(store)  # no card can edit: nothing to point the model at
    assert "input_image" not in plain.description


async def test_edit_requires_image_edit_card(store, ctx):
    photo = save_photo(store)
    tool, gpu, comfy = make_tool(store, comfy=UploadingComfy(), cards=(FLUX_EDIT, ZIMAGE))
    args = {"prompt": "make it blue", "model": ZIMAGE.id, "input_image": photo}
    result = await tool.run(args, ctx)
    assert result.ok is False
    assert result.content == (
        "Z-Image Turbo can't edit photos. To edit the owner's photo, use FLUX.2 Klein 4B "
        "(flux2-klein-4b)."
    )
    assert comfy.graphs == [] and comfy.uploads == [] and gpu.log == []

    no_editor, _, comfy = make_tool(store, comfy=UploadingComfy())
    result = await no_editor.run({"prompt": "make it blue", "input_image": photo}, ctx)
    assert (result.ok, result.content) == (False, "None of the image models here can edit photos.")
    assert comfy.graphs == [] and comfy.uploads == []


async def test_edit_uses_edit_workflow_and_uploads_input(store, ctx):
    photo = save_photo(store)
    gpu = FakeGpu()
    comfy = UploadingComfy(gpu)
    # The default is Z-Image, but a photo to edit picks the card that can edit.
    tool, _, _ = make_tool(store, gpu=gpu, comfy=comfy, cards=(FLUX_EDIT, ZIMAGE))
    result = await tool.run(
        {"prompt": "make the bag blue", "input_image": photo, "count": 2, "seed": 3}, ctx
    )
    assert result.ok is True, result.content
    assert result.content.startswith("Created 2 image(s) with FLUX.2 Klein 4B at 1024x1024.")
    assert photo not in result.content
    # Uploaded once, with the original bytes, under a ComfyUI-safe name, while leased.
    assert comfy.uploads == [(JPEG, f"hk-{photo}.jpg", FLUX_EDIT.id)]
    assert len(comfy.graphs) == 2
    for seed, graph in zip((3, 4), comfy.graphs, strict=True):
        assert graph["76"] == {"class_type": "LoadImage",
                               "inputs": {"image": f"stored-hk-{photo}.jpg"}}  # fmt: skip
        assert graph["74"]["inputs"]["text"] == "make the bag blue"
        assert graph["73"]["inputs"]["noise_seed"] == seed
        assert graph["62"]["inputs"] == {"steps": 4, "width": 1024, "height": 1024}
        assert graph["70"]["inputs"]["unet_name"] == "u.safetensors"
        assert graph["9"]["inputs"]["filename_prefix"] == "hearthkeep/turn-1"
    assert comfy.held_during == [FLUX_EDIT.id, FLUX_EDIT.id]
    # Saved through the image store exactly like generated images.
    records = [store.get(i) for i in image_ids(result)]
    assert [(r.model_id, r.seed, r.prompt) for r in records] == [
        (FLUX_EDIT.id, 3, "make the bag blue"),
        (FLUX_EDIT.id, 4, "make the bag blue"),
    ]
    assert tool.summarize({"prompt": "x", "input_image": photo, "count": 1}) == (
        "Edit the owner's photo with FLUX.2 Klein 4B: x"
    )


async def test_edit_with_explicit_edit_card(store, ctx):
    photo = save_photo(store, data=PNG, mime="image/png", ext="png")
    tool, _, comfy = make_tool(store, comfy=UploadingComfy(), cards=(FLUX_EDIT, ZIMAGE))
    result = await tool.run(
        {"prompt": "x", "input_image": photo, "model": FLUX_EDIT.id, "count": 1}, ctx
    )
    assert result.ok is True
    assert comfy.uploads[0][:2] == (PNG, f"hk-{photo}.png")


@pytest.mark.parametrize("photo", ["unknown-id", "../../hk.db", "", "a" * 40])
async def test_edit_with_unknown_upload_is_error_result(store, ctx, photo):
    tool, gpu, comfy = make_tool(store, comfy=UploadingComfy(), cards=(FLUX_EDIT, ZIMAGE))
    result = await tool.run({"prompt": "x", "input_image": photo}, ctx)
    assert (result.ok, result.content) == (False, NOT_FOUND)
    assert comfy.uploads == [] and comfy.graphs == [] and gpu.log == []


async def test_edit_with_missing_upload_file_is_error_result(store, ctx, tmp_path):
    photo = save_photo(store)
    (tmp_path / "workspace" / "uploads" / f"{photo}.jpg").unlink()
    tool, _, comfy = make_tool(store, comfy=UploadingComfy(), cards=(FLUX_EDIT, ZIMAGE))
    result = await tool.run({"prompt": "x", "input_image": photo}, ctx)
    assert (result.ok, result.content) == (False, NOT_FOUND)
    assert comfy.uploads == []


async def test_edit_upload_failure_is_error_result(store, ctx):
    photo = save_photo(store)
    comfy = UploadingComfy(fail_upload=RenderError("Image generation failed.", "HTTP 500"))
    tool, _, _ = make_tool(store, comfy=comfy, cards=(FLUX_EDIT, ZIMAGE))
    result = await tool.run({"prompt": "x", "input_image": photo}, ctx)
    assert (result.ok, result.content) == (False, "Image generation failed.")
    assert comfy.graphs == []


@pytest.mark.parametrize(
    ("size", "args", "expected"),
    [
        ((1600, 900), {}, (1536, 864)),  # landscape
        ((900, 1600), {}, (864, 1536)),  # portrait
        ((1000, 980), {}, (1024, 1024)),  # near square
        ((3000, 1200), {}, (1536, 640)),  # banner
        ((900, 1600), {"aspect": "banner"}, (1536, 640)),  # an explicit aspect wins
        ((900, 1600), {"width": 1024}, (1024, 1024)),  # so does an explicit side
    ],
)
async def test_edit_keeps_the_photos_shape(store, ctx, size, args, expected):
    photo = save_photo(store, width=size[0], height=size[1])
    tool, _, comfy = make_tool(store, comfy=UploadingComfy(), cards=(FLUX_EDIT, ZIMAGE))
    result = await tool.run({"prompt": "x", "input_image": photo, "count": 1, **args}, ctx)
    assert result.ok, result.content
    assert f"at {expected[0]}x{expected[1]}." in result.content
    assert comfy.graphs[0]["66"]["inputs"]["width"] == expected[0]
    assert comfy.graphs[0]["66"]["inputs"]["height"] == expected[1]


def test_input_image_schema_rejects_paths(store):
    import jsonschema

    tool, _, _ = make_tool(store, cards=(FLUX_EDIT, ZIMAGE))
    jsonschema.validate({"prompt": "x", "input_image": "Abc_12-3"}, tool.parameters)
    for bad in ("../hk.db", "a" * 33, "", "a.png"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"prompt": "x", "input_image": bad}, tool.parameters)


async def test_workflow_error_mid_sequence_keeps_earlier_images(store, ctx, monkeypatch):
    from hearthkeep.tools import images as tool_module

    real = tool_module.fill_workflow
    filled: list[int] = []

    def fill(graph, values):
        filled.append(values["seed"])
        if len(filled) == 3:
            raise KeyError("unreplaced workflow placeholder: {{missing}}")
        return real(graph, values)

    monkeypatch.setattr(tool_module, "fill_workflow", fill)
    tool, _, comfy = make_tool(store)
    result = await tool.run({"prompt": "fox", "seed": 1}, ctx)
    assert result.ok is False
    assert result.content == "Image generation failed. 2 of 4 images were made before it stopped."
    assert len(image_ids(result)) == 2 and len(comfy.graphs) == 2
    assert "missing" not in result.content


async def test_upload_that_rejects_the_photo_is_error_result(store, ctx):
    photo = save_photo(store)
    comfy = UploadingComfy(fail_upload=ValueError("upload must be a PNG, JPEG or WebP image"))
    tool, _, _ = make_tool(store, comfy=comfy, cards=(FLUX_EDIT, ZIMAGE))
    result = await tool.run({"prompt": "x", "input_image": photo}, ctx)
    assert (result.ok, result.content) == (False, "Image generation failed.")
    assert result.data == {"attachments": []} and comfy.graphs == []
