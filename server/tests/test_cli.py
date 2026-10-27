"""`hearthkeep serve` wiring: external vs owned chat model, ComfyUI, startup line, shutdown."""

import pytest
from fastapi.testclient import TestClient

from hearthkeep import cli
from hearthkeep.providers.chat import OpenAIChatClient
from hearthkeep.providers.comfyui import ComfyUIClient
from hearthkeep.providers.scheduled_chat import ScheduledChatClient
from hearthkeep.runtime.gpu import ComfyBackend, LlamaBackend
from hearthkeep.secrets import SecretStore

ORIGIN = "http://localhost:8765"


class FakeProcess:
    def __init__(self, name: str, log: list) -> None:
        self.name = name
        self.log = log
        self.running = False

    async def start(self) -> None:
        self.log.append(("start", self.name))
        self.running = True

    async def stop(self, timeout: float = 10.0) -> None:
        self.log.append(("stop", self.name, timeout))
        self.running = False

    async def is_healthy(self) -> bool:
        return self.running


class Serve:
    """Runs `cmd_serve` with fake process builders; `run_server` starts and stops the app
    (lifespan included) and keeps what the app was built with."""

    def __init__(self, data, monkeypatch) -> None:
        self.data = data
        self.log: list = []
        self.llama_calls: list = []
        self.comfy_calls: list = []
        self.hk = None
        self.gpu_status = None
        monkeypatch.setattr(cli, "run_server", self._run_server)

    def _run_server(self, app, host, port) -> None:
        assert host == "127.0.0.1"
        with TestClient(app, base_url=ORIGIN):
            self.hk = app.state.hk
            self.gpu_status = self.hk.gpu.status()
            self.log.append(("running",))

    def make_llama(self, card, rt, data_dir, api_key_file, *, api_key):
        self.llama_calls.append((card, rt, data_dir, api_key_file, api_key))
        return FakeProcess("llama-server", self.log)

    def make_comfy(self, rt, data_dir):
        self.comfy_calls.append((rt, data_dir))
        return FakeProcess("ComfyUI", self.log)

    def __call__(self, *extra: str, vram=12.0) -> int:
        args = cli.build_parser().parse_args(["serve", "--data", str(self.data), *extra])
        return cli.cmd_serve(
            args,
            make_llama=self.make_llama,
            make_comfy=self.make_comfy,
            detect_vram=lambda: vram,
        )


def write(data, name: str, text: str) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / name).write_text(text, encoding="utf-8")


@pytest.fixture
def serve(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write(data, "config.toml", "docker_available = false\n")
    return Serve(data, monkeypatch)


def owned_models_toml(tmp_path, comfy: bool = False) -> str:
    lines = [
        f"llama_server_exe = '{tmp_path / 'llama-server.exe'}'",
        "llama_port = 8091",
        "[model_dirs]",
        f"'gemma-4-12b' = '{tmp_path / 'gemma'}'",
    ]
    if comfy:
        lines[2:2] = [
            f"comfyui_python = '{tmp_path / 'python.exe'}'",
            f"comfyui_main = '{tmp_path / 'ComfyUI' / 'main.py'}'",
            f"comfyui_model_root = '{tmp_path / 'models'}'",
        ]
    return "\n".join(lines) + "\n"


def test_serve_without_models_toml_keeps_external_chat_server(serve, capsys):
    SecretStore(serve.data).put("chat_api_key", "external-key")
    assert serve() == 0
    client = serve.hk.runner._loop._client
    assert type(client) is OpenAIChatClient
    assert client._url == "http://127.0.0.1:8080/v1/chat/completions"
    assert client._api_key == "external-key" and client._model == "gemma-4-12b"
    assert serve.llama_calls == [] and serve.comfy_calls == []
    assert serve.hk.registry.get("generate_image") is None
    assert serve.hk.managed == frozenset()
    assert serve.gpu_status["resident"] == []
    out = capsys.readouterr().out
    assert "Models: " in out and "gemma-4-12b (chat, external server)" in out
    assert "z-image-turbo (image)" in out and "Card problems: 0." in out
    assert "Chat: using the chat server at chat_base_url" in out


def test_serve_owned_chat_model(serve, tmp_path, capsys):
    write(serve.data, "models.toml", owned_models_toml(tmp_path))
    assert serve() == 0
    client = serve.hk.runner._loop._client
    assert isinstance(client, ScheduledChatClient)
    assert client._model_id == "gemma-4-12b"
    inner = client._inner
    assert inner._url == "http://127.0.0.1:8091/v1/chat/completions"
    assert inner._model == "gemma-4-12b"
    key = SecretStore(serve.data).get("chat_api_key")
    assert key and inner._api_key == key  # generated and stored on first run
    [(card, rt, data_dir, key_file, api_key)] = serve.llama_calls
    assert card.id == "gemma-4-12b" and rt.llama_port == 8091
    assert key_file == serve.data / "runtime" / "llama-api-key"
    assert api_key() == key
    assert isinstance(serve.hk.gpu._backends["llama_server"], LlamaBackend)
    assert serve.hk.managed == frozenset({"gemma-4-12b"})
    assert serve.gpu_status["total_gb"] == 12.0
    assert serve.hk.registry.get("generate_image") is None  # ComfyUI not configured
    # nothing is started until the first lease
    assert ("start", "llama-server") not in serve.log
    captured = capsys.readouterr()
    assert "gemma-4-12b (chat, managed)" in captured.out
    assert "differs" in captured.err and "chat_base_url" in captured.err
    assert str(tmp_path) not in captured.out + captured.err


def test_serve_owned_mode_no_warning_when_chat_base_url_matches(serve, tmp_path, capsys):
    write(serve.data, "config.toml",
          "docker_available = false\nchat_base_url = 'http://127.0.0.1:8091/v1'\n")  # fmt: skip
    write(serve.data, "models.toml", owned_models_toml(tmp_path))
    assert serve() == 0
    assert "differs" not in capsys.readouterr().err


def test_serve_llama_without_model_dir_stays_external(serve, tmp_path, capsys):
    write(serve.data, "models.toml", f"llama_server_exe = '{tmp_path / 'llama-server.exe'}'\n")
    assert serve() == 0
    assert type(serve.hk.runner._loop._client) is OpenAIChatClient
    assert serve.llama_calls == []
    assert "model_dirs" in capsys.readouterr().err


def test_serve_with_comfyui_registers_image_tool(serve, tmp_path, capsys):
    write(serve.data, "models.toml", owned_models_toml(tmp_path, comfy=True))
    assert serve() == 0
    assert serve.hk.registry.get("generate_image") is not None
    [(rt, data_dir)] = serve.comfy_calls
    assert data_dir == serve.data
    backend = serve.hk.gpu._backends["comfyui"]
    assert isinstance(backend, ComfyBackend)
    assert backend._free.__func__ is ComfyUIClient.free
    assert backend._free.__self__._base == "http://127.0.0.1:8188"
    assert serve.hk.managed == frozenset({"gemma-4-12b", "z-image-turbo", "flux2-klein-4b"})
    out = capsys.readouterr().out
    assert "z-image-turbo (image, managed)" in out


def test_serve_external_chat_with_comfyui_keeps_chat_out_of_scheduler(serve, tmp_path):
    toml = owned_models_toml(tmp_path, comfy=True).replace("llama_server_exe", "# ")
    write(serve.data, "models.toml", toml)
    assert serve() == 0
    assert type(serve.hk.runner._loop._client) is OpenAIChatClient
    assert serve.hk.managed == frozenset({"z-image-turbo", "flux2-klein-4b"})
    assert set(serve.hk.gpu._backends) == {"comfyui"}
    assert serve.hk.registry.get("generate_image") is not None


def test_serve_stops_children_on_shutdown(serve, tmp_path):
    write(serve.data, "models.toml", owned_models_toml(tmp_path, comfy=True))
    assert serve() == 0
    stops = [entry for entry in serve.log if entry[0] == "stop"]
    assert sorted(stops) == [("stop", "ComfyUI", 10.0), ("stop", "llama-server", 10.0)]
    assert serve.log.index(("running",)) < serve.log.index(stops[0])


def test_serve_gpu_memory_falls_back_to_16(serve, tmp_path):
    write(serve.data, "models.toml", owned_models_toml(tmp_path))
    assert serve(vram=None) == 0
    assert serve.gpu_status["total_gb"] == 16.0


def test_serve_gpu_memory_from_models_toml(serve, tmp_path):
    write(serve.data, "models.toml", owned_models_toml(tmp_path) .replace(
        "llama_port = 8091", "llama_port = 8091\ngpu_vram_gb = 24"))  # fmt: skip
    assert serve(vram=None) == 0
    assert serve.gpu_status["total_gb"] == 24.0


def test_serve_lists_card_problems_without_paths(serve, capsys):
    write(serve.data / "models" / "cards", "broken.toml", "id = 'x'\n")
    assert serve() == 0
    out = capsys.readouterr().out
    assert "Card problems: 1." in out
    assert "Card problem in broken.toml: missing required field name" in out
    assert [e.id_or_file for e in serve.hk.card_errors] == ["broken.toml"]


def test_unreadable_card_path_not_in_startup_line_or_api(serve, capsys):
    # /api/models returns these same CardError objects (see test_api_models_images).
    folder = serve.data / "models" / "cards" / "unreadable.toml"
    folder.mkdir(parents=True)  # a folder named like a card: reading it fails with OSError
    assert serve() == 0
    out = capsys.readouterr().out
    assert "Card problem in unreadable.toml: cannot read card: " in out
    # No path in any spelling (str(OSError) on Windows doubles the backslashes).
    line = next(x for x in out.splitlines() if x.startswith("Card problem in"))
    assert "\\" not in line and "/" not in line and serve.data.parent.name not in out
    [error] = serve.hk.card_errors
    assert "\\" not in error.message and serve.data.parent.name not in error.message


def test_health_owned_chat_model_starts_on_first_use(serve, tmp_path, monkeypatch):
    write(serve.data, "models.toml", owned_models_toml(tmp_path))
    seen = []

    def run_server(app, host, port):
        with TestClient(app, base_url=ORIGIN) as client:
            seen.append(client.get("/api/health").json())
            gpu = app.state.hk.gpu

            async def use_chat():
                async with gpu.lease("gemma-4-12b"):
                    pass

            client.portal.call(use_chat)
            seen.append(client.get("/api/health").json())

    monkeypatch.setattr(cli, "run_server", run_server)
    assert serve(vram=16.0) == 0
    assert seen[0]["chat_model_reachable"] is True
    assert seen[0]["chat_model_state"] == "starts_on_first_use"
    assert seen[0]["chat_model"] == "gemma-4-12b"
    assert seen[1]["chat_model_state"] == "ready"


# ---- start at login --------------------------------------------------------------------


def test_cli_startup_commands(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys
    from pathlib import Path

    from hearthkeep import startup

    data = tmp_path / "data"
    xml: list[str] = []

    def fake_run(argv, **kw):
        if "/Create" in argv:
            xml[:] = [open(argv[argv.index("/XML") + 1], encoding="utf-16").read()]
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "/Delete" in argv:
            xml.clear()
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if xml:
            return subprocess.CompletedProcess(argv, 0, xml[0].encode("utf-16"), b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr(startup, "is_windows", lambda: True)
    monkeypatch.setattr(cli, "startup_run", fake_run)
    startup.clear_cache()

    assert cli.main(["startup", "status", "--data", str(data)]) == 0
    assert capsys.readouterr().out.strip() == "Start at login: off"

    assert cli.main(["startup", "enable", "--data", str(data)]) == 0
    log = data.resolve() / "logs" / "hearthkeep.log"
    assert capsys.readouterr().out.strip() == (
        f"Hearthkeep will start when you sign in to Windows. Logs: {log}"
    )
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert f"<Command>{pythonw}</Command>" in xml[0]

    assert cli.main(["startup", "status", "--data", str(data)]) == 0
    assert capsys.readouterr().out.strip() == f"Start at login: on (data folder {data.resolve()})"

    assert cli.main(["startup", "disable", "--data", str(data)]) == 0
    capsys.readouterr()
    assert cli.main(["startup", "status", "--data", str(data)]) == 0
    assert capsys.readouterr().out.strip() == "Start at login: off"

    monkeypatch.setattr(startup, "is_windows", lambda: False)
    for command in ("enable", "disable", "status"):
        assert cli.main(["startup", command, "--data", str(data)]) == 2
        assert capsys.readouterr().err.strip() == (
            "Start at login is built in only on Windows. "
            "See docs/install.md for macOS and Linux."
        )


def test_serve_log_file_redirects_output(serve, monkeypatch):
    import sys

    log = serve.data / "logs" / "hearthkeep.log"
    log.parent.mkdir(parents=True)
    log.write_text("earlier line\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    try:
        assert serve("--log-file", str(log)) == 0
        assert sys.stdout is sys.stderr and sys.stdout.line_buffering
        print("é after start", file=sys.stderr)
    finally:
        if sys.stdout.name == str(log):
            sys.stdout.close()
    text = log.read_text(encoding="utf-8")
    assert text.startswith("earlier line\n")
    assert "Models:" in text and "Chat:" in text and "é after start" in text


def test_main_module_calls_cli_main(monkeypatch):
    import runpy

    calls = []
    monkeypatch.setattr(cli, "main", lambda argv=None: calls.append(argv) or 0)
    with pytest.raises(SystemExit) as exit_:
        runpy.run_module("hearthkeep", run_name="__main__")
    assert exit_.value.code == 0 and calls == [None]
