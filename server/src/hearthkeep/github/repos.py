"""The repos the owner added: a row each, and a clone under `<data>/workspace/repos/<id>`.

Adding checks the name, asks GitHub about the repo, clones it into `runtime/` and only then
moves the clone into place and inserts the row: a failure at any step leaves neither. The
remote URL is always passed explicitly; the token travels only in git's environment.
Repo names are the owner's: errors raised here name no repo, and nothing here logs.
"""

from __future__ import annotations

import asyncio
import os
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


def _remove_tree(path: Path) -> None:
    """Remove a clone; git marks its object files read-only, which Windows won't delete."""

    def writable_then_retry(func, name, _exc) -> None:
        try:
            os.chmod(name, stat.S_IWRITE)
            func(name)
        except OSError:
            pass

    if path.exists() or path.is_symlink():
        shutil.rmtree(path, onexc=writable_then_retry)


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
        return self._data_dir / "workspace" / "repos" / repo.id

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
        await asyncio.to_thread(_remove_tree, self.dir(repo))
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
