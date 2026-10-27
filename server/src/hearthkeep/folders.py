"""Folders on the owner's PC that the `files` tool may look in, and every path rule it follows.

A folder is added by the owner (with a passkey step-up) and stored by its real path. Every use
re-resolves it and refuses it if it moved or changed. Inside a folder:

- nothing is followed through a link: symlinks, junctions and every other reparse point
  (OneDrive placeholders among them) are refused at every depth, as are hidden and system
  entries, names starting with `.` or `~$`, and files with more than one hard link;
- protected paths (Hearthkeep's data folder, its repository, AppData, Windows, Program Files,
  ProgramData) are never listed or read, even when a shared folder contains them;
- Windows path forms that could alias a file are refused outright: UNC and device paths,
  alternate data streams, reserved device names, and 8.3 short names (the real path must be
  the path written).

Files are opened without following links and the open handle's own final path is compared
with the checked one (`open_checked`), so a swap between the check and the open is caught.
Nothing here writes, moves or deletes anything in the owner's folders.

Folder paths are never logged or audited: only ids and labels.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from secrets import token_urlsafe
from typing import BinaryIO

from hearthkeep import db

FOLDER_MOVED = "That folder has moved or changed. Remove it and add it again."
BAD_PATH = "Enter the full path of a folder on this PC (on a local drive, not a network share)."
NO_FOLDER = "There's no folder at that path."
NOT_REAL_PATH = "Enter the folder's real path, not a link, shortcut or short name."
IS_ROOT = "Choose a folder, not a whole drive."
PROTECTED = (
    "That folder can't be shared: it is or is inside Hearthkeep's own data, Windows or "
    "installed apps."
)
HIDDEN_FOLDER = "Hidden and system folders can't be shared."
ALREADY_ADDED = "That folder is already shared."
NO_FILE = "There's no file Hearthkeep can open at that path in that folder."
ONLINE_ONLY = "That file is online-only; make it available offline in OneDrive to use it."
ONLINE_ONLY_FOLDER = (
    "That folder is online-only; make it available offline in OneDrive to share it."
)
ONLINE_SKIPPED = (
    "{n} online-only files skipped; make them available offline in OneDrive to use them"
)
UNKNOWN_FOLDER = "There's no shared folder with that name."

MAX_PATH_CHARS = 1024
MAX_DEPTH = 8
MAX_ENTRIES = 20_000
SEARCH_SECONDS = 5
MAX_MATCHES = 50

# Windows file attributes.
_HIDDEN = 0x2
_SYSTEM = 0x4
_REPARSE_POINT = 0x400
_OFFLINE = 0x1000
_RECALL_ON_OPEN = 0x40000
_RECALL_ON_DATA_ACCESS = 0x400000
# Data not on this PC: reading it would make OneDrive (or another provider) download it.
_ONLINE_ONLY = _OFFLINE | _RECALL_ON_OPEN | _RECALL_ON_DATA_ACCESS
# Reparse tags. A cloud-files tag (OneDrive) is a placeholder, not a link: its data is the
# file's own. Every other tag (symlinks 0xA000000C, junctions and mount points 0xA0000003,
# and anything unknown) is refused.
_TAG_CLOUD_MASK = 0xFFFF0FFF
_TAG_CLOUD = 0x9000001A

# Hooks (tests fake attributes, reparse tags and endless folders through these).
_lstat = os.lstat
_fstat = os.fstat
_scandir = os.scandir


def _entry_stat(entry: os.DirEntry) -> os.stat_result:
    return entry.stat(follow_symlinks=False)


_WINDOWS = sys.platform == "win32"
_DRIVE_PATH = re.compile(r"[A-Za-z]:\\")
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{n}" for port in ("COM", "LPT") for n in [*"123456789", "¹", "²", "³"]}
)
# The kinds search lists, by extension (bring decides the real kind from the content).
KIND_BY_EXT = {
    "csv": "csv",
    "xlsx": "xlsx",
    "xls": "xls",
    "pdf": "pdf",
    "txt": "txt",
    "md": "txt",
    "log": "txt",
    "json": "json",
}


class FolderError(Exception):
    """A folder or path Hearthkeep won't use; `message` is safe to show (it holds no path)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class AllowedFolder:
    id: str
    path: Path
    label: str
    created_at: str


@dataclass
class FoundFile:
    folder: str  # the folder's label
    path: str  # relative to the folder, `/`-separated
    size: int
    modified: str  # local time, ISO 8601 to the minute
    kind: str


@dataclass
class SearchOutcome:
    found: list[FoundFile]
    cut: bool = False  # stopped at the entry or time budget
    moved: list[str] = field(default_factory=list)  # labels of folders that moved
    online_only: int = 0  # matching files skipped because their data isn't on this PC


# ---- protected paths -----------------------------------------------------------------------------


def _home() -> Path:
    return Path.home()


def _repo_root() -> Path:
    """The repository this package runs from; when installed elsewhere, the package itself."""
    here = Path(__file__).resolve()
    root = here.parents[3]
    if (root / "server" / "src" / "hearthkeep").is_dir():
        return root
    return here.parent


def system_paths() -> list[Path]:
    """The owner's AppData and the system's own folders (Windows only)."""
    if not _WINDOWS:
        return []
    found = [_home() / "AppData"]
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if root:
        found.append(Path(root))
    for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        value = os.environ.get(name)
        if value:
            found.append(Path(value))
    system_drive = os.environ.get("SystemDrive", "C:")
    found += Path(system_drive + "\\").glob("Program Files*")
    found += [Path(system_drive + "\\Windows"), Path(system_drive + "\\ProgramData")]
    return _resolved_unique(found)


def _resolved_unique(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            real = path.resolve()
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(str(real))
        if key not in seen:
            seen.add(key)
            out.append(real)
    return out


def protected_paths(data_dir: Path) -> list[Path]:
    """Never shared, listed or read: the data folder, the repository root, and on Windows the
    owner's AppData, Windows, Program Files and ProgramData (all resolved)."""
    return _resolved_unique([Path(data_dir), _repo_root(), *system_paths()])


def _key(path: Path | str) -> str:
    return os.path.normcase(str(path))


def _within(path: Path | str, root: Path | str) -> bool:
    """`path` is `root` or inside it (case-insensitively on Windows)."""
    p, r = _key(path), _key(root)
    if p == r:
        return True
    return p.startswith(r if r.endswith(os.sep) else r + os.sep)


# ---- path checks ---------------------------------------------------------------------------------


def _reserved(name: str) -> bool:
    stem = name.rstrip(" .").split(".", 1)[0].rstrip(" ")
    return stem.upper() in _RESERVED


def _attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def _cloud_tag(st: os.stat_result) -> bool:
    tag = getattr(st, "st_reparse_tag", 0)
    return bool(_attributes(st) & _REPARSE_POINT) and tag & _TAG_CLOUD_MASK == _TAG_CLOUD


def _online_only(st: os.stat_result) -> bool:
    return bool(_attributes(st) & _ONLINE_ONLY)


def _is_cloud(st: os.stat_result) -> bool:
    """A OneDrive (cloud files) placeholder whose data is on this PC."""
    return _cloud_tag(st) and not _online_only(st)


def _is_link(st: os.stat_result) -> bool:
    """A symlink, a junction or mount point, or any other reparse point except a cloud
    placeholder."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(_attributes(st) & _REPARSE_POINT) and not _cloud_tag(st)


def _skipped(name: str, st: os.stat_result) -> str | None:
    """Why an entry is never listed or followed: "refused" (a link, junction or other
    reparse point, a hidden or system entry, a dot or `~$` or reserved name), "online" (its
    data isn't on this PC), or None."""
    if name.startswith((".", "~$")) or _reserved(name):
        return "refused"
    if _attributes(st) & (_HIDDEN | _SYSTEM) or _is_link(st):
        return "refused"
    if _online_only(st):
        return "online"
    return None


def _check_raw_folder(raw: str) -> None:
    """Refuse every path form that isn't a plain absolute path on a local drive."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_CHARS or "\x00" in raw:
        raise FolderError(BAD_PATH)
    if _WINDOWS:
        path = raw.replace("/", "\\")
        # UNC (\\server\share), device (\\?\, \\.\) and NT (\??\) paths.
        if path.startswith("\\\\") or path.startswith("\\??\\"):
            raise FolderError(BAD_PATH)
        if not _DRIVE_PATH.match(path):
            raise FolderError(BAD_PATH)  # relative, drive-relative or rooted without a drive
        if ":" in path[2:]:
            raise FolderError(BAD_PATH)  # an alternate data stream
        parts = path[3:].split("\\")
    else:
        if not raw.startswith("/"):
            raise FolderError(BAD_PATH)
        parts = raw[1:].split("/")
    for part in parts:
        if not part:
            continue  # a doubled or trailing separator
        # Windows quietly drops a trailing dot or space: the path would name another folder.
        if _reserved(part) or part != part.rstrip(" ."):
            raise FolderError(BAD_PATH)


def _real_folder(raw: str) -> Path:
    """The folder `raw` names, checked: it exists, it is the path written (no link, junction,
    short name or other alias anywhere along it), and it is a plain directory that isn't
    hidden or system. Touches no database (safe on a worker thread)."""
    _check_raw_folder(raw)
    try:
        real = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise FolderError(NO_FOLDER) from None
    if str(real).startswith("\\\\"):
        raise FolderError(BAD_PATH)  # a mapped network drive resolves to a UNC path
    if _key(os.path.abspath(raw)) != _key(real):
        raise FolderError(NOT_REAL_PATH)
    if real.parent == real:
        raise FolderError(IS_ROOT)
    _check_chain(real)
    try:
        st = _lstat(real)
    except OSError:
        raise FolderError(NO_FOLDER) from None
    if not stat.S_ISDIR(st.st_mode):
        raise FolderError(NO_FOLDER)
    if _attributes(st) & (_HIDDEN | _SYSTEM):
        raise FolderError(HIDDEN_FOLDER)
    return real


def _check_chain(real: Path) -> None:
    """No component from the drive down to `real` is a link, junction or reparse point other
    than a cloud placeholder, or online-only (the drive root itself is skipped: it is hidden
    and system on Windows)."""
    anchor = Path(real.anchor)
    current = anchor
    for part in real.relative_to(anchor).parts:
        current = current / part
        try:
            st = _lstat(current)
        except OSError:
            raise FolderError(NO_FOLDER) from None
        if _is_link(st):
            raise FolderError(NOT_REAL_PATH)
        if _online_only(st):
            raise FolderError(ONLINE_ONLY_FOLDER)


def _split_rel(rel: str) -> list[str]:
    """The components of a path relative to a folder, or FolderError: no absolute or
    drive-qualified path, no `.`/`..`/empty component, no stream, no reserved name, no
    trailing dot or space (Windows would quietly drop it)."""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_CHARS or "\x00" in rel:
        raise FolderError(NO_FILE)
    if ":" in rel or rel.startswith(("/", "\\")):
        raise FolderError(NO_FILE)
    parts = re.split(r"[\\/]", rel) if _WINDOWS else rel.split("/")
    for part in parts:
        if part in ("", ".", "..") or part != part.rstrip(" .") or _reserved(part):
            raise FolderError(NO_FILE)
    return parts


def _verify_handle(fd: int, handle_path: str | None, expected: Path, st: os.stat_result) -> None:
    """Fail closed unless the open file is the regular file the walk checked: not a link and
    not online-only, the same file id as the walk's own lstat, one hard link only, and (on
    Windows) a final path, read from the handle itself, that is the checked path.

    The final-path check is Windows-only: on POSIX, O_NOFOLLOW guards the last component and
    the file id comparison is what catches a swap anywhere along the path."""
    opened = _fstat(fd)
    if not stat.S_ISREG(opened.st_mode) or _is_link(opened):
        raise FolderError(NO_FILE)
    if _online_only(opened):
        raise FolderError(ONLINE_ONLY)
    if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino) or opened.st_nlink != 1:
        raise FolderError(NO_FILE)
    if _WINDOWS and (handle_path is None or _key(handle_path) != _key(expected)):
        raise FolderError(NO_FILE)


def _final_path(handle: int) -> str | None:
    """GetFinalPathNameByHandleW, without the `\\\\?\\` prefix; None if it can't be read."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    get_final.restype = ctypes.c_uint32
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        n = get_final(handle, buffer, size, 0)  # FILE_NAME_NORMALIZED | VOLUME_NAME_DOS
        if n == 0:
            return None
        if n < size:
            break
        size = n + 1
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _open_no_follow(path: Path) -> tuple[int, str | None]:
    """An fd for reading `path`, opened without following a final link; on Windows also the
    handle's final path."""
    if _WINDOWS:
        import _winapi
        import msvcrt

        generic_read = 0x80000000
        share_all = 0x1 | 0x2 | 0x4  # read, write, delete: never in the owner's way
        open_existing = 3
        flags = 0x00200000 | 0x08000000  # FILE_FLAG_OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
        try:
            handle = _winapi.CreateFile(
                str(path), generic_read, share_all, 0, open_existing, flags, 0
            )
        except OSError:
            raise FolderError(NO_FILE) from None
        try:
            final = _final_path(handle)
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
        return fd, final
    try:
        return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), None
    except OSError:
        raise FolderError(NO_FILE) from None


# ---- the store -----------------------------------------------------------------------------------


def _folder(row: sqlite3.Row) -> AllowedFolder:
    return AllowedFolder(row["id"], Path(row["path"]), row["label"], row["created_at"])


class FolderStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conn = conn
        self._protected = protected_paths(Path(data_dir))
        self._clock = clock

    # -- adding and removing (database: the event loop's thread) --

    def check_new(self, raw_path: str) -> Path:
        """The real path of a folder that may be shared, or FolderError. Disk only, no
        database: run it on a worker thread, then `add_checked`."""
        real = _real_folder(raw_path)
        if any(_within(real, p) for p in self._protected):
            raise FolderError(PROTECTED)
        return real

    def add_checked(self, real: Path) -> AllowedFolder:
        existing = self.list()
        if any(_key(f.path) == _key(real) for f in existing):
            raise FolderError(ALREADY_ADDED)
        taken = {f.label.casefold() for f in existing}
        base = real.name
        label, n = base, 2
        while label.casefold() in taken:
            label, n = f"{base} {n}", n + 1
        folder = AllowedFolder(token_urlsafe(12), real, label, db.utcnow())
        try:
            self._conn.execute(
                "INSERT INTO allowed_folders (id, path, label, created_at) VALUES (?, ?, ?, ?)",
                (folder.id, str(folder.path), folder.label, folder.created_at),
            )
        except sqlite3.IntegrityError:
            raise FolderError(ALREADY_ADDED) from None
        return folder

    def add(self, raw_path: str) -> AllowedFolder:
        return self.add_checked(self.check_new(raw_path))

    def remove(self, folder_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM allowed_folders WHERE id = ?", (folder_id,))
        return cursor.rowcount > 0

    def list(self) -> list[AllowedFolder]:
        rows = self._conn.execute(
            "SELECT id, path, label, created_at FROM allowed_folders ORDER BY created_at, rowid"
        )
        return [_folder(r) for r in rows]

    def by_label(self, label: str) -> AllowedFolder | None:
        wanted = label.strip().casefold()
        return next((f for f in self.list() if f.label.casefold() == wanted), None)

    def suggestions(self) -> list[dict]:
        """Common home folders that exist, may be shared and aren't yet: for the owner's
        session only (never the model)."""
        return self.suggest(self.list())

    def suggest(self, added_folders: list[AllowedFolder]) -> list[dict]:
        """`suggestions` given the folders already added: disk only (a worker thread)."""
        home = _home()
        candidates = [
            ("Documents", home / "Documents"),
            ("Downloads", home / "Downloads"),
            ("Desktop", home / "Desktop"),
            ("OneDrive Documents", home / "OneDrive" / "Documents"),
            ("OneDrive Desktop", home / "OneDrive" / "Desktop"),
        ]
        # Work or school OneDrive: "OneDrive - <organisation>".
        try:
            business = sorted(p for p in home.glob("OneDrive - *") if p.is_dir())
        except OSError:
            business = []
        for root in business:
            candidates += [(f"{root.name} {n}", root / n) for n in ("Documents", "Desktop")]
        added = {_key(f.path) for f in added_folders}
        out = []
        for label, path in candidates:
            try:
                real = self.check_new(str(path))
            except FolderError:
                continue
            if _key(real) not in added:
                out.append({"label": label, "path": str(real)})
        return out

    # -- using a folder (disk only: safe on a worker thread) --

    def live_root(self, folder: AllowedFolder) -> Path:
        """The folder's real path now, which must still be the one stored."""
        try:
            real = self.check_new(str(folder.path))
        except FolderError:
            raise FolderError(FOLDER_MOVED) from None
        if _key(real) != _key(folder.path):
            raise FolderError(FOLDER_MOVED)
        return real

    def _protected_inside(self, path: Path) -> bool:
        return any(_within(path, p) for p in self._protected)

    def _checked(self, folder: AllowedFolder, rel: str) -> tuple[Path, os.stat_result]:
        root = self.live_root(folder)
        parts = _split_rel(rel)
        current = root
        st: os.stat_result | None = None
        for part in parts:
            current = current / part
            try:
                st = _lstat(current)
            except OSError:
                raise FolderError(NO_FILE) from None
            if self._protected_inside(current):
                raise FolderError(NO_FILE)
            reason = _skipped(part, st)
            if reason == "online":
                raise FolderError(ONLINE_ONLY)  # never a download
            if reason is not None:
                raise FolderError(NO_FILE)
        assert st is not None
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise FolderError(NO_FILE)  # a directory, a device, or a hard link
        try:
            real = current.resolve(strict=True)
        except (OSError, RuntimeError):
            raise FolderError(NO_FILE) from None
        # The resolved path must be exactly the path walked: no alias (an 8.3 name) anywhere.
        if _key(real) != _key(current) or not _within(real, root) or self._protected_inside(real):
            raise FolderError(NO_FILE)
        return real, st

    def resolve_inside(self, folder: AllowedFolder, rel: str) -> Path:
        """The regular file at `rel` inside the folder, checked component by component, or
        FolderError."""
        return self._checked(folder, rel)[0]

    def open_checked(self, folder: AllowedFolder, rel: str) -> tuple[BinaryIO, Path]:
        """The file at `rel`, opened for reading after the final check without following a
        link, and verified through the open handle itself. Read from the handle only."""
        # The handle is compared with the walk's own lstat of the file: there is no second
        # look at the path, which could be swapped in between.
        path, st = self._checked(folder, rel)
        fd, handle_path = _open_no_follow(path)
        try:
            _verify_handle(fd, handle_path, path, st)
            return os.fdopen(fd, "rb"), path
        except BaseException:
            os.close(fd)
            raise

    # -- search --

    def search(
        self,
        *,
        query: str | None,
        folder: str | None,
        kinds: list[str] | None,
        modified_after: date | None,
        modified_before: date | None,
    ) -> list[FoundFile]:
        if folder is not None:
            chosen = self.by_label(folder)
            if chosen is None:
                raise FolderError(UNKNOWN_FOLDER)
            self.live_root(chosen)
            targets = [chosen]
        else:
            targets = self.list()
        outcome = self.search_in(
            targets,
            query=query,
            kinds=kinds,
            modified_after=modified_after,
            modified_before=modified_before,
        )
        return outcome.found

    def search_in(
        self,
        folders: list[AllowedFolder],
        *,
        query: str | None,
        kinds: list[str] | None,
        modified_after: date | None,
        modified_before: date | None,
    ) -> SearchOutcome:
        """Walk the folders (never through a link), at most MAX_DEPTH levels, MAX_ENTRIES
        entries and SEARCH_SECONDS; the newest MAX_MATCHES matches. Disk only: run it on a
        worker thread."""
        terms = [t.casefold() for t in (query or "").split()]
        wanted = set(kinds) if kinds else None
        deadline = self._clock() + SEARCH_SECONDS
        entries = 0
        matches: list[tuple[float, FoundFile]] = []
        outcome = SearchOutcome(found=[])

        def wanted_file(name: str, rel: str, st: os.stat_result) -> tuple[str, datetime] | None:
            kind = KIND_BY_EXT.get(name.rpartition(".")[2].lower()) if "." in name else None
            if kind is None or (wanted is not None and kind not in wanted):
                return None
            if terms and not all(t in rel.casefold() for t in terms):
                return None
            try:
                modified = datetime.fromtimestamp(st.st_mtime)
            except (OverflowError, OSError, ValueError):
                return None
            if modified_after is not None and modified.date() < modified_after:
                return None
            if modified_before is not None and modified.date() > modified_before:
                return None
            return kind, modified

        def listing(directory: Path, is_root: bool) -> list[tuple[os.DirEntry, os.stat_result]]:
            """The directory's entries, read within the entry and time budgets (checked as
            each entry arrives); dropped if the directory changed while it was read."""
            nonlocal entries
            try:
                before = _lstat(directory)
                if not is_root and (_is_link(before) or _online_only(before)):
                    return []
                got: list[tuple[os.DirEntry, os.stat_result]] = []
                with _scandir(directory) as found:
                    for entry in found:
                        entries += 1
                        if entries > MAX_ENTRIES or self._clock() > deadline:
                            outcome.cut = True
                            break
                        try:
                            if entry.is_symlink() or entry.is_junction():
                                continue
                            got.append((entry, _entry_stat(entry)))
                        except OSError:
                            continue
                after = _lstat(directory)
            except OSError:
                return []
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or _is_link(after):
                return []  # swapped while it was read: none of it is trusted
            return got

        for folder in folders:
            try:
                root = self.live_root(folder)
            except FolderError:
                outcome.moved.append(folder.label)
                continue
            stack: list[tuple[Path, str, int]] = [(root, "", 0)]
            while stack and not outcome.cut:
                directory, prefix, depth = stack.pop()
                got = listing(directory, directory == root)
                # Sorted for a steady order: only what was read within the budget.
                for entry, st in sorted(got, key=lambda item: item[0].name):
                    reason = _skipped(entry.name, st)
                    rel = f"{prefix}{entry.name}"
                    if reason == "online":
                        if stat.S_ISREG(st.st_mode) and wanted_file(entry.name, rel, st):
                            outcome.online_only += 1
                        continue
                    if reason is not None:
                        continue
                    full = Path(entry.path)
                    if self._protected_inside(full):
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if depth + 1 <= MAX_DEPTH:
                            stack.append((full, rel + "/", depth + 1))
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    hit = wanted_file(entry.name, rel, st)
                    if hit is None:
                        continue
                    try:
                        full_st = _lstat(full)  # st_nlink: DirEntry.stat leaves it 0 on Windows
                    except OSError:
                        continue
                    if full_st.st_nlink > 1 or _skipped(entry.name, full_st) is not None:
                        continue
                    kind, modified = hit
                    found = FoundFile(folder.label, rel, st.st_size,
                                      modified.isoformat(timespec="minutes"), kind)  # fmt: skip
                    matches.append((st.st_mtime, found))
            if outcome.cut:
                break
        matches.sort(key=lambda m: m[0], reverse=True)
        outcome.found = [m[1] for m in matches[:MAX_MATCHES]]
        return outcome
