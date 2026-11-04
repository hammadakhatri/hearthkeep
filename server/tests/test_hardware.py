import asyncio
import subprocess
import threading
from collections import namedtuple
from dataclasses import replace

import pytest

from hearthkeep import hardware
from hearthkeep.hardware import PcCache, PcInfo, fits, read_pc, recommend, total_memory_gb
from hearthkeep.models.cards import load_cards
from hearthkeep.models.catalog import load_catalog
from hearthkeep.runtime import gpu
from hearthkeep.runtime.gpu import nvidia_smi_query

Usage = namedtuple("Usage", "total used free")


def pc(vram: float | None, nvidia: bool = True) -> PcInfo:
    return PcInfo(
        gpu_name="GPU" if nvidia else None,
        vram_gb=vram,
        nvidia=nvidia,
        ram_gb=32.0,
        free_disk_gb=500.0,
    )


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.fixture(scope="module")
def cards(tmp_path_factory):
    return load_cards(tmp_path_factory.mktemp("data"))[0]


@pytest.mark.parametrize(
    ("vram", "nvidia", "chat", "runtime", "reason"),
    [
        (24, True, "chat-gemma-4-12b", "llama-cuda", "fits_gpu"),
        (12, True, "chat-gemma-4-12b", "llama-cuda", "fits_gpu"),
        (11.0, True, "chat-gemma-4-12b", "llama-cuda", "fits_gpu"),
        (10.9, True, "chat-qwen3.5-9b", "llama-cuda", "fits_gpu"),
        (8, True, "chat-qwen3.5-9b", "llama-cuda", "fits_gpu"),
        (6, True, "chat-qwen3.5-4b", "llama-cuda", "fits_gpu"),
        (2, True, "chat-qwen3.5-4b", "llama-cpu", "small_gpu"),
        (None, False, "chat-qwen3.5-4b", "llama-cpu", "no_nvidia"),
    ],
)
def test_recommend_by_vram(catalog, cards, vram, nvidia, chat, runtime, reason):
    rec = recommend(pc(vram, nvidia), catalog, cards)
    assert (rec.chat, rec.runtime, rec.reason) == (chat, runtime, reason)


def test_gpu_vram_gb_in_models_toml_wins(tmp_path):
    (tmp_path / "models.toml").write_text("gpu_vram_gb = 16\n", encoding="utf-8")
    info = read_pc(
        tmp_path,
        smi=lambda: ("GPU", 8.0),
        memory=lambda: 32.0,
        disk=lambda p: Usage(1000 * 2**30, 0, 250 * 2**30),
    )
    assert info.vram_gb == 16 and info.nvidia and info.gpu_name == "GPU"
    assert info.ram_gb == 32.0 and info.free_disk_gb == pytest.approx(250)


def test_broken_models_toml_falls_back_to_the_query(tmp_path):
    (tmp_path / "models.toml").write_text("this is [ not toml", encoding="utf-8")
    info = read_pc(
        tmp_path, smi=lambda: ("GPU", 8.0), memory=lambda: None, disk=lambda p: Usage(1, 0, 2**30)
    )
    assert info.vram_gb == 8.0 and info.ram_gb is None


def test_no_gpu_at_all(tmp_path):
    info = read_pc(
        tmp_path, smi=lambda: None, memory=lambda: 8.0, disk=lambda p: Usage(1, 0, 2**30)
    )
    assert (info.gpu_name, info.vram_gb, info.nvidia) == (None, None, False)
    assert info.to_json() == {
        "gpu_name": None,
        "vram_gb": None,
        "nvidia": False,
        "ram_gb": 8.0,
        "free_disk_gb": 1.0,
    }


def _run(outcome):
    def run(argv, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return run


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("gone"),
        subprocess.TimeoutExpired("nvidia-smi", 5),
        subprocess.CompletedProcess([], 1, stdout="", stderr="err"),
        subprocess.CompletedProcess([], 0, stdout="garbage\n", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="GPU, N/A\n", stderr=""),
    ],
)
def test_nvidia_smi_missing_or_failing(tmp_path, monkeypatch, outcome):
    exe = tmp_path / "nvidia-smi.exe"
    monkeypatch.setattr(gpu, "_nvidia_smi_path", lambda: exe)
    assert nvidia_smi_query(run=_run(outcome)) is None


def test_nvidia_smi_not_installed(monkeypatch):
    monkeypatch.setattr(gpu, "_nvidia_smi_path", lambda: None)
    assert nvidia_smi_query(run=_run(AssertionError("must not run"))) is None


def test_nvidia_smi_parses_name_and_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "_nvidia_smi_path", lambda: tmp_path / "nvidia-smi.exe")
    done = subprocess.CompletedProcess([], 0, stdout="NVIDIA GeForce RTX 4080, 16376\n", stderr="")
    name, vram = nvidia_smi_query(run=_run(done))
    assert name == "NVIDIA GeForce RTX 4080" and vram == pytest.approx(16376 / 1024)


def test_fits_rules(catalog, cards):
    nv, no_nv = pc(24), pc(None, False)
    assert not fits(catalog["image-z-image-turbo"], no_nv, cards, catalog)
    assert not fits(catalog["comfyui"], no_nv, cards, catalog)
    assert fits(catalog["image-z-image-turbo"], nv, cards, catalog)
    assert fits(catalog["comfyui"], nv, cards, catalog)
    assert not fits(catalog["llama-cuda"], no_nv, cards, catalog)
    assert fits(catalog["llama-cuda"], nv, cards, catalog)
    assert fits(catalog["llama-vulkan"], no_nv, cards, catalog)
    assert fits(catalog["llama-cpu"], no_nv, cards, catalog)
    assert fits(catalog["chat-qwen3.5-4b"], no_nv, cards, catalog)  # the smallest always fits
    assert fits(catalog["chat-qwen3.5-4b"], pc(1), cards, catalog)
    assert not fits(catalog["chat-qwen3.5-9b"], pc(6), cards, catalog)
    assert fits(catalog["chat-qwen3.5-9b"], pc(7.5), cards, catalog)
    assert not fits(catalog["chat-gemma-4-12b"], no_nv, cards, catalog)


def test_total_memory_is_a_number_or_none():
    value = total_memory_gb()
    assert value is None or value > 0


def test_owner_cards_never_change_the_smallest_catalog_chat_model(catalog, cards):
    owner = {
        **cards,
        "tiny-owner": replace(cards["qwen3.5-4b"], id="tiny-owner", vram_gb=1),
        "qwen3.5-4b": replace(cards["qwen3.5-4b"], vram_gb=9),
    }
    no_gpu = pc(None, False)
    assert recommend(no_gpu, catalog, owner).chat == "chat-qwen3.5-4b"
    fitting = [
        i.id for i in catalog.values() if i.kind == "chat" and fits(i, no_gpu, owner, catalog)
    ]
    assert fitting == ["chat-qwen3.5-4b"]


def test_recommend_without_any_chat_card_is_a_clear_error(catalog):
    with pytest.raises(ValueError, match="no chat model in the catalog"):
        recommend(pc(8), catalog, {})


def test_owner_vram_setting_without_nvidia_still_picks_the_cpu_build(tmp_path, catalog, cards):
    (tmp_path / "models.toml").write_text("gpu_vram_gb = 16\n", encoding="utf-8")
    info = read_pc(
        tmp_path, smi=lambda: None, memory=lambda: None, disk=lambda p: Usage(1, 0, 2**30)
    )
    assert info.vram_gb == 16 and not info.nvidia
    assert recommend(info, catalog, cards).runtime == "llama-cpu"


def test_read_pc_survives_unreadable_models_toml_and_disk(tmp_path, monkeypatch):
    def broken(data_dir):
        raise PermissionError("locked")

    def no_disk(path):
        raise OSError("gone")

    monkeypatch.setattr(hardware, "load_runtime", broken)
    info = read_pc(tmp_path, smi=lambda: ("GPU", 8.0), memory=lambda: None, disk=no_disk)
    assert info.vram_gb == 8.0 and info.free_disk_gb == 0.0


def test_pc_cache_reads_off_the_loop_and_keeps_the_answer_a_minute():
    now = [0.0]
    reads: list[bool] = []
    pc = PcInfo(gpu_name="GPU", vram_gb=8.0, nvidia=True, ram_gb=16.0, free_disk_gb=100.0)

    def read():
        reads.append(threading.current_thread() is threading.main_thread())
        return pc

    cache = PcCache(read, clock=lambda: now[0])
    assert cache.last().nvidia is False  # before the first read: no GPU assumed, nothing read

    async def run():
        assert await cache.get() == pc
        now[0] = 59.0
        await cache.get()
        assert len(reads) == 1
        now[0] = 60.0
        await cache.get()

    asyncio.run(run())
    assert reads == [False, False] and cache.last() == pc
