import json
import re
from pathlib import Path

import pytest

from hearthkeep.config import ConfigError
from hearthkeep.models.cards import CardError, load_cards, load_workflow
from hearthkeep.models.runtime import RuntimeConfig, load_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]

GOOD_CHAT = """
id = "{id}"
name = "Owner chat"
kind = "chat"
provider = "llama_server"
vram_gb = 6.0
context = 8192
capabilities = ["tools"]
launch_args = ["--model", "{{model}}", "--host", "127.0.0.1", "--port", "{{port}}"]

[files]
model = "owner.gguf"

[sampling]
temperature = 0.7
"""


def put_card(data: Path, name: str, body: str) -> None:
    folder = data / "models" / "cards"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(body, encoding="utf-8")


def test_shipped_cards_load(tmp_path):
    cards, errors = load_cards(tmp_path)
    assert errors == []
    assert set(cards) == {"gemma-4-12b", "z-image-turbo", "flux2-klein-4b"}
    gemma, zimage, flux = cards["gemma-4-12b"], cards["z-image-turbo"], cards["flux2-klein-4b"]
    assert (gemma.kind, gemma.provider, gemma.vram_gb) == ("chat", "llama_server", 11.0)
    assert (zimage.kind, zimage.provider, zimage.vram_gb) == ("image", "comfyui", 10.5)
    assert (flux.kind, flux.provider, flux.vram_gb) == ("image", "comfyui", 10.0)
    assert gemma.context == 32768 and gemma.sampling["top_k"] == 64
    assert gemma.capabilities == ("vision", "tools", "thinking")
    assert gemma.files == {"model": "gemma-4-12b-it-Q6_K.gguf", "mmproj": "mmproj-F16.gguf"}
    assert "--api-key-file" in gemma.launch_args and "{api_key_file}" in gemma.launch_args
    assert zimage.default_steps == 8 and flux.default_steps == 4
    assert zimage.workflow == "z-image-turbo.json" and flux.workflow == "flux2-klein-4b.json"
    assert flux.capabilities == ("photoreal",) and flux.edit_workflow is None
    assert zimage.capabilities == ("text-in-image", "photoreal")
    assert zimage.prefer_when and zimage.guidance
    assert all(c.source == "shipped" for c in cards.values())


def test_owner_card_overrides_shipped(tmp_path):
    put_card(tmp_path, "mine.toml", GOOD_CHAT.format(id="gemma-4-12b"))
    put_card(tmp_path, "extra.toml", GOOD_CHAT.format(id="owner-extra"))
    cards, errors = load_cards(tmp_path)
    assert errors == []
    assert cards["gemma-4-12b"].source == "owner"
    assert cards["gemma-4-12b"].name == "Owner chat"
    assert cards["owner-extra"].source == "owner"
    assert cards["z-image-turbo"].source == "shipped"


def test_bad_card_reported_not_fatal(tmp_path):
    put_card(tmp_path, "good.toml", GOOD_CHAT.format(id="good-one"))
    put_card(tmp_path, "nokind.toml", GOOD_CHAT.format(id="no-kind").replace('kind = "chat"\n', ""))
    put_card(tmp_path, "badvram.toml", GOOD_CHAT.format(id="bad-vram").replace("6.0", '"six"'))
    put_card(tmp_path, "weird.toml", GOOD_CHAT.format(id="weird").replace('"chat"', '"video"'))
    put_card(tmp_path, "syntax.toml", "id = [")
    put_card(tmp_path, "BadId.toml", GOOD_CHAT.format(id="Bad Id"))
    cards, errors = load_cards(tmp_path)
    assert "good-one" in cards and "z-image-turbo" in cards
    assert not {"no-kind", "bad-vram", "weird", "Bad Id"} & set(cards)
    assert {e.id_or_file for e in errors} == {
        "nokind.toml",
        "badvram.toml",
        "weird.toml",
        "syntax.toml",
        "BadId.toml",
    }
    assert all(isinstance(e, CardError) and e.message for e in errors)


@pytest.mark.parametrize(
    "args",
    [
        '["--host", "0.0.0.0"]',
        '["--host", "192.168.1.5"]',
        '["--listen", "0.0.0.0"]',
        '["--port", "1", "--host"]',
    ],
)
def test_card_rejects_non_loopback_host(tmp_path, args):
    body = re.sub(r"launch_args = .*", f"launch_args = {args}", GOOD_CHAT.format(id="evil"))
    put_card(tmp_path, "evil.toml", body)
    cards, errors = load_cards(tmp_path)
    assert "evil" not in cards
    assert [e.id_or_file for e in errors] == ["evil.toml"]


def test_card_rejects_out_of_range_vram_and_missing_workflow(tmp_path):
    put_card(tmp_path, "big.toml", GOOD_CHAT.format(id="big").replace("6.0", "200"))
    image = """
id = "img"
name = "Img"
kind = "image"
provider = "comfyui"
vram_gb = 8
workflow = "does-not-exist.json"
[files]
unet = "a.safetensors"
text_encoder = "b.safetensors"
vae = "c.safetensors"
"""
    put_card(tmp_path, "img.toml", image)
    cards, errors = load_cards(tmp_path)
    assert "big" not in cards and "img" not in cards
    assert {e.id_or_file for e in errors} == {"big.toml", "img.toml"}


def test_load_workflow_returns_copy(tmp_path):
    cards, _ = load_cards(tmp_path)
    card = cards["z-image-turbo"]
    first = load_workflow(card)
    first["9"]["inputs"]["filename_prefix"] = "mutated"
    first["new"] = {}
    second = load_workflow(card)
    assert second["9"]["inputs"]["filename_prefix"] == "{{prefix}}"
    assert "new" not in second


def test_load_workflow_errors(tmp_path):
    cards, _ = load_cards(tmp_path)
    with pytest.raises(ValueError):
        load_workflow(cards["flux2-klein-4b"], edit=True)
    with pytest.raises(ValueError):
        load_workflow(cards["gemma-4-12b"])


@pytest.mark.parametrize("card_id", ["z-image-turbo", "flux2-klein-4b"])
def test_workflows_have_placeholders(tmp_path, card_id):
    cards, _ = load_cards(tmp_path)
    graph = load_workflow(cards[card_id])
    text = json.dumps(graph)
    for name in (
        "prompt", "width", "height", "seed", "steps", "prefix", "unet", "text_encoder", "vae"
    ):
        assert "{{" + name + "}}" in text, name
    assert ".safetensors" not in text
    assert all({"class_type", "inputs"} <= set(node) for node in graph.values())
    assert graph["9"]["inputs"]["filename_prefix"] == "{{prefix}}"


def test_runtime_defaults_without_file(tmp_path):
    rt = load_runtime(tmp_path)
    assert isinstance(rt, RuntimeConfig)
    assert rt.llama_server_exe is None and rt.comfyui_python is None
    assert rt.comfyui_main is None and rt.comfyui_model_root is None
    assert rt.gpu_vram_gb is None and rt.model_dirs == {}
    assert (rt.llama_port, rt.comfyui_port) == (8080, 8188)
    assert (rt.default_chat_model, rt.default_image_model) == ("gemma-4-12b", "z-image-turbo")


def test_runtime_reads_values_and_treats_placeholders_as_unset(tmp_path):
    (tmp_path / "models.toml").write_text(
        'llama_server_exe = "<path to llama-server>"\n'
        'comfyui_python = ""\n'
        'comfyui_main = "tools/comfy/main.py"\n'
        'comfyui_model_root = "models-root"\n'
        "llama_port = 9090\ngpu_vram_gb = 16\n"
        'default_image_model = "flux2-klein-4b"\n'
        '[model_dirs]\n"gemma-4-12b" = "gemma-dir"\n"other" = "<folder>"\n',
        encoding="utf-8",
    )
    rt = load_runtime(tmp_path)
    assert rt.llama_server_exe is None and rt.comfyui_python is None
    assert rt.comfyui_main == Path("tools/comfy/main.py")
    assert rt.comfyui_model_root == Path("models-root")
    assert rt.llama_port == 9090 and rt.gpu_vram_gb == 16.0
    assert rt.default_image_model == "flux2-klein-4b"
    assert rt.model_dirs == {"gemma-4-12b": Path("gemma-dir")}


def test_runtime_rejects_unknown_key(tmp_path):
    (tmp_path / "models.toml").write_text('surprise = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_runtime(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "llama_port = 0",
        "comfyui_port = 70000",
        'llama_port = "80"',
        "gpu_vram_gb = 0",
        "llama_server_exe = 5",
        "[model_dirs]\nx = 5",
        "not toml [",
    ],
)
def test_runtime_rejects_bad_values(tmp_path, body):
    (tmp_path / "models.toml").write_text(body + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_runtime(tmp_path)


def test_example_runtime_has_placeholders_only(tmp_path):
    text = (REPO_ROOT / "models.example.toml").read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:[\\/]", text)
    assert "comfyui_model_root" in text and "[model_dirs]" in text
    (tmp_path / "models.toml").write_text(text, encoding="utf-8")
    rt = load_runtime(tmp_path)
    assert rt.llama_server_exe is None and rt.comfyui_model_root is None
    assert rt.model_dirs == {}
