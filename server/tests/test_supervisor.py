import asyncio
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from hearthkeep.models.cards import ModelCard, load_cards
from hearthkeep.models.runtime import RuntimeConfig
from hearthkeep.runtime import supervisor
from hearthkeep.runtime.supervisor import (
    ManagedProcess,
    ProcessError,
    build_comfyui_process,
    build_llama_process,
    ensure_chat_api_key,
    write_api_key_file,
)
from hearthkeep.secrets import SecretStore

FAKE = Path(__file__).parent / "fixtures" / "fake_service.py"
TIMEOUT = 5.0
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows behaviour")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_argv(port: int, *extra: str) -> list[str]:
    return [sys.executable, str(FAKE), "--port", str(port), *extra]


def key_source(value: str = "test-chat-key"):
    return lambda: value


@pytest.fixture
async def managed(tmp_path):
    """Builds fake-service processes; every one is stopped afterwards, even on failure."""
    made: list[ManagedProcess] = []

    def make(*extra: str, port: int | None = None, **kwargs) -> ManagedProcess:
        port = port or free_port()
        kwargs.setdefault("ready_timeout", TIMEOUT)
        proc = ManagedProcess(
            "fake",
            fake_argv(port, *extra),
            f"http://127.0.0.1:{port}/health",
            tmp_path / "logs" / "fake.log",
            data_dir=tmp_path,
            **kwargs,
        )
        made.append(proc)
        return proc

    yield make
    for proc in made:
        await proc.stop(timeout=1)


async def health_ok(url: str) -> bool:
    async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(1, connect=0.3)) as client:
        try:
            return (await client.get(url)).status_code == 200
        except httpx.HTTPError:
            return False


async def until(condition, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


def log_text(tmp_path: Path, name: str = "fake.log") -> str:
    return (tmp_path / "logs" / name).read_text(encoding="utf-8")


# --- ManagedProcess ------------------------------------------------------------------------


async def test_start_waits_for_health(managed):
    proc = managed("--slow", "1")
    started = time.monotonic()
    await proc.start()
    assert time.monotonic() - started >= 0.9
    assert proc.running and await proc.is_healthy()
    await proc.stop(timeout=1)
    assert not proc.running and not await proc.is_healthy()


async def test_start_noop_when_own_child_healthy(managed, tmp_path):
    proc = managed()
    await proc.start()
    child = proc._proc
    await proc.start()
    assert proc._proc is child and proc.running
    assert log_text(tmp_path).count("--- start ") == 1


@pytest.mark.parametrize("listener", ["http", "raw"])
async def test_start_refuses_foreign_listener(managed, tmp_path, listener):
    port = free_port()
    if listener == "http":  # even a healthy server is not adopted
        outside = subprocess.Popen(
            fake_argv(port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        stop_outside = lambda: (outside.kill(), outside.wait())  # noqa: E731
        deadline = time.monotonic() + TIMEOUT
        while not await health_ok(f"http://127.0.0.1:{port}/health"):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
    else:
        sock = socket.socket()
        sock.bind(("127.0.0.1", port))
        sock.listen()
        stop_outside = sock.close
    try:
        proc = managed(port=port)
        with pytest.raises(ProcessError) as exc:
            await proc.start()
        assert exc.value.user_message == (
            f"Port {port} is already in use by another program. Change llama_port "
            "(or comfyui_port) in <data>/models.toml, or stop that program."
        )
        assert not proc.running
        assert not (tmp_path / "logs" / "fake.log").exists()  # nothing was spawned
    finally:
        stop_outside()


async def test_start_timeout_raises_and_kills_child(managed, tmp_path):
    proc = managed("--slow", "30", ready_timeout=1.0)
    with pytest.raises(ProcessError) as exc:
        await proc.start()
    assert not proc.running and not await proc.is_healthy()
    assert "<data>/logs/fake.log" in exc.value.user_message
    assert str(tmp_path) not in exc.value.user_message


async def test_child_exit_during_startup_fails_fast(managed, tmp_path):
    proc = managed("--fail")
    started = time.monotonic()
    with pytest.raises(ProcessError) as exc:
        await proc.start()
    assert time.monotonic() - started < TIMEOUT - 1
    assert exc.value.user_message == "fake couldn't start. See <data>/logs/fake.log."
    assert str(tmp_path) not in exc.value.user_message
    assert not proc.running
    assert "--- exited with code 1 ---" in log_text(tmp_path)


async def test_missing_executable_raises_process_error(tmp_path):
    proc = ManagedProcess(
        "ghost",
        [str(tmp_path / "no-such-server.exe")],
        f"http://127.0.0.1:{free_port()}/health",
        tmp_path / "logs" / "ghost.log",
        data_dir=tmp_path,
        ready_timeout=1,
    )
    with pytest.raises(ProcessError) as exc:
        await proc.start()
    assert exc.value.user_message == "ghost couldn't start. See <data>/logs/ghost.log."


async def test_health_probe_ignores_env_proxies(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    clients: list[httpx.AsyncClient] = []
    real = httpx.AsyncClient

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", Spy)
    proc = ManagedProcess("x", ["x"], "http://127.0.0.1:1/health", tmp_path / "x.log")
    assert await proc.is_healthy() is False
    assert len(clients) == 1 and clients[0].trust_env is False and clients[0]._mounts == {}


async def test_stop_kills_unresponsive_child(managed):
    proc = managed("--ignore-term")
    await proc.start()
    started = time.monotonic()
    await proc.stop(timeout=1)
    assert time.monotonic() - started < TIMEOUT
    assert not proc.running and not await proc.is_healthy()
    await proc.stop(timeout=1)  # stopping twice is harmless


async def test_stop_interrupts_inflight_start(managed):
    proc = managed("--slow", "30")
    starting = asyncio.create_task(proc.start())
    await until(lambda: proc.running)
    child = proc._proc
    started = time.monotonic()
    await proc.stop(timeout=1)
    with pytest.raises(ProcessError) as exc:
        await starting
    assert "stopped before it was ready" in exc.value.user_message
    assert time.monotonic() - started < 3
    assert child.returncode is not None and not proc.running


async def test_cancelled_start_stops_and_reaps_child(managed):
    proc = managed("--slow", "30")
    starting = asyncio.create_task(proc.start())
    await until(lambda: proc.running)
    child = proc._proc
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert child.returncode is not None
    assert proc._proc is None and not proc.running


async def unhealthy_own_child(managed, monkeypatch, ready_timeout: float = 0.5):
    """A started child that stays alive but stops answering health checks."""
    proc = managed()
    await proc.start()
    proc.ready_timeout = ready_timeout

    async def never_healthy() -> bool:
        return False

    monkeypatch.setattr(proc, "is_healthy", never_healthy)
    return proc


async def test_failed_start_keeps_child_it_did_not_spawn(managed, monkeypatch):
    proc = await unhealthy_own_child(managed, monkeypatch)
    child = proc._proc
    with pytest.raises(ProcessError):
        await proc.start()  # waits on the existing child, then times out
    assert proc._proc is child and child.returncode is None  # not this call's child: kept


async def test_cancelled_start_keeps_child_it_did_not_spawn(managed, monkeypatch):
    proc = await unhealthy_own_child(managed, monkeypatch, ready_timeout=30)
    child = proc._proc
    starting = asyncio.create_task(proc.start())
    await asyncio.sleep(0.1)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert proc._proc is child and child.returncode is None


class _FailingTerminateJob:
    """A job whose TerminateJobObject fails and whose close kills nothing."""

    def __init__(self) -> None:
        self.closed = False

    def terminate(self, exit_code: int = 1) -> None:
        raise OSError("TerminateJobObject failed")

    def close(self) -> None:
        self.closed = True


async def test_stop_falls_back_when_job_terminate_fails(managed):
    proc = managed("--ignore-term")
    await proc.start()
    child = proc._proc
    real_job, fake = proc._job, _FailingTerminateJob()
    proc._job = fake
    try:
        await asyncio.wait_for(proc.stop(timeout=0.5), TIMEOUT)
    finally:
        if real_job is not None:
            real_job.close()
    assert fake.closed
    assert child.returncode is not None and not proc.running


@windows_only
async def test_job_closed_when_assign_fails(managed, monkeypatch):
    jobs = []

    class FailingAssignJob:
        def __init__(self) -> None:
            self.closed = False
            jobs.append(self)

        def assign(self, pid: int) -> None:
            raise OSError("AssignProcessToJobObject failed")

        def terminate(self, exit_code: int = 1) -> None:
            raise AssertionError("the child is not in this job")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(supervisor, "KillOnCloseJob", FailingAssignJob)
    proc = managed()
    with pytest.raises(ProcessError) as exc:
        await proc.start()
    assert "could not supervise" in exc.value.user_message
    assert len(jobs) == 1 and jobs[0].closed
    assert not proc.running and proc._proc is None and proc._job is None


async def test_cancelled_stop_still_reaps_child(managed):
    proc = managed("--ignore-term")
    await proc.start()
    child = proc._proc
    stopping = asyncio.create_task(proc.stop(timeout=1))
    await asyncio.sleep(0)  # let it begin
    stopping.cancel()
    with suppress(asyncio.CancelledError):
        await stopping
    assert child.returncode is not None
    assert proc._proc is None and proc._exit_task is None


async def test_start_again_after_child_crashed(managed, tmp_path):
    proc = managed()
    await proc.start()
    crashed = proc._proc
    crashed.kill()  # the server dies on its own
    await crashed.wait()
    await until(lambda: "--- exited with code" in log_text(tmp_path))
    assert not proc.running
    await proc.start()
    assert proc.running and proc._proc is not crashed and await proc.is_healthy()


async def test_output_goes_to_log_file(managed, tmp_path):
    proc = managed()
    await proc.start()
    await proc.stop(timeout=1)
    await proc.start()
    await proc.stop(timeout=1)
    text = log_text(tmp_path)
    assert len(re.findall(r"^--- start \d{4}-\d\d-\d\dT\S+ ---$", text, re.M)) == 2
    assert len(re.findall(r"^--- exited with code -?\d+ ---$", text, re.M)) == 2
    assert text.count("fake service ready") == 2  # appended, not truncated
    assert "GET /health" in text  # stderr is captured too


@windows_only
async def test_windows_child_dies_when_job_handle_closes(managed):
    proc = managed()
    await proc.start()
    child = proc._proc
    assert proc._job is not None
    proc._job.close()  # what Windows does when Hearthkeep exits or crashes
    await asyncio.wait_for(child.wait(), TIMEOUT)
    assert not proc.running


# --- llama-server ------------------------------------------------------------------------


def gemma(tmp_path: Path) -> ModelCard:
    cards, _ = load_cards(tmp_path)
    return cards["gemma-4-12b"]


def llama_rt(tmp_path: Path, **kw) -> RuntimeConfig:
    values = {
        "llama_server_exe": tmp_path / "bin" / "llama-server.exe",
        "llama_port": 18080,
        "model_dirs": {"gemma-4-12b": tmp_path / "models" / "gemma"},
    }
    values.update(kw)
    return RuntimeConfig(**values)


def test_llama_argv_filled_and_loopback(tmp_path):
    key_file = tmp_path / "runtime" / "llama-api-key"
    proc = build_llama_process(
        gemma(tmp_path), llama_rt(tmp_path), tmp_path, key_file, api_key=key_source()
    )
    argv = proc.argv
    assert argv[0] == str(tmp_path / "bin" / "llama-server.exe")
    assert argv[argv.index("--host") + 1] == "127.0.0.1" and argv.count("--host") == 1
    model = Path(argv[argv.index("--model") + 1])
    mmproj = Path(argv[argv.index("--mmproj") + 1])
    assert model.is_absolute()
    assert model == tmp_path / "models" / "gemma" / "gemma-4-12b-it-Q6_K.gguf"
    assert mmproj.is_absolute() and mmproj.name == "mmproj-F16.gguf"
    assert argv[argv.index("--port") + 1] == "18080"
    assert argv[argv.index("--api-key-file") + 1] == str(key_file)
    assert not any("{" in a or "}" in a for a in argv)
    assert proc.health_url == "http://127.0.0.1:18080/health"
    assert proc.log_path == tmp_path / "logs" / "llama-server.log"


def test_llama_requires_a_key_source(tmp_path):
    with pytest.raises(TypeError):
        build_llama_process(gemma(tmp_path), llama_rt(tmp_path), tmp_path, tmp_path / "k")


def bare_card(*args: str) -> ModelCard:
    return ModelCard(
        id="bare", name="Bare", kind="chat", provider="llama_server", vram_gb=1.0,
        files={"model": "m.gguf"}, launch_args=args, context=1024,
    )  # fmt: skip


def build_bare(tmp_path: Path, *args: str) -> ManagedProcess:
    rt = llama_rt(tmp_path, model_dirs={"bare": tmp_path / "m"})
    key_file = tmp_path / "runtime" / "llama-api-key"
    return build_llama_process(bare_card(*args), rt, tmp_path, key_file, api_key=key_source())


def test_llama_injects_host_and_key_file_when_card_omits_them(tmp_path):
    argv = build_bare(tmp_path, "--model", "{model}").argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--api-key-file") + 1] == str(tmp_path / "runtime" / "llama-api-key")
    assert argv[argv.index("--port") + 1] == "18080"


@pytest.mark.parametrize(
    "args",
    [
        ("--host", "0.0.0.0"),
        ("--host", "192.168.1.5"),
        ("--host=::",),
        ("--host",),
        ("--api-key", "inline-key"),
        ("--api-key-file", "elsewhere.txt"),
        ("--port", "9999"),
    ],
)
def test_llama_refuses_unsafe_card_args(tmp_path, args):
    with pytest.raises(ProcessError):
        build_bare(tmp_path, "--model", "{model}", *args)


def test_llama_passes_other_braces_through(tmp_path):
    kwargs = '{"enable_thinking": false}'
    argv = build_bare(
        tmp_path, "--model", "{model}", "--chat-template-kwargs", kwargs, "--x", "{nope}"
    ).argv
    assert argv[argv.index("--chat-template-kwargs") + 1] == kwargs
    assert argv[argv.index("--x") + 1] == "{nope}"


@pytest.mark.parametrize(
    ("rt_kwargs", "missing"),
    [
        ({"llama_server_exe": None}, "llama_server_exe"),
        ({"model_dirs": {}}, "model_dirs"),
    ],
)
def test_missing_runtime_path_raises_plain_error(tmp_path, rt_kwargs, missing):
    with pytest.raises(ProcessError) as exc:
        build_llama_process(
            gemma(tmp_path),
            llama_rt(tmp_path, **rt_kwargs),
            tmp_path,
            tmp_path / "k",
            api_key=key_source(),
        )
    assert missing in exc.value.user_message and "models.toml" in exc.value.user_message
    assert str(tmp_path) not in exc.value.user_message


def test_api_key_file_written_with_value(tmp_path):
    path = write_api_key_file(tmp_path, "local-123")
    assert path == tmp_path / "runtime" / "llama-api-key"
    assert path.read_text(encoding="utf-8") == "local-123"
    write_api_key_file(tmp_path, "local-456")  # re-written, not appended
    assert path.read_text(encoding="utf-8") == "local-456"
    assert [p.name for p in path.parent.iterdir()] == ["llama-api-key"]  # no temp files left
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.fixture
def fake_windows_acl(monkeypatch):
    """Pretend to be on Windows and record the icacls calls instead of running them."""
    calls: list[list[str]] = []
    monkeypatch.setattr(supervisor, "_IS_WINDOWS", True)
    monkeypatch.setattr(supervisor, "_run_icacls", calls.append)
    monkeypatch.setenv("USERDOMAIN", "TESTDOMAIN")
    monkeypatch.setenv("USERNAME", "owner")
    monkeypatch.setenv("SystemRoot", str(Path("sysroot").absolute()))
    return calls


def test_key_file_gets_owner_only_acl_on_windows(tmp_path, fake_windows_acl):
    path = write_api_key_file(tmp_path, "local-123")
    folder, file = fake_windows_acl
    assert folder[1:] == [
        str(tmp_path / "runtime"), "/inheritance:r", "/grant:r", "TESTDOMAIN\\owner:(OI)(CI)F"
    ]  # fmt: skip
    assert Path(file[1]).parent == path.parent and Path(file[1]).name.endswith(".tmp")
    assert file[2:] == ["/inheritance:r", "/grant:r", "TESTDOMAIN\\owner:F"]
    assert folder[0] == str(Path("sysroot").absolute() / "System32" / "icacls.exe")
    assert path.read_text(encoding="utf-8") == "local-123"


def test_icacls_fails_closed_without_systemroot(tmp_path, fake_windows_acl, monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    with pytest.raises(ProcessError) as exc:
        write_api_key_file(tmp_path, "local-123")
    assert exc.value.user_message == supervisor._ACL_FAILED
    assert fake_windows_acl == []  # no bare `icacls` looked up on PATH
    assert not (tmp_path / "runtime" / "llama-api-key").exists()


@pytest.mark.parametrize("failing_call", [0, 1])  # the folder ACL, then the file ACL
def test_acl_failure_blocks_key_write(tmp_path, fake_windows_acl, monkeypatch, failing_call):
    calls: list[list[str]] = []

    def icacls(argv):
        calls.append(argv)
        if len(calls) - 1 == failing_call:
            raise ProcessError(supervisor._ACL_FAILED, "icacls exited with code 5")

    monkeypatch.setattr(supervisor, "_run_icacls", icacls)
    with pytest.raises(ProcessError) as exc:
        write_api_key_file(tmp_path, "local-123")
    assert "local-123" not in str(exc.value) and "local-123" not in exc.value.user_message
    assert list((tmp_path / "runtime").iterdir()) == []  # neither the key nor a temp file


async def test_acl_failure_stops_llama_start(tmp_path, fake_windows_acl, monkeypatch):
    def icacls(argv):
        raise ProcessError(supervisor._ACL_FAILED, "icacls exited with code 5")

    monkeypatch.setattr(supervisor, "_run_icacls", icacls)
    rt = RuntimeConfig(
        llama_server_exe=Path(sys.executable), llama_port=free_port(),
        model_dirs={"bare": tmp_path / "m"},
    )  # fmt: skip
    key_file = tmp_path / "runtime" / "llama-api-key"
    proc = build_llama_process(bare_card(str(FAKE)), rt, tmp_path, key_file, api_key=key_source())
    with pytest.raises(ProcessError) as exc:
        await proc.start()
    assert exc.value.user_message == supervisor._ACL_FAILED
    assert not key_file.exists() and not proc.running


def windows_aces(path: Path) -> list[str]:
    out = subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, errors="replace", check=True
    ).stdout
    lines = out.splitlines()
    aces = [lines[0][len(str(path)) :].strip()]
    for line in lines[1:]:
        if not line.strip():
            break
        aces.append(line.strip())
    return [ace.lower() for ace in aces]


@windows_only
def test_windows_key_file_acl_is_owner_only(tmp_path):
    path = write_api_key_file(tmp_path, "local-123")
    user = os.environ["USERNAME"].lower()
    assert len(windows_aces(path)) == 1
    assert windows_aces(path)[0].endswith(f"\\{user}:(f)")  # explicit, nothing inherited
    assert len(windows_aces(path.parent)) == 1
    assert windows_aces(path.parent)[0].endswith(f"\\{user}:(oi)(ci)(f)")


def test_ensure_chat_api_key_generates_and_stores(tmp_path):
    store = SecretStore(tmp_path)
    assert store.get("chat_api_key") is None
    key = ensure_chat_api_key(store)
    assert len(key) >= 32 and store.get("chat_api_key") == key
    assert ensure_chat_api_key(store) == key  # stable once stored
    store.put("chat_api_key", "owner-chosen")
    assert ensure_chat_api_key(store) == "owner-chosen"


async def test_llama_start_with_empty_store_generates_key_file_and_never_logs_key(tmp_path):
    store = SecretStore(tmp_path)
    assert store.get("chat_api_key") is None
    port = free_port()
    card = bare_card(str(FAKE), "--model", "{model}")
    rt = RuntimeConfig(
        llama_server_exe=Path(sys.executable), llama_port=port,
        model_dirs={"bare": tmp_path / "m"},
    )  # fmt: skip
    key_file = tmp_path / "runtime" / "llama-api-key"
    proc = build_llama_process(
        card, rt, tmp_path, key_file, api_key=lambda: ensure_chat_api_key(store)
    )
    proc.ready_timeout = TIMEOUT
    assert not key_file.exists()  # written on start, not at build time
    try:
        await proc.start()
    finally:
        await proc.stop(timeout=1)
    key = store.get("chat_api_key")
    assert key and key_file.read_text(encoding="utf-8") == key
    assert key not in " ".join(proc.argv) and str(key_file) in proc.argv
    log = log_text(tmp_path, "llama-server.log")
    assert str(key_file) in log and key not in log


# --- ComfyUI -----------------------------------------------------------------------------


def comfy_rt(tmp_path: Path, **kw) -> RuntimeConfig:
    values = {
        "comfyui_python": tmp_path / "comfy" / "python.exe",
        "comfyui_main": tmp_path / "comfy" / "main.py",
        "comfyui_port": 18188,
        "comfyui_model_root": tmp_path / "image models",
        "model_dirs": {"gemma-4-12b": tmp_path / "chat models"},
    }
    values.update(kw)
    return RuntimeConfig(**values)


def test_comfyui_argv(tmp_path):
    proc = build_comfyui_process(comfy_rt(tmp_path), tmp_path)
    yaml_path = tmp_path / "runtime" / "comfyui-paths.yaml"
    assert proc.argv == [
        str(tmp_path / "comfy" / "python.exe"), "-s", str(tmp_path / "comfy" / "main.py"),
        "--listen", "127.0.0.1", "--port", "18188", "--disable-auto-launch",
        "--extra-model-paths-config", str(yaml_path),
    ]  # fmt: skip
    assert proc.health_url == "http://127.0.0.1:18188/system_stats"
    assert proc.log_path == tmp_path / "logs" / "comfyui.log"


@pytest.mark.parametrize("missing", ["comfyui_python", "comfyui_main", "comfyui_model_root"])
def test_missing_comfyui_path_raises_plain_error(tmp_path, missing):
    rt = replace(comfy_rt(tmp_path), **{missing: None})
    with pytest.raises(ProcessError) as exc:
        build_comfyui_process(rt, tmp_path)
    assert missing in exc.value.user_message and "models.toml" in exc.value.user_message


def test_comfyui_paths_yaml_written(tmp_path):
    proc = build_comfyui_process(comfy_rt(tmp_path), tmp_path)
    yaml_path = tmp_path / "runtime" / "comfyui-paths.yaml"
    assert not yaml_path.exists()  # written on start, not at build time
    proc.prepare()
    text = yaml_path.read_text(encoding="utf-8")
    root = str(tmp_path / "image models")
    assert text == (
        "hearthkeep:\n"
        f"  base_path: '{root}'\n"
        "  diffusion_models: diffusion_models/\n"
        "  text_encoders: text_encoders/\n"
        "  vae: vae/\n"
        "  loras: loras/\n"
    )
    assert "chat models" not in text  # only the image model root, no other paths


async def test_comfyui_start_writes_paths_yaml(tmp_path):
    port = free_port()
    rt = comfy_rt(
        tmp_path, comfyui_python=Path(sys.executable), comfyui_main=FAKE, comfyui_port=port
    )
    proc = build_comfyui_process(rt, tmp_path)
    proc.ready_timeout = TIMEOUT
    yaml_path = tmp_path / "runtime" / "comfyui-paths.yaml"
    try:
        await proc.start()
        assert "base_path:" in yaml_path.read_text(encoding="utf-8")
    finally:
        await proc.stop(timeout=1)
