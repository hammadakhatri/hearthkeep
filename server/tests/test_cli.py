"""`hearthkeep serve` wiring: external vs owned chat model, ComfyUI, startup line, shutdown."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hearthkeep import cli, db
from hearthkeep.api.app import DB_FILE
from hearthkeep.lifecycle import EXIT_RESTART, EXIT_USAGE, SUPERVISED_ENV
from hearthkeep.providers.chat import OpenAIChatClient
from hearthkeep.providers.comfyui import ComfyUIClient
from hearthkeep.providers.scheduled_chat import ScheduledChatClient
from hearthkeep.runtime.gpu import ComfyBackend, LlamaBackend
from hearthkeep.runtime.supervisor import write_api_key_file
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
        self.during = None  # called with the TestClient while the server runs
        self.control = None
        self.exit_code = None  # requested on the control while the server runs
        monkeypatch.setattr(cli, "run_server", self._run_server)

    def _run_server(self, app, host, port, *, control=None, **kw) -> int:
        assert host == "127.0.0.1"
        self.control = control
        with TestClient(app, base_url=ORIGIN) as client:
            self.hk = app.state.hk
            self.gpu_status = self.hk.gpu.status()
            self.log.append(("running",))
            if self.during is not None:
                self.during(client)
            if self.exit_code is not None:
                control.request(self.exit_code)
        return control.exit_code or 0

    def query(self, sql: str) -> list[tuple]:
        conn = db.connect(self.data / DB_FILE)
        try:
            return [tuple(row) for row in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def add_passkey(self) -> None:
        cli._settings(self.data)  # creates the database
        conn = db.connect(self.data / DB_FILE)
        conn.execute("INSERT INTO credentials (id, public_key, sign_count) VALUES ('k', x'00', 0)")
        conn.close()

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


# Every existing install's config.toml was copied from the old example, which set
# chat_base_url: that keeps it on the chat server it already uses.
SERVE_CONFIG = "docker_available = false\nchat_base_url = 'http://127.0.0.1:8080/v1'\n"


@pytest.fixture
def serve(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write(data, "config.toml", SERVE_CONFIG)
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
    key = SecretStore(serve.data).get("llama_api_key")
    assert key and inner._api_key == key  # generated and stored on first run
    assert SecretStore(serve.data).get("chat_api_key") is None  # the external key's name
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


def test_serve_owned_mode_no_warning_when_chat_base_url_is_not_in_config(serve, tmp_path, capsys):
    # A fresh install's config.toml has no chat_base_url: nothing there can differ.
    write(serve.data, "config.toml", "docker_available = false\n")
    write(serve.data, "models.toml", owned_models_toml(tmp_path))
    assert serve() == 0
    assert "differs" not in capsys.readouterr().err


def test_serve_llama_without_model_dir_stays_external(serve, tmp_path, capsys):
    write(serve.data, "models.toml", f"llama_server_exe = '{tmp_path / 'llama-server.exe'}'\n")
    assert serve() == 0
    assert type(serve.hk.runner._loop._client) is OpenAIChatClient
    assert serve.llama_calls == []
    assert "model_dirs" in capsys.readouterr().err


def test_chat_server_external_uses_chat_base_url(serve, tmp_path, capsys):
    # Owned models are installed, but the owner chose their own chat server: nothing deleted,
    # nothing warned, and Hearthkeep doesn't run llama-server.
    toml = owned_models_toml(tmp_path).replace(
        "llama_port = 8091", "llama_port = 8091\nchat_server = 'external'"
    )
    write(serve.data, "models.toml", toml)
    SecretStore(serve.data).put("chat_api_key", "external-key")
    assert serve() == 0
    client = serve.hk.runner._loop._client
    assert type(client) is OpenAIChatClient
    assert client._url == "http://127.0.0.1:8080/v1/chat/completions"
    assert client._api_key == "external-key"
    assert serve.llama_calls == []
    assert serve.hk.managed == frozenset()
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Chat: using the chat server at chat_base_url" in captured.out


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

    def run_server(app, host, port, **kw):
        with TestClient(app, base_url=ORIGIN) as client:
            seen.append(client.get("/api/health").json())
            gpu = app.state.hk.gpu

            async def use_chat():
                async with gpu.lease("gemma-4-12b"):
                    pass

            client.portal.call(use_chat)
            seen.append(client.get("/api/health").json())
        return 0

    monkeypatch.setattr(cli, "run_server", run_server)
    assert serve(vram=16.0) == 0
    assert seen[0]["chat_model_reachable"] is True
    assert seen[0]["chat_model_state"] == "starts_on_first_use"
    assert seen[0]["chat_model"] == "gemma-4-12b"
    assert seen[1]["chat_model_state"] == "ready"


# ---- start at login --------------------------------------------------------------------


def test_cli_startup_commands(tmp_path, monkeypatch, capsys, pythonw_beside_python):
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
            "Start at login is built in only on Windows. See docs/install.md for macOS and Linux."
        )


def test_cli_startup_other_folder_and_schtasks_failures(
    tmp_path, monkeypatch, capsys, pythonw_beside_python
):
    import subprocess

    from hearthkeep import startup

    data, other = tmp_path / "data", tmp_path / "other"
    xml = startup.task_xml(pythonw=tmp_path / "pythonw.exe", data_dir=other, user="me")
    answer = subprocess.CompletedProcess([], 0, xml.encode("utf-16"), b"")
    monkeypatch.setattr(startup, "is_windows", lambda: True)
    monkeypatch.setattr(cli, "startup_run", lambda argv, **kw: answer)
    startup.clear_cache()
    assert cli.main(["startup", "status", "--data", str(data)]) == 0
    assert capsys.readouterr().out.strip() == (
        f"Start at login: on, for another data folder ({other})"
    )

    def hangs(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 15)

    monkeypatch.setattr(cli, "startup_run", hangs)
    for command in ("enable", "disable"):
        assert cli.main(["startup", command, "--data", str(data)]) == 1
        assert capsys.readouterr().err.strip() == (
            "error: schtasks didn't answer within 15 seconds."
        )


def test_serve_log_file_redirects_output(serve, monkeypatch):
    import sys

    serve.add_passkey()  # without one, serve --log-file stops before starting (C3)
    log = serve.data / "logs" / "hearthkeep.log"
    log.parent.mkdir(parents=True, exist_ok=True)
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


def test_serve_returns_the_requested_exit_code(serve, monkeypatch):
    monkeypatch.delenv(SUPERVISED_ENV, raising=False)
    serve.exit_code = EXIT_RESTART
    assert serve() == EXIT_RESTART
    assert serve.control.supervised is False and serve.control.exit_code == EXIT_RESTART

    monkeypatch.setenv(SUPERVISED_ENV, "1")
    serve.exit_code = None
    assert serve() == 0
    assert serve.control.supervised is True and serve.control.exit_code is None


def test_serve_with_log_file_never_writes_the_code(serve, tmp_path, monkeypatch):
    import sys

    # _log_to rebinds stdout and stderr; put them back after the test (C20)
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    log = tmp_path / "out.log"
    try:
        # C3: no passkey under the launcher: say so and stop, so the launcher stops too
        assert serve("--log-file", str(log)) == EXIT_USAGE
    finally:
        if sys.stdout.name == str(log):
            sys.stdout.close()
    text = log.read_text(encoding="utf-8")
    assert cli.NO_PASSKEY_YET in text and "enrollment code" not in text.lower()
    assert serve.query("SELECT COUNT(*) FROM enrollment_codes") == [(0,)]
    assert serve.hk is None  # the server never started


def test_serve_without_a_console_issues_no_code(serve, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert serve() == 0  # no log file: it still runs (developers, CI)
    out = capsys.readouterr().out
    assert cli.NO_PASSKEY_YET in out and "enrollment code" not in out.lower()
    assert serve.query("SELECT COUNT(*) FROM enrollment_codes") == [(0,)]


def test_serve_prints_the_code_on_an_interactive_console(serve, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert serve() == 0
    out = capsys.readouterr().out
    assert "First-device enrollment code:" in out and cli.NO_PASSKEY_YET not in out
    assert serve.query("SELECT COUNT(*) FROM enrollment_codes") == [(1,)]


def test_serve_with_a_passkey_says_nothing_about_setup(serve, monkeypatch, capsys):
    serve.add_passkey()
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    assert serve() == 0
    out = capsys.readouterr().out
    assert "enrollment code" not in out.lower() and cli.NO_PASSKEY_YET not in out


def test_build_app_returns_the_app_and_lines_and_issues_no_code(serve):
    from hearthkeep.auth.webauthn import PyWebAuthnVerifier
    from hearthkeep.lifecycle import ServerControl

    settings = cli._settings(serve.data)
    app, lines = cli.build_app(
        settings,
        verifier=PyWebAuthnVerifier(settings),
        control=ServerControl(supervised=False),
        make_llama=serve.make_llama,
        make_comfy=serve.make_comfy,
        detect_vram=lambda: 12.0,
    )
    assert isinstance(app, FastAPI) and lines[0].startswith("Models: ")
    assert serve.query("SELECT COUNT(*) FROM enrollment_codes") == [(0,)]  # C1


def test_run_server_returns_the_requested_exit_code(monkeypatch):
    import threading

    import uvicorn

    from hearthkeep.lifecycle import ServerControl

    built = {}

    class FakeServer:
        def __init__(self, config):
            built["config"] = config
            self.started = False
            self.should_exit = False

        def run(self):
            self.started = True
            built["serving"].wait(5)  # serving until the started callback has run
            built["control"].request(EXIT_RESTART)
            built["should_exit"] = self.should_exit

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    control = ServerControl(supervised=True)
    built["control"] = control
    started = threading.Event()
    built["serving"] = started
    assert cli.run_server(object(), "127.0.0.1", 8765, control=control,
                          on_started=started.set) == EXIT_RESTART  # fmt: skip
    assert built["should_exit"] is True
    config = built["config"]
    assert config.proxy_headers is False and config.timeout_graceful_shutdown == 5
    assert (config.host, config.port) == ("127.0.0.1", 8765)
    assert (config.log_level, config.access_log) == ("info", True)  # serve: full logs
    assert started.wait(5)

    # Setup's window is the owner's: warnings only, no access log.
    cli.run_server(object(), "127.0.0.1", 8765, log_level="warning", access_log=False)
    assert (built["config"].log_level, built["config"].access_log) == ("warning", False)

    class NeverStarts(FakeServer):
        def run(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", NeverStarts)
    # uvicorn.run exits 3 when the server could not start (port busy); keep that code
    assert cli.run_server(object(), "127.0.0.1", 8765) == 3

    class Stops(FakeServer):
        def run(self):
            self.started = True

    monkeypatch.setattr(uvicorn, "Server", Stops)
    assert cli.run_server(object(), "127.0.0.1", 8765) == 0


def test_run_server_waits_for_a_slow_start_and_not_for_an_exit(monkeypatch):
    """The started callback is not dropped after a fixed wait: a first start that takes long
    still announces itself. A server that is exiting before it started never announces."""
    import threading
    import time

    import uvicorn

    monkeypatch.setattr(cli, "STARTED_POLL_S", 0.01)
    monkeypatch.setattr(cli, "STARTED_WAIT_S", 0.05, raising=False)  # the old fixed wait
    announced = threading.Event()

    class SlowStart:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        def run(self):
            time.sleep(0.3)  # longer than any fixed wait above
            self.started = True
            announced.wait(5)

    monkeypatch.setattr(uvicorn, "Server", SlowStart)
    assert cli.run_server(object(), "127.0.0.1", 8765, on_started=announced.set) == 0
    assert announced.is_set()

    calls = []

    class ExitsFirst(SlowStart):
        def run(self):
            self.should_exit = True
            time.sleep(0.1)

    monkeypatch.setattr(uvicorn, "Server", ExitsFirst)
    assert cli.run_server(object(), "127.0.0.1", 8765, on_started=lambda: calls.append(1)) == 3
    time.sleep(0.05)
    assert calls == []


def test_run_server_reraises_an_exit_after_it_started(monkeypatch):
    import uvicorn

    class ExitsLater:
        def __init__(self, config):
            self.started = False
            self.should_exit = False

        def run(self):
            self.started = True
            raise SystemExit(1)

    monkeypatch.setattr(uvicorn, "Server", ExitsLater)
    with pytest.raises(SystemExit):
        cli.run_server(object(), "127.0.0.1", 8765)


def test_local_base_url_maps_localhost_and_brackets_ipv6(tmp_path):
    from hearthkeep.config import Settings, bind_address, local_base_url

    assert bind_address("localhost") == "127.0.0.1" and bind_address("::1") == "::1"
    url = local_base_url
    assert url(Settings(data_dir=tmp_path, host="localhost", port=9)) == "http://127.0.0.1:9"
    assert url(Settings(data_dir=tmp_path, host="127.0.0.1", port=9)) == "http://127.0.0.1:9"
    assert url(Settings(data_dir=tmp_path, host="::1", port=9)) == "http://[::1]:9"


def test_main_module_calls_cli_main(monkeypatch):
    import runpy

    calls = []
    monkeypatch.setattr(cli, "main", lambda argv=None: calls.append(argv) or 0)
    with pytest.raises(SystemExit) as exit_:
        runpy.run_module("hearthkeep", run_name="__main__")
    assert exit_.value.code == 0 and calls == [None]


def test_sandbox_build_builds_both_images(tmp_path, monkeypatch, capsys):
    from hearthkeep.sandbox import image

    built = []
    monkeypatch.setattr(image, "docker_ok", lambda docker=("docker",): True)

    def fake_build(tag, docker=("docker",), image_dir=image.IMAGE_DIR):
        built.append((tag, Path(image_dir)))
        return 0

    monkeypatch.setattr(image, "build_image", fake_build)
    monkeypatch.setattr(image, "image_present", lambda tag, docker=("docker",): True)
    data = str(tmp_path / "data")
    assert cli.main(["sandbox", "build", "--data", data]) == 0
    sandbox_tag = image.image_tag()
    repo_tag = image.image_tag(image.REPO_IMAGE_DIR, image.REPO_IMAGE_NAME)
    assert repo_tag.startswith("hearthkeep-repo:") and len(repo_tag) == len("hearthkeep-repo:") + 12
    assert built == [(sandbox_tag, image.IMAGE_DIR), (repo_tag, image.REPO_IMAGE_DIR)]
    out = capsys.readouterr().out
    assert f"Sandbox image ready: {sandbox_tag}" in out
    assert f"Repo image ready: {repo_tag}" in out

    assert cli.main(["sandbox", "status", "--data", data]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "Docker: running",
        f"Sandbox image: {sandbox_tag} (present)",
        f"Repo image: {repo_tag} (present)",
    ]


def test_sandbox_build_stops_when_the_first_image_fails(tmp_path, monkeypatch, capsys):
    from hearthkeep.sandbox import image

    built = []
    monkeypatch.setattr(image, "docker_ok", lambda docker=("docker",): True)

    def fake_build(tag, docker=("docker",), image_dir=image.IMAGE_DIR):
        built.append(tag)
        return 1

    monkeypatch.setattr(image, "build_image", fake_build)
    assert cli.main(["sandbox", "build", "--data", str(tmp_path / "data")]) == 1
    assert built == [image.image_tag()]
    assert "sandbox image build failed" in capsys.readouterr().err


def test_owned_llama_key_is_its_own_secret(serve, tmp_path):
    """The external server's key (settings.chat_api_key_secret) never reaches llama-server:
    setting or deleting it changes nothing llama-server gets, and switching back to local
    never writes it to the key file."""
    store = SecretStore(serve.data)
    store.put("chat_api_key", "external-key")
    write(serve.data, "models.toml", owned_models_toml(tmp_path))
    assert serve() == 0
    [(card, rt, data_dir, key_file, api_key)] = serve.llama_calls
    llama_key = store.get("llama_api_key")
    assert llama_key and llama_key != "external-key"
    assert serve.hk.runner._loop._client._inner._api_key == llama_key
    assert api_key() == llama_key
    store.put("chat_api_key", "another-external-key")
    assert api_key() == llama_key
    store.delete("chat_api_key")
    assert api_key() == llama_key
    write_api_key_file(serve.data, api_key())  # what prepare() does before every start
    assert key_file.read_text(encoding="utf-8") == llama_key


def test_external_mode_without_a_key_never_sends_the_llama_key(serve, tmp_path):
    store = SecretStore(serve.data)
    store.put("llama_api_key", "llama-generated-key")
    toml = owned_models_toml(tmp_path).replace(
        "llama_port = 8091", "llama_port = 8091\nchat_server = 'external'"
    )
    write(serve.data, "models.toml", toml)
    assert serve() == 0
    client = serve.hk.runner._loop._client
    assert type(client) is OpenAIChatClient and client._api_key == ""
    assert serve.llama_calls == []
