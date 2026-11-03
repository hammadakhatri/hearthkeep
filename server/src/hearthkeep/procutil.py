"""Finding and running helper programs safely: never a bare name, never the current folder."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS: int = getattr(subprocess, "DETACHED_PROCESS", 0)
CREATE_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def system32_exe(name: str) -> Path | None:
    """`name` in Windows' System32 folder, never whatever PATH finds first; None when neither
    SystemRoot nor windir is set."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if not root:
        return None
    return Path(root) / "System32" / name


def detached_popen_kwargs() -> dict:
    """Popen keyword arguments for a child that outlives this process: no console, no inherited
    handles, and its own process group (Windows) or session (elsewhere)."""
    streams = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, **streams}
    return {"start_new_session": True, **streams}


def find_executable(name: str, fallbacks: Sequence[Path] = ()) -> Path | None:
    """`name` as an absolute path: from PATH (a match in the current folder or through a
    relative PATH entry is ignored), else the first fallback that is an absolute existing
    file; None if absent."""
    found = shutil.which(name)
    if found:
        path = Path(found)
        if path.is_absolute() and path.parent.resolve() != Path.cwd().resolve():
            return path
    for candidate in fallbacks:
        candidate = Path(candidate)
        if candidate.is_absolute() and candidate.is_file():
            return candidate
    return None
