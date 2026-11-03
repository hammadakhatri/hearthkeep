"""The repos the owner added: a row each, and a clone under `<data>/workspace/repos/<id>`.

Adding checks the name, asks GitHub about the repo, clones it into `runtime/` and only then
moves the clone into place and inserts the row: a failure at any step leaves neither. The
remote URL is always passed explicitly; the token travels only in git's environment.
Repo names are the owner's: errors raised here name no repo, and nothing here logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Literal

from hearthkeep import db
from hearthkeep.github.api import TOKEN_SECRET, GitHubClient, GitHubError
from hearthkeep.github.git import GitError, GitRunner
from hearthkeep.github.paths import check_repo_name
from hearthkeep.secrets import SecretStore, SecretStoreError

_log = logging.getLogger(__name__)
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and other Windows links
_REPO_FOLDER = re.compile(r"[A-Za-z0-9_-]{1,32}")  # a repo id, as the routes accept it
_STAGING_FOLDER = re.compile(r"clone-[A-Za-z0-9_-]{1,32}")

CLONE_TIMEOUT_S = 600.0
FETCH_TIMEOUT_S = 300.0

RepoReason = Literal["no_token", "name_format", "repo_not_found", "already_added", "clone_failed"]
_MESSAGES: dict[str, str] = {
    "no_token": "Save a GitHub token first.",
    "name_format": "Use the owner/name form, like octo/hello-world.",
    "repo_not_found": "GitHub can't find that repo, or the token can't see it.",
    "already_added": "That repo is already added.",
    "clone_failed": "Couldn't copy the repo from GitHub.",
}


class RepoError(Exception):
    def __init__(self, reason: RepoReason, detail: str | None = None) -> None:
        self.reason: RepoReason = reason
        self.message = _MESSAGES[reason] + (f" {detail}" if detail else "")
        super().__init__(self.message)


@dataclass(frozen=True)
class Repo:
    id: str
    full_name: str
    default_branch: str
    private: bool
    created_at: str


@dataclass(frozen=True)
class RepoStatus:
    branch: str
    changed: list[str]
    ahead: int
    behind: int


def _row(row: sqlite3.Row) -> Repo:
    return Repo(
        id=row["id"],
        full_name=row["full_name"],
        default_branch=row["default_branch"],
        private=bool(row["private"]),
        created_at=row["created_at"],
    )


def _is_link(path: Path) -> bool:
    """A symlink or (Windows) reparse point; also when it can't be told."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _remove_tree(path: Path) -> bool:
    """Remove a clone (or a clone in progress); True once it is gone. Git marks its object
    files read-only, which Windows won't delete, so those are made writable and retried. A
    link is never walked into. What can't go now (a file held open, say) is left for
    `sweep_repo_folders`; only the error type is logged, never the path."""

    def writable_then_retry(func, name, _exc) -> None:
        try:
            os.chmod(name, stat.S_IWRITE)
            func(name)
        except OSError as exc:
            _log.warning("could not remove part of a repo folder: %s", type(exc).__name__)

    if not os.path.lexists(path):
        return True
    if _is_link(path):
        _log.warning("left in place: a repo folder that is a link")
        return False
    shutil.rmtree(path, onexc=writable_then_retry)
    return not os.path.lexists(path)


def clone_dir(data_dir: Path, repo_id: str) -> Path:
    """Where a repo's clone lives: `<data>/workspace/repos/<id>`."""
    return Path(data_dir) / "workspace" / "repos" / repo_id


def is_link(path: Path) -> bool:
    """A symlink or (Windows) reparse point, such as a junction; also when it can't be told."""
    return _is_link(path)


def remove_tree(path: Path) -> bool:
    """Remove a folder filled from a clone (a test run's copy, say) the way a clone is
    removed: read-only files too, never through a link. True once it is gone."""
    return _remove_tree(path)


def _own_dir(folder: Path) -> bool:
    """`folder` is a real directory, with no link on the way from the data folder."""
    return folder.is_dir() and not _is_link(folder)


def sweep_repo_folders(conn: sqlite3.Connection, data_dir: Path) -> int:
    """Finish what a failed add or remove left on disk (run at startup, before any add can
    be under way): every `runtime/clone-<id>` staging folder, and every
    `workspace/repos/<id>` folder without a row. Never follows a link. Logs ids at most,
    never names. Returns how many folders were removed."""
    data_dir = Path(data_dir)
    known = {r[0] for r in conn.execute("SELECT id FROM repos") if r[0]}
    candidates: list[Path] = []
    for parent, pattern in (
        (data_dir / "workspace" / "repos", _REPO_FOLDER),
        (data_dir / "runtime", _STAGING_FOLDER),
    ):
        if not _own_dir(parent.parent) or not _own_dir(parent):
            continue
        try:
            with os.scandir(parent) as found:
                names = sorted(entry.name for entry in found)
        except OSError as exc:  # never stops Hearthkeep from starting
            _log.warning("could not sweep repo folders: %s", type(exc).__name__)
            continue
        for name in names:
            if pattern.fullmatch(name) and (pattern is _STAGING_FOLDER or name not in known):
                candidates.append(parent / name)
    removed = 0
    for folder in candidates:
        if _is_link(folder) or not folder.is_dir():
            continue
        if _remove_tree(folder):
            removed += 1
    return removed


def _parse_status(text: str) -> RepoStatus:
    """`git status --porcelain=v2 --branch -z`."""
    branch, ahead, behind = "", 0, 0
    changed: list[str] = []
    items = text.split("\0")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if item.startswith("# branch.head "):
            branch = item.removeprefix("# branch.head ")
        elif item.startswith("# branch.ab "):
            a, b = item.removeprefix("# branch.ab ").split()
            ahead, behind = int(a.lstrip("+")), int(b.lstrip("-"))
        elif item.startswith("1 "):
            changed.append(item.split(" ", 8)[8])
        elif item.startswith("2 "):
            changed.append(item.split(" ", 9)[9])
            i += 1  # the original path of a rename or copy follows
        elif item.startswith("u "):
            changed.append(item.split(" ", 10)[10])
        elif item.startswith("? "):
            changed.append(item[2:])
    return RepoStatus(branch=branch, changed=changed, ahead=ahead, behind=behind)


class RepoStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        data_dir: Path,
        git: GitRunner,
        api: GitHubClient,
        secrets: SecretStore,
    ) -> None:
        self.conn = conn
        self.git = git
        self._data_dir = Path(data_dir)
        self._api = api
        self._secrets = secrets
        # Run before a repo's rows and folder go (Task 5 removes its dependency volumes).
        self.on_remove: list[Callable[[Repo], Awaitable[None]]] = []

    def list(self) -> list[Repo]:
        rows = self.conn.execute("SELECT * FROM repos ORDER BY created_at, id").fetchall()
        return [_row(r) for r in rows]

    def get(self, repo_id: str) -> Repo | None:
        row = self.conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return _row(row) if row else None

    def by_name(self, full_name: str) -> Repo | None:
        # The column is COLLATE NOCASE: GitHub names compare case-insensitively.
        row = self.conn.execute("SELECT * FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        return _row(row) if row else None

    def dir(self, repo: Repo) -> Path:
        return clone_dir(self._data_dir, repo.id)

    def token(self) -> str | None:
        try:
            return self._secrets.get(TOKEN_SECRET)
        except SecretStoreError:
            return None

    async def add(self, full_name: str) -> Repo:
        token = self.token()
        if token is None:
            raise RepoError("no_token")
        try:
            check_repo_name(full_name)
        except ValueError:
            raise RepoError("name_format") from None
        if self.by_name(full_name) is not None:
            raise RepoError("already_added")
        try:
            info = await self._api.repo(full_name)
        except GitHubError as error:
            if error.reason in ("not_found", "forbidden", "refused"):
                raise RepoError("repo_not_found") from None
            raise
        if self.by_name(info.full_name) is not None:
            raise RepoError("already_added")

        repo_id = token_hex(8)
        runtime = self._data_dir / "runtime"
        staging = runtime / f"clone-{repo_id}"
        final = self._data_dir / "workspace" / "repos" / repo_id
        try:
            runtime.mkdir(parents=True, exist_ok=True)
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                await self.git.run(
                    runtime,
                    "clone", "--quiet", "--no-recurse-submodules",
                    f"--template={self.git.no_hooks.as_posix()}",
                    "--", self.git.remote_url(info.full_name), staging.name,
                    token=token, timeout=CLONE_TIMEOUT_S,
                )  # fmt: skip
            except GitError as error:
                raise RepoError("clone_failed", error.message) from None
            os.replace(staging, final)
            repo = Repo(repo_id, info.full_name, info.default_branch, info.private, db.utcnow())
            try:
                with db.transaction(self.conn):
                    self.conn.execute(
                        "INSERT INTO repos (id, full_name, default_branch, private, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (repo.id, repo.full_name, repo.default_branch, int(repo.private),
                         repo.created_at),
                    )  # fmt: skip
            except sqlite3.IntegrityError:
                raise RepoError("already_added") from None  # added meanwhile
        except BaseException:
            await asyncio.to_thread(_remove_tree, staging)
            await asyncio.to_thread(_remove_tree, final)
            raise
        return repo

    async def remove(self, repo_id: str) -> bool:
        repo = self.get(repo_id)
        if repo is None:
            return False
        for hook in self.on_remove:
            await hook(repo)
        with db.transaction(self.conn):
            self.conn.execute("DELETE FROM repo_runs WHERE repo_id = ?", (repo.id,))
            self.conn.execute("DELETE FROM repo_deps WHERE repo_id = ?", (repo.id,))
            self.conn.execute("DELETE FROM repos WHERE id = ?", (repo.id,))
        # The row is gone either way; a folder that can't go now (a file held open) is
        # finished by the next startup's sweep_repo_folders.
        if not await asyncio.to_thread(_remove_tree, self.dir(repo)):
            _log.warning("repo %s: folder left for the startup sweep", repo.id)
        return True

    async def fetch(self, repo: Repo) -> None:
        await self.git.run(
            self.dir(repo),
            "fetch", "--quiet", "--prune", "--no-recurse-submodules",
            self.git.remote_url(repo.full_name), "+refs/heads/*:refs/remotes/origin/*",
            token=self.token(), timeout=FETCH_TIMEOUT_S,
        )  # fmt: skip

    async def status(self, repo: Repo) -> RepoStatus:
        result = await self.git.run(self.dir(repo), "status", "--porcelain=v2", "--branch", "-z")
        return _parse_status(result.out)
