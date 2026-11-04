"""Setup mode and `hearthkeep setup`: the secret-protected hand-over of the first-device code,
the hand-over to the launcher once the first passkey exists, and what a re-run does."""
# ruff: noqa: F811  (the harness fixture is imported from test_api)

from __future__ import annotations

import json
import logging
import socket
import sys
import tomllib
from pathlib import Path

import pytest
from copy_rules import forbidden_in
from fakes import FakeVerifier
from fastapi.testclient import TestClient
from test_api import (  # noqa: F401  (harness is a fixture)
    CSRF,
    ORIGIN,
    enroll,
    harness,
)

from hearthkeep import cli, db, startup
from hearthkeep import host as host_module
from hearthkeep.api import app as app_module
from hearthkeep.api import routes_setup
from hearthkeep.api.app import DB_FILE
from hearthkeep.cli import (
    ALREADY_SET_UP,
    BROWSER_FAILED,
    BROWSER_OPENING,
    FINISHED,
    FINISHED_SLOW,
    FOLDER_NOT_PROTECTED,
    KEEP_OPEN,
    LAUNCHER_NOT_STARTED,
    LOCATION_NOT_SAVED,
    MOVING_DATA,
    OLD_FOLDER_KEPT,
    PORT_BUSY,
    SETUP_INTRO,
    SHORTCUT_FAILED,
    STEP_START,
    STEP_WEB,
    STOPPED_EARLY,
)
from hearthkeep.config import DATA_SUBDIRS
from hearthkeep.host import BAD_CHARACTERS, UNSUPPORTED_HOST, WindowsHost
from hearthkeep.lifecycle import EXIT_RELOCATE, EXIT_STOPPED, EXIT_USAGE, ServerControl
from hearthkeep.push import VAPID_SECRET
from hearthkeep.runtime.supervisor import LLAMA_API_KEY_SECRET, ProcessError
from hearthkeep.secrets import SecretStore
from hearthkeep.setupmode import SetupMode, setup_url
from hearthkeep.shortcut import ShortcutError

SECRET = "s3cret-" + "x" * 30
REAL_RUN_SERVER = cli.run_server  # the fixture replaces cli.run_server; some tests need uvicorn
LOOPBACK_PEER = ("127.0.0.1", 50000)


# ---- helpers ----------------------------------------


def setup_client(harness, secret=SECRET, **kw):
    return harness.start(setup=SetupMode(secret), **kw), secret


def dist_with_index(tmp_path: Path) -> Path:
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>setup</html>", encoding="utf-8")
    return dist


def query(data: Path, sql: str) -> list[tuple]:
    conn = db.connect(data / DB_FILE)
    try:
        return [tuple(row) for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


class FakeHost(WindowsHost):
    """Windows, recorded: the locator and the start-at-login task live in memory (never the
    real %LOCALAPPDATA% or Task Scheduler), the shortcut and the launcher are only noted, and
    every drive is a local one."""

    def __init__(self, tmp_path: Path) -> None:
        self.default = tmp_path / "default-data"
        self.location: Path | None = None
        self.location_writes: list[Path] = []
        self.location_error = False
        self.shortcuts: list[Path] = []
        self.shortcut_error = False
        self.spawned: list[list[str]] = []
        self.spawn_error = False
        self.task: str | None = None  # the start-at-login task's XML; None: no task
        self.enabled: list[Path] = []
        self.protected: list[Path] = []
        self.protect_error = False
        self.reset: list[Path] = []
        self.reset_error = False
        self.status_error = False
        self.status_strict: list[bool] = []

    def drive_type(self, root: str) -> int:
        return 3  # a fixed local disk

    def default_data_dir(self) -> Path:
        return self.default

    def read_location(self) -> Path | None:
        return self.location if self.location is not None and self.location.is_dir() else None

    def write_location(self, data_dir: Path) -> None:
        if self.location_error:
            raise OSError("LOCALAPPDATA is not set, so the data folder can't be recorded.")
        self.location = Path(data_dir).resolve()
        self.location_writes.append(self.location)

    def startup_status(self, *, run=None, strict=False) -> startup.StartupStatus:
        self.status_strict.append(strict)
        if self.status_error:
            raise startup.StartupError("schtasks didn't answer within 30 seconds.")
        if self.task is None:
            return startup.StartupStatus(True, False, None)
        return startup._parse(self.task)

    def enable_startup(self, data_dir: Path, *, run=None) -> None:
        self.enabled.append(Path(data_dir))
        self.task = startup.task_xml(
            pythonw=Path(r"C:\py\pythonw.exe"), data_dir=Path(data_dir), user=r"HOMEPC\owner"
        )

    def install_shortcut(self, data_dir: Path, *, run=None) -> None:
        if self.shortcut_error:
            raise ShortcutError("powershell failed")
        self.shortcuts.append(Path(data_dir))

    def protect_folder(self, path: Path, *, run=None) -> None:
        if self.protect_error:
            raise host_module.FolderProtectError("icacls.exe exit code 5")
        self.protected.append(Path(path))

    def reset_folder_permissions(self, path: Path, *, run=None) -> None:
        if self.reset_error:
            raise host_module.FolderProtectError("icacls.exe exit code 5")
        self.reset.append(Path(path))

    def spawn_detached(self, argv: list[str], *, popen=None):
        if self.spawn_error:
            raise OSError("pythonw.exe is missing")
        self.spawned.append(list(argv))
        return None


class FakeRunServer:
    """Stands in for uvicorn: runs the app's lifespan in a TestClient from this PC, announces
    itself, and (unless `enroll` is False) does what the setup page does: check the secret,
    fetch the code and make the first passkey with it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.kwargs: list[dict] = []
        self.enroll = True
        self.never_starts = False  # as uvicorn when the port was taken: STARTUP_FAILURE
        self.secret: str | None = None
        self.code: str | None = None
        # Folders the setup page moves the data to, one per run, before anything else.
        self.moves: list[Path] = []
        self.before_move = lambda data: None  # called with the current folder before a move
        self.after_move = lambda: None  # called once the page's move was accepted
        self.secrets: list[str] = []
        self.data_dirs: list[Path] = []
        self.created: list[bool] = []
        self.existed: list[bool] = []

    def __call__(self, app, host, port, *, control=None, on_started=None, **kw) -> int:
        self.calls.append((host, port))
        self.kwargs.append(kw)
        if self.never_starts:
            return cli.STARTUP_FAILURE
        with TestClient(app, base_url=ORIGIN, client=LOOPBACK_PEER) as client:
            hk = app.state.hk
            self.secret = hk.setup.secret
            self.secrets.append(self.secret)
            self.data_dirs.append(Path(hk.settings.data_dir))
            self.created.append(hk.setup.created_folder)
            self.existed.append(hk.setup.folder_existed)
            if on_started is not None:
                on_started()
            if self.moves:
                self.before_move(Path(hk.settings.data_dir))
                body = {"secret": self.secret, "path": str(self.moves.pop(0))}
                moved = client.post("/api/setup/data-folder", json=body, headers=CSRF)
                assert moved.status_code == 202, moved.text
                self.after_move()
                return control.exit_code
            if self.enroll:
                body = {"secret": self.secret}
                status = client.post("/api/setup/status", json=body, headers=CSRF)
                assert status.json() == {"state": "needs_passkey"}, status.text
                issued = client.post("/api/setup/enrollment-code", json=body, headers=CSRF)
                assert issued.status_code == 200, issued.text
                self.code = issued.json()["code"]
                enroll(client, self.code)
        return control.exit_code or 0


class SetupEnv:
    def __init__(self, tmp_path: Path) -> None:
        self.host = FakeHost(tmp_path)
        self.run_server = FakeRunServer()
        self.bundle_calls = 0
        self.bundle_outcome = "ready"
        self.opened: list[str] = []

    def ensure_bundle(self, *, say):
        self.bundle_calls += 1
        say("Hearthkeep's web app is ready.")
        return self.bundle_outcome

    def open_url(self, url: str) -> bool:
        self.opened.append(url)
        return True

    def __call__(self, data: Path | None, **over) -> int:
        argv = ["setup"] + (["--data", str(data)] if data is not None else [])
        args = cli.build_parser().parse_args(argv)
        kwargs = {
            "host": self.host,
            "open_url": self.open_url,
            "ensure_bundle": self.ensure_bundle,
            "make_verifier": lambda settings: FakeVerifier(),
            "wait": lambda *a, **k: True,
            "free": lambda host, port: True,
            "program_folder": lambda: None,  # a developer checkout
            "detect_vram": lambda: 12.0,
        }
        kwargs.update(over)
        return cli.cmd_setup(args, **kwargs)


@pytest.fixture
def run_cmd_setup(tmp_path, monkeypatch):
    """`cmd_setup` on a fake Windows host. The path-text rules are the Windows ones, so on any
    OS a temp folder passes them unless its name has a `%` (the rest of check_data_dir runs)."""
    monkeypatch.setattr(
        host_module, "check_data_dir_text", lambda text: BAD_CHARACTERS if "%" in text else None
    )
    monkeypatch.setattr(app_module, "detect_docker", lambda: False)
    env = SetupEnv(tmp_path)
    monkeypatch.setattr(cli, "run_server", env.run_server)
    # The setup page's folder move asks the same fake host, and finds plenty of space.
    monkeypatch.setattr(routes_setup, "current_host", lambda: env.host)
    monkeypatch.setattr(routes_setup, "free_bytes", lambda path: 100 * 1024**3)
    return env


# ---- SetupMode ----------------------------------------


def test_setup_mode_holds_the_secret_until_complete():
    mode = SetupMode(SECRET)
    assert mode.active and not mode.completed and mode.secret == SECRET
    assert mode.check(SECRET)
    assert not mode.check("wrong") and not mode.check("") and not mode.check(SECRET + "x")
    assert SECRET not in repr(mode)
    mode.complete()
    assert not mode.active and mode.completed and mode.secret is None
    assert not mode.check(SECRET)


def test_setup_mode_makes_a_long_random_secret():
    a, b = SetupMode(), SetupMode()
    assert a.secret != b.secret and len(a.secret) >= 43


def test_setup_url_puts_the_secret_in_the_fragment():
    assert setup_url("http://localhost:8765/", "abc") == "http://localhost:8765/setup#abc"


# ---- the endpoints ----------------------------------------


def test_setup_code_needs_the_secret_and_loopback(harness):
    client, secret = setup_client(harness)
    codes = "SELECT COUNT(*) FROM enrollment_codes"
    before = harness.query(codes)
    cases = (
        ({}, None, 422, "invalid_request"),
        ({"secret": "wrong"}, None, 403, "setup_secret"),
        ({"secret": ""}, None, 422, "invalid_request"),
        # The secret in a ?secret= query is never read: only the body counts.
        ({"secret": "wrong"}, {"secret": secret}, 403, "setup_secret"),
    )
    for body, params, status, reason in cases:
        r = client.post("/api/setup/enrollment-code", json=body, params=params, headers=CSRF)
        assert (r.status_code, r.json()["reason"]) == (status, reason), (body, params)
    no_csrf = client.post("/api/setup/enrollment-code", json={"secret": secret})
    assert (no_csrf.status_code, no_csrf.json()) == (403, {"reason": "csrf"})
    other_origin = {"X-Hearthkeep": "1", "Origin": "http://evil.example"}
    wrong_origin = client.post(
        "/api/setup/enrollment-code", json={"secret": secret}, headers=other_origin
    )
    assert (wrong_origin.status_code, wrong_origin.json()) == (403, {"reason": "csrf"})
    remote = harness.start(setup=SetupMode(secret), peer=("192.168.1.20", 50000))
    r = remote.post("/api/setup/enrollment-code", json={"secret": secret}, headers=CSRF)
    assert (r.status_code, r.json()["reason"]) == (403, "loopback_only")
    assert harness.query(codes) == before  # nothing was issued

    ok = client.post("/api/setup/enrollment-code", json={"secret": secret}, headers=CSRF)
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert ok.json()["expires_in_minutes"] == 15 and len(ok.json()["code"]) == 8
    code = ok.json()["code"]

    for _ in range(60):  # the setup group allows 60 a minute per client
        last = client.post("/api/setup/status", json={"secret": "guess"}, headers=CSRF)
    assert (last.status_code, last.json()["reason"]) == (429, "rate_limited")

    denied = [e for e in harness.events() if e["event"] == "setup.denied"]
    assert denied and all(e["detail"] == {"reason": "bad_secret", "client": "127.0.0.1"}
                          for e in denied)  # fmt: skip
    issued = [e for e in harness.events() if e["event"] == "enrollment.code_issued"]
    assert [e["detail"] for e in issued] == [{"purpose": "first_device"}]
    for (detail,) in harness.query("SELECT detail FROM audit"):
        assert secret not in detail and code not in detail


def test_forwarded_for_does_not_make_a_remote_peer_local(harness):
    client, secret = setup_client(harness, peer=("192.168.1.20", 50000))
    headers = {**CSRF, "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
    r = client.post("/api/setup/enrollment-code", json={"secret": secret}, headers=headers)
    assert (r.status_code, r.json()["reason"]) == (403, "loopback_only")


def test_setup_answers_ipv6_loopback(harness):
    client, secret = setup_client(harness, peer=("::1", 50000))
    r = client.post("/api/setup/status", json={"secret": secret}, headers=CSRF)
    assert r.json() == {"state": "needs_passkey"}


def test_setup_endpoints_are_404_without_setup_mode(harness):
    client = harness.start()
    for path in ("/api/setup/status", "/api/setup/enrollment-code"):
        r = client.post(path, json={"secret": SECRET}, headers=CSRF)
        assert (r.status_code, r.json()["reason"]) == (404, "not_found")


def test_setup_endpoints_are_404_once_a_passkey_exists(harness):
    client, secret = setup_client(harness)
    conn = harness.conn()  # a passkey made some other way: setup is still "active" in memory
    conn.execute("INSERT INTO credentials (id, public_key, sign_count) VALUES ('k', x'00', 0)")
    conn.close()
    for path in ("/api/setup/status", "/api/setup/enrollment-code"):
        assert client.post(path, json={"secret": secret}, headers=CSRF).status_code == 404


def test_setup_page_is_served_only_in_setup_mode(harness, tmp_path):
    dist = dist_with_index(tmp_path)
    plain = harness.start(web_dist=dist)
    gone = plain.get("/setup", follow_redirects=False)
    assert (gone.status_code, gone.headers["location"]) == (302, "/")
    client, _ = setup_client(harness, web_dist=dist)
    page = client.get("/setup", follow_redirects=False)
    assert page.status_code == 200 and page.text == "<html>setup</html>"
    assert page.headers["cache-control"] == "no-store"


def test_setup_rerun_after_passkey_opens_app_and_setup_is_gone(
    harness, tmp_path, capsys, run_cmd_setup
):
    control = ServerControl(supervised=False, setup_mode=True)
    stopped = []
    control.attach(lambda: stopped.append(True))
    client, secret = setup_client(harness, control=control, web_dist=dist_with_index(tmp_path))
    assert client.get("/setup", follow_redirects=False).status_code == 200
    issued = client.post("/api/setup/enrollment-code", json={"secret": secret}, headers=CSRF)
    enroll(client, issued.json()["code"])  # the existing passkey flow, FakeVerifier
    assert control.exit_code == EXIT_STOPPED and stopped == [True]  # the setup server hands over
    assert client.app.state.hk.setup.secret is None
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/"
    for path in ("/api/setup/status", "/api/setup/enrollment-code"):
        assert client.post(path, json={"secret": secret}, headers=CSRF).status_code == 404

    # Setup.cmd again on that data folder: no setup-mode server, no web app step, opener once.
    calls = []
    opener = lambda s, h, **k: calls.append((s.public_origin, k["open_url"])) or 0  # noqa: E731
    assert run_cmd_setup(harness.data, opener=opener) == 0
    assert calls == [("http://localhost:8765", run_cmd_setup.open_url)]
    assert ALREADY_SET_UP in capsys.readouterr().out
    assert run_cmd_setup.run_server.calls == [] and run_cmd_setup.bundle_calls == 0


def test_add_device_enrolment_does_not_end_setup(harness):
    control = ServerControl(supervised=False, setup_mode=True)
    client, secret = setup_client(harness, control=control)
    conn = harness.conn()
    conn.execute("INSERT INTO credentials (id, public_key, sign_count) VALUES ('k', x'00', 0)")
    conn.close()
    enroll(client, harness.code("add_device"), cred_id="cred-2")
    assert control.exit_code is None and client.app.state.hk.setup.secret == secret


def test_health_says_setup_mode_while_setting_up(harness):
    client, _ = setup_client(harness)
    assert client.get("/api/health").json()["setup_mode"] is True
    assert harness.start().get("/api/health").json()["setup_mode"] is False


# ---- `hearthkeep setup` ----------------------------------------


def test_setup_secret_and_code_never_logged(tmp_path, caplog, capsys, run_cmd_setup):
    caplog.set_level(logging.DEBUG)
    data = tmp_path / "data"
    assert run_cmd_setup(data, open_url=lambda url: False) == 0
    out = capsys.readouterr().out
    secret, code = run_cmd_setup.run_server.secret, run_cmd_setup.run_server.code
    assert secret and code
    assert out.count(secret) == 1 and f"http://localhost:8765/setup#{secret}\n" in out
    assert "?" not in out.split(secret)[0][-40:]
    assert BROWSER_FAILED in out and BROWSER_OPENING not in out
    assert code not in out and secret not in caplog.text and code not in caplog.text
    for (detail,) in query(data, "SELECT detail FROM audit"):
        assert secret not in detail and code not in detail
    assert out.startswith(SETUP_INTRO) and STEP_WEB in out and STEP_START in out
    assert FINISHED in out
    assert out.index(STEP_WEB) < out.index(STEP_START) < out.index(FINISHED)
    assert "Models:" not in out and "Chat:" not in out  # build_app's lines are not printed
    host = run_cmd_setup.host
    assert host.location_writes == [data.resolve()]
    assert host.shortcuts == [data.resolve()]
    assert len(host.spawned) == 1 and host.spawned[0][3:5] == ["launch", "--data"]
    assert host.spawned[0][5] == str(data.resolve())
    assert run_cmd_setup.run_server.calls == [("127.0.0.1", 8765)]
    # The window is the owner's: no uvicorn banner or access-log lines in it.
    # critical: not even uvicorn's raw bind error or a traceback reaches the owner's window.
    assert run_cmd_setup.run_server.kwargs == [{"log_level": "critical", "access_log": False}]


def test_setup_opens_the_browser_without_printing_the_link(tmp_path, capsys, run_cmd_setup):
    assert run_cmd_setup(tmp_path / "data") == 0
    out = capsys.readouterr().out
    secret = run_cmd_setup.run_server.secret
    assert run_cmd_setup.opened == [f"http://localhost:8765/setup#{secret}"]
    assert secret not in out and BROWSER_OPENING in out and KEEP_OPEN in out


def test_setup_prints_the_link_when_the_browser_fails(tmp_path, capsys, run_cmd_setup):
    def broken(url):
        raise OSError("no browser")

    assert run_cmd_setup(tmp_path / "data", open_url=broken) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    link = lines[lines.index(BROWSER_FAILED) + 1]
    assert link == f"http://localhost:8765/setup#{run_cmd_setup.run_server.secret}"


def test_setup_stopped_before_passkey_says_so(tmp_path, capsys, run_cmd_setup):
    run_cmd_setup.run_server.enroll = False
    assert run_cmd_setup(tmp_path / "data") == 1
    out = capsys.readouterr().out
    assert STOPPED_EARLY in out and FINISHED not in out
    assert run_cmd_setup.host.spawned == [] and run_cmd_setup.host.shortcuts == []


def test_setup_server_that_never_starts_says_the_port_is_busy(tmp_path, capsys, run_cmd_setup):
    run_cmd_setup.run_server.never_starts = True
    answers = iter([True, False])  # free at the check, taken when the server tried to bind
    assert run_cmd_setup(tmp_path / "data", free=lambda host, port: next(answers)) == 1
    out = capsys.readouterr().out
    assert PORT_BUSY.format(port=8765) in out and STOPPED_EARLY not in out
    assert run_cmd_setup.host.spawned == []


def test_setup_start_failure_with_a_free_port_is_not_called_a_busy_port(
    tmp_path, capsys, run_cmd_setup
):
    """uvicorn also exits with STARTUP_FAILURE when the app's own startup fails: with the port
    still free, the owner is not told to close another program."""
    run_cmd_setup.run_server.never_starts = True
    probes = []
    assert run_cmd_setup(tmp_path / "data", free=lambda h, p: probes.append(p) or True) == 1
    out = capsys.readouterr().out
    assert STOPPED_EARLY in out and PORT_BUSY.format(port=8765) not in out
    assert probes == [8765, 8765]  # checked again after the failed start


def test_setup_says_at_once_when_the_launcher_cant_start(tmp_path, capsys, run_cmd_setup):
    run_cmd_setup.host.spawn_error = True
    waited = []
    result = run_cmd_setup(tmp_path / "data", wait=lambda *a, **k: waited.append(a) or True)
    assert result == 1 and waited == []  # no 60-second wait for a launcher that never ran
    out = capsys.readouterr().out
    assert LAUNCHER_NOT_STARTED in out
    assert FINISHED not in out and FINISHED_SLOW not in out


def _held_port() -> socket.socket:
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    held.listen()
    return held


def test_run_server_returns_startup_failure_when_uvicorn_cant_bind():
    """uvicorn's own startup calls sys.exit(3) when the bind fails: that is STARTUP_FAILURE,
    not an exit of the whole process."""
    from fastapi import FastAPI

    from hearthkeep.lifecycle import ServerControl

    with _held_port() as held:
        port = held.getsockname()[1]
        control = ServerControl(supervised=False)
        code = REAL_RUN_SERVER(
            FastAPI(), "127.0.0.1", port, control=control, log_level="critical", access_log=False
        )
    assert code == cli.STARTUP_FAILURE == 3


def test_setup_with_a_real_server_on_a_taken_port_says_the_port_is_busy(
    tmp_path, capsys, monkeypatch, run_cmd_setup
):
    """Something takes the port after the free-port check: the real uvicorn can't bind and
    setup says the port is busy (not that setup stopped early, and no traceback)."""
    monkeypatch.setattr(cli, "run_server", REAL_RUN_SERVER)
    with _held_port() as held:
        port = held.getsockname()[1]
        data = tmp_path / "data"
        data.mkdir()
        (data / "config.toml").write_text(
            f"port = {port}\npublic_origin = 'http://localhost:{port}'\n", encoding="utf-8"
        )
        checks = iter([True])  # the first check misses it; after the failed start the
        # real bind test sees the port taken
        free = lambda host, p: next(checks, None) or cli.port_free(host, p)  # noqa: E731
        assert run_cmd_setup(data, free=free) == 1
    out = capsys.readouterr().out
    assert PORT_BUSY.format(port=port) in out and STOPPED_EARLY not in out
    assert run_cmd_setup.host.spawned == []


def test_setup_refuses_a_bad_data_folder_before_creating_it(tmp_path, capsys, run_cmd_setup):
    data = tmp_path / "my%data"
    assert run_cmd_setup(data) == 1
    out = capsys.readouterr().out
    assert BAD_CHARACTERS in out and STEP_WEB not in out
    assert not data.exists() and run_cmd_setup.bundle_calls == 0
    assert run_cmd_setup.host.location_writes == []


def test_setup_keeps_an_existing_data_folder_whatever_its_name(tmp_path, capsys, run_cmd_setup):
    """An install from before setup existed may sit in a folder today's rules refuse: it is
    used as it is (the rules are for new folders)."""
    data = tmp_path / "old%data"
    data.mkdir()
    (data / "config.toml").write_text("docker_available = false\n", encoding="utf-8")
    assert run_cmd_setup(data) == 0
    assert BAD_CHARACTERS not in capsys.readouterr().out


def test_setup_uses_the_recorded_folder_then_the_default(tmp_path, run_cmd_setup):
    host = run_cmd_setup.host
    assert run_cmd_setup(None) == 0  # nothing recorded: the default folder
    assert (host.default / "hearthkeep.db").is_file()
    assert host.location_writes == [host.default.resolve()]

    recorded = tmp_path / "recorded"
    recorded.mkdir()
    host.location = recorded
    assert run_cmd_setup(None) == 0
    assert (recorded / "hearthkeep.db").is_file()  # the recorded folder, not the default
    assert host.location_writes == [host.default.resolve()]  # an existing record is kept


def _task_for(host: FakeHost, data: Path) -> None:
    host.task = startup.task_xml(
        pythonw=Path(r"C:\py\pythonw.exe"), data_dir=data, user=r"HOMEPC\owner"
    )


def test_setup_rerun_without_a_record_finds_the_install_through_start_at_login(
    tmp_path, run_cmd_setup
):
    """An install whose record is missing (LOCALAPPDATA reset, a new Windows profile copy) but
    whose start-at-login task names its data folder: a re-run uses that folder, not the
    default, and records it again."""
    host = run_cmd_setup.host
    data = tmp_path / "old-install"
    data.mkdir()
    (data / "config.toml").write_text("docker_available = false\n", encoding="utf-8")
    _task_for(host, data)
    assert run_cmd_setup(None) == 0
    assert (data / "hearthkeep.db").is_file() and not host.default.exists()
    assert host.location_writes == [data.resolve()]
    assert host.status_strict[0] is False  # finding the folder never uses the strict check


def test_setup_ignores_a_task_whose_folder_is_not_a_data_folder(tmp_path, run_cmd_setup):
    host = run_cmd_setup.host
    _task_for(host, tmp_path / "gone")  # the folder the task names is gone
    assert run_cmd_setup(None) == 0
    assert host.location_writes == [host.default.resolve()]
    assert not (tmp_path / "gone").exists()


def test_setup_falls_back_to_the_default_when_the_task_cant_be_read(tmp_path, run_cmd_setup):
    host = run_cmd_setup.host
    host.status_error = True
    assert run_cmd_setup(None) == 0
    assert host.location_writes == [host.default.resolve()]


def test_setup_prefers_the_record_and_the_argument_over_the_task(tmp_path, run_cmd_setup):
    host = run_cmd_setup.host
    task_folder = tmp_path / "task"
    task_folder.mkdir()
    (task_folder / "config.toml").write_text("docker_available = false\n", encoding="utf-8")
    _task_for(host, task_folder)
    asked = tmp_path / "asked"
    assert run_cmd_setup(asked) == 0
    assert (asked / "hearthkeep.db").is_file() and not (task_folder / "hearthkeep.db").exists()


def test_setup_keeps_hearthkeep_warnings_out_of_its_window(
    tmp_path, capsys, monkeypatch, run_cmd_setup
):
    """Setup's window is the owner's: a warning Hearthkeep's code logs while setup runs (with
    no logging set up, Python prints it through logging.lastResort) does not reach it."""
    import logging

    monkeypatch.setattr(logging.getLogger(), "handlers", [])  # as in the real console
    real_build_app = cli.build_app

    def noisy_build_app(*args, **kwargs):
        logging.getLogger("hearthkeep.somewhere").warning("noisy technical warning")
        return real_build_app(*args, **kwargs)

    monkeypatch.setattr(cli, "build_app", noisy_build_app)
    assert run_cmd_setup(tmp_path / "data") == 0
    captured = capsys.readouterr()
    assert "noisy technical warning" not in captured.err + captured.out
    logger = logging.getLogger("hearthkeep")
    assert logger.propagate and not any(
        isinstance(h, logging.NullHandler) for h in logger.handlers
    )  # put back as it was once setup is done


def test_setup_with_another_folder_keeps_a_live_record(tmp_path, run_cmd_setup):
    host = run_cmd_setup.host
    live = tmp_path / "live"
    live.mkdir()
    host.location = live
    assert run_cmd_setup(tmp_path / "other") == 0
    assert host.location == live and host.location_writes == []


def test_setup_goes_on_when_the_folder_cant_be_recorded(tmp_path, capsys, run_cmd_setup):
    run_cmd_setup.host.location_error = True
    assert run_cmd_setup(tmp_path / "data") == 0
    out = capsys.readouterr().out
    assert LOCATION_NOT_SAVED in out and FINISHED in out


def test_setup_stops_when_the_bundle_fails(tmp_path, capsys, run_cmd_setup):
    run_cmd_setup.bundle_outcome = "failed"
    assert run_cmd_setup(tmp_path / "data") == 1
    assert run_cmd_setup.run_server.calls == [] and run_cmd_setup.host.spawned == []
    assert STEP_START not in capsys.readouterr().out


def test_setup_port_busy(tmp_path, capsys, run_cmd_setup):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        assert not cli.port_free("127.0.0.1", port)
        data = tmp_path / "data"
        data.mkdir()
        (data / "config.toml").write_text(
            f"port = {port}\npublic_origin = 'http://localhost:{port}'\n", encoding="utf-8"
        )
        assert run_cmd_setup(data, free=cli.port_free) == 1
    assert PORT_BUSY.format(port=port) in capsys.readouterr().out
    assert run_cmd_setup.run_server.calls == []
    assert cli.port_free("127.0.0.1", port)  # a bind test only: nothing is left holding it


def test_setup_says_when_a_model_server_cant_be_set_up(
    tmp_path, capsys, monkeypatch, run_cmd_setup
):
    def broken(*args, **kwargs):
        raise ProcessError("The chat model's settings can't be used.", detail="C:\\x\\y.gguf")

    monkeypatch.setattr(cli, "build_app", broken)
    assert run_cmd_setup(tmp_path / "data") == 1
    out = capsys.readouterr().out
    assert "The chat model's settings can't be used." in out
    assert "y.gguf" not in out and "Traceback" not in out
    assert run_cmd_setup.run_server.calls == []


def test_setup_shortcut_failure_still_finishes(tmp_path, capsys, run_cmd_setup):
    run_cmd_setup.host.shortcut_error = True
    assert run_cmd_setup(tmp_path / "data") == 0
    out = capsys.readouterr().out
    assert SHORTCUT_FAILED in out and FINISHED in out
    assert len(run_cmd_setup.host.spawned) == 1


def test_setup_waits_for_the_launcher_and_says_when_it_is_slow(tmp_path, capsys, run_cmd_setup):
    waited = []

    def wait(base, *args, **kwargs):
        waited.append((base, kwargs.get("want_setup_mode")))
        return False

    assert run_cmd_setup(tmp_path / "data", wait=wait) == 0
    out = capsys.readouterr().out
    assert FINISHED_SLOW in out and FINISHED not in out
    assert waited == [("http://127.0.0.1:8765", False)]


def test_setup_says_so_when_the_launcher_ended_before_hearthkeep_answered(
    tmp_path, capsys, monkeypatch, run_cmd_setup
):
    class Ended:
        def poll(self):
            return 1  # the launcher gave up

    monkeypatch.setattr(run_cmd_setup.host, "spawn_detached", lambda argv, **kw: Ended())
    assert run_cmd_setup(tmp_path / "data", wait=lambda *a, **k: False) == 1
    out = capsys.readouterr().out
    assert LAUNCHER_NOT_STARTED in out and FINISHED_SLOW not in out and FINISHED not in out


def test_setup_checks_health_on_the_configured_loopback_address(tmp_path, capsys, run_cmd_setup):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text("host = '::1'\n", encoding="utf-8")
    waited = []
    assert run_cmd_setup(data, wait=lambda base, *a, **k: waited.append(base) or True) == 0
    assert waited == ["http://[::1]:8765"]
    assert run_cmd_setup.run_server.calls == [("::1", 8765)]


def test_setup_protects_a_new_data_folder_before_writing_in_it(tmp_path, run_cmd_setup):
    host = run_cmd_setup.host
    data = tmp_path / "data"
    seen = []
    real = host.protect_folder

    def protect(path, *, run=None):
        seen.append(sorted(p.name for p in Path(path).iterdir()))
        real(path)

    host.protect_folder = protect
    assert run_cmd_setup(data) == 0
    assert host.protected == [data.resolve()]
    assert seen == [[]]  # nothing was in it yet


def test_setup_leaves_an_existing_data_folder_as_it_is(tmp_path, run_cmd_setup):
    """An install that has been used (it has its database) keeps its permissions (C2)."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text("docker_available = false\n", encoding="utf-8")
    db.connect(data / DB_FILE).close()
    assert run_cmd_setup(data) == 0
    assert run_cmd_setup.host.protected == []


def test_setup_uses_a_tailscale_config_made_before_it_runs(tmp_path, run_cmd_setup):
    """The supported way to use Hearthkeep through Tailscale from the first passkey: the
    owner writes config.toml in the data folder first. Setup keeps it, opens the setup page on
    that address, and still protects the folder (nothing of the owner's is in it yet)."""
    host = run_cmd_setup.host
    host.default.mkdir()
    (host.default / "config.toml").write_text(
        'public_origin = "https://homepc.example.ts.net"\nrp_id = "homepc.example.ts.net"\n',
        encoding="utf-8",
    )
    run_cmd_setup.run_server.enroll = False  # the fake browser here only speaks localhost
    run_cmd_setup(None)
    secret = run_cmd_setup.run_server.secret
    assert run_cmd_setup.opened == [f"https://homepc.example.ts.net/setup#{secret}"]
    assert host.protected == [host.default.resolve()]
    kept = tomllib.loads((host.default / "config.toml").read_text("utf-8"))
    assert kept == {
        "public_origin": "https://homepc.example.ts.net",
        "rp_id": "homepc.example.ts.net",
    }


def test_setup_protects_the_program_folder_of_a_release_only(tmp_path, run_cmd_setup):
    program = tmp_path / "hearthkeep-0.1.0"
    program.mkdir()
    data = tmp_path / "data"
    assert run_cmd_setup(data, program_folder=lambda: program) == 0
    assert run_cmd_setup.host.protected == [data.resolve(), program]
    assert cli._release_program_folder() is None  # this checkout is the developers' version


@pytest.mark.parametrize("clone", [False, True])
def test_release_program_folder_is_never_a_git_clone(tmp_path, clone):
    """A git clone of a release tag has a release version too, but it is a developer's
    checkout (node_modules and all): only an unzipped release folder is protected."""
    root = tmp_path / "hearthkeep"
    root.mkdir()
    if clone:
        (root / ".git").mkdir()
    found = cli._release_program_folder(root=root, version="0.1.0")
    assert found == (None if clone else root)
    assert cli._release_program_folder(root=root, version="0.0.0") is None


def test_setup_carries_on_when_a_folder_cant_be_protected(tmp_path, capsys, run_cmd_setup):
    run_cmd_setup.host.protect_error = True
    assert run_cmd_setup(tmp_path / "data") == 0
    out = capsys.readouterr().out
    assert out.count(FOLDER_NOT_PROTECTED) == 1 and FINISHED in out
    assert "icacls" not in out and "exit code" not in out


# ---- moving the data folder ----------------------------------------


def test_move_restarts_setup_in_the_new_folder_with_the_same_secret(
    tmp_path, capsys, run_cmd_setup
):
    old, new = tmp_path / "data", tmp_path / "elsewhere" / "data"
    server = run_cmd_setup.run_server
    server.moves = [new]
    assert run_cmd_setup(old) == 0
    out = capsys.readouterr().out
    assert len(server.calls) == 2
    assert server.data_dirs == [old.resolve(), new.resolve()]
    assert server.secrets[0] and server.secrets == [server.secrets[0]] * 2  # the same secret
    assert server.created == [True, True]
    assert run_cmd_setup.opened == [f"http://localhost:8765/setup#{server.secrets[0]}"]
    assert out.count(MOVING_DATA) == 1 and out.count(BROWSER_OPENING) == 1
    assert OLD_FOLDER_KEPT not in out
    assert out.index(MOVING_DATA) < out.index(FINISHED)
    assert run_cmd_setup.bundle_calls == 1  # the web app is the program's, not the folder's
    assert not old.exists()  # it held only setup's own files
    assert (new / "hearthkeep.db").is_file()
    host = run_cmd_setup.host
    assert host.location_writes == [old.resolve(), new.resolve()]
    assert host.location == new.resolve()
    assert host.protected == [old.resolve(), new.resolve()]
    assert host.shortcuts == [new.resolve()]
    assert host.reset == []  # removed whole: no permissions to give back
    assert len(host.spawned) == 1 and host.spawned[0][5] == str(new.resolve())


def test_move_keeps_the_old_folder_when_something_else_is_in_it(tmp_path, capsys, run_cmd_setup):
    old, new = tmp_path / "data", tmp_path / "elsewhere"
    server = run_cmd_setup.run_server
    server.moves = [new]
    server.before_move = lambda data: (data / "logs" / "mine.txt").write_text("x", "utf-8")
    assert run_cmd_setup(old) == 0
    assert (old / "logs" / "mine.txt").is_file() and (old / "hearthkeep.db").is_file()
    assert (new / "hearthkeep.db").is_file()
    assert run_cmd_setup.host.location == new.resolve()
    out = capsys.readouterr().out
    assert MOVING_DATA in out and out.count(OLD_FOLDER_KEPT) == 1


def test_move_says_so_when_the_old_folder_cant_be_removed(
    tmp_path, capsys, monkeypatch, run_cmd_setup
):
    """A file another program still has open: nothing is removed, and the owner is told."""
    old, new = tmp_path / "data", tmp_path / "elsewhere"
    monkeypatch.setattr(cli, "_removable", lambda path: path.name != DB_FILE)
    run_cmd_setup.run_server.moves = [new]
    assert run_cmd_setup(old) == 0
    assert (old / DB_FILE).is_file() and (old / "secrets" / "store.bin").is_file()
    assert (old / "config.toml").is_file()
    assert capsys.readouterr().out.count(OLD_FOLDER_KEPT) == 1


def test_move_keeps_a_folder_that_was_there_before_setup(tmp_path, run_cmd_setup):
    """An empty folder the owner chose: setup's own files leave it, the folder stays."""
    old, new = tmp_path / "chosen", tmp_path / "elsewhere"
    old.mkdir()
    server = run_cmd_setup.run_server
    server.moves = [new]
    assert run_cmd_setup(old) == 0
    assert server.created == [True, True] and server.existed == [True, False]
    assert old.is_dir() and list(old.iterdir()) == []
    assert (new / "hearthkeep.db").is_file()
    # The folder is the owner's again: its permissions inherit as before setup protected it.
    assert run_cmd_setup.host.reset == [old.resolve()]


def test_move_goes_on_when_a_kept_folders_permissions_cant_be_reset(
    tmp_path, capsys, run_cmd_setup
):
    old = tmp_path / "chosen"
    old.mkdir()
    run_cmd_setup.host.reset_error = True
    run_cmd_setup.run_server.moves = [tmp_path / "elsewhere"]
    assert run_cmd_setup(old) == 0
    out = capsys.readouterr().out
    assert FINISHED in out and "icacls" not in out and OLD_FOLDER_KEPT not in out


def test_move_removes_the_old_folders_secret_key(tmp_path, run_cmd_setup, memory_keyring):
    old, new = tmp_path / "data", tmp_path / "elsewhere"
    run_cmd_setup.run_server.moves = [new]
    assert run_cmd_setup(old) == 0
    old_user = SecretStore(old)._key_user
    assert not old.exists()
    assert all(user != old_user for _, user in memory_keyring.store)


def test_move_keeps_a_live_record_of_another_folder(tmp_path, run_cmd_setup):
    """A record naming another, existing install is never repointed, even by a move."""
    host = run_cmd_setup.host
    live = tmp_path / "live"
    live.mkdir()
    host.location = live
    run_cmd_setup.run_server.moves = [tmp_path / "elsewhere"]
    assert run_cmd_setup(tmp_path / "data") == 0
    assert host.location == live and host.location_writes == []
    assert not (tmp_path / "data").exists()


def test_move_can_happen_twice_and_never_reopens_the_browser(tmp_path, run_cmd_setup):
    first, second, third = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    server = run_cmd_setup.run_server
    server.moves = [second, third]
    assert run_cmd_setup(first) == 0
    assert server.data_dirs == [first.resolve(), second.resolve(), third.resolve()]
    assert len(run_cmd_setup.opened) == 1
    assert not first.exists() and not second.exists() and (third / "hearthkeep.db").is_file()
    assert run_cmd_setup.host.location_writes == [p.resolve() for p in (first, second, third)]


def test_setup_marks_only_a_folder_it_made(tmp_path, run_cmd_setup):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.toml").write_text("docker_available = false\n", encoding="utf-8")
    assert run_cmd_setup(data) == 0
    assert run_cmd_setup.run_server.created == [False]
    assert run_cmd_setup(tmp_path / "fresh") == 0
    assert run_cmd_setup.run_server.created == [False, True]


def test_move_stops_when_the_new_folder_fails_its_check_late(tmp_path, capsys, run_cmd_setup):
    """The page checked the folder, but it filled up before setup got to it: setup says why,
    and the old folder and its record are kept."""
    old, new = tmp_path / "data", tmp_path / "elsewhere"
    server = run_cmd_setup.run_server

    def fill():
        new.mkdir()
        (new / "other.txt").write_text("x", encoding="utf-8")

    server.moves = [new]
    server.after_move = fill
    assert run_cmd_setup(old) == 1
    out = capsys.readouterr().out
    assert host_module.NOT_EMPTY in out and MOVING_DATA not in out
    assert (old / "hearthkeep.db").is_file()
    assert run_cmd_setup.host.location == old.resolve()
    assert len(server.calls) == 1


def _setup_files(folder: Path) -> None:
    folder.mkdir()
    for name in ("config.toml", "hearthkeep.db", "hearthkeep.db-wal", "hearthkeep.db-shm"):
        (folder / name).write_text("x", encoding="utf-8")
    for sub in DATA_SUBDIRS:
        (folder / sub).mkdir(parents=True, exist_ok=True)


def test_old_folder_removed_only_when_setup_made_it_and_it_is_empty_of_owner_files(tmp_path):
    clean = tmp_path / "clean"
    _setup_files(clean)
    assert cli._remove_setup_folder(clean) is True
    assert not clean.exists()

    bare = tmp_path / "bare"  # some of setup's files only: still all setup's own
    bare.mkdir()
    (bare / "config.toml").write_text("x", encoding="utf-8")
    assert cli._remove_setup_folder(bare) is True and not bare.exists()


@pytest.mark.parametrize(
    "extra",
    [
        "notes.txt",  # an extra file
        "photos/",  # an extra subfolder, even an empty one
        "logs/server.log",  # one of setup's subfolders, not empty
        "workspace/chats/a/",  # a folder inside one of setup's subfolders
        "workspace/other/",  # a folder beside setup's subfolders
        "secrets/chat_api_key",  # a secret saved while setup ran
    ],
)
def test_old_folder_is_kept_when_anything_else_is_in_it(tmp_path, extra):
    folder = tmp_path / "old"
    _setup_files(folder)
    target = folder / extra
    if extra.endswith("/"):
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.write_text("mine", encoding="utf-8")
    before = sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))
    assert cli._remove_setup_folder(folder) is False
    assert sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) == before


def _with_own_keys(folder: Path) -> None:
    """What starting the setup server adds: git's empty settings file, and the keys
    Hearthkeep makes itself in the secret store."""
    (folder / "runtime" / "git-home" / "empty.gitconfig").write_bytes(b"")
    store = SecretStore(folder)
    store.put(VAPID_SECRET, "made-by-hearthkeep")
    store.put(LLAMA_API_KEY_SECRET, "made-by-hearthkeep")


def test_old_folder_that_was_there_before_is_emptied_and_kept(tmp_path, memory_keyring):
    folder = tmp_path / "old"
    _setup_files(folder)
    _with_own_keys(folder)
    assert len(memory_keyring.store) == 1
    assert cli._remove_setup_folder(folder, keep_folder=True) is True
    assert folder.is_dir() and list(folder.iterdir()) == []
    assert memory_keyring.store == {}  # its master key is forgotten too


def test_old_folder_keyring_failure_keeps_the_folder(tmp_path, memory_keyring, monkeypatch):
    from keyring.errors import KeyringLocked

    folder = tmp_path / "old"
    _setup_files(folder)
    _with_own_keys(folder)

    def broken(*args):
        raise KeyringLocked("the keyring is locked")

    monkeypatch.setattr(memory_keyring, "delete_password", broken)
    assert cli._remove_setup_folder(folder) is False
    assert (folder / DB_FILE).is_file()


def test_old_folder_with_only_hearthkeeps_own_keys_is_removed(tmp_path, memory_keyring):
    folder = tmp_path / "old"
    _setup_files(folder)
    _with_own_keys(folder)
    assert cli._remove_setup_folder(folder) is True and not folder.exists()
    assert memory_keyring.store == {}


@pytest.mark.parametrize("change", ["owner_key", "git_settings", "unreadable_store"])
def test_old_folder_is_kept_when_hearthkeeps_files_hold_more(tmp_path, change):
    folder = tmp_path / "old"
    _setup_files(folder)
    _with_own_keys(folder)
    if change == "owner_key":  # a key the owner typed in setup (an external chat server)
        SecretStore(folder).put("chat_api_key", "the-owners-key")
    elif change == "git_settings":
        (folder / "runtime" / "git-home" / "empty.gitconfig").write_text("[user]\n", "utf-8")
    else:
        (folder / "secrets" / "store.bin").write_bytes(b"not a store")
    assert cli._remove_setup_folder(folder) is False
    assert (folder / "secrets" / "store.bin").is_file() and (folder / DB_FILE).is_file()


def test_old_folder_that_is_a_file_or_gone_is_left_alone(tmp_path):
    assert cli._remove_setup_folder(tmp_path / "gone") is False
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")
    assert cli._remove_setup_folder(file) is False and file.is_file()


def test_old_folder_with_a_locked_file_is_left_whole(tmp_path, memory_keyring, monkeypatch):
    """One dry pass first: a file that can't be deleted keeps everything, the key included."""
    folder = tmp_path / "old"
    _setup_files(folder)
    _with_own_keys(folder)
    before = sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))
    monkeypatch.setattr(cli, "_removable", lambda path: path.name != "hearthkeep.db-wal")
    assert cli._remove_setup_folder(folder) is False
    assert sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) == before
    assert len(memory_keyring.store) == 1 and SecretStore(folder).get(VAPID_SECRET)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to delete an open file")
def test_old_folder_with_an_open_database_is_left_whole(tmp_path):
    folder = tmp_path / "old"
    _setup_files(folder)
    (folder / DB_FILE).unlink()
    conn = db.connect(folder / DB_FILE)
    try:
        assert cli._remove_setup_folder(folder) is False
        assert (folder / "config.toml").is_file() and (folder / DB_FILE).is_file()
    finally:
        conn.close()
    assert not any(p.name.endswith(".hk-probe") for p in folder.rglob("*"))
    assert cli._removable(folder / "config.toml") and (folder / "config.toml").is_file()


def test_move_copy_is_ascii_and_clean():
    assert MOVING_DATA == "Moving Hearthkeep's data to the folder you chose."
    assert OLD_FOLDER_KEPT == "The old data folder was left in place. You can delete it yourself."
    for text in (MOVING_DATA, OLD_FOLDER_KEPT):
        assert text.isascii() and forbidden_in(text) == [], text
    assert EXIT_RELOCATE == 76


def test_setup_on_other_os_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(startup, "is_windows", lambda: False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert cli.main(["setup", "--data", str(tmp_path / "d")]) == EXIT_USAGE
    assert UNSUPPORTED_HOST in capsys.readouterr().out
    assert not (tmp_path / "d").exists() and not (tmp_path / "local").exists()


def test_port_free_on_a_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert cli.port_free("127.0.0.1", port)


def test_setup_parser_entry():
    args = cli.build_parser().parse_args(["setup"])
    assert args.func is cli.cmd_setup and args.data is None
    assert cli.build_parser().parse_args(["setup", "--data", "x"]).data == "x"


def test_setup_copy_is_ascii_without_commands():
    copy = [
        SETUP_INTRO,
        STEP_WEB,
        STEP_START,
        BROWSER_OPENING,
        KEEP_OPEN,
        BROWSER_FAILED,
        ALREADY_SET_UP,
        PORT_BUSY.format(port=8765),
        STOPPED_EARLY,
        SHORTCUT_FAILED,
        FINISHED,
        FINISHED_SLOW,
        LOCATION_NOT_SAVED,
        LAUNCHER_NOT_STARTED,
        FOLDER_NOT_PROTECTED,
    ]
    for text in copy:
        assert text.isascii(), text
        assert forbidden_in(text) == [], text
        assert "<" not in text and "{" not in text, text
    assert SETUP_INTRO.startswith("Setting up Hearthkeep\n")
    # Nothing retries a fresh install's shortcut, so the copy must not promise a retry.
    assert SHORTCUT_FAILED == (
        "The Start menu shortcut couldn't be made. You can still open Hearthkeep by "
        "double-clicking Setup.cmd."
    )


def test_security_doc_describes_setup_mode():
    doc = (Path(__file__).resolve().parents[2] / "docs" / "security.md").read_text("utf-8")
    assert "setup mode" in doc.lower() and "fragment" in doc
    # Ruling C24: what the folder protection covers, and what it leaves alone.
    assert "icacls" in doc and "SYSTEM and Administrators" in doc
    assert "existing data folder is left exactly as it was" in doc
    assert "keeps out other users and services" not in doc  # the old, wider claim
    for event in ("server.stop", "server.restart", "_denied", "setup.denied", "upgrade.ran"):
        assert f"`{event}" in doc or f"`*{event}" in doc, event
    assert "`update_note.dismissed`" in doc and "`delete` and `server`" in doc


def test_health_never_carries_the_secret(harness):
    client, secret = setup_client(harness)
    assert secret not in json.dumps(client.get("/api/health").json())


def test_install_docs_send_tailscale_owners_to_step_7_before_setup():
    root = Path(__file__).resolve().parents[2]
    sentence = "If you'll use Hearthkeep from your phone through Tailscale, read"
    for doc in (root / "README.md", root / "docs" / "install.md"):
        text = doc.read_text("utf-8")
        assert sentence in text and "before you run Setup.cmd" in text, doc.name
    install = (root / "docs" / "install.md").read_text("utf-8")
    assert "Settings › General › Stop Hearthkeep" in install
    assert 'public_origin = "https://homepc.tail1234.ts.net"' in install
