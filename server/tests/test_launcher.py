"""The launcher keeps `serve` running, `ServerControl` carries restart and stop as exit codes,
and `open` starts the launcher only when Hearthkeep isn't answering. Processes, clocks and
sleeps are fakes: nothing real is started."""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from copy_rules import forbidden_in

from hearthkeep import cli, launcher, startup
from hearthkeep.config import Settings
from hearthkeep.host import UNSUPPORTED_HOST, UnsupportedHost, WindowsHost
from hearthkeep.launcher import (
    CRASH_LIMIT,
    CRASH_PAUSE_S,
    CRASH_WINDOW_S,
    LAUNCHER_LOG,
    LOCK_FILE,
    acquire_lock,
    health,
    launcher_argv,
    open_app,
    run_launcher,
    serve_argv,
    wait_for_health,
)
from hearthkeep.lifecycle import (
    EXIT_RESTART,
    EXIT_STOPPED,
    EXIT_USAGE,
    SUPERVISED_ENV,
    ServerControl,
)


class FakeChild:
    def __init__(self, argv, env, creationflags, code):
        self.argv, self.env, self.creationflags = argv, env, creationflags
        self._code = code

    def wait(self):
        return self._code


class FakePopen:
    """Stands in for subprocess.Popen: each start records the child, whose wait() returns
    the next exit code from `codes()`."""

    def __init__(self, codes):
        self._codes = codes
        self.started: list[FakeChild] = []

    def __call__(self, argv, *, env=None, creationflags=0, **kw):
        child = FakeChild(list(argv), env, creationflags, self._codes())
        self.started.append(child)
        return child


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def no_sleep(seconds):
    pass


def always_lock(path):
    return object()


def never_lock(path):
    return None


# ---- lifecycle -------------------------------------------------------------------------


def test_exit_codes():
    assert (EXIT_STOPPED, EXIT_RESTART, EXIT_USAGE) == (0, 75, 2)
    assert SUPERVISED_ENV == "HEARTHKEEP_SUPERVISED"
    assert cli.EXIT_USAGE is EXIT_USAGE  # C16: cli re-exports it


def test_server_control_first_request_wins():
    calls = []
    c = ServerControl(supervised=True)
    c.attach(lambda: calls.append(1))
    c.request(EXIT_RESTART)
    c.request(EXIT_STOPPED)
    assert c.exit_code == EXIT_RESTART and calls == [1]


def test_server_control_request_before_attach_only_records():
    calls = []
    c = ServerControl(supervised=False, setup_mode=True)
    assert (c.supervised, c.setup_mode, c.exit_code, c.restart_needed) == (False, True, None, set())
    c.request(EXIT_STOPPED)
    assert c.exit_code == EXIT_STOPPED and calls == []
    c.attach(lambda: calls.append(1))
    c.request(EXIT_RESTART)
    assert c.exit_code == EXIT_STOPPED and calls == []


def test_server_control_clock_and_restart_needed():
    moment = datetime(2026, 11, 3, 9, 30, tzinfo=UTC)
    c = ServerControl(supervised=True, clock=lambda: moment)
    assert c.started_at == moment
    c.mark_restart_needed("addon.analysis")
    c.mark_restart_needed("addon.analysis")
    assert c.restart_needed == {"addon.analysis"}
    # the default clock is aware UTC (C17), so started_at.isoformat() works for /api/server
    assert ServerControl(supervised=False).started_at.tzinfo is not None


# ---- the launcher ----------------------------------------------------------------------


def test_argv(tmp_path, monkeypatch):
    data = tmp_path / "My data"
    assert serve_argv(data) == [
        sys.executable, "-m", "hearthkeep", "serve", "--data", str(data),
        "--log-file", str(startup.log_path(data)),
    ]  # fmt: skip
    assert launcher_argv(data) == [
        str(startup.pythonw_path()), "-m", "hearthkeep", "launch", "--data", str(data),
    ]  # fmt: skip
    assert LAUNCHER_LOG == Path("logs") / "launcher.log"
    assert LOCK_FILE == Path("runtime") / "launcher.lock"
    assert (CRASH_LIMIT, CRASH_WINDOW_S, CRASH_PAUSE_S) == (3, 600.0, 5.0)


def test_restart_exit_starts_a_new_child_and_stop_ends(tmp_path):
    codes = iter([EXIT_RESTART, EXIT_RESTART, 0])
    popen = FakePopen(lambda: next(codes))
    assert run_launcher(tmp_path, popen=popen, sleep=no_sleep, lock=always_lock) == 0
    assert len(popen.started) == 3
    assert all(
        c.env[SUPERVISED_ENV] == "1" and c.argv[3:5] == ["serve", "--data"] for c in popen.started
    )
    assert all(c.argv == serve_argv(tmp_path) for c in popen.started)
    from hearthkeep.procutil import NO_WINDOW

    assert all(c.creationflags == NO_WINDOW for c in popen.started)


def test_config_error_ends_the_launcher(tmp_path):
    popen = FakePopen(lambda: EXIT_USAGE)
    slept = []
    assert run_launcher(tmp_path, popen=popen, sleep=slept.append, lock=always_lock) == 2
    assert len(popen.started) == 1 and slept == []


def test_crash_loop_gives_up_after_three_in_ten_minutes(tmp_path):
    clock = FakeClock()

    def crash_three_minutes_later():
        clock.now += 180.0
        return 1

    popen = FakePopen(crash_three_minutes_later)
    slept = []
    result = run_launcher(tmp_path, popen=popen, sleep=slept.append, clock=clock, lock=always_lock)
    assert result == 1
    assert len(popen.started) == 3  # crashes at 3, 6 and 9 minutes
    assert slept == [CRASH_PAUSE_S, CRASH_PAUSE_S]

    clock = FakeClock()
    codes = iter([1, 1, 1, 1, 0])

    def crash_eleven_minutes_later():
        clock.now += 660.0
        return next(codes)

    popen = FakePopen(crash_eleven_minutes_later)
    slept = []
    result = run_launcher(tmp_path, popen=popen, sleep=slept.append, clock=clock, lock=always_lock)
    assert result == 0
    assert len(popen.started) == 5 and slept == [CRASH_PAUSE_S] * 4


def test_a_child_that_cannot_start_counts_as_a_crash(tmp_path):
    def broken(argv, **kw):
        raise OSError("no such file")

    assert run_launcher(tmp_path, popen=broken, sleep=no_sleep, lock=always_lock) == 1


def test_second_launcher_exits_at_once(tmp_path):
    lock_path = tmp_path / "runtime" / "launcher.lock"
    first = acquire_lock(lock_path)
    try:
        assert first is not None
        assert acquire_lock(lock_path) is None
        popen = FakePopen(lambda: 0)
        assert run_launcher(tmp_path, popen=popen, sleep=no_sleep) == 0
        assert popen.started == []
    finally:
        first.close()
    again = acquire_lock(lock_path)  # released with the handle
    assert again is not None
    again.close()


def test_launcher_releases_its_lock(tmp_path):
    assert run_launcher(tmp_path, popen=FakePopen(lambda: 0), sleep=no_sleep) == 0
    handle = acquire_lock(tmp_path / LOCK_FILE)
    assert handle is not None
    handle.close()


def test_launcher_log_has_no_paths(tmp_path):
    data = tmp_path / "data folder"
    codes = iter([EXIT_RESTART, 1, 0])
    run_launcher(data, popen=FakePopen(lambda: next(codes)), sleep=no_sleep, lock=always_lock)
    text = (data / LAUNCHER_LOG).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 6  # three starts, three exits
    assert str(tmp_path) not in text and "data folder" not in text
    assert "--data" not in text and "serve" not in text
    assert lines[0].endswith(" start") and " exit 75 " in lines[1] and " exit 1 " in lines[3]
    assert " exit 0 " in lines[-1]


# ---- health and open -------------------------------------------------------------------


def test_health_never_raises_and_needs_setup_mode(monkeypatch):
    import httpx

    class Response:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    answers = {}
    seen = {}

    def fake_get(url, *, timeout, trust_env):
        seen.update(url=url, timeout=timeout, trust_env=trust_env)
        answer = answers["next"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(httpx, "get", fake_get)
    answers["next"] = Response(200, {"status": "ok", "setup_mode": False})
    assert health("http://127.0.0.1:8765") == {"status": "ok", "setup_mode": False}
    assert seen == {"url": "http://127.0.0.1:8765/api/health", "timeout": 1.0, "trust_env": False}
    for answer in (
        Response(200, {"status": "ok"}),
        Response(200, {"status": "bad", "setup_mode": False}),
        Response(503, {"status": "ok", "setup_mode": False}),
        Response(200, ValueError("not json")),
        Response(200, ["ok"]),
        httpx.ConnectError("refused"),
        OSError("boom"),
    ):
        answers["next"] = answer
        assert health("http://127.0.0.1:8765") is None


def test_wait_for_health():
    clock = FakeClock()

    def sleep(seconds):
        clock.now += seconds

    answers = iter(
        [None, None, {"status": "ok", "setup_mode": True}, {"status": "ok", "setup_mode": False}]
    )
    assert wait_for_health("b", probe=lambda base: next(answers), sleep=sleep, clock=clock,
                           want_setup_mode=False)  # fmt: skip
    clock.now = 0.0
    assert not wait_for_health("b", 5.0, probe=lambda base: None, sleep=sleep, clock=clock)
    assert clock.now >= 5.0
    assert wait_for_health("b", probe=lambda base: {"status": "ok", "setup_mode": True},
                           sleep=sleep, clock=clock)  # fmt: skip


class RecordingHost(WindowsHost):
    def __init__(self, location=None):
        self.spawned: list[list[str]] = []
        self.location = location

    def spawn_detached(self, argv, *, popen=None):
        self.spawned.append(list(argv))

    def read_location(self):
        return self.location


def test_open_starts_the_launcher_only_when_not_answering(tmp_path):
    settings = Settings(data_dir=tmp_path, port=8799, public_origin="http://localhost:8799")
    host = RecordingHost()
    opened, waited, probed = [], [], []

    def down(base):
        probed.append(base)
        return None

    assert open_app(settings, host, probe=down, open_url=opened.append, wait=waited.append) == 0
    assert probed == ["http://127.0.0.1:8799"]
    assert host.spawned == [launcher_argv(tmp_path)]
    assert waited == ["http://127.0.0.1:8799"]
    assert opened == ["http://localhost:8799"]

    host = RecordingHost()
    opened, waited = [], []
    up = {"status": "ok", "setup_mode": False}
    assert open_app(settings, host, probe=lambda b: up, open_url=opened.append,
                    wait=waited.append) == 0  # fmt: skip
    assert host.spawned == [] and waited == [] and opened == ["http://localhost:8799"]


def test_open_and_launch_without_a_location_say_not_set_up(tmp_path, monkeypatch, capsys):
    host = RecordingHost(location=None)
    popen = FakePopen(lambda: 0)
    opened = []
    for argv, run in (
        (["launch"], lambda args: cli.cmd_launch(args, host=host, popen=popen)),
        (["open"], lambda args: cli.cmd_open(args, host=host, open_url=opened.append)),
    ):
        assert run(cli.build_parser().parse_args(argv)) == 1
        captured = capsys.readouterr()
        assert cli.NOT_SET_UP in captured.out + captured.err
    assert popen.started == [] and opened == [] and host.spawned == []


def test_launch_and_open_use_the_recorded_location(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    host = RecordingHost(location=data)
    seen = {}
    monkeypatch.setattr(
        launcher, "run_launcher", lambda d, *, popen: seen.setdefault("launch", d) and 0
    )
    assert cli.cmd_launch(cli.build_parser().parse_args(["launch"]), host=host) == 0
    assert seen["launch"] == data.resolve()

    real_open_app = launcher.open_app
    up = {"status": "ok", "setup_mode": False}
    monkeypatch.setattr(
        launcher, "open_app", lambda s, h, **kw: real_open_app(s, h, probe=lambda b: up, **kw)
    )
    opened = []
    args = cli.build_parser().parse_args(["open", "--data", str(data)])
    assert cli.cmd_open(args, host=host, open_url=opened.append) == 0
    assert opened == ["http://localhost:8765"] and host.spawned == []


def test_open_and_launch_on_other_os_are_unsupported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(startup, "is_windows", lambda: False)
    popen = FakePopen(lambda: 0)
    opened = []
    data = str(tmp_path / "data")
    for argv in (["launch"], ["launch", "--data", data], ["open"], ["open", "--data", data]):
        args = cli.build_parser().parse_args(argv)
        if argv[0] == "launch":
            assert cli.cmd_launch(args, popen=popen) == EXIT_USAGE
        else:
            assert cli.cmd_open(args, open_url=opened.append) == EXIT_USAGE
        captured = capsys.readouterr()
        assert UNSUPPORTED_HOST in captured.out + captured.err
    assert popen.started == [] and opened == []
    assert not (tmp_path / "data").exists()
    # the host itself refuses to spawn
    with pytest.raises(Exception, match="only on Windows"):
        UnsupportedHost().spawn_detached(["x"])


def test_new_console_copy_follows_the_rules():
    for text in (cli.NOT_SET_UP, cli.NO_PASSKEY_YET):
        assert text.isascii() and forbidden_in(text) == [], text
        assert "Setup.cmd" in text
    assert forbidden_in("run hearthkeep launch") and forbidden_in("run hearthkeep open")
    assert forbidden_in("run hearthkeep setup")


def test_parser_has_launch_and_open():
    parser = cli.build_parser()
    assert parser.parse_args(["launch"]).func is cli.cmd_launch
    assert parser.parse_args(["open"]).data is None
    assert parser.parse_args(["open", "--data", "x"]).func is cli.cmd_open
