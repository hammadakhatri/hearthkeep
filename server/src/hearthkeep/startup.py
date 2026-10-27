"""Start at login on Windows: a Task Scheduler task that runs `pythonw -m hearthkeep serve`
when the owner signs in, with no window and its output in `<data>/logs/hearthkeep.log`.

Only the CLI changes the task; the web app shows its state (`cached_status`)."""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

TASK_NAME = "Hearthkeep"
LOG_FILE = Path("logs") / "hearthkeep.log"
STATUS_CACHE_SECONDS = 30.0
UNSUPPORTED = (
    "Start at login is built in only on Windows. See docs/install.md for macOS and Linux."
)
SCHTASKS_TIMEOUT_S = 15

_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DATA_ARG = re.compile(r'--data\s+(?:"([^"]*)"|(\S+))')

Run = Callable[..., subprocess.CompletedProcess]


class StartupError(Exception):
    """The task could not be changed; the message is for the owner."""


@dataclass(frozen=True)
class StartupStatus:
    supported: bool
    enabled: bool
    data_dir: str | None


def is_windows() -> bool:
    return sys.platform == "win32"


def log_path(data_dir: Path) -> Path:
    return Path(data_dir) / LOG_FILE


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    name = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def task_xml(*, pythonw: Path, data_dir: Path, user: str) -> str:
    """The task definition: at the user's logon, no window, no time limit, restarted on
    failure. Every value is XML-escaped."""
    data = str(data_dir)
    arguments = f'-m hearthkeep serve --data "{data}" --log-file "{log_path(data_dir)}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Hearthkeep, your private AI at home.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(pythonw))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(data)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(run: Run, *args: str) -> subprocess.CompletedProcess:
    return run(
        ["schtasks", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=SCHTASKS_TIMEOUT_S,
        check=False,
        creationflags=_NO_WINDOW,
    )


def _error_text(result: subprocess.CompletedProcess) -> str:
    return _decode(result.stderr or b"").strip() or f"schtasks exit code {result.returncode}"


def enable(data_dir: Path, *, run: Run = subprocess.run, pythonw: Path | None = None) -> None:
    """Create (or replace) the task for the current user."""
    if not is_windows():
        raise StartupError(UNSUPPORTED)
    data = Path(data_dir).resolve()
    exe = pythonw or Path(sys.executable).with_name("pythonw.exe")
    xml = task_xml(pythonw=exe, data_dir=data, user=_current_user())
    fd, name = tempfile.mkstemp(prefix="hearthkeep-task-", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        result = _schtasks(run, "/Create", "/TN", TASK_NAME, "/XML", name, "/F")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)
    clear_cache()
    if result.returncode != 0:
        raise StartupError(f"Windows refused to create the task: {_error_text(result)}")


def disable(*, run: Run = subprocess.run) -> bool:
    """Delete the task; False if there was none."""
    if not is_windows():
        raise StartupError(UNSUPPORTED)
    result = _schtasks(run, "/Delete", "/TN", TASK_NAME, "/F")
    clear_cache()
    return result.returncode == 0


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if b"\x00" in raw[:200]:
        return raw.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if is_windows() else "latin-1", errors="replace")


def _parse(xml: str) -> StartupStatus:
    body = xml.strip().lstrip("﻿")
    if body.startswith("<?xml"):
        body = body.split("?>", 1)[1]
    root = ET.fromstring(body)
    enabled = (root.findtext("t:Settings/t:Enabled", "true", _NS) or "").strip() == "true"
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", "", _NS) or ""
    match = _DATA_ARG.search(arguments)
    data = (match.group(1) or match.group(2)) if match else None
    return StartupStatus(supported=True, enabled=enabled, data_dir=data)


def status(*, run: Run = subprocess.run) -> StartupStatus:
    """Whether the task exists and is enabled, and the data folder it serves."""
    if not is_windows():
        return StartupStatus(supported=False, enabled=False, data_dir=None)
    try:
        result = _schtasks(run, "/Query", "/TN", TASK_NAME, "/XML")
    except (OSError, subprocess.SubprocessError):
        return StartupStatus(supported=True, enabled=False, data_dir=None)
    if result.returncode != 0:
        return StartupStatus(supported=True, enabled=False, data_dir=None)
    try:
        return _parse(_decode(result.stdout or b""))
    except ET.ParseError:
        return StartupStatus(supported=True, enabled=False, data_dir=None)


# The web app's view of the task, kept STATUS_CACHE_SECONDS so the page cannot make the server
# spawn schtasks over and over. Any change made in this process busts it.
_cache: tuple[float, StartupStatus] | None = None
_cache_lock = threading.Lock()


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def cached_status(now: float) -> StartupStatus:
    global _cache
    with _cache_lock:
        if _cache is not None and now - _cache[0] < STATUS_CACHE_SECONDS:
            return _cache[1]
    fresh = status()
    with _cache_lock:
        _cache = (now, fresh)
    return fresh
