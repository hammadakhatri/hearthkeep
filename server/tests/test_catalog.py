from __future__ import annotations

import re

import pytest
from copy_rules import forbidden_in

from hearthkeep.models.cards import load_shipped_cards
from hearthkeep.models.catalog import SHIPPED_CATALOG, CatalogError, load_catalog

IDS = [
    "llama-cuda",
    "llama-vulkan",
    "llama-cpu",
    "chat-qwen3.5-4b",
    "chat-qwen3.5-9b",
    "chat-gemma-4-12b",
    "comfyui",
    "image-z-image-turbo",
    "image-flux2-klein-4b",
]
HF = "https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF/resolve/"
PIN = "182be2fd6c7bc44887d88a91cb03ff009cc9f549"


def shipped_text() -> str:
    return SHIPPED_CATALOG.read_text(encoding="utf-8")


def test_shipped_catalog_loads():
    catalog = load_catalog()
    assert list(catalog) == IDS
    gemma = catalog["chat-gemma-4-12b"]
    assert gemma.size_bytes == 10239326688 + 122031552
    assert [f.save_as for f in gemma.files] == ["model", "mmproj"]
    cuda = catalog["llama-cuda"]
    assert cuda.needs_nvidia and cuda.variant == "cuda"
    assert cuda.install_dir == "tools/llama.cpp/b12099-cuda"
    assert [f.unpack for f in cuda.files] == ["zip", "zip"]
    assert catalog["llama-vulkan"].variant == "vulkan" and not catalog["llama-vulkan"].needs_nvidia
    assert catalog["comfyui"].files[0].unpack == "7z"
    assert catalog["comfyui"].main.endswith("ComfyUI/main.py")
    assert all(i.needs_nvidia for i in catalog.values() if i.kind == "image")


def test_owner_text_is_plain_words():
    for item in load_catalog().values():
        assert forbidden_in(item.name) == [] and forbidden_in(item.note) == []
        assert "/" not in item.name and "\\" not in item.note


def test_chat_items_save_as_card_file_names():
    cards = load_shipped_cards()
    for item in load_catalog().values():
        if item.kind in ("chat", "image"):
            card = cards[item.card]
            assert card.kind == item.kind
            assert {f.save_as for f in item.files} == set(card.files)


def test_spec_sizes_are_covered():
    cards = load_shipped_cards()
    chat = [c for c in cards.values() if c.kind == "chat"]
    assert any(c.vram_gb <= 6 for c in chat)
    assert any(6 < c.vram_gb <= 9 for c in chat)
    assert "gemma-4-12b" in cards


def _sub(old: str, new: str, count: int = 1):
    return lambda text: text.replace(old, new, count)


def _drop_cards_of(text: str) -> str:
    return re.sub(r'card = "qwen3.5-9b"\n', "", text, count=1)


def _no_files(text: str) -> str:
    start = text.index('id = "llama-cpu"')
    end = text.index("[[item]]", start)
    block = text[start:end]
    return text[:start] + block[: block.index("[[item.file]]")] + text[end:]


BREAKS = {
    "duplicate id": _sub('id = "llama-vulkan"', 'id = "llama-cpu"'),
    "bad id": _sub('id = "llama-vulkan"', 'id = "Bad Id"'),
    "unknown kind": _sub('kind = "runtime"', 'kind = "gadget"'),
    "no files": _no_files,
    "http url": _sub(
        "https://github.com/ggml-org/llama.cpp/releases",
        "http://github.com/ggml-org/llama.cpp/releases",
    ),
    "bad host": _sub(
        "https://github.com/ggml-org/llama.cpp/releases",
        "https://evil.example.com/ggml-org/llama.cpp/releases",
    ),
    "hf main": _sub(PIN, "main"),
    "hf short pin": _sub(PIN, PIN[:39]),
    "short sha": _sub("a3c901e3289eff7bfbe1315e855bba3026ef03929c88f6104a1d9bf8b093450b", "a3c9"),
    "upper sha": _sub(
        "a3c901e3289eff7bfbe1315e855bba3026ef03929c88f6104a1d9bf8b093450b",
        "A3C901E3289EFF7BFBE1315E855BBA3026EF03929C88F6104A1D9BF8B093450B",
    ),
    "zero size": _sub("size = 37660003", "size = 0"),
    "http licence": _sub(
        'licence_url = "https://huggingface.co/Qwen/Qwen3.5-9B"', 'licence_url = "http://x.org/l"'
    ),
    "missing card": _drop_cards_of,
    "unknown card": _sub('card = "qwen3.5-9b"', 'card = "nope"'),
    "wrong save_as": _sub('save_as = "mmproj"', 'save_as = "extra"'),
    "no variant": _sub('variant = "vulkan"\n', ""),
    "no install_dir": _sub('install_dir = "tools/llama.cpp/b12099-vulkan"\n', ""),
    "no exe": _sub('exe = "llama-server.exe"\n', ""),
    "no comfy python": _sub('python = "ComfyUI_windows_portable/python_embeded/python.exe"\n', ""),
    "absolute install_dir": _sub("tools/llama.cpp/b12099-vulkan", "/tools/llama.cpp/b12099-vulkan"),
    "backslash install_dir": _sub(
        "tools/llama.cpp/b12099-vulkan", "tools\\llama.cpp\\b12099-vulkan"
    ),
    "dotdot install_dir": _sub("tools/llama.cpp/b12099-vulkan", "tools/../b12099-vulkan"),
    "windows drive install_dir": _sub("tools/llama.cpp/b12099-vulkan", "C:/tools/b12099-vulkan"),
    "dotdot exe": _sub('exe = "llama-server.exe"', 'exe = "../llama-server.exe"'),
    "absolute exe": _sub('exe = "llama-server.exe"', 'exe = "/llama-server.exe"'),
    "bad unpack": _sub('unpack = "7z"', 'unpack = "rar"'),
    "not toml": lambda text: "this is = = not toml",
    "wrong schema": _sub("schema = 1", "schema = 2"),
}


@pytest.mark.parametrize("name", sorted(BREAKS))
def test_catalog_rule_refuses(name):
    broken = BREAKS[name](shipped_text())
    assert broken != shipped_text() or name == "not toml"
    with pytest.raises(CatalogError):
        load_catalog(broken)


def _without_items(text: str, *item_ids: str) -> str:
    for item_id in item_ids:
        start = text.rindex("[[item]]", 0, text.index(f'id = "{item_id}"'))
        nxt = text.find("[[item]]", start + 1)
        text = text[:start] + (text[nxt:] if nxt != -1 else "")
    return text


@pytest.mark.parametrize("removed", ["chat-qwen3.5-4b", "chat-qwen3.5-9b"])
def test_catalog_refuses_a_missing_size_band(removed):
    with pytest.raises(CatalogError):
        load_catalog(_without_items(shipped_text(), removed))


def test_catalog_refuses_missing_gemma():
    with pytest.raises(CatalogError):
        load_catalog(_without_items(shipped_text(), "chat-gemma-4-12b"))


def test_catalog_refuses_renamed_gemma():
    with pytest.raises(CatalogError):
        load_catalog(shipped_text().replace('id = "chat-gemma-4-12b"', 'id = "chat-gemma-big"'))


def test_catalog_refuses_chat_item_with_image_card():
    text = shipped_text().replace('card = "qwen3.5-9b"', 'card = "z-image-turbo"')
    with pytest.raises(CatalogError, match="card"):
        load_catalog(text)


def test_catalog_refuses_unparseable_licence_url():
    text = shipped_text().replace(
        'licence_url = "https://huggingface.co/Qwen/Qwen3.5-9B"',
        'licence_url = "https://x.org/\\u0000"',
    )
    with pytest.raises(CatalogError, match="licence_url"):
        load_catalog(text)
