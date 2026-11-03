"""Finding and running helper programs safely: never a bare name, never the current folder."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
