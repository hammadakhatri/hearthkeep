import asyncio
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from hearthkeep.models.cards import ModelCard, load_cards
from hearthkeep.models.runtime import RuntimeConfig
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_argv(port: int, *extra: str) -> list[str]:
    return [sys.executable, str(FAKE), "--port", str(port), *extra]


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


async def test_start_waits_for_health(managed):
    proc = managed("--slow", "1")
    started = time.monotonic()
    await proc.start()
    assert time.monotonic() - started >= 0.9
    assert proc.running and await proc.is_healthy()
    await proc.stop(timeout=1)
    assert not proc.running and not await proc.is_healthy()


async def test_start_noop_when_already_healthy(managed, tmp_path):
    port = free_port()
    outside = subprocess.Popen(
        fake_argv(port), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + TIMEOUT
        while not await health_ok(f"http://127.0.0.1:{port}/health"):
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        proc = managed("--fail", port=port)  # would fail if it were actually spawned
        await proc.start()
        assert not proc.running
        assert not (tmp_path / "logs" / "fake.log").exists()
    finally:
        outside.kill()
        outside.wait()


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


async def test_missing_executable_raises_process_error(tmp_path):
    proc = ManagedProcess(
        "ghost",
        [str(tmp_path / "no-such-server.exe")],
        "http://127.0.0.1:1/health",
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


async def test_start_again_after_child_crashed(managed):
    proc = managed()
    await proc.start()
    crashed = proc._proc
    crashed.kill()  # the server dies on its own
    await crashed.wait()
    assert not proc.running
    await proc.start()
    assert proc.running and proc._proc is not crashed and await proc.is_healthy()


async def test_output_goes_to_log_file(managed, tmp_path):
    proc = managed()
    await proc.start()
    await proc.stop(timeout=1)
    await proc.start()
    await proc.stop(timeout=1)
    text = (tmp_path / "logs" / "fake.log").read_text(encoding="utf-8")
    assert len(re.findall(r"^--- start \d{4}-\d\d-\d\dT\S+ ---$", text, re.M)) == 2
    assert text.count("fake service ready") == 2  # appended, not truncated
    assert "GET /health" in text  # stderr is captured too


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
    proc = build_llama_process(gemma(tmp_path), llama_rt(tmp_path), tmp_path, key_file)
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


def bare_card(*args: str) -> ModelCard:
    return ModelCard(
        id="bare", name="Bare", kind="chat", provider="llama_server", vram_gb=1.0,
        files={"model": "m.gguf"}, launch_args=args, context=1024,
    )  # fmt: skip


def test_llama_injects_host_and_key_file_when_card_omits_them(tmp_path):
    key_file = tmp_path / "runtime" / "llama-api-key"
    rt = llama_rt(tmp_path, model_dirs={"bare": tmp_path / "m"})
    argv = build_llama_process(bare_card("--model", "{model}"), rt, tmp_path, key_file).argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--api-key-file") + 1] == str(key_file)
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
    rt = llama_rt(tmp_path, model_dirs={"bare": tmp_path / "m"})
    card = bare_card("--model", "{model}", *args)
    with pytest.raises(ProcessError):
        build_llama_process(card, rt, tmp_path, tmp_path / "runtime" / "llama-api-key")


def test_llama_passes_other_braces_through(tmp_path):
    rt = llama_rt(tmp_path, model_dirs={"bare": tmp_path / "m"})
    kwargs = '{"enable_thinking": false}'
    card = bare_card("--model", "{model}", "--chat-template-kwargs", kwargs, "--x", "{nope}")
    argv = build_llama_process(card, rt, tmp_path, tmp_path / "k").argv
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
            gemma(tmp_path), llama_rt(tmp_path, **rt_kwargs), tmp_path, tmp_path / "k"
        )
    assert missing in exc.value.user_message and "models.toml" in exc.value.user_message
    assert str(tmp_path) not in exc.value.user_message


@pytest.mark.parametrize("missing", ["comfyui_python", "comfyui_main", "comfyui_model_root"])
def test_missing_comfyui_path_raises_plain_error(tmp_path, missing):
    rt = replace(comfy_rt(tmp_path), **{missing: None})
    with pytest.raises(ProcessError) as exc:
        build_comfyui_process(rt, tmp_path)
    assert missing in exc.value.user_message and "models.toml" in exc.value.user_message


def test_api_key_file_written_with_value(tmp_path):
    path = write_api_key_file(tmp_path, "sk-local-123")
    assert path == tmp_path / "runtime" / "llama-api-key"
    assert path.read_text(encoding="utf-8") == "sk-local-123"
    write_api_key_file(tmp_path, "sk-local-456")  # re-written, not appended
    assert path.read_text(encoding="utf-8") == "sk-local-456"
    assert [p.name for p in path.parent.iterdir()] == ["llama-api-key"]  # no temp files left
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_ensure_chat_api_key_generates_and_stores(tmp_path):
    store = SecretStore(tmp_path)
    assert store.get("chat_api_key") is None
    key = ensure_chat_api_key(store)
    assert len(key) >= 32 and store.get("chat_api_key") == key
    assert ensure_chat_api_key(store) == key  # stable once stored
    store.put("chat_api_key", "owner-chosen")
    assert ensure_chat_api_key(store) == "owner-chosen"


async def test_llama_start_writes_key_file_and_never_logs_key(tmp_path):
    secret = "fake-chat-key-for-tests-0123456789"  # distinctive, so a leak is easy to spot
    port = free_port()
    card = bare_card(str(FAKE), "--model", "{model}")
    rt = RuntimeConfig(
        llama_server_exe=Path(sys.executable), llama_port=port,
        model_dirs={"bare": tmp_path / "m"},
    )  # fmt: skip
    key_file = tmp_path / "runtime" / "llama-api-key"
    proc = build_llama_process(card, rt, tmp_path, key_file, api_key=lambda: secret)
    proc.ready_timeout = TIMEOUT
    assert secret not in " ".join(proc.argv)
    try:
        await proc.start()
        assert key_file.read_text(encoding="utf-8") == secret
    finally:
        await proc.stop(timeout=1)
    log = (tmp_path / "logs" / "llama-server.log").read_text(encoding="utf-8")
    assert str(key_file) in log and secret not in log


# --- ComfyUI -----------------------------------------------------------------------------


def comfy_rt(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        comfyui_python=tmp_path / "comfy" / "python.exe",
        comfyui_main=tmp_path / "comfy" / "main.py",
        comfyui_port=18188,
        comfyui_model_root=tmp_path / "image models",
        model_dirs={"gemma-4-12b": tmp_path / "chat models"},
    )


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


def test_comfyui_paths_yaml_written(tmp_path):
    build_comfyui_process(comfy_rt(tmp_path), tmp_path)
    text = (tmp_path / "runtime" / "comfyui-paths.yaml").read_text(encoding="utf-8")
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
