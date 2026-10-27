"""Start at login: the Task Scheduler task, its XML and the schtasks calls."""

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hearthkeep import startup

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
PYTHONW = Path("<venv>/Scripts/pythonw.exe")


def parse(xml: str) -> ET.Element:
    # ElementTree refuses a str that still carries its encoding declaration.
    return ET.fromstring(xml.split("?>", 1)[1])


class FakeSchtasks:
    """Records every argv and answers like schtasks: the task exists once created."""

    def __init__(self, exists: bool = False, enabled: bool = True):
        self.calls: list[tuple[list[str], dict]] = []
        self.xml: str | None = None
        self.created_xml: str | None = None
        if exists:
            self.xml = startup.task_xml(pythonw=PYTHONW, data_dir=Path("<data>/hk"), user="PC\\me")
            if not enabled:
                on = "<Enabled>true</Enabled>\n  </Settings>"
                self.xml = self.xml.replace(on, on.replace("true", "false"))

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if "/Create" in argv:
            self.created_xml = Path(argv[argv.index("/XML") + 1]).read_text(encoding="utf-16")
            self.xml = self.created_xml
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "/Delete" in argv:
            existed, self.xml = self.xml is not None, None
            return subprocess.CompletedProcess(argv, 0 if existed else 1, b"", b"")
        if "/Query" in argv:
            if self.xml is None:
                return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: not found")
            return subprocess.CompletedProcess(argv, 0, self.xml.encode("utf-16"), b"")
        raise AssertionError(argv)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(startup, "is_windows", lambda: True)
    startup.clear_cache()
    yield
    startup.clear_cache()


def test_task_xml_has_logon_trigger_no_window_no_time_limit():
    data = Path("Hearth & Keep/<data>")
    xml = startup.task_xml(pythonw=PYTHONW, data_dir=data, user='PC\\o"wner')
    root = parse(xml)
    trigger = root.find("t:Triggers/t:LogonTrigger", NS)
    assert trigger is not None and trigger.findtext("t:UserId", namespaces=NS) == 'PC\\o"wner'
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.findtext("t:LogonType", namespaces=NS) == "InteractiveToken"
    assert principal.findtext("t:RunLevel", namespaces=NS) == "LeastPrivilege"
    settings = root.find("t:Settings", NS)
    assert settings.findtext("t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
    assert settings.findtext("t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert settings.findtext("t:DisallowStartIfOnBatteries", namespaces=NS) == "false"
    assert settings.findtext("t:StopIfGoingOnBatteries", namespaces=NS) == "false"
    assert settings.findtext("t:RestartOnFailure/t:Interval", namespaces=NS) == "PT1M"
    assert settings.findtext("t:RestartOnFailure/t:Count", namespaces=NS) == "3"
    exec_ = root.find("t:Actions/t:Exec", NS)
    assert exec_.findtext("t:Command", namespaces=NS) == str(PYTHONW)
    assert exec_.findtext("t:Command", namespaces=NS).endswith("pythonw.exe")
    args = exec_.findtext("t:Arguments", namespaces=NS)
    log = data / "logs" / "hearthkeep.log"
    assert args == f'-m hearthkeep serve --data "{data}" --log-file "{log}"'
    assert "--insecure-dev-auth" not in xml
    assert exec_.findtext("t:WorkingDirectory", namespaces=NS) == str(data)
    # the values are escaped, not pasted into the markup
    assert "& Keep" not in xml and "<data>" not in xml


def test_enable_disable_status_call_schtasks(windows, tmp_path):
    run = FakeSchtasks()
    startup.enable(tmp_path, run=run, pythonw=PYTHONW)
    argv, kw = run.calls[0]
    assert argv[:5] == ["schtasks", "/Create", "/TN", "Hearthkeep", "/XML"]
    assert argv[6:] == ["/F"]
    assert not Path(argv[5]).exists()  # the temp XML is removed
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert f'--data "{tmp_path.resolve()}"' in run.created_xml

    status = startup.status(run=run)
    assert status == startup.StartupStatus(True, True, str(tmp_path.resolve()))
    assert run.calls[-1][0] == ["schtasks", "/Query", "/TN", "Hearthkeep", "/XML"]

    assert startup.disable(run=run) is True
    assert run.calls[-1][0] == ["schtasks", "/Delete", "/TN", "Hearthkeep", "/F"]
    assert startup.status(run=run) == startup.StartupStatus(True, False, None)
    assert startup.disable(run=run) is False


def test_status_reads_a_disabled_task(windows):
    run = FakeSchtasks(exists=True, enabled=False)
    assert startup.status(run=run) == startup.StartupStatus(True, False, str(Path("<data>/hk")))


def test_status_off_when_task_missing(windows):
    assert startup.status(run=FakeSchtasks()) == startup.StartupStatus(True, False, None)


def test_enable_failure_raises(windows, tmp_path):
    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: Access is denied.")

    with pytest.raises(startup.StartupError):
        startup.enable(tmp_path, run=failing, pythonw=PYTHONW)


def test_not_windows_is_unsupported(monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "is_windows", lambda: False)
    run = FakeSchtasks()
    assert startup.status(run=run) == startup.StartupStatus(False, False, None)
    with pytest.raises(startup.StartupError, match="only on Windows"):
        startup.enable(tmp_path, run=run, pythonw=PYTHONW)
    with pytest.raises(startup.StartupError, match="only on Windows"):
        startup.disable(run=run)
    assert run.calls == []


def test_cached_status_kept_30_seconds_and_busted_by_enable(windows, monkeypatch, tmp_path):
    calls = []

    def fake_status(*, run=subprocess.run):
        calls.append(1)
        return startup.StartupStatus(True, False, None)

    monkeypatch.setattr(startup, "status", fake_status)
    startup.cached_status(100.0)
    startup.cached_status(129.9)
    assert len(calls) == 1
    startup.cached_status(130.0)
    assert len(calls) == 2
    startup.enable(tmp_path, run=FakeSchtasks(), pythonw=PYTHONW)
    startup.cached_status(130.1)
    assert len(calls) == 3
