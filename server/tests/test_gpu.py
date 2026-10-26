import asyncio
import subprocess
from contextlib import AsyncExitStack

import pytest
from fakes import CHAT, IMAGE, FakeBackend, Holder, card, settle, until

from hearthkeep.models.cards import ModelCard
from hearthkeep.runtime import gpu
from hearthkeep.runtime.gpu import (
    RESERVED_GB,
    ComfyBackend,
    GpuScheduler,
    LlamaBackend,
    detect_vram_gb,
)
from hearthkeep.runtime.supervisor import ProcessError


def scheduler(*cards: ModelCard, total: float = 16.0) -> tuple[GpuScheduler, FakeBackend]:
    backend = FakeBackend()
    sched = GpuScheduler(
        {c.id: c for c in cards}, {"llama_server": backend, "comfyui": backend}, total
    )
    return sched, backend


async def test_resident_model_lease_is_immediate_and_shared():
    sched, backend = scheduler(CHAT, IMAGE)
    async with sched.lease("chat"):
        pass
    assert backend.ops() == [("load", "chat")]
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(sched.lease("chat"))
        await stack.enter_async_context(sched.lease("chat"))  # shared, not queued behind
        assert sched.status()["waiting"] == 0
    assert backend.ops() == [("load", "chat")]
    assert sched.resident() == ["chat"]


async def test_image_lease_waits_for_active_chat_stream():
    sched, backend = scheduler(CHAT, IMAGE)
    chat = Holder(sched, "chat")
    await chat.entered.wait()
    image = Holder(sched, "image")
    await settle()
    assert not image.entered.is_set()
    assert backend.ops() == [("load", "chat")]  # the stream is never cut off
    assert sched.status()["waiting"] == 1
    await chat.release()
    await asyncio.wait_for(image.entered.wait(), 1)
    assert backend.ops() == [("load", "chat"), ("unload", "chat"), ("load", "image")]
    assert sched.resident() == ["image"]
    await image.release()


async def test_fifo_no_starvation():
    sched, backend = scheduler(CHAT, IMAGE)
    order: list[str] = []
    chat1 = Holder(sched, "chat", order, "chat1")
    await chat1.entered.wait()
    image = Holder(sched, "image", order)
    await settle()
    chat2 = Holder(sched, "chat", order, "chat2")  # chat is resident, but the image came first
    await settle()
    assert order == ["chat1"]
    assert sched.status()["waiting"] == 2
    await chat1.release()
    await asyncio.wait_for(image.entered.wait(), 1)
    await settle()
    assert order == ["chat1", "image"] and not chat2.entered.is_set()
    await image.release()
    await asyncio.wait_for(chat2.entered.wait(), 1)
    assert order == ["chat1", "image", "chat2"]
    assert backend.ops() == [
        ("load", "chat"), ("unload", "chat"), ("load", "image"),
        ("unload", "image"), ("load", "chat"),
    ]  # fmt: skip
    await chat2.release()


SMALL = [card(name, 5.0) for name in ("a", "b", "c")]
TWO_FIT = RESERVED_GB + 10.0 + 1.0  # room for two 5 GB cards, not three


async def test_lru_eviction_only_as_needed():
    sched, backend = scheduler(*SMALL, total=TWO_FIT)
    for model_id in ("a", "b", "a"):  # b is now the least recently used
        async with sched.lease(model_id):
            pass
    assert backend.ops() == [("load", "a"), ("load", "b")]  # both fit: nothing evicted
    async with sched.lease("c"):
        pass
    assert backend.ops("unload") == [("unload", "b")]  # just enough, LRU first
    assert sorted(sched.resident()) == ["a", "c"]


async def test_eviction_skips_models_in_use():
    sched, backend = scheduler(*SMALL, total=TWO_FIT)
    a = Holder(sched, "a")  # a is the oldest, but in use
    await a.entered.wait()
    async with sched.lease("b"):
        pass
    async with sched.lease("c"):
        pass
    assert backend.ops("unload") == [("unload", "b")]
    assert sorted(sched.resident()) == ["a", "c"]
    await a.release()


async def test_resident_lease_does_not_wait_for_a_load_that_evicts_nothing():
    sched, backend = scheduler(*SMALL, total=TWO_FIT)
    async with sched.lease("a"):
        pass
    backend.gates["b"] = asyncio.Event()
    loading_b = Holder(sched, "b")
    await until(lambda: ("load", "b") in backend.calls)
    async with sched.lease("a"):  # b needs no eviction, so a is granted at once
        assert not loading_b.entered.is_set()
    backend.gates["b"].set()
    await asyncio.wait_for(loading_b.entered.wait(), 1)
    await loading_b.release()


async def test_oversized_card_raises():
    huge = card("huge", 15.0)
    sched, backend = scheduler(huge, CHAT)
    with pytest.raises(ProcessError) as exc:
        async with sched.lease("huge"):
            pass
    assert exc.value.user_message == "This model needs more GPU memory than this PC has."
    assert backend.calls == [] and sched.status()["waiting"] == 0


async def test_unknown_model_raises():
    sched, backend = scheduler(CHAT)
    with pytest.raises(ProcessError):
        async with sched.lease("nope"):
            pass
    assert backend.calls == []


async def test_failed_load_marks_nothing_resident():
    sched, backend = scheduler(CHAT, IMAGE)
    backend.failures["chat"] = 1
    backend.gates["chat"] = gate = asyncio.Event()
    first = asyncio.create_task(sched.lease("chat").__aenter__())
    await until(lambda: ("load", "chat") in backend.calls)
    second = Holder(sched, "chat")  # queued behind the failing load
    await settle()
    backend.gates["chat"] = second_gate = asyncio.Event()
    gate.set()
    with pytest.raises(ProcessError):
        await first
    await until(lambda: backend.ops("load") == [("load", "chat")] * 2)  # next waiter woken
    assert sched.resident() == [] and sched.status()["used_gb"] == 0.0
    second_gate.set()
    await asyncio.wait_for(second.entered.wait(), 1)
    assert backend.ops("load") == [("load", "chat"), ("load", "chat")]
    assert sched.resident() == ["chat"]
    await second.release()


async def test_failed_load_alone_leaves_nothing_resident():
    sched, backend = scheduler(CHAT, IMAGE)
    backend.failures["chat"] = 1
    with pytest.raises(ProcessError):
        async with sched.lease("chat"):
            pass
    assert sched.resident() == []
    assert sched.status() == {"total_gb": 16.0, "used_gb": 0.0, "resident": [], "waiting": 0}
    async with sched.lease("image"):  # nothing left to evict
        pass
    assert backend.ops() == [
        ("load", "chat"), ("unload", "chat"),  # best-effort cleanup of the failed start
        ("load", "image"),
    ]  # fmt: skip


async def test_crashed_resident_model_is_reloaded():
    sched, backend = scheduler(CHAT, IMAGE)
    async with sched.lease("chat"):
        pass
    backend.ready["chat"] = False  # llama-server died on its own
    backend.calls.clear()
    async with sched.lease("chat"):
        assert backend.calls == [("ready", "chat"), ("load", "chat")]
        assert backend.ready["chat"]
    assert sched.resident() == ["chat"]


async def test_cancelled_waiter_leaves_queue_cleanly():
    sched, backend = scheduler(CHAT, IMAGE)
    chat = Holder(sched, "chat")
    await chat.entered.wait()
    image = Holder(sched, "image")
    await settle()
    image.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await image.task
    assert sched.status()["waiting"] == 0
    async with sched.lease("chat"):  # no eviction pending any more: granted at once
        pass
    await chat.release()
    assert backend.ops() == [("load", "chat")]
    assert sched.resident() == ["chat"]


async def test_waiter_cancelled_right_after_being_granted_gives_it_back():
    sched, backend = scheduler(CHAT, IMAGE)
    chat = sched.lease("chat")
    await chat.__aenter__()
    image = Holder(sched, "image")
    await settle()
    await chat.__aexit__(None, None, None)  # grants the image its load, without yielding
    image.task.cancel()  # ...and it is cancelled before it could start
    with pytest.raises(asyncio.CancelledError):
        await image.task
    assert backend.ops() == [("load", "chat")]  # chat was never unloaded
    assert sched.resident() == ["chat"] and sched.status()["waiting"] == 0
    async with sched.lease("image"):
        pass
    assert sched.resident() == ["image"]


async def test_cancelled_load_unloads_and_marks_nothing_resident():
    sched, backend = scheduler(CHAT, IMAGE)
    backend.gates["chat"] = asyncio.Event()
    loading = Holder(sched, "chat")
    await until(lambda: ("load", "chat") in backend.calls)
    loading.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loading.task
    assert backend.ops() == [("load", "chat"), ("unload", "chat")]
    assert sched.resident() == [] and sched.status()["waiting"] == 0
    del backend.gates["chat"]
    async with sched.lease("chat"):  # the scheduler is free again
        pass
    assert sched.resident() == ["chat"]


async def test_failed_unload_raises_and_keeps_model_resident():
    sched, backend = scheduler(CHAT, IMAGE)
    async with sched.lease("chat"):
        pass
    backend.unload_failures["chat"] = 1
    with pytest.raises(ProcessError):
        async with sched.lease("image"):
            pass
    assert sched.resident() == ["chat"]  # its memory may still be in use
    async with sched.lease("image"):  # tried again on the next lease
        pass
    assert sched.resident() == ["image"]


async def test_failed_reload_keeps_server_other_leases_still_use():
    sched, backend = scheduler(CHAT, IMAGE)
    holder = Holder(sched, "chat")
    await holder.entered.wait()
    backend.ready["chat"] = False  # crashed under the holder
    backend.failures["chat"] = 1
    with pytest.raises(ProcessError):
        async with sched.lease("chat"):
            pass
    assert backend.ops() == [("load", "chat"), ("load", "chat")]  # no unload under the holder
    await holder.release()


def test_every_card_provider_needs_a_backend():
    with pytest.raises(ValueError, match="comfyui"):
        GpuScheduler({"chat": CHAT, "image": IMAGE}, {"llama_server": FakeBackend()}, 16.0)


async def test_swallowed_errors_are_logged_by_type_only(caplog):
    sched, backend = scheduler(CHAT, IMAGE)
    async with sched.lease("chat"):
        pass

    async def broken_ready(c):
        raise RuntimeError("secret detail")

    backend.is_ready = broken_ready
    backend.failures["chat"] = 1
    backend.unload_failures["chat"] = 1  # the cleanup after the failed reload fails too
    with caplog.at_level("WARNING", logger="hearthkeep.runtime.gpu"):
        with pytest.raises(ProcessError):
            async with sched.lease("chat"):
                pass
    records = [r for r in caplog.records if r.name == "hearthkeep.runtime.gpu"]
    assert len(records) == 2
    assert all("RuntimeError" in r.getMessage() for r in records)
    assert not any("secret detail" in r.getMessage() or "free failed" in r.getMessage()
                   or r.exc_info for r in records)  # fmt: skip


async def test_status_shape():
    sched, backend = scheduler(card("chat", 11.04), IMAGE)
    assert sched.status() == {"total_gb": 16.0, "used_gb": 0.0, "resident": [], "waiting": 0}
    chat = Holder(sched, "chat")
    await chat.entered.wait()
    image = Holder(sched, "image")
    await settle()
    status = sched.status()
    assert status == {"total_gb": 16.0, "used_gb": 11.0, "resident": ["chat"], "waiting": 1}
    assert isinstance(status["total_gb"], float) and isinstance(status["used_gb"], float)
    await chat.release()
    await image.entered.wait()
    await image.release()


# --- backends --------------------------------------------------------------------------------


class FakeProcess:
    def __init__(self) -> None:
        self.running = False
        self.healthy = False
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")
        self.running = self.healthy = True

    async def stop(self, timeout: float = 10.0) -> None:
        self.calls.append("stop")
        self.running = self.healthy = False

    async def is_healthy(self) -> bool:
        return self.healthy


async def test_llama_backend_starts_stops_and_checks_its_own_child():
    built: list[str] = []
    procs: dict[str, FakeProcess] = {}

    def process_for(c: ModelCard) -> FakeProcess:
        built.append(c.id)
        return procs.setdefault(c.id, FakeProcess())

    backend = LlamaBackend(process_for)
    assert await backend.is_ready(CHAT) is False  # nothing started yet
    await backend.load(CHAT)
    assert await backend.is_ready(CHAT) is True
    procs["chat"].healthy = False  # crashed
    assert await backend.is_ready(CHAT) is False
    await backend.load(CHAT)
    await backend.unload(CHAT)
    assert procs["chat"].calls == ["start", "start", "stop"]
    assert built == ["chat"]  # one process per card, reused


class FakeComfyControl:
    """`free` and the VRAM reading of a fake ComfyUI. `used` is served in order (the last one
    repeats); `free_error` makes free raise; `read_error` makes reading VRAM raise."""

    def __init__(self, used=(8.0, 1.0), free_error=None, read_error=None) -> None:
        self.used = list(used)
        self.free_error = free_error
        self.read_error = read_error
        self.calls: list[str] = []

    async def free(self) -> None:
        self.calls.append("free")
        if self.free_error is not None:
            raise self.free_error

    async def vram_used(self) -> float:
        self.calls.append("read")
        if self.read_error is not None:
            raise self.read_error
        return self.used.pop(0) if len(self.used) > 1 else self.used[0]


def comfy_backend(proc, control: FakeComfyControl) -> ComfyBackend:
    return ComfyBackend(proc, control.free, control.vram_used, settle_timeout=0.05,
                        poll_interval=0.001)  # fmt: skip


async def test_comfy_backend_frees_models_but_keeps_comfyui_running():
    proc = FakeProcess()
    control = FakeComfyControl(used=(8.0, 8.0, 1.0))  # memory is released a moment later
    backend = comfy_backend(proc, control)
    await backend.unload(IMAGE)  # ComfyUI not running: nothing to free
    assert control.calls == []
    assert await backend.is_ready(IMAGE) is False
    await backend.load(IMAGE)
    assert await backend.is_ready(IMAGE) is True
    await backend.unload(IMAGE)
    assert control.calls == ["read", "free", "read", "read"]
    assert proc.calls == ["start"] and proc.running


async def test_comfy_backend_stops_comfyui_when_memory_never_drops():
    proc = FakeProcess()
    control = FakeComfyControl(used=(8.0,))  # /free answered, but nothing was released
    backend = comfy_backend(proc, control)
    await backend.load(IMAGE)
    await backend.unload(IMAGE)
    assert control.calls[:2] == ["read", "free"] and len(control.calls) > 3  # it polled
    assert proc.calls == ["start", "stop"] and not proc.running


@pytest.mark.parametrize(
    "control",
    [
        FakeComfyControl(free_error=TimeoutError("ReadTimeout")),  # hung
        FakeComfyControl(free_error=RuntimeError("HTTP 500 from /free")),
        FakeComfyControl(read_error=RuntimeError("HTTP 500 from /system_stats")),
    ],
)
async def test_comfy_that_wont_free_is_stopped_and_chat_still_starts(control, caplog):
    proc = FakeProcess()
    chat_backend = FakeBackend()
    sched = GpuScheduler(
        {"chat": CHAT, "image": IMAGE},
        {"llama_server": chat_backend, "comfyui": comfy_backend(proc, control)},
        16.0,
    )
    async with sched.lease("image"):
        pass
    async with sched.lease("chat"):  # evicts the image model
        assert sched.resident() == ["chat"]
    assert proc.calls == ["start", "stop"]
    assert chat_backend.ops() == [("load", "chat")]
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert "ComfyUI" in warning and "500" not in warning  # the type only, never the text


# --- VRAM detection --------------------------------------------------------------------------


@pytest.fixture
def smi(tmp_path, monkeypatch):
    """An nvidia-smi.exe on PATH at an absolute place outside the current folder."""
    exe = tmp_path / "driver" / "nvidia-smi.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(gpu.shutil, "which", lambda name: str(exe))
    return exe


def test_detect_vram_gb_reads_nvidia_smi(monkeypatch, smi):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["timeout"] = argv, kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, stdout="16376\n", stderr="")

    monkeypatch.setattr(gpu.subprocess, "run", run)
    assert detect_vram_gb() == pytest.approx(16376 / 1024)
    assert seen["argv"] == [
        str(smi), "--query-gpu=memory.total", "--format=csv,noheader,nounits"
    ]  # fmt: skip
    assert seen["timeout"] == 5


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("no nvidia-smi"),
        subprocess.TimeoutExpired("nvidia-smi", 5),
        subprocess.CompletedProcess([], 9, stdout="", stderr="no devices"),
        subprocess.CompletedProcess([], 0, stdout="N/A\n", stderr=""),
    ],
)
def test_detect_vram_gb_none_on_failure(monkeypatch, smi, outcome):
    def run(argv, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(gpu.subprocess, "run", run)
    assert detect_vram_gb() is None


def _install(root, *parts: str):
    exe = root.joinpath(*parts)
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    return exe


@pytest.mark.parametrize("which_result", [None, "nvidia-smi.exe", r".\nvidia-smi.exe", "cwd"])
def test_nvidia_smi_fallbacks_and_never_a_bare_or_cwd_name(tmp_path, monkeypatch, which_result):
    monkeypatch.chdir(_install(tmp_path, "cwd", "nvidia-smi.exe").parent)
    if which_result == "cwd":
        which_result = str(tmp_path / "cwd" / "nvidia-smi.exe")  # found in the current folder
    monkeypatch.setattr(gpu.shutil, "which", lambda name: which_result)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "win"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    ran: list[list[str]] = []

    def run(argv, **kwargs):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="8192\n", stderr="")

    monkeypatch.setattr(gpu.subprocess, "run", run)
    assert detect_vram_gb() is None and ran == []  # nothing installed outside the cwd

    nvsmi = _install(tmp_path, "pf", "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")
    assert detect_vram_gb() == 8.0 and ran[-1][0] == str(nvsmi)
    system32 = _install(tmp_path, "win", "System32", "nvidia-smi.exe")
    assert detect_vram_gb() == 8.0 and ran[-1][0] == str(system32)  # preferred over NVSMI


def test_detect_vram_gb_none_without_any_location(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda name: None)
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    assert detect_vram_gb() is None
