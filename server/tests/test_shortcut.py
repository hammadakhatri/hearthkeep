"""The Start menu shortcut: a fixed PowerShell script, values passed in the environment."""

import logging
import subprocess
from pathlib import Path

import pytest

from hearthkeep import procutil, shortcut, startup
from hearthkeep.shortcut import (
    SHORTCUT_NAME,
    SHORTCUT_SCRIPT,
    ShortcutError,
    install_shortcut,
    powershell_exe,
    shortcut_path,
)

PYTHONW = Path(r"C:\py\pythonw.exe")
PS_TAIL = r"WindowsPowerShell\v1.0\powershell.exe"


@pytest.fixture(autouse=True)
def windows_env(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\AppData\Roaming")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")


class Recorder:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if self.error:
            raise self.error
        return self.result or subprocess.CompletedProcess(argv, 0, b"", b"")


def test_constants_and_script_are_fixed():
    assert SHORTCUT_NAME == "Hearthkeep.lnk"
    assert SHORTCUT_SCRIPT == (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:HK_LNK); "
        "$s.TargetPath = $env:HK_TARGET; $s.Arguments = $env:HK_ARGS; "
        "$s.WorkingDirectory = $env:HK_DIR; $s.Description = 'Open Hearthkeep'; $s.Save()"
    )


def test_paths():
    assert str(shortcut_path()).endswith(r"Start Menu\Programs\Hearthkeep.lnk")
    assert str(shortcut_path()).startswith(r"C:\AppData\Roaming")
    assert powershell_exe().endswith(PS_TAIL) and "System32" in powershell_exe()
    assert Path(powershell_exe()) == procutil.system32_exe(PS_TAIL)


def test_missing_environment_is_a_plain_error(monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("windir", raising=False)
    with pytest.raises(ShortcutError):
        powershell_exe()
    monkeypatch.delenv("APPDATA")
    with pytest.raises(ShortcutError):
        shortcut_path()


def test_shortcut_passes_values_by_environment():
    run = Recorder()
    install_shortcut(Path(r"C:\Hearth keep\data"), run=run, pythonw=PYTHONW)
    [(argv, kw)] = run.calls
    assert argv[1:-1] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
    assert argv[-1] == SHORTCUT_SCRIPT and argv[0].endswith(PS_TAIL)
    env = kw["env"]
    assert env["HK_ARGS"] == r'-m hearthkeep open --data "C:\Hearth keep\data"'
    assert env["HK_LNK"].endswith(r"Start Menu\Programs\Hearthkeep.lnk")
    assert env["HK_TARGET"] == str(PYTHONW)
    assert env["HK_DIR"] == r"C:\Hearth keep\data"
    assert kw["capture_output"] is True and kw["timeout"] == 30
    assert kw["creationflags"] == procutil.NO_WINDOW
    # nothing from the values reaches the script text
    assert "Hearth keep" not in " ".join(argv)


def test_default_target_is_pythonw_beside_python(monkeypatch):
    monkeypatch.setattr(shortcut, "pythonw_path", lambda: PYTHONW)
    run = Recorder()
    install_shortcut(Path(r"C:\hk"), run=run)
    assert run.calls[0][1]["env"]["HK_TARGET"] == str(PYTHONW)


@pytest.mark.parametrize(
    "run",
    [
        Recorder(result=subprocess.CompletedProcess([], 1, b"", rb"denied C:\Secret\path")),
        Recorder(error=subprocess.TimeoutExpired("powershell", 30)),
        Recorder(error=OSError(r"no such file C:\Secret\path")),
    ],
    ids=["exit-1", "timeout", "oserror"],
)
def test_shortcut_failure_is_a_plain_error(run, caplog):
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ShortcutError) as caught:
            install_shortcut(Path(r"C:\hk"), run=run, pythonw=PYTHONW)
    assert str(caught.value) == "The Start menu shortcut couldn't be made."
    assert "Secret" not in str(caught.value) and "C:" not in str(caught.value)
    assert "Secret" not in caplog.text and "C:\\" not in caplog.text


def test_installing_twice_runs_the_same_overwriting_script():
    run = Recorder()
    for _ in range(2):
        install_shortcut(Path(r"C:\hk"), run=run, pythonw=PYTHONW)
    assert run.calls[0] == run.calls[1]


def test_drive_root_folder_keeps_its_closing_quote():
    run = Recorder()
    install_shortcut(Path("Q:\\"), run=run, pythonw=PYTHONW)
    env = run.calls[0][1]["env"]
    assert env["HK_ARGS"] == '-m hearthkeep open --data "Q:\\\\"'
    assert env["HK_ARGS"].endswith(startup.quote_arg("Q:\\"))
    assert env["HK_DIR"] == "Q:\\"
