"""The host interface: Windows data-folder rules, the data-folder locator, start at login,
detached processes, and the "unsupported" host every other OS gets."""

import json
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest
from copy_rules import forbidden_in

from hearthkeep import host as host_module
from hearthkeep import procutil, shortcut, startup
from hearthkeep.host import (
    DEFAULT_DATA_DIR,
    LOCATION_FILE,
    MAX_DATA_DIR_CHARS,
    REPO_ROOT,
    UNSUPPORTED_HOST,
    HostUnsupported,
    UnsupportedHost,
    WindowsHost,
    check_data_dir,
    check_data_dir_text,
    current_host,
)
from hearthkeep.startup import StartupStatus


@pytest.fixture
def no_path_rules(monkeypatch):
    """tmp_path is not a Windows drive path on Linux CI; skip the pure path rules."""
    monkeypatch.setattr(host_module, "check_data_dir_text", lambda text: None)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(startup, "is_windows", lambda: True)


@pytest.fixture
def local_app_data(tmp_path, monkeypatch):
    base = tmp_path / "localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    return base


def test_constants():
    assert DEFAULT_DATA_DIR == PureWindowsPath(r"C:\Hearthkeep\data")
    assert MAX_DATA_DIR_CHARS == 100
    assert LOCATION_FILE == "location.json"
    assert (REPO_ROOT / "server" / "src" / "hearthkeep" / "host.py").is_file()
    assert check_data_dir_text(str(DEFAULT_DATA_DIR)) is None


@pytest.mark.parametrize(
    "text, ok",
    [
        (r"C:\Hearthkeep\data", True),
        (r"D:\My Hearthkeep\data (2)", True),
        ("C:\\" + "a" * 97, True),  # 100 characters
        ("C:\\" + "a" * 98, False),  # 101 characters
        (r"\\nas\share\hearthkeep", False),
        (r"hearthkeep\data", False),  # relative
        (r"C:hearthkeep\data", False),  # drive but not absolute
        (r"1:\data", False),  # the drive must be a letter
        (r"é:\data", False),
        (r"$:\data", False),
        (r"\hearthkeep\data", False),  # no drive
        (r"C:\Dåta\Hearthkeep", False),  # non-ASCII
        (r"C:\Hearth%PATH%\data", False),
        (r"C:\Hearth!\data", False),
        (r"C:\A&B\data", False),
        ('C:\\Hearth"keep', False),
        (r"C:\data. \x", False),
        (r"C:\data.\x", False),
    ],
)
def test_unusual_data_paths(text, ok):
    message = check_data_dir_text(text)
    assert (message is None) == ok
    if message:
        assert message.isascii() and "<" not in message and forbidden_in(message) == []


def test_each_rule_has_its_own_message():
    assert "own drive" in check_data_dir_text(r"\\nas\share\hk")
    assert "too long" in check_data_dir_text("C:\\" + "a" * 98)
    assert f"{MAX_DATA_DIR_CHARS} characters" in check_data_dir_text("C:\\" + "a" * 98)
    assert "English letters" in check_data_dir_text(r"C:\A&B\data")


def test_too_long_message_takes_its_number_from_the_limit():
    import re

    assert re.findall(r"\d+", host_module.TOO_LONG) == [str(MAX_DATA_DIR_CHARS)]
    source = Path(host_module.__file__).read_text(encoding="utf-8")
    assert "100 characters" not in source  # not written into the message by hand


def test_check_data_dir_refuses_repo_and_foreign_folders(tmp_path, no_path_rules):
    repo = tmp_path / "repo"
    (repo / "server").mkdir(parents=True)
    assert (
        check_data_dir(repo / "data", repo_root=repo)
        == "Keep Hearthkeep's data outside the Hearthkeep program folder."
    )
    assert check_data_dir(repo, repo_root=repo) is not None
    foreign = tmp_path / "photos"
    foreign.mkdir()
    (foreign / "a.jpg").write_bytes(b"x")
    assert "other files" in check_data_dir(foreign, repo_root=repo)
    mine = tmp_path / "hk"
    mine.mkdir()
    (mine / "config.toml").write_text("")
    assert check_data_dir(mine, repo_root=repo) is None  # an existing data folder is fine
    db_only = tmp_path / "hk2"
    db_only.mkdir()
    (db_only / "hearthkeep.db").write_bytes(b"")
    assert check_data_dir(db_only, repo_root=repo) is None


def test_check_data_dir_accepts_missing_and_empty_and_refuses_a_file(tmp_path, no_path_rules):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert check_data_dir(tmp_path / "new" / "data", repo_root=repo) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check_data_dir(empty, repo_root=repo) is None
    a_file = tmp_path / "notes.txt"
    a_file.write_text("x")
    assert "other files" in check_data_dir(a_file, repo_root=repo)


def test_check_data_dir_applies_the_path_rules_first(tmp_path):
    assert "own drive" in check_data_dir(Path("relative") / "data", repo_root=tmp_path)


def test_other_os_reports_unsupported_and_never_acts(monkeypatch):
    monkeypatch.setattr(startup, "is_windows", lambda: False)
    host = current_host()
    assert isinstance(host, UnsupportedHost) and host.name == "unsupported"
    assert host.supported is False and host.default_data_dir() is None
    assert host.check_data_dir(Path("x")) == UNSUPPORTED_HOST
    assert host.read_location() is None

    def forbidden(*a, **k):
        raise AssertionError("ran something")

    assert host.startup_status(run=forbidden) == StartupStatus(False, False, None)
    for act in (
        lambda: host.enable_startup(Path("x"), run=forbidden),
        lambda: host.install_shortcut(Path("x"), run=forbidden),
        lambda: host.spawn_detached(["x"], popen=forbidden),
        lambda: host.write_location(Path("x")),
    ):
        with pytest.raises(HostUnsupported) as caught:
            act()
        assert str(caught.value) == UNSUPPORTED_HOST
    assert host.disable_startup(run=forbidden) is False
    assert UNSUPPORTED_HOST.isascii() and forbidden_in(UNSUPPORTED_HOST) == []


def test_windows_host_basics(windows):
    host = current_host()
    assert isinstance(host, WindowsHost)
    assert host.name == "windows" and host.supported is True
    assert str(host.default_data_dir()) == r"C:\Hearthkeep\data"


def test_windows_host_spawns_detached(windows):
    calls = []

    def popen(argv, **kw):
        calls.append((argv, kw))
        return "the process"

    started = current_host().spawn_detached(["pythonw.exe", "-m", "hearthkeep"], popen=popen)
    assert started == "the process"
    assert calls == [(["pythonw.exe", "-m", "hearthkeep"], procutil.detached_popen_kwargs())]


def test_windows_host_startup_delegates_to_startup(windows, monkeypatch):
    seen = []
    run = object()
    monkeypatch.setattr(startup, "status", lambda *, run: seen.append(("status", run)) or "st")
    monkeypatch.setattr(
        startup, "enable", lambda data_dir, *, run: seen.append(("enable", data_dir, run))
    )
    monkeypatch.setattr(startup, "disable", lambda *, run: seen.append(("disable", run)) or True)
    host = current_host()
    assert host.startup_status(run=run) == "st"
    host.enable_startup(Path("d"), run=run)
    assert host.disable_startup(run=run) is True
    assert seen == [("status", run), ("enable", Path("d"), run), ("disable", run)]


def test_windows_host_installs_the_shortcut_through_shortcut_module(windows, monkeypatch):
    seen = []
    run = object()
    monkeypatch.setattr(
        shortcut, "install_shortcut", lambda data_dir, *, run: seen.append((data_dir, run))
    )
    current_host().install_shortcut(Path("d"), run=run)
    assert seen == [(Path("d"), run)]


def test_location_round_trip_and_bad_file(tmp_path, local_app_data, windows):
    host = current_host()
    assert host.read_location() is None  # no file yet
    data = tmp_path / "data"
    data.mkdir()
    host.write_location(data)
    file = local_app_data / "Hearthkeep" / LOCATION_FILE
    assert json.loads(file.read_text(encoding="utf-8")) == {"data_dir": str(data.resolve())}
    assert host.read_location() == data.resolve()
    assert [p.name for p in file.parent.iterdir()] == [LOCATION_FILE]  # no temp file left
    host.write_location(data)  # replacing an existing file works
    assert host.read_location() == data.resolve()

    for garbage in ("{not json", "[]", '{"data_dir": 3}', '{"data_dir": ""}', "{}"):
        file.write_text(garbage, encoding="utf-8")
        assert host.read_location() is None
    file.write_bytes(b"\xff\xfe\x00bad")
    assert host.read_location() is None


def test_location_must_be_absolute(tmp_path, local_app_data, windows, monkeypatch):
    host = current_host()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()  # a relative "data" would exist if it were honoured
    file = local_app_data / "Hearthkeep" / LOCATION_FILE
    file.parent.mkdir(parents=True)
    file.write_text(json.dumps({"data_dir": "data"}), encoding="utf-8")
    assert host.read_location() is None


def test_location_names_a_folder_that_is_gone(tmp_path, local_app_data, windows):
    host = current_host()
    data = tmp_path / "data"
    data.mkdir()
    host.write_location(data)
    data.rmdir()
    assert host.read_location() is None


def test_location_without_localappdata(monkeypatch, windows):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    host = current_host()
    assert host.read_location() is None
    with pytest.raises(OSError):
        host.write_location(Path("x"))


def test_detached_kwargs_on_each_os(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    win = procutil.detached_popen_kwargs()
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    assert win == {
        "creationflags": flags,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    monkeypatch.setattr(sys, "platform", "linux")
    assert procutil.detached_popen_kwargs() == {
        "start_new_session": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }


def test_system32_exe(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert (
        procutil.system32_exe("schtasks.exe") == Path(r"C:\Windows") / "System32" / "schtasks.exe"
    )
    assert startup.schtasks_exe() == str(procutil.system32_exe("schtasks.exe"))
    monkeypatch.delenv("SystemRoot")
    monkeypatch.delenv("windir", raising=False)
    assert procutil.system32_exe("schtasks.exe") is None
    with pytest.raises(startup.StartupError, match="SystemRoot"):
        startup.schtasks_exe()


def test_pythonw_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "python.exe"))
    assert startup.pythonw_path() == tmp_path / "venv" / "pythonw.exe"
