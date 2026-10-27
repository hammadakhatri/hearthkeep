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

    def serves(self, data_dir: Path | str) -> bool:
        """Whether the task starts Hearthkeep with this data folder."""
        if self.data_dir is None:
            return False
        return same_folder(self.data_dir, data_dir)


def same_folder(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(
        os.path.normpath(str(b))
    )


def on_for_another_folder(state: StartupStatus, data_dir: Path | str) -> bool:
    """The task is on but starts Hearthkeep with another data folder. `data_dir` is resolved
    first, as `enable` resolves the folder it writes into the task."""
    if not state.enabled or state.data_dir is None:
        return False
    return not state.serves(Path(data_dir).resolve())


def quote_arg(value: str) -> str:
    """One Windows command-line argument in double quotes. Backslashes before the closing
    quote are doubled, so a drive root (a data folder at the top of a drive) keeps its
    quote."""
    trailing = len(value) - len(value.rstrip("\\"))
    return '"' + value + "\\" * trailing + '"'


def _unquoted(value: str) -> str:
    """The value quote_arg wrapped (its doubled trailing backslashes halved)."""
    trailing = len(value) - len(value.rstrip("\\"))
    return value[: len(value) - trailing // 2] if trailing else value


def schtasks_exe() -> str:
    """schtasks.exe from System32, never whatever PATH finds first."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if not root:
        raise StartupError("Windows' SystemRoot folder is not set, so schtasks can't be found.")
    return str(Path(root) / "System32" / "schtasks.exe")


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
    arguments = (
        f"-m hearthkeep serve --data {quote_arg(data)} "
        f"--log-file {quote_arg(str(log_path(data_dir)))}"
    )
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
        [schtasks_exe(), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=SCHTASKS_TIMEOUT_S,
        check=False,
        creationflags=_NO_WINDOW,
    )


def _call(run: Run, *args: str) -> subprocess.CompletedProcess:
    """schtasks for a change: no answer in time, or no schtasks at all, is a StartupError."""
    try:
        return _schtasks(run, *args)
    except subprocess.TimeoutExpired:
        raise StartupError(f"schtasks didn't answer within {SCHTASKS_TIMEOUT_S} seconds.") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise StartupError(f"schtasks couldn't run ({type(exc).__name__}).") from None


def _error_text(result: subprocess.CompletedProcess) -> str:
    return _decode(result.stderr or b"").strip() or f"schtasks exit code {result.returncode}"


def enable(data_dir: Path, *, run: Run = subprocess.run, pythonw: Path | None = None) -> None:
    """Create (or replace) the task for the current user."""
    if not is_windows():
        raise StartupError(UNSUPPORTED)
    data = Path(data_dir).resolve()
    exe = pythonw or Path(sys.executable).with_name("pythonw.exe")
    if not exe.is_file():
        raise StartupError(
            f"pythonw.exe was not found next to this Python ({exe}), so Hearthkeep can't "
            "start without a window. Reinstall Python with its standard files."
        )
    xml = task_xml(pythonw=exe, data_dir=data, user=_current_user())
    fd, name = tempfile.mkstemp(prefix="hearthkeep-task-", suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        result = _call(run, "/Create", "/TN", TASK_NAME, "/XML", name, "/F")
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
    result = _call(run, "/Delete", "/TN", TASK_NAME, "/F")
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
    data = None
    if match:
        data = _unquoted(match.group(1)) if match.group(1) is not None else match.group(2)
    return StartupStatus(supported=True, enabled=enabled, data_dir=data)


def status(*, run: Run = subprocess.run) -> StartupStatus:
    """Whether the task exists and is enabled, and the data folder it serves."""
    if not is_windows():
        return StartupStatus(supported=False, enabled=False, data_dir=None)
    try:
        result = _schtasks(run, "/Query", "/TN", TASK_NAME, "/XML")
    except (OSError, subprocess.SubprocessError, StartupError):
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
