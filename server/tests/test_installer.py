"""Install jobs. Every file is tiny and served by `httpx.MockTransport`; `tar.exe` is a fake that
builds the tree it would have unpacked. Nothing touches the network or the real data folder."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hearthkeep import fetching, installer
from hearthkeep.hardware import PcInfo
from hearthkeep.installer import (
    InstallBusy,
    Installer,
    NotEnoughSpace,
    NothingToDo,
)
from hearthkeep.lifecycle import ServerControl
from hearthkeep.models.catalog import SHIPPED_CATALOG, load_catalog
from hearthkeep.models.runtime import load_runtime
from hearthkeep.procutil import NO_WINDOW

QWEN4B = "Qwen_Qwen3.5-4B-Q4_K_M.gguf"
CPU_DIR = Path("tools") / "llama.cpp" / "b12099-cpu"
COMFY_PY = Path("tools/comfyui/ComfyUI_windows_portable/python_embeded/python.exe")
COMFY_MAIN = Path("tools/comfyui/ComfyUI_windows_portable/ComfyUI/main.py")
NO_GPU = PcInfo(gpu_name=None, vram_gb=None, nvidia=False, ram_gb=16.0, free_disk_gb=500.0)
NVIDIA = PcInfo(gpu_name="GPU", vram_gb=16.0, nvidia=True, ram_gb=32.0, free_disk_gb=500.0)


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return buf.getvalue()


def body_for(url: str) -> bytes:
    """Each file's bytes depend on its name only, so the shared text encoder (two URLs, one
    SHA-256) is the same file from either repo."""
    name = url.rsplit("/", 1)[-1]
    if name.startswith("llama-") and name.endswith(".zip"):
        return _zip({"llama-server.exe": b"exe:" + name.encode(), "ggml.dll": b"dll"})
    if name.startswith("cudart-"):
        return _zip({"cudart64_12.dll": b"cudart"})
    return f"bytes of {name}\n".encode() * 5


def fake_catalog_text() -> str:
    """The shipped catalog with every size and SHA-256 swapped for the tiny test file's."""

    def swap(match: re.Match) -> str:
        url = match.group(1)
        data = body_for(url)
        return f'url = "{url}"\nsize = {len(data)}\nsha256 = "{hashlib.sha256(data).hexdigest()}"'

    text = SHIPPED_CATALOG.read_text(encoding="utf-8")
    return re.sub(r'url = "([^"]+)"\nsize = \d+\nsha256 = "[0-9a-f]{64}"', swap, text)


CATALOG = load_catalog(fake_catalog_text())


class Server:
    """A fake download server: serves `body_for(url)`, honours `Range`, and records requests.
    `override[name]` (a URL's last segment) replaces the response for that file."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.override: dict = {}

    def names(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        if name in self.override:
            return self.override[name](request)
        return serve_bytes(request, body_for(str(request.url)))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), trust_env=False)


def serve_bytes(request: httpx.Request, data: bytes) -> httpx.Response:
    asked = request.headers.get("range")
    if asked:
        start = int(asked.removeprefix("bytes=").rstrip("-"))
        return httpx.Response(
            206,
            content=data[start:],
            headers={"content-range": f"bytes {start}-{len(data) - 1}/{len(data)}"},
        )
    return httpx.Response(200, content=data)


class Stall:
    """A response that sends the first `head` bytes in two pieces, then waits forever."""

    def __init__(self, head: int = 8) -> None:
        self.head = head
        self.sent = asyncio.Event()
        self.release = asyncio.Event()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        data = body_for(str(request.url))

        async def stream():
            yield data[: self.head // 2]
            yield data[self.head // 2 : self.head]
            self.sent.set()
            await self.release.wait()
            yield data[self.head :]

        return httpx.Response(200, content=stream())


class FakeTar:
    """Stands in for `subprocess.run` with tar.exe: builds the ComfyUI tree under `-C`."""

    def __init__(self, link: bool = False) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.link = link

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        dest = Path(argv[argv.index("-C") + 1])
        root = dest / "ComfyUI_windows_portable"
        (root / "python_embeded").mkdir(parents=True)
        (root / "python_embeded" / "python.exe").write_bytes(b"py")
        (root / "ComfyUI").mkdir()
        (root / "ComfyUI" / "main.py").write_text("print('hi')\n")
        if self.link:
            make_link(root / "ComfyUI" / "sneaky", dest.parent.parent)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def make_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # a junction needs no special rights on Windows

        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def data(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(installer, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(fetching, "CHUNK", 4)


def make(data, server=None, *, pc=NO_GPU, free=10**12, run=None, **kw):
    server = server or Server()
    control = ServerControl(supervised=True)
    events: list[tuple[str, dict]] = []
    inst = Installer(
        data,
        control,
        catalog=CATALOG,
        client_factory=server.client,
        run=run or FakeTar(),
        disk=lambda path: SimpleNamespace(free=free),
        record=lambda event, detail: events.append((event, detail)),
        pc=lambda: pc,
        **kw,
    )
    inst.events = events  # type: ignore[attr-defined]
    return inst, server, control


def put_runtime(data: Path) -> None:
    exe = data / CPU_DIR / "llama-server.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"exe")


def put_comfyui(data: Path) -> None:
    for rel in (COMFY_PY, COMFY_MAIN):
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_bytes(b"x")


def part_of(data: Path, item_id: str, index: int = 0) -> Path:
    sha = CATALOG[item_id].files[index].sha256
    return data / "runtime" / "downloads" / f"{sha}.part"


def models_toml(data: Path) -> dict:
    return tomllib.loads((data / "models.toml").read_text(encoding="utf-8"))


async def test_install_chat_model_end_to_end(data):
    inst, server, control = make(data)
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    assert [i.id for i in job.items] == ["llama-cpu", "chat-qwen3.5-4b"]
    assert job.state == "running" and inst.current() is job
    await inst.wait()

    assert job.state == "done", job.to_json()
    assert [(i.state, i.error) for i in job.items] == [("done", None), ("done", None)]
    assert all(i.done_bytes == i.total_bytes > 0 for i in job.items)
    exe = data / CPU_DIR / "llama-server.exe"
    assert exe.read_bytes().startswith(b"exe:")
    model = data / "models" / "chat" / "qwen3.5-4b" / QWEN4B
    assert model.read_bytes() == body_for(QWEN4B)
    assert list((data / "runtime" / "downloads").iterdir()) == []

    rt = load_runtime(data)
    assert rt.llama_server_exe == exe.resolve()
    assert rt.model_dirs == {"qwen3.5-4b": (data / "models" / "chat" / "qwen3.5-4b").resolve()}
    assert rt.default_chat_model == "qwen3.5-4b" and rt.chat_server == "local"
    assert control.restart_needed == {"models"}
    assert job.to_json()["restart_needed"] is True
    assert set(job.to_json()) == {"id", "state", "items", "restart_needed"}
    assert set(job.to_json()["items"][0]) == {
        "id", "name", "state", "done_bytes", "total_bytes", "speed_bps", "error"
    }  # fmt: skip
    # The job installed both, and now plan finds nothing left to do.
    assert inst.installed(CATALOG["llama-cpu"]) and inst.installed(CATALOG["chat-qwen3.5-4b"])
    assert inst.plan(["chat-qwen3.5-4b"]) == []

    started, finished = inst.events
    assert started == (
        "models.install_started",
        {"items": ["llama-cpu", "chat-qwen3.5-4b"], "bytes": started[1]["bytes"]},
    )
    assert started[1]["bytes"] > 0
    assert finished == (
        "models.install_finished",
        {"items": ["llama-cpu", "chat-qwen3.5-4b"], "state": "done"},
    )
    assert "http" not in repr(inst.events) and str(data) not in repr(inst.events)


async def test_nvidia_pc_gets_the_cuda_runtime_with_its_cudart_zip(data):
    inst, server, _ = make(data, pc=NVIDIA)
    job = inst.start(["chat-qwen3.5-4b"], make_default=False)
    assert [i.id for i in job.items] == ["llama-cuda", "chat-qwen3.5-4b"]
    await inst.wait()
    assert job.state == "done"
    folder = data / "tools" / "llama.cpp" / "b12099-cuda"
    assert (folder / "llama-server.exe").is_file() and (folder / "cudart64_12.dll").is_file()
    toml = models_toml(data)
    assert "default_chat_model" not in toml and "chat_server" not in toml
    assert toml["model_dirs"] == {"qwen3.5-4b": str((data / "models/chat/qwen3.5-4b").resolve())}


async def test_owner_gemma_counts_as_installed(data, tmp_path):
    owner = tmp_path / "owner models"
    owner.mkdir()
    (owner / "gemma-4-12b-it-Q6_K.gguf").write_bytes(b"model")
    (owner / "mmproj-F16.gguf").write_bytes(b"proj")
    (data / "models.toml").write_text(
        f"[model_dirs]\n\"gemma-4-12b\" = '{owner}'\n", encoding="utf-8"
    )
    inst, server, _ = make(data)
    assert inst.installed(CATALOG["chat-gemma-4-12b"])
    assert inst.plan(["chat-gemma-4-12b"]) == []
    with pytest.raises(NothingToDo):
        inst.start(["chat-gemma-4-12b"], make_default=True)
    (owner / "mmproj-F16.gguf").unlink()
    assert not inst.installed(CATALOG["chat-gemma-4-12b"])
    assert server.requests == []


def test_plan_expands_and_refuses_unknown_ids(data):
    inst, _, _ = make(data, pc=NVIDIA)
    plan = [i.id for i in inst.plan(["image-flux2-klein-4b", "chat-qwen3.5-9b"])]
    assert plan == ["llama-cuda", "comfyui", "chat-qwen3.5-9b", "image-flux2-klein-4b"]
    # An explicitly chosen runtime replaces the recommended one.
    assert [i.id for i in inst.plan(["chat-qwen3.5-9b", "llama-vulkan"])] == [
        "llama-vulkan",
        "chat-qwen3.5-9b",
    ]
    put_runtime(data)
    put_comfyui(data)
    plan = [i.id for i in inst.plan(["image-flux2-klein-4b", "chat-qwen3.5-9b"])]
    assert plan == ["chat-qwen3.5-9b", "image-flux2-klein-4b"]
    with pytest.raises(KeyError):
        inst.plan(["chat-qwen3.5-4b", "not-in-the-catalog"])


def test_owner_llama_server_counts_as_a_runtime(data, tmp_path):
    exe = tmp_path / "own" / "llama-server.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"exe")
    (data / "models.toml").write_text(f"llama_server_exe = '{exe}'\n", encoding="utf-8")
    inst, _, _ = make(data)
    assert [i.id for i in inst.plan(["chat-qwen3.5-4b"])] == ["chat-qwen3.5-4b"]
    assert not inst.installed(CATALOG["llama-cpu"])  # not under the catalog's folder


async def test_shared_image_file_downloads_once(data):
    tar = FakeTar()
    inst, server, control = make(data, pc=NVIDIA, run=tar)
    job = inst.start(["image-z-image-turbo", "image-flux2-klein-4b"], make_default=False)
    assert [i.id for i in job.items] == ["comfyui", "image-z-image-turbo", "image-flux2-klein-4b"]
    await inst.wait()
    assert job.state == "done", job.to_json()

    assert server.names().count("qwen_3_4b.safetensors") == 1
    image = data / "models" / "image"
    assert (image / "text_encoders" / "qwen_3_4b.safetensors").is_file()
    assert (image / "diffusion_models" / "flux-2-klein-4b.safetensors").is_file()
    assert (image / "vae" / "ae.safetensors").is_file()
    assert (data / COMFY_PY).is_file() and (data / COMFY_MAIN).is_file()

    [(argv, kw)] = tar.calls
    assert Path(argv[0]).name == "tar.exe" and Path(argv[0]).parent.name == "System32"
    staging = data / "runtime" / "downloads" / "comfyui.unpack"
    assert argv[1:] == ["-xf", str(part_of(data, "comfyui")), "-C", str(staging)]
    assert kw["timeout"] == 30 * 60 and kw["creationflags"] == NO_WINDOW
    assert not staging.exists()

    rt = load_runtime(data)
    assert rt.comfyui_python == (data / COMFY_PY).resolve()
    assert rt.comfyui_main == (data / COMFY_MAIN).resolve()
    assert rt.comfyui_model_root == image.resolve()
    assert rt.default_image_model == "z-image-turbo"  # the default's files are there now
    assert "default_image_model" not in models_toml(data)
    assert control.restart_needed == {"models"}


async def test_image_default_moves_to_an_installed_model(data):
    put_comfyui(data)
    inst, _, _ = make(data, pc=NVIDIA)
    job = inst.start(["image-flux2-klein-4b"], make_default=False)
    await inst.wait()
    assert job.state == "done"
    # z-image-turbo (the default) has no files, so the new model becomes the default.
    assert models_toml(data)["default_image_model"] == "flux2-klein-4b"


def test_not_enough_space_refuses_before_starting(data):
    put_runtime(data)
    inst, server, _ = make(data, free=100)
    with pytest.raises(NotEnoughSpace) as caught:
        inst.start(["chat-qwen3.5-4b"], make_default=True)
    size = CATALOG["chat-qwen3.5-4b"].size_bytes
    assert caught.value.needed == size and caught.value.free == 100
    assert server.requests == [] and inst.current() is None


def test_needed_bytes_counts_parts_shared_files_and_unpacking(data):
    inst, _, _ = make(data)
    model = CATALOG["chat-qwen3.5-4b"]
    cpu = CATALOG["llama-cpu"]
    assert inst.needed_bytes([cpu, model]) == 2 * cpu.size_bytes + model.size_bytes
    part = part_of(data, "chat-qwen3.5-4b")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"12345")
    assert inst.needed_bytes([model]) == model.size_bytes - 5
    z, flux = CATALOG["image-z-image-turbo"], CATALOG["image-flux2-klein-4b"]
    shared = z.files[1].size
    assert inst.needed_bytes([z, flux]) == z.size_bytes + flux.size_bytes - shared


async def test_disk_full_mid_download_fails_item_and_keeps_part(data, monkeypatch):
    put_runtime(data)
    real_open = fetching._open_part

    class Full:
        def __init__(self, f):
            self.f, self.writes = f, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, chunk):
            self.writes += 1
            if self.writes > 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return self.f.write(chunk)

    monkeypatch.setattr(fetching, "_open_part", lambda path, resume: Full(real_open(path, resume)))
    inst, server, control = make(data)
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await inst.wait()
    assert job.state == "failed"
    [item] = job.items
    assert (item.state, item.error) == ("failed", "no_space")
    assert part_of(data, "chat-qwen3.5-4b").stat().st_size == 8
    assert not (data / "models").exists()
    assert not (data / "models.toml").exists() and control.restart_needed == set()
    assert len(server.requests) == 1  # a full disk is not retried
    assert inst.events[-1] == (
        "models.install_finished",
        {"items": ["chat-qwen3.5-4b"], "state": "failed"},
    )


async def test_network_error_retries_three_times_then_fails(data):
    put_runtime(data)
    inst, server, _ = make(data)

    def broken(request):
        raise httpx.ConnectError("unreachable", request=request)

    server.override[QWEN4B] = broken
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await inst.wait()
    assert job.state == "failed"
    assert (job.items[0].state, job.items[0].error) == ("failed", "network")
    assert server.names() == [QWEN4B] * 4  # the first try and three retries


async def test_network_error_then_success_finishes(data):
    put_runtime(data)
    inst, server, _ = make(data)
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow", request=request)
        return serve_bytes(request, body_for(str(request.url)))

    server.override[QWEN4B] = flaky
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await inst.wait()
    assert job.state == "done" and len(calls) == 3


async def test_checksum_mismatch_fails_at_once(data):
    put_runtime(data)
    inst, server, _ = make(data)
    good = body_for(QWEN4B)
    server.override[QWEN4B] = lambda request: httpx.Response(200, content=b"x" * len(good))
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await inst.wait()
    assert (job.items[0].state, job.items[0].error) == ("failed", "checksum")
    assert server.names() == [QWEN4B]
    assert not part_of(data, "chat-qwen3.5-4b").exists()
    assert not (data / "models").exists()


async def test_restart_mid_job_resumes_from_part(data):
    put_runtime(data)
    first, server, _ = make(data)
    stall = Stall(head=8)
    server.override[QWEN4B] = stall
    job = first.start(["chat-qwen3.5-4b"], make_default=True)
    await asyncio.wait_for(stall.sent.wait(), 5)
    await first.shutdown()
    assert job.state == "cancelled"
    part = part_of(data, "chat-qwen3.5-4b")
    assert part.stat().st_size == 8

    second, server2, _ = make(data)
    job2 = second.start(["chat-qwen3.5-4b"], make_default=True)
    await second.wait()
    assert job2.state == "done"
    [request] = server2.requests
    assert request.headers["range"] == "bytes=8-"
    model = data / "models" / "chat" / "qwen3.5-4b" / QWEN4B
    assert model.read_bytes() == body_for(QWEN4B)


async def test_cancel_then_retry(data):
    inst, server, control = make(data)
    stall = Stall(head=8)
    server.override[QWEN4B] = stall
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await asyncio.wait_for(stall.sent.wait(), 5)
    inst.cancel()
    await inst.wait()
    assert job.state == "cancelled"
    assert [(i.id, i.state) for i in job.items] == [
        ("llama-cpu", "done"),
        ("chat-qwen3.5-4b", "cancelled"),
    ]
    assert part_of(data, "chat-qwen3.5-4b").is_file()
    assert control.restart_needed == set()

    del server.override[QWEN4B]
    again = inst.retry()
    assert again is job and job.state == "running"
    assert [i.state for i in job.items] == ["done", "queued"]
    await inst.wait()
    assert job.state == "done"
    assert server.names().count("llama-b12099-bin-win-cpu-x64.zip") == 1
    rt = load_runtime(data)
    assert rt.llama_server_exe == (data / CPU_DIR / "llama-server.exe").resolve()
    assert rt.default_chat_model == "qwen3.5-4b"
    with pytest.raises(NothingToDo):
        inst.retry()


async def test_7z_with_a_link_is_refused(data):
    inst, _, control = make(data, pc=NVIDIA, run=FakeTar(link=True))
    job = inst.start(["comfyui"], make_default=False)
    await inst.wait()
    assert job.state == "failed"
    assert (job.items[0].state, job.items[0].error) == ("failed", "unpack")
    assert not (data / "tools").exists()
    assert not (data / "runtime" / "downloads" / "comfyui.unpack").exists()
    assert control.restart_needed == set()


async def test_7z_that_fails_or_lacks_main_is_refused(data):
    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 2, b"", b"bad archive")

    inst, _, _ = make(data, pc=NVIDIA, run=failing)
    job = inst.start(["comfyui"], make_default=False)
    await inst.wait()
    assert job.items[0].error == "unpack"

    def empty(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    inst, _, _ = make(data, pc=NVIDIA, run=empty)
    job = inst.start(["comfyui"], make_default=False)
    await inst.wait()
    assert job.items[0].error == "unpack"
    assert not (data / "tools").exists()


async def test_unsupported_models_toml_ends_config_not_updated(data):
    put_runtime(data)
    original = b"model_dirs = { \"gemma-4-12b\" = 'C:/elsewhere' }\n"
    (data / "models.toml").write_bytes(original)
    inst, server, control = make(data)
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await inst.wait()
    assert job.state == "failed"
    assert (job.items[-1].state, job.items[-1].error) == ("failed", "config_not_updated")
    model = data / "models" / "chat" / "qwen3.5-4b" / QWEN4B
    assert model.read_bytes() == body_for(QWEN4B)
    assert (data / "models.toml").read_bytes() == original
    assert not (data / "models.toml.before-hearthkeep").exists()
    assert control.restart_needed == set()

    # Fixed by hand: a retry only redoes the edit.
    (data / "models.toml").write_text("llama_port = 8080\n", encoding="utf-8")
    requests = len(server.requests)
    inst.retry()
    await inst.wait()
    assert job.state == "done" and len(server.requests) == requests
    assert load_runtime(data).default_chat_model == "qwen3.5-4b"
    assert (data / "models.toml.before-hearthkeep").read_text() == "llama_port = 8080\n"


async def test_busy(data):
    put_runtime(data)
    inst, server, _ = make(data)
    stall = Stall()
    server.override[QWEN4B] = stall
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await asyncio.wait_for(stall.sent.wait(), 5)
    with pytest.raises(InstallBusy) as caught:
        inst.start(["chat-qwen3.5-9b"], make_default=False)
    assert caught.value.job is job
    with pytest.raises(InstallBusy):
        inst.retry()
    await inst.shutdown()
    assert inst.current() is job and job.state == "cancelled"


async def test_progress_reports_state_and_speed(data, monkeypatch):
    put_runtime(data)
    ticks = iter(range(0, 1000))
    monkeypatch.setattr(fetching, "_now", lambda: next(ticks))
    inst, server, _ = make(data, clock=lambda: next(ticks))
    seen: list[tuple[str, int, float]] = []
    stall = Stall(head=8)
    server.override[QWEN4B] = stall
    job = inst.start(["chat-qwen3.5-4b"], make_default=True)
    await asyncio.wait_for(stall.sent.wait(), 5)
    item = job.items[0]
    seen.append((item.state, item.done_bytes, item.speed_bps))
    await inst.shutdown()
    state, done, speed = seen[0]
    assert state == "downloading" and done > 0 and speed > 0
