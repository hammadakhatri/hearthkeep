"""The one place that knows which operating system Hearthkeep runs on.

Setup, the launcher, start at login and the Start menu shortcut ask `current_host()` and
never look at `sys.platform` themselves. Windows is the only supported host for now; every
other OS gets an `UnsupportedHost` that reports "unsupported" and never creates, spawns or
runs anything."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Protocol

from hearthkeep import procutil, shortcut, startup
from hearthkeep.startup import StartupStatus

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = PureWindowsPath(r"C:\Hearthkeep\data")
# llama.cpp's 260-character limit minus the deepest path Hearthkeep makes in the data folder.
MAX_DATA_DIR_CHARS = 100
LOCATION_FILE = "location.json"
DRIVE_REMOTE = 4  # what GetDriveTypeW answers for a network (mapped) drive
_DATA_MARKERS = ("config.toml", "hearthkeep.db")

UNSUPPORTED_HOST = (
    "Setting up Hearthkeep and running it in the background is built in only on Windows for now."
)
NOT_LOCAL = "Choose a folder on this PC's own drive, like C:\\Hearthkeep\\data."
TOO_LONG = (
    f"That folder path is too long. Choose one of {MAX_DATA_DIR_CHARS} characters or fewer, "
    "like C:\\Hearthkeep\\data."
)
BAD_CHARACTERS = "Use only English letters, numbers, spaces and - _ . ( ) in the folder path."
INSIDE_PROGRAM = "Keep Hearthkeep's data outside the Hearthkeep program folder."
NOT_EMPTY = "That folder already has other files in it. Choose an empty folder."

_PART = re.compile(r"[A-Za-z0-9 _.()-]+")


class HostUnsupported(Exception):
    """This OS can't do that; the message is for the owner."""

    def __init__(self, message: str = UNSUPPORTED_HOST):
        super().__init__(message)


def check_data_dir_text(text: str) -> str | None:
    """The Windows rules for a data-folder path, on the text alone (runs on any OS): the
    first failing rule's message, or None when the path is acceptable."""
    path = PureWindowsPath(text)
    drive = path.drive
    if text.startswith(("\\\\", "//")) or not path.root or not re.fullmatch(r"[A-Za-z]:", drive):
        return NOT_LOCAL
    if len(str(path)) > MAX_DATA_DIR_CHARS:
        return TOO_LONG
    for part in path.parts[1:]:
        if not _PART.fullmatch(part) or part.endswith((" ", ".")):
            return BAD_CHARACTERS
    return None


def _inside(path: Path, root: Path) -> bool:
    """Whether `path` is `root` or below it, ignoring case (Windows folders)."""
    parts = [p.casefold() for p in path.parts]
    root_parts = [p.casefold() for p in root.parts]
    return parts[: len(root_parts)] == root_parts


def check_data_dir(path: Path, *, repo_root: Path = REPO_ROOT) -> str | None:
    """`check_data_dir_text`, then: not inside the Hearthkeep folder, and missing, empty or
    already a Hearthkeep data folder. None when the folder can be used."""
    message = check_data_dir_text(str(path))
    if message:
        return message
    path = Path(path)
    if _inside(path.resolve(), Path(repo_root).resolve()):
        return INSIDE_PROGRAM
    if path.is_dir():
        if any(path.iterdir()) and not any((path / name).exists() for name in _DATA_MARKERS):
            return NOT_EMPTY
    elif path.exists():
        return NOT_EMPTY
    return None


class Host(Protocol):
    name: str
    supported: bool

    def default_data_dir(self) -> Path | None: ...

    def check_data_dir(self, path: Path) -> str | None: ...

    def startup_status(self, *, run=subprocess.run, strict: bool = False) -> StartupStatus: ...

    def enable_startup(self, data_dir: Path, *, run=subprocess.run) -> None: ...

    def disable_startup(self, *, run=subprocess.run) -> bool: ...

    def install_shortcut(self, data_dir: Path, *, run=subprocess.run) -> None: ...

    def spawn_detached(
        self, argv: list[str], *, popen=subprocess.Popen
    ) -> subprocess.Popen | None: ...

    def read_location(self) -> Path | None: ...

    def write_location(self, data_dir: Path) -> None: ...


def _drive_type(root: str) -> int:
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(root)


class WindowsHost:
    name = "windows"
    supported = True

    def default_data_dir(self) -> Path | None:
        return Path(str(DEFAULT_DATA_DIR))

    def drive_type(self, root: str) -> int:
        return _drive_type(root)

    def on_network_drive(self, path: Path) -> bool:
        """Whether `path` is on a mapped network drive (`Z:` and the like pass the path rules).
        False when Windows can't say."""
        drive = PureWindowsPath(str(path)).drive
        if not re.fullmatch(r"[A-Za-z]:", drive):
            return False
        try:
            return self.drive_type(drive + "\\") == DRIVE_REMOTE
        except Exception:  # no kernel32 (not Windows) or no answer: not known to be remote
            return False

    def check_data_dir(self, path: Path) -> str | None:
        """`check_data_dir`, plus: not on a mapped network drive."""
        message = check_data_dir(path)
        if message is None and self.on_network_drive(path):
            return NOT_LOCAL
        return message

    def startup_status(self, *, run=subprocess.run, strict: bool = False) -> StartupStatus:
        return startup.status(run=run, strict=strict)

    def enable_startup(self, data_dir: Path, *, run=subprocess.run) -> None:
        startup.enable(data_dir, run=run)

    def disable_startup(self, *, run=subprocess.run) -> bool:
        return startup.disable(run=run)

    def install_shortcut(self, data_dir: Path, *, run=subprocess.run) -> None:
        shortcut.install_shortcut(data_dir, run=run)

    def spawn_detached(self, argv: list[str], *, popen=subprocess.Popen) -> subprocess.Popen | None:
        """Start `argv` detached; the process, so the caller can see whether it has ended."""
        return popen(argv, **procutil.detached_popen_kwargs())

    @staticmethod
    def _location_file() -> Path | None:
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "Hearthkeep" / LOCATION_FILE if base else None

    def read_location(self) -> Path | None:
        """The data folder setup recorded; None when there is no record, it can't be read, or
        the folder it names is gone."""
        file = self._location_file()
        if file is None:
            return None
        try:
            data_dir = json.loads(file.read_text(encoding="utf-8")).get("data_dir")
        except (OSError, ValueError, AttributeError):
            return None
        if not isinstance(data_dir, str) or not data_dir:
            return None
        path = Path(data_dir)
        return path if path.is_absolute() and path.is_dir() else None

    def write_location(self, data_dir: Path) -> None:
        """Record the data folder (resolved), replacing the file in one step."""
        file = self._location_file()
        if file is None:
            raise OSError("LOCALAPPDATA is not set, so the data folder can't be recorded.")
        file.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"data_dir": str(Path(data_dir).resolve())})
        fd, temp = tempfile.mkstemp(prefix=".location-", suffix=".tmp", dir=file.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(temp, file)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise


class UnsupportedHost:
    name = "unsupported"
    supported = False

    def default_data_dir(self) -> Path | None:
        return None

    def check_data_dir(self, path: Path) -> str | None:
        return UNSUPPORTED_HOST

    def startup_status(self, *, run=subprocess.run, strict: bool = False) -> StartupStatus:
        return StartupStatus(False, False, None)

    def enable_startup(self, data_dir: Path, *, run=subprocess.run) -> None:
        raise HostUnsupported()

    def disable_startup(self, *, run=subprocess.run) -> bool:
        return False

    def install_shortcut(self, data_dir: Path, *, run=subprocess.run) -> None:
        raise HostUnsupported()

    def spawn_detached(self, argv: list[str], *, popen=subprocess.Popen) -> subprocess.Popen | None:
        raise HostUnsupported()

    def read_location(self) -> Path | None:
        return None

    def write_location(self, data_dir: Path) -> None:
        raise HostUnsupported()


def current_host() -> Host:
    """The host this process runs on."""
    return WindowsHost() if startup.is_windows() else UnsupportedHost()
