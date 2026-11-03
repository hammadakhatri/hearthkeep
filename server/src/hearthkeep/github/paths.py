"""Paths inside a clone and the names around it.

`safe_path` turns a relative POSIX path the model gave into a path inside the clone, refusing
anything that could reach `.git` (in any spelling Windows treats as the same folder), leave
the clone, or pass through a symlink or junction on the way.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from hearthkeep.github.api import REPO_NAME_RE, valid_repo_name

MAX_PATH_CHARS = 400
REFUSED = "That path is outside the repo or inside its .git folder."
BRANCH_PREFIX = "hearthkeep/"
BRANCH_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
REPO_NAME = REPO_NAME_RE

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and other Windows links
_SHORT_GIT = re.compile(r"git~\d+", re.IGNORECASE)  # the 8.3 name of `.git`


class PathRefused(Exception):
    def __init__(self, message: str = REFUSED) -> None:
        self.message = message
        super().__init__(message)


def check_repo_name(full_name: str) -> str:
    """`owner/name` by GitHub's rules, else ValueError."""
    if not valid_repo_name(full_name):
        raise ValueError("Use the owner/name form, like octo/hello-world.")
    return full_name


def _component_ok(part: str) -> bool:
    if part in ("", ".", ".."):
        return False
    if ":" in part or any(ord(ch) < 0x20 for ch in part):
        return False
    # Windows drops trailing dots and spaces, so `.git.` and `.git ` are `.git`; a name made
    # only of them (`...`) is no real name at all.
    stripped = part.rstrip(". ")
    if stripped == "":
        return False
    return stripped.lower() != ".git" and not _SHORT_GIT.fullmatch(stripped)


def _is_link(path: Path) -> bool | None:
    """Whether `path` is a symlink or reparse point; None when it does not exist."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return True  # can't tell: treat as unsafe
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def safe_path(root: Path, rel: str) -> Path:
    """`root / rel` when `rel` is a safe relative POSIX path inside the clone, else
    PathRefused. The path need not exist yet (a file about to be written)."""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_CHARS:
        raise PathRefused()
    if "\\" in rel or rel.startswith("/"):
        raise PathRefused()
    parts = rel.split("/")
    if not all(_component_ok(part) for part in parts):
        raise PathRefused()
    root = Path(root)
    current = root
    for part in parts:
        current = current / part
        link = _is_link(current)
        if link is None:
            break  # nothing further exists, so nothing further can be a link
        if link:
            raise PathRefused()
    target = root.joinpath(*parts)
    # Belt and braces: whatever the walk saw, the resolved path is still inside the clone.
    real_root = os.path.realpath(root)
    if os.path.commonpath([real_root, os.path.realpath(target)]) != real_root:
        raise PathRefused()
    return target
