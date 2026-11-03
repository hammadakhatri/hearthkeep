"""A repo's dependencies, installed once per lock file into a Docker volume.

The lock file decides the ecosystem, in this order: `uv.lock` (with `pyproject.toml`),
`requirements.txt` (and `requirements-dev.txt`), `package-lock.json` (with `package.json`).
The lock hash covers the ecosystem, the repo image's tag and each dependency file's name and
bytes, so a changed lock file or a new image names a new volume, `hk-deps-<repo id>-<hash12>`.

The install container is the repo image with the test sandbox's limits, except that it has
the default bridge network (installing needs the internet; that is full network access, like
any program on the PC, the home network included) and more processes. It is given
only copies of the dependency files (read-only, at `/src`) and the volume: never the clone,
never the GitHub token or any other secret. Python lands in `/deps/venv`, Node in
`node_modules`. Repo names never reach a log here: only repo ids.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex
from typing import Literal

from hearthkeep import db
from hearthkeep.github.paths import PathRefused, safe_path
from hearthkeep.github.repos import Repo, is_link, remove_tree
from hearthkeep.sandbox import image as sandbox_image
from hearthkeep.sandbox.runner import DATA_PATH_MESSAGE

_log = logging.getLogger(__name__)

Ecosystem = Literal["uv", "pip", "npm"]
DepsState = Literal["none", "needed", "ready", "outdated"]

INSTALL_TIMEOUT_S = 900
VOLUME_CACHE_S = 30.0
VOLUME_CHECK_TIMEOUT_S = 5.0
DOCKER_TIMEOUT_S = 30.0  # `volume ls` / `volume rm` / `kill`
KILL_GRACE_S = 5.0
MAX_DEP_FILE_BYTES = 50 * 1024 * 1024
OUTPUT_HEAD = 64 * 1024
OUTPUT_TAIL = 192 * 1024
OUTPUT_CUT = "… output cut …"
TMPFS = "/tmp:rw,nosuid,nodev,size=1073741824"
VOLUME_PREFIX = "hk-deps-"
CONTAINER_PREFIX = "hk-install-"
LABEL = "hearthkeep.install=1"
_REPO_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_VOLUME = re.compile(re.escape(VOLUME_PREFIX) + r"[A-Za-z0-9_-]{1,32}-[0-9a-f]{12}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{1,64}")
_UNSAFE_MOUNT = re.compile(r'[,"\'\n\r\x00]')
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

UV_ENV = ("UV_PROJECT_ENVIRONMENT=/deps/venv",)
PYTHON_TARGET = "/deps"
NODE_TARGET = "/src/node_modules"

TIMED_OUT = f"The install stopped after {INSTALL_TIMEOUT_S} s."
ALREADY_INSTALLED = "Dependencies are already installed for this lock file."
NO_START = "The install container couldn't start."
FILES_CHANGED = "The dependency files changed while they were being copied. Try again."
# repo_install's and repo_test's own words (the analysis tool's mention Python).
REPO_NO_DOCKER = (
    "Installing or testing a repo needs Docker, and Docker isn't running on this PC. "
    "Start Docker Desktop and try again."
)
REPO_NO_IMAGE = (
    "Installing or testing a repo needs the repo sandbox image (hearthkeep-repo), which "
    "isn't built yet. Build it once with: hearthkeep sandbox build --data <data>"
)


class DepsTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class DepsPlan:
    ecosystem: Ecosystem
    files: list[str]
    command: str
    lock_hash: str
    volume: str


# (ecosystem, files that must all be there, files used when there)
_ECOSYSTEMS: tuple[tuple[Ecosystem, tuple[str, ...], tuple[str, ...]], ...] = (
    ("uv", ("pyproject.toml", "uv.lock"), ()),
    ("pip", ("requirements.txt",), ("requirements-dev.txt",)),
    ("npm", ("package.json", "package-lock.json"), ()),
)


def _command(ecosystem: Ecosystem, files: list[str]) -> str:
    if ecosystem == "uv":
        # Only the dependency files are there, so the project itself is never built.
        return "uv sync --frozen --no-install-project"
    if ecosystem == "pip":
        reqs = " ".join(f"-r {name}" for name in files)
        return f"python -m venv /deps/venv && /deps/venv/bin/pip install {reqs}"
    return "npm ci"


def lock_hash(ecosystem: str, image_tag: str, files: Sequence[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for part in (ecosystem, image_tag):
        digest.update(part.encode("utf-8") + b"\0")
    for name, data in files:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return digest.hexdigest()


def _read(root: Path, name: str) -> bytes | None:
    """A dependency file's bytes, or None when it isn't a plain file inside the clone."""
    try:
        path = safe_path(root, name)
    except PathRefused:
        return None
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_DEP_FILE_BYTES:
            raise DepsTooLarge(f"{name} is over 50 MB.")
        return path.read_bytes()
    except OSError:
        return None


MODEL_OUTPUT_CHARS = 8000  # as much printed output as the model is shown (python's too)
MODEL_HEAD_CHARS = 2000
MODEL_TAIL_CHARS = MODEL_OUTPUT_CHARS - MODEL_HEAD_CHARS


def for_model(output: str, kept_in: str | None = None) -> str:
    """At most 8,000 characters of `output` for the model: the first 2,000 and the last
    6,000, with a line saying how much was cut (and where the whole of it is, if anywhere)."""
    if len(output) <= MODEL_OUTPUT_CHARS:
        return output
    cut = len(output) - MODEL_OUTPUT_CHARS
    note = f"… {cut} characters cut; {kept_in} …" if kept_in else f"… {cut} characters cut …"
    return f"{output[:MODEL_HEAD_CHARS]}\n{note}\n{output[-MODEL_TAIL_CHARS:]}"


def cap_output(data: bytes, cut: bool = False) -> str:
    """The first 64 KB and the last 192 KB, with `… output cut …` between when more was
    printed (`cut` says so when the middle was already dropped while reading)."""
    if not cut and len(data) <= OUTPUT_HEAD + OUTPUT_TAIL:
        return data.decode("utf-8", "replace")
    head = data[:OUTPUT_HEAD].decode("utf-8", "replace")
    tail = data[-OUTPUT_TAIL:].decode("utf-8", "replace")
    return f"{head}\n{OUTPUT_CUT}\n{tail}"


class HeadTail:
    """The first 64 KB and the last 192 KB of what was read so far. It lives outside the
    reading task, so what was read is still there if that task is cancelled."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = False

    def add(self, chunk: bytes) -> None:
        room = OUTPUT_HEAD - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        if len(self.tail) > OUTPUT_TAIL:
            del self.tail[: len(self.tail) - OUTPUT_TAIL]
            self.dropped = True

    def result(self) -> tuple[bytes, bool]:
        """(those bytes joined, whether anything between was dropped)."""
        return bytes(self.head + self.tail), self.dropped


async def read_head_tail(
    stream: asyncio.StreamReader | None, kept: HeadTail | None = None
) -> tuple[bytes, bool]:
    """Read a stream to its end keeping only the first 64 KB and the last 192 KB (in `kept`,
    when given); (those bytes joined, whether anything between was dropped)."""
    kept = kept if kept is not None else HeadTail()
    if stream is not None:
        while chunk := await stream.read(65536):
            kept.add(chunk)
    return kept.result()


def mount_source(path: Path) -> str:
    """A bind mount's source: the real path, refused when --mount's list would misread it."""
    source = str(Path(path).resolve())
    if _UNSAFE_MOUNT.search(source):
        raise ValueError("a mount path may not contain commas, quotes or line breaks")
    return source


def _spawn_flags() -> dict:
    return {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}


async def _wait_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while proc.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return proc.returncode is not None


async def run_docker(
    docker: Sequence[str], *args: str, timeout: float = DOCKER_TIMEOUT_S
) -> tuple[int | None, bytes]:
    """A short docker command: (exit code, or None when it couldn't start or hung; stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *docker,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **_spawn_flags(),
        )
    except OSError:
        return None, b""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        if isinstance(exc, TimeoutError | OSError):
            await _wait_exit(proc, 2.0)
            return None, b""
        raise
    return proc.returncode, out


async def stop_container(
    docker: Sequence[str], name: str, proc: asyncio.subprocess.Process
) -> None:
    """`docker kill` the container (killing the client alone would leave it running), then
    the `docker run` client too if it is still there after a grace period."""
    await run_docker(docker, "kill", name)
    if not await _wait_exit(proc, KILL_GRACE_S):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await _wait_exit(proc, KILL_GRACE_S)


@dataclass(frozen=True)
class Ran:
    """How one container run ended: the exit code (None when it never ended by itself), the
    output's head and tail joined, whether its middle was dropped, and whether it timed out."""

    code: int | None
    data: bytes
    dropped: bool
    timed_out: bool

    def output(self) -> str:
        return cap_output(self.data, self.dropped)


async def run_container(
    docker: Sequence[str], argv: Sequence[str], name: str, timeout: float
) -> Ran:
    """Run `argv` (a `docker run` that names its container `name`), its stdout and stderr
    read together keeping the head and tail, for at most `timeout` seconds; then (or when
    the caller is cancelled) the container is killed. OSError when docker can't start."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **_spawn_flags(),
    )
    kept = HeadTail()
    reader = asyncio.ensure_future(read_head_tail(proc.stdout, kept))
    timed_out = False
    try:
        try:
            await asyncio.wait_for(asyncio.shield(reader), timeout)
            await _wait_exit(proc, KILL_GRACE_S * 2)
        except TimeoutError:
            timed_out = True
        if timed_out or proc.returncode is None:
            await stop_container(docker, name, proc)
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(stop_container(docker, name, proc))
        reader.cancel()
        raise
    # The output normally ends as the client exits; something else may still hold the pipe,
    # so wait only briefly. Whatever was read by then is kept either way, never dropped.
    done, _ = await asyncio.wait({reader}, timeout=2.0)
    if not done:
        reader.cancel()
        # Let the pipe go now (asyncio has no public way): it would otherwise linger open.
        with contextlib.suppress(Exception):
            proc._transport.close()  # type: ignore[attr-defined]
    elif not reader.cancelled():
        reader.exception()  # read (a broken pipe ends the output), so it is never logged
    data, dropped = kept.result()
    return Ran(None if timed_out else proc.returncode, data, dropped, timed_out)


async def remove_labelled(docker: Sequence[str], label: str) -> int:
    """Kill and remove every container carrying `label` (running or not): what a stopped
    Hearthkeep left behind. How many were removed; 0 when Docker can't be asked."""
    code, out = await run_docker(docker, "ps", "-aq", "--filter", f"label={label}")
    if code != 0:
        return 0
    ids = [i for i in out.decode("ascii", "replace").split() if _CONTAINER_ID.fullmatch(i)]
    if not ids:
        return 0
    code, _ = await run_docker(docker, "rm", "-f", *ids)
    return len(ids) if code == 0 else 0


def install_jobs_dir(data_dir: Path) -> Path:
    """Where each install stages its dependency files: `<data>/runtime/deps/<job>`."""
    return Path(data_dir) / "runtime" / "deps"


def sweep_install_jobs(data_dir: Path) -> None:
    """Remove the job folders a stopped Hearthkeep left (at startup: no install is live)."""
    jobs = install_jobs_dir(data_dir)
    if not jobs.is_dir() or is_link(jobs):
        return
    for leftover in jobs.iterdir():
        remove_tree(leftover)


class DepsCache:
    def __init__(self, conn: sqlite3.Connection, docker: Sequence[str], image_tag: str) -> None:
        self.conn = conn
        self.docker = tuple(docker)
        self.image_tag = image_tag
        self._volumes: dict[str, tuple[float, bool]] = {}  # volume -> (checked at, exists)
        self._locks: dict[str, asyncio.Lock] = {}  # repo id -> one install at a time

    # -- what to install -------------------------------------------------------------------

    def plan(self, repo: Repo, root: Path) -> DepsPlan | None:
        """What `repo_install` would do for the clone at `root`; None when it has none of the
        dependency files. Raises DepsTooLarge for a dependency file over 50 MB."""
        if not _REPO_ID.fullmatch(repo.id):
            raise ValueError("not a repo id")
        for ecosystem, required, optional in _ECOSYSTEMS:
            found = [(name, _read(root, name)) for name in required]
            if any(data is None for _, data in found):
                continue
            for name in optional:
                data = _read(root, name)
                if data is not None:
                    found.append((name, data))
            files = [name for name, _ in found]
            digest = lock_hash(ecosystem, self.image_tag, found)  # type: ignore[arg-type]
            return DepsPlan(
                ecosystem=ecosystem,
                files=files,
                command=_command(ecosystem, files),
                lock_hash=digest,
                volume=f"{VOLUME_PREFIX}{repo.id}-{digest[:12]}",
            )
        return None

    # -- whether it is there ---------------------------------------------------------------

    def _row(self, repo: Repo, plan: DepsPlan) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM repo_deps WHERE repo_id = ? AND lock_hash = ? AND volume = ?",
            (repo.id, plan.lock_hash, plan.volume),
        ).fetchone()
        return row is not None

    def volume_exists(self, volume: str) -> bool:
        """`docker volume inspect`, remembered for 30 s (a quick check: at most 5 s)."""
        now = time.monotonic()
        seen = self._volumes.get(volume)
        if seen is not None and now - seen[0] < VOLUME_CACHE_S:
            return seen[1]
        try:
            proc = subprocess.run(
                [*self.docker, "volume", "inspect", volume],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=VOLUME_CHECK_TIMEOUT_S,
                check=False,
                creationflags=_NO_WINDOW,
            )
            exists = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            exists = False
        self._volumes[volume] = (time.monotonic(), exists)
        return exists

    def known_installed(self, repo: Repo, plan: DepsPlan) -> bool:
        """A row for this lock hash, and Docker said the volume exists less than 30 s ago.
        Never asks Docker (safe on the event loop): with no fresh answer, False."""
        if not self._row(repo, plan):
            return False
        seen = self._volumes.get(plan.volume)
        return seen is not None and time.monotonic() - seen[0] < VOLUME_CACHE_S and seen[1]

    def installed(self, repo: Repo, plan: DepsPlan) -> bool:
        """A row for this lock hash, and its volume still exists. Docker is asked only when
        there is a row, and then at most every 30 s."""
        return self._row(repo, plan) and self.volume_exists(plan.volume)

    async def is_installed(self, repo: Repo, plan: DepsPlan) -> bool:
        """`installed`, with Docker asked in a thread (the database stays on this one)."""
        if not self._row(repo, plan):
            return False
        return await asyncio.to_thread(self.volume_exists, plan.volume)

    def state(self, repo: Repo, root: Path) -> DepsState:
        try:
            plan = self.plan(repo, root)
        except DepsTooLarge:
            return "needed"
        if plan is None:
            return "none"
        if self.installed(repo, plan):
            return "ready"
        return self._not_ready(repo)

    async def current_state(self, repo: Repo, root: Path) -> DepsState:
        """`state` without holding up the event loop: the files are hashed and Docker asked
        in a thread first (the database stays on this thread)."""
        try:
            plan = await asyncio.to_thread(self.plan, repo, root)
        except DepsTooLarge:
            return "needed"
        if plan is None:
            return "none"
        if await self.is_installed(repo, plan):
            return "ready"
        return self._not_ready(repo)

    def _not_ready(self, repo: Repo) -> DepsState:
        older = self.conn.execute(
            "SELECT 1 FROM repo_deps WHERE repo_id = ? LIMIT 1", (repo.id,)
        ).fetchone()
        return "outdated" if older is not None else "needed"

    async def unavailable(self) -> str | None:
        """Why nothing can be installed now (Docker or the repo image missing), or None."""
        if not await asyncio.to_thread(sandbox_image.docker_ok, self.docker):
            return REPO_NO_DOCKER
        if not await asyncio.to_thread(sandbox_image.image_present, self.image_tag, self.docker):
            return REPO_NO_IMAGE
        return None

    # -- installing ------------------------------------------------------------------------

    def stage(self, plan: DepsPlan, root: Path, job_dir: Path) -> Path:
        """Copy the dependency files into `<job_dir>/src` (the only thing the container
        sees) and check the copies still hash to the plan; ValueError when they don't."""
        src = Path(job_dir) / "src"
        src.mkdir(parents=True)
        copied = []
        for name in plan.files:
            data = _read(root, name)
            if data is None:
                raise ValueError(FILES_CHANGED)
            (src / name).write_bytes(data)
            copied.append((name, data))
        if lock_hash(plan.ecosystem, self.image_tag, copied) != plan.lock_hash:
            raise ValueError(FILES_CHANGED)
        return src

    def install_argv(self, plan: DepsPlan, src_dir: Path) -> list[str]:
        """The `docker run` for one install: the test sandbox's limits, but the default
        network and 1024 processes; each dependency file mounted read-only at `/src`, the
        volume read-write. Never `--gpus`, never the Docker socket, never a secret."""
        if not plan.volume.startswith(VOLUME_PREFIX):
            raise ValueError("not a dependency volume")
        mounts: list[str] = []
        for name in plan.files:
            source = mount_source(Path(src_dir) / name)
            mounts += ["--mount", f"type=bind,source={source},target=/src/{name},readonly"]
        target = NODE_TARGET if plan.ecosystem == "npm" else PYTHON_TARGET
        mounts += ["--mount", f"type=volume,source={plan.volume},target={target}"]
        env = ["--env", "HOME=/tmp", "--env", "CI=1"]
        if plan.ecosystem == "uv":
            for pair in UV_ENV:
                env += ["--env", pair]
        return [
            *self.docker,
            "run",
            "--rm",
            "--pull", "never",
            "--log-driver", "none",
            "--read-only",
            "--user", "10001:10001",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", "4g",
            "--memory-swap", "4g",
            "--cpus", "2",
            "--pids-limit", "1024",
            "--tmpfs", TMPFS,
            *mounts,
            "--name", f"{CONTAINER_PREFIX}{token_hex(8)}",
            "--label", LABEL,
            *env,
            "--workdir", "/src",
            self.image_tag,
            "sh", "-c", plan.command,
        ]  # fmt: skip

    async def _docker(self, *args: str) -> tuple[int | None, bytes]:
        return await run_docker(self.docker, *args)

    async def _remove_volumes(self, names: Sequence[str]) -> None:
        if not names:
            return
        await self._docker("volume", "rm", "-f", *names)
        for name in names:
            self._volumes.pop(name, None)

    async def _repo_volumes(self, repo: Repo) -> list[str]:
        prefix = f"{VOLUME_PREFIX}{repo.id}-"
        code, out = await self._docker("volume", "ls", "-q", "--filter", f"name={prefix}")
        names = out.decode("ascii", "replace").split() if code == 0 else []
        pattern = re.compile(re.escape(prefix) + r"[0-9a-f]{12}")
        return [n for n in names if pattern.fullmatch(n)]

    async def install(self, repo: Repo, plan: DepsPlan, job_dir: Path) -> tuple[bool, str]:
        """Run the install on the files `stage` put in `<job_dir>/src`, at most 900 s. On
        success the row is recorded and the repo's older volumes removed; on failure the
        half-filled volume is removed. (ok, output: the head and tail kept). One install per
        repo at a time: a second one waits, then finds the first one's result."""
        lock = self._locks.setdefault(repo.id, asyncio.Lock())
        async with lock:
            if self._row(repo, plan):
                self._volumes.pop(plan.volume, None)  # ask Docker afresh, not the cache
                if await self.is_installed(repo, plan):
                    return True, ALREADY_INSTALLED
            return await self._install(repo, plan, job_dir)

    async def _install(self, repo: Repo, plan: DepsPlan, job_dir: Path) -> tuple[bool, str]:
        try:
            argv = self.install_argv(plan, Path(job_dir) / "src")
        except ValueError:
            return False, DATA_PATH_MESSAGE
        name = argv[argv.index("--name") + 1]
        try:
            ran = await run_container(self.docker, argv, name, INSTALL_TIMEOUT_S)
        except OSError:
            return False, NO_START
        except BaseException:
            with contextlib.suppress(BaseException):
                await asyncio.shield(self._remove_volumes([plan.volume]))
            raise
        output = ran.output()
        if ran.timed_out or ran.code != 0:
            await self._remove_volumes([plan.volume])
            if ran.timed_out:
                return False, f"{TIMED_OUT}\n{output}".rstrip("\n") + "\n"
            if ran.code in (125, 126, 127) and not ran.data:
                return False, NO_START
            return False, output
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO repo_deps "
                "(repo_id, lock_hash, ecosystem, volume, installed_at) VALUES (?, ?, ?, ?, ?)",
                (repo.id, plan.lock_hash, plan.ecosystem, plan.volume, db.utcnow()),
            )
            older = [
                r[0]
                for r in self.conn.execute(
                    "SELECT volume FROM repo_deps WHERE repo_id = ? AND lock_hash != ?",
                    (repo.id, plan.lock_hash),
                )
            ]
            self.conn.execute(
                "DELETE FROM repo_deps WHERE repo_id = ? AND lock_hash != ?",
                (repo.id, plan.lock_hash),
            )
        self._volumes[plan.volume] = (time.monotonic(), True)
        stale = set(older) | set(await self._repo_volumes(repo))
        stale.discard(plan.volume)
        await self._remove_volumes(sorted(v for v in stale if v and v.startswith(VOLUME_PREFIX)))
        return True, output

    # -- after a crash ---------------------------------------------------------------------

    async def sweep(self) -> None:
        """At startup (no install can be live): kill and remove install containers a stopped
        Hearthkeep left running, and remove dependency volumes that have no `repo_deps` row
        (half-filled by a cut-short install, or left when Docker was down as a repo went).
        A volume with a row is never removed. Never raises: without Docker nothing happens."""
        try:
            await remove_labelled(self.docker, LABEL)
            code, out = await self._docker(
                "volume", "ls", "-q", "--filter", f"name={VOLUME_PREFIX}"
            )
            if code != 0:
                return
            kept = {r[0] for r in self.conn.execute("SELECT volume FROM repo_deps")}
            names = out.decode("ascii", "replace").split()
            await self._remove_volumes(
                sorted(n for n in names if _VOLUME.fullmatch(n) and n not in kept)
            )
        except Exception as exc:
            _log.warning("dependency leftovers not swept (%s)", type(exc).__name__)

    # -- a repo goes ------------------------------------------------------------------------

    async def remove_for(self, repo: Repo) -> None:
        """Remove every dependency volume of `repo` (a `RepoStore.on_remove` hook). Never
        raises: without Docker the volumes stay, and the repo is still removed."""
        try:
            rows = [
                r[0]
                for r in self.conn.execute(
                    "SELECT volume FROM repo_deps WHERE repo_id = ?", (repo.id,)
                )
            ]
            names = set(rows) | set(await self._repo_volumes(repo))
            await self._remove_volumes(
                sorted(n for n in names if n and n.startswith(VOLUME_PREFIX))
            )
        except Exception as exc:
            _log.warning(
                "repo %s: dependency volumes not removed (%s)", repo.id, type(exc).__name__
            )


def remove_job(job_dir: Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)
