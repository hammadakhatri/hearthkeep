"""`repo_test`'s runs: a repo's tests in the repo sandbox, on a throwaway copy of its tree.

Each run copies the clone's working tree, without `.git` and `node_modules` and never through
a link, into `<data>/runtime/repo-jobs/<run-id>/tree`, the only read-write mount. The
container has no network, the repo's dependency volume read-only (Python at `/deps`, Node at
`/repo/node_modules`) and the test sandbox's limits; it is stopped after 600 s. Nothing from
`.git`, the data folder, a secret or the GitHub token ever reaches it, so a test that plants a
hook or rewrites a file changes only the copy, which is removed after every run.

The output is kept as its first 64 KB and last 192 KB, and each run is saved in `repo_runs`.
Only repo ids reach a log here, never names.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Literal

from hearthkeep import db
from hearthkeep.github.deps import (
    OUTPUT_HEAD,
    OUTPUT_TAIL,
    PYTHON_TARGET,
    TMPFS,
    UV_ENV,
    VOLUME_PREFIX,
    DepsCache,
    DepsPlan,
    DepsTooLarge,
    mount_source,
    run_container,
    run_docker,
)
from hearthkeep.github.repos import Repo, clone_dir, is_link, remove_tree
from hearthkeep.sandbox.runner import DATA_PATH_MESSAGE

_log = logging.getLogger(__name__)

RunStatus = Literal["passed", "failed", "timed_out", "error"]

TEST_TIMEOUT_S = 600
HEAD_BYTES = OUTPUT_HEAD
TAIL_BYTES = OUTPUT_TAIL
MAX_TREE_BYTES = 500 * 1024**2
MAX_TREE_FILES = 50_000
CHUNK = 1024 * 1024
NAME_PREFIX = "hk-repo-"
LABEL = "hearthkeep.repo=1"
NODE_TARGET = "/repo/node_modules"
SKIPPED = ("node_modules",)
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and other Windows links
_CONTAINER_ID = re.compile(r"[0-9a-f]{1,64}")

TREE_TOO_LARGE = (
    "The repo is too large to test: over 500 MB or 50,000 files "
    "(not counting .git and node_modules)."
)
TREE_HAS_LINK = (
    "The repo has a symbolic link or junction in its files, so it isn't copied for a test run."
)
TREE_UNREADABLE = "The repo's files couldn't be copied for a test run."
DEPS_MISSING = "Dependencies aren't installed for the current lock file. Call repo_install first."
NO_START = "The test container couldn't start."


class TreeRefused(ValueError):
    """The working tree can't be copied for a run; the message says why (for the model)."""


@dataclass(frozen=True)
class RepoRun:
    id: str
    repo_id: str
    chat_id: str | None
    command: str
    status: RunStatus
    exit_code: int | None
    duration_ms: int
    output: str
    created_at: str


# -- the copy ------------------------------------------------------------------------------


def _skipped(name: str) -> bool:
    """`.git` in any spelling Windows treats as it (any case, trailing dots or spaces), and
    `node_modules` (the Node dependency volume's place)."""
    return name.rstrip(". ").lower() == ".git" or name.lower() in SKIPPED


def _entry_is_link(entry: os.DirEntry) -> bool:
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return entry.is_symlink() or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _scan(src: Path) -> tuple[list[str], list[str]]:
    """(folders, files) under `src` as relative POSIX paths, `.git` and `node_modules` left
    out. Refuses a link anywhere, and a tree over the caps, before anything is copied."""
    if is_link(src) or not src.is_dir():
        raise TreeRefused(TREE_HAS_LINK if is_link(src) else TREE_UNREADABLE)
    folders: list[str] = []
    files: list[str] = []
    total = 0
    pending = [""]
    while pending:
        rel = pending.pop()
        with os.scandir(src / rel if rel else src) as found:
            entries = sorted(found, key=lambda e: e.name)
        for entry in entries:
            if _skipped(entry.name):
                continue
            if _entry_is_link(entry):
                raise TreeRefused(TREE_HAS_LINK)
            child = f"{rel}/{entry.name}" if rel else entry.name
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                folders.append(child)
                pending.append(child)
            elif stat.S_ISREG(st.st_mode):
                files.append(child)
                total += st.st_size
                if len(files) > MAX_TREE_FILES or total > MAX_TREE_BYTES:
                    raise TreeRefused(TREE_TOO_LARGE)
            # sockets, pipes and devices are left out
    return folders, files


def _open_up(path: Path, executable: bool = False) -> None:
    """On Linux the container's user (10001) must be able to write the copy; Docker Desktop
    on Windows and macOS maps bind mounts for it already."""
    if os.name == "posix":
        os.chmod(path, 0o777 if executable or path.is_dir() else 0o666)


def copy_tree(src: Path, dest: Path) -> None:
    """Copy the working tree at `src` into a new folder `dest`: never `.git` or
    `node_modules`, never through a link (one anywhere refuses the copy), at most 500 MB in
    50,000 files (TreeRefused, checked before anything is copied and again while copying)."""
    src, dest = Path(src), Path(dest)
    folders, files = _scan(src)
    dest.mkdir(parents=True)
    _open_up(dest)
    for rel in folders:
        (dest / rel).mkdir()
        _open_up(dest / rel)
    total = 0
    for rel in files:
        source = src / rel
        if is_link(source):  # replaced by a link since the scan
            raise TreeRefused(TREE_HAS_LINK)
        target = dest / rel
        with open(source, "rb") as reader, open(target, "xb") as writer:
            while chunk := reader.read(CHUNK):
                total += len(chunk)
                if total > MAX_TREE_BYTES:  # grew since the scan
                    raise TreeRefused(TREE_TOO_LARGE)
                writer.write(chunk)
        _open_up(target, executable=bool(os.stat(source).st_mode & stat.S_IXUSR))


# -- runs ----------------------------------------------------------------------------------


def _row(row: sqlite3.Row, with_output: bool = True) -> RepoRun:
    return RepoRun(
        id=row["id"],
        repo_id=row["repo_id"],
        chat_id=row["chat_id"],
        command=row["command"],
        status=row["status"],
        exit_code=row["exit_code"],
        duration_ms=row["duration_ms"] or 0,
        output=(row["output"] or "") if with_output else "",
        created_at=row["created_at"],
    )


class RepoTestRunner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        data_dir: Path,
        docker: Sequence[str],
        image_tag: str,
        deps: DepsCache,
    ) -> None:
        self.conn = conn
        self.data_dir = Path(data_dir)
        self.docker = tuple(docker)
        self.image_tag = image_tag
        self.deps = deps
        self._jobs = self.data_dir / "runtime" / "repo-jobs"

    # -- the container ---------------------------------------------------------------------

    def argv(self, run_id: str, tree: Path, plan: DepsPlan | None, command: str) -> list[str]:
        """The `docker run` for one test run: the copy read-write at `/repo`, the dependency
        volume read-only, no network. Never `--gpus`, the Docker socket, `.git` or a secret."""
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("not a run id")
        mounts = ["--mount", f"type=bind,source={mount_source(tree)},target=/repo"]
        env = ["--env", "HOME=/tmp", "--env", "CI=1"]
        if plan is not None:
            if not plan.volume.startswith(VOLUME_PREFIX):
                raise ValueError("not a dependency volume")
            target = NODE_TARGET if plan.ecosystem == "npm" else PYTHON_TARGET
            mounts += ["--mount", f"type=volume,source={plan.volume},target={target},readonly"]
            if plan.ecosystem == "uv":
                for pair in UV_ENV:
                    env += ["--env", pair]
        return [
            *self.docker,
            "run",
            "--rm",
            "--pull", "never",
            "--log-driver", "none",
            "--network", "none",
            "--read-only",
            "--user", "10001:10001",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", "4g",
            "--memory-swap", "4g",
            "--cpus", "2",
            "--pids-limit", "512",
            "--tmpfs", TMPFS,
            *mounts,
            "--name", f"{NAME_PREFIX}{run_id}",
            "--label", LABEL,
            *env,
            "--workdir", "/repo",
            self.image_tag,
            "sh", "-c", command,
        ]  # fmt: skip

    # -- a run -----------------------------------------------------------------------------

    async def run(self, repo: Repo, chat_id: str, command: str) -> RepoRun:
        """Copy the tree, run `command` on the copy (at most 600 s), save and return the run.
        Something that stops it running (a refused copy, the dependencies gone) is saved as
        an error run; the job folder is removed whatever happens."""
        run_id = token_hex(16)
        job = self._jobs / run_id
        started = time.monotonic()
        try:
            status, code, output = await self._run(run_id, job, repo, command)
        finally:
            await asyncio.to_thread(remove_tree, job)
        duration_ms = int((time.monotonic() - started) * 1000)
        run = RepoRun(
            id=run_id,
            repo_id=repo.id,
            chat_id=chat_id,
            command=command,
            status=status,
            exit_code=code,
            duration_ms=duration_ms,
            output=output,
            created_at=db.utcnow(),
        )
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO repo_runs (id, repo_id, chat_id, command, status, exit_code, "
                "duration_ms, output, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.id, run.repo_id, run.chat_id, run.command, run.status, run.exit_code,
                 run.duration_ms, run.output, run.created_at),
            )  # fmt: skip
        _log.info("repo %s: test run %s %s", repo.id, run_id, status)
        return run

    async def _run(
        self, run_id: str, job: Path, repo: Repo, command: str
    ) -> tuple[RunStatus, int | None, str]:
        clone = clone_dir(self.data_dir, repo.id)
        try:
            plan = await asyncio.to_thread(self.deps.plan, repo, clone)
        except DepsTooLarge:
            return "error", None, DEPS_MISSING
        if plan is not None and not await self.deps.is_installed(repo, plan):
            # Never mount a volume that isn't there: Docker would make an empty one.
            return "error", None, DEPS_MISSING
        tree = job / "tree"
        try:
            await asyncio.to_thread(self._prepare, clone, job, tree, plan)
        except TreeRefused as refused:
            return "error", None, str(refused)
        except OSError:
            return "error", None, TREE_UNREADABLE
        try:
            argv = self.argv(run_id, tree, plan, command)
        except ValueError:
            return "error", None, DATA_PATH_MESSAGE
        name = f"{NAME_PREFIX}{run_id}"
        try:
            ran = await run_container(self.docker, argv, name, TEST_TIMEOUT_S)
        except OSError:
            return "error", None, NO_START
        if ran.timed_out:
            return "timed_out", None, ran.output()
        if ran.code == 125 or ran.code is None:
            # Docker itself failed: its own words may name host paths, so they aren't kept.
            return "error", ran.code, NO_START
        return ("passed" if ran.code == 0 else "failed"), ran.code, ran.output()

    def _prepare(self, clone: Path, job: Path, tree: Path, plan: DepsPlan | None) -> None:
        self._jobs.mkdir(parents=True, exist_ok=True)
        job.mkdir()
        copy_tree(clone, tree)
        if plan is not None and plan.ecosystem == "npm":
            (tree / "node_modules").mkdir()  # where the volume is mounted
            _open_up(tree / "node_modules")

    # -- saved runs ------------------------------------------------------------------------

    def runs(self, repo_id: str, limit: int = 20) -> list[RepoRun]:
        """The repo's latest runs, newest first, without their output."""
        rows = self.conn.execute(
            "SELECT id, repo_id, chat_id, command, status, exit_code, duration_ms, created_at, "
            "NULL AS output FROM repo_runs WHERE repo_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (repo_id, limit),
        ).fetchall()
        return [_row(r, with_output=False) for r in rows]

    def get(self, run_id: str) -> RepoRun | None:
        row = self.conn.execute("SELECT * FROM repo_runs WHERE id = ?", (run_id,)).fetchone()
        return _row(row) if row else None

    # -- after a crash ---------------------------------------------------------------------

    def sweep_jobs(self) -> None:
        """Remove job folders a stopped Hearthkeep left (at startup: no run can be live)."""
        if not self._jobs.is_dir() or is_link(self._jobs):
            return
        for leftover in self._jobs.iterdir():
            remove_tree(leftover)

    async def sweep_containers(self) -> int:
        """Remove test containers a stopped Hearthkeep left; how many were removed."""
        code, out = await run_docker(self.docker, "ps", "-aq", "--filter", f"label={LABEL}")
        if code != 0:
            return 0
        ids = [i for i in out.decode("ascii", "replace").split() if _CONTAINER_ID.fullmatch(i)]
        if not ids:
            return 0
        code, _ = await run_docker(self.docker, "rm", "-f", *ids)
        return len(ids) if code == 0 else 0
