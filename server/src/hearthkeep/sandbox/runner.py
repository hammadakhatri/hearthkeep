"""Run one job in the sandbox: one locked-down container per run (the Global Constraints'
flags, exactly), its output tar streamed back under strict limits and unpacked by
Hearthkeep itself.

Nothing that comes back from the container is trusted. The script runs as the same user as
the entry and can reach the container's stdout, so the tar is checked member by member
(names on an allow-list, regular files only, at most `max_members`) and `status.json` is
advisory: the run time is the host's own, and a host timeout always wins.

Each run has a job folder, `<data>/runtime/sandbox/jobs/<run-id>/`:
  in/main.py, in/manifest.json   mounted read-only at /job (nothing else is ever in there)
  out.tar                        the container's stdout, as it streams in
  out/m000, out/m001, ...        the accepted members, under names Hearthkeep chose
The folder is removed when the run fails in any way (including cancellation), and
otherwise by `RawRun.cleanup()` once the caller has kept what it needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from hearthkeep.files.store import CHAT_ID
from hearthkeep.images import UPLOAD_ID
from hearthkeep.sandbox import image as sandbox_image

RunStatus = Literal["ok", "failed", "timeout", "too_much_output", "sandbox_error"]
MODES = ("script", "inspect", "pages")
RUN_ID = UPLOAD_ID
IMAGE_TAG = re.compile(r"hearthkeep-sandbox:[0-9a-f]{12}", re.ASCII)
LABEL = "hearthkeep.run=1"
NAME_PREFIX = "hk-run-"
TMPFS_BYTES = 268_435_456
STDERR_BYTES = 65_536
STATUS_BYTES = 65_536
DIAGNOSTICS_BYTES = 2048
HEADER_BYTES = 65_536  # a PAX or GNU long-name header larger than this is refused unread
STATUS_CACHE_S = 30.0
CHUNK = 65_536
KILL_TIMEOUT_S = 15.0  # for `docker kill` / `docker rm` themselves
KILL_GRACE_S = 5.0  # how long the `docker run` client may outlive `docker kill`
EXIT_GRACE_S = 10.0  # after its stdout closes, the client should exit about at once
SWEEP_TIMEOUT_S = 30.0
_IS_WINDOWS = sys.platform == "win32"
_log = logging.getLogger(__name__)

# Exactly these names, and nothing else, may come back (ASCII only: no Unicode digits).
MEMBER_NAME = re.compile(
    r"status\.json|stdout\.txt|results\.jsonl|inspect\.json"
    r"|files/[A-Za-z0-9][A-Za-z0-9._ -]{0,119}|pages/[0-9]{1,3}\.png",
    re.ASCII,
)
# The PAX keys a plain member may carry (the entry writes nothing more exotic).
# No `size`: a PAX size overrides the ustar one, so it would make Hearthkeep's header scan
# and tarfile walk different archives (the entry never needs it under the 300 MB cap).
_PAX_KEYS = frozenset({"path", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname"})
_REGULAR = (tarfile.REGTYPE, tarfile.AREGTYPE)
# A bind-mount source is one value in --mount's comma-separated list: keep it plain.
_UNSAFE_MOUNT = re.compile(r'[,"\'\n\r\x00]')

NO_DOCKER_MESSAGE = (
    "Running Python needs Docker, and Docker isn't running on this PC. "
    "Start Docker Desktop and try again."
)
NO_IMAGE_MESSAGE = (
    "Running Python needs the sandbox image, which isn't built yet. "
    "Build it once with: hearthkeep sandbox build --data <data>"
)
DATA_PATH_MESSAGE = (
    "Hearthkeep's data folder path contains a comma or quote, which Docker can't mount. "
    "Move the data folder to a simpler path."
)
# What a failed run says (the model sees these): never Docker's own words.
ERROR_START = "The sandbox couldn't start the container."
ERROR_ENTRY = "The sandbox couldn't run its entry program."
ERROR_OUTPUT = "The sandbox returned output Hearthkeep couldn't read."
ERROR_UNEXPECTED = "The sandbox stopped unexpectedly."
DOCKER_KILLED_EXIT = 137  # the container was killed: by its memory limit, unless the host did
SCRIPT_KILLED_EXIT = -9  # the entry's report for a script ended by SIGKILL (outside a timeout)
# Tar header types whose data is header text for the next member.
_LONG_HEADER_TYPES = frozenset(b"xgLK")


@dataclass(frozen=True)
class SandboxLimits:
    memory_gb: int = 4
    cpus: float = 2.0
    timeout_s: float = 120.0
    script_timeout_s: float = 110.0
    stdout_bytes: int = 262_144
    tar_bytes: int = 300 * 1024 * 1024
    max_members: int = 200
    pids: int = 256


DEFAULT_LIMITS = SandboxLimits()


@dataclass
class SandboxStatus:
    docker: bool
    image: bool
    tag: str


class SandboxUnavailable(Exception):
    """Docker or the sandbox image is missing: nothing ran (and nothing runs on the host)."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class RawRun:
    status: RunStatus
    exit_code: int | None
    stdout: str
    stdout_truncated: bool
    duration_ms: int
    members: dict[str, Path]  # accepted member name -> its copy inside the job folder
    error: str | None
    # Docker's stderr (first 2 KB, the data and home folders replaced) or why the output was
    # refused: for the server log only, never saved with the run or shown to the model.
    diagnostics: str | None = field(default=None, repr=False)
    job_dir: Path | None = field(default=None, repr=False)

    def cleanup(self) -> None:
        """Remove the run's job folder (and with it every member copy). Safe to repeat."""
        if self.job_dir is not None:
            _remove_tree(self.job_dir)
        self.members = {}


class _BadOutput(Exception):
    """The container's output is not acceptable; the message says why (never the content)."""


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _spawn_flags() -> dict:
    # No console window flashes up for each docker command on Windows.
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _mount_source(path: Path) -> str:
    source = str(Path(path).resolve())
    if _UNSAFE_MOUNT.search(source):
        raise ValueError("a sandbox mount path may not contain commas, quotes or line breaks")
    return source


def _windows_names(path: str) -> set[str]:
    """The 8.3 short and the long spelling of an existing path (Windows; else nothing)."""
    names: set[str] = set()
    if not _IS_WINDOWS:
        return names
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for convert in (kernel32.GetShortPathNameW, kernel32.GetLongPathNameW):
            buf = ctypes.create_unicode_buffer(32_768)
            if 0 < convert(path, buf, len(buf)) < len(buf):
                names.add(buf.value)
    except Exception:  # no ctypes, no such call, or the path is gone: just fewer spellings
        pass
    return names


def _spellings(path: Path) -> set[str]:
    raw = str(path)
    found = {raw, str(path.resolve()), os.path.realpath(raw)}
    for name in list(found):
        found |= _windows_names(name)
    return found


def _path_pattern(spelling: str) -> str | None:
    """A pattern for one spelling of a folder: any run of / or \\ is a separator, and the
    drive may also show as Docker Desktop's `/c` (with any prefix before it)."""
    parts = [part for part in re.split(r"[\\/]+", spelling) if part]
    if not parts or len("".join(parts)) < 3:
        return None
    sep = r"[\\/]+"
    head, rest = parts[0], parts[1:]
    if re.fullmatch(r"[A-Za-z]:", head):
        start = f"(?:{re.escape(head)}|{sep}{re.escape(head[0])})"
    else:
        start = sep + re.escape(head) if spelling[:1] in "\\/" else re.escape(head)
    # Ends where the last name ends (a longer sibling like `data2` is not this folder).
    return start + "".join(sep + re.escape(part) for part in rest) + r"(?![\w~-])"


def redact(text: str, data_dir: Path) -> str:
    """`text` with the data folder shown as <data> and the home folder as <home>, however a
    message spells them: either separator (doubled or mixed), any case, the 8.3 short name,
    the real path behind a link, or Docker Desktop's `/c/Users/...` form."""
    pairs = []
    for base, label in ((Path(data_dir), "<data>"), (Path.home(), "<home>")):
        for spelling in _spellings(base):
            pattern = _path_pattern(spelling)
            if pattern is not None:
                pairs.append((pattern, label))
    # Longest first, so the data folder wins over the home folder that holds it.
    for pattern, label in sorted(set(pairs), key=lambda pair: -len(pair[0])):
        text = re.sub(pattern, label, text, flags=re.IGNORECASE)
    return text


def _one_line(text: str) -> str:
    """Control characters (line breaks included) as spaces, so a log entry stays one line."""
    return "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in text)


def _format_number(value: float) -> str:
    return f"{value:g}"


def _check_name(member: tarfile.TarInfo) -> str:
    name = member.name
    if not isinstance(name, str) or not name:
        raise _BadOutput("a member has no name")
    if "\\" in name or ".." in name or name.startswith("/"):
        raise _BadOutput("a member name is not allowed")
    if any(unicodedata.category(ch) == "Cc" for ch in name):
        raise _BadOutput("a member name has a control character")
    if not MEMBER_NAME.fullmatch(name):
        raise _BadOutput("a member name is not allowed")
    return name


def _check_member(member: tarfile.TarInfo) -> str:
    if set(member.pax_headers) - _PAX_KEYS:
        raise _BadOutput("a member carries unexpected headers")
    name = _check_name(member)
    if member.type not in _REGULAR or member.issparse():
        raise _BadOutput("a member is not a regular file")
    return name


def _header_size(field_bytes: bytes) -> int:
    """A tar header's size field: octal text, or GNU base-256 (high bit set)."""
    if field_bytes[0] & 0x80:
        if field_bytes[0] & 0x40:
            raise _BadOutput("a member has a negative size")
        return int.from_bytes(bytes([field_bytes[0] & 0x3F]) + field_bytes[1:], "big")
    text = field_bytes.split(b"\0", 1)[0].strip(b" ")
    try:
        return int(text or b"0", 8)
    except ValueError:
        raise _BadOutput("a member has a bad size") from None


def _pax_keys(body: bytes) -> list[str]:
    """The keys of a PAX extended header's records (`<len> <key>=<value>\\n`), strictly."""
    keys = []
    pos = 0
    while pos < len(body) and body[pos] != 0:
        space = body.find(b" ", pos, pos + 20)
        digits = body[pos:space] if space > pos else b""
        if not digits.isdigit():
            raise _BadOutput("a PAX header is malformed")
        length = int(digits)
        record = body[pos : pos + length]
        equals = record.find(b"=")
        if length <= len(digits) + 1 or len(record) != length or equals < 0:
            raise _BadOutput("a PAX header is malformed")
        if not record.endswith(b"\n"):
            raise _BadOutput("a PAX header is malformed")
        try:
            keys.append(record[len(digits) + 1 : equals].decode("utf-8"))
        except UnicodeDecodeError:
            raise _BadOutput("a PAX header is malformed") from None
        pos += length
    return keys


def _scan_headers(tar_path: Path) -> None:
    """Walk the raw headers before tarfile does. tarfile reads a PAX or GNU long-name header
    into memory whole, so a huge one is refused here, unread. Global PAX headers are refused,
    and each extended one is read (it is small by then) and held to the member key list:
    without a PAX `size`, this walk and tarfile's step over exactly the same bytes."""
    with open(tar_path, "rb") as raw:
        while True:
            block = raw.read(512)
            if len(block) < 512 or not block.strip(b"\0"):
                return  # the end (or a cut stream, which tarfile then refuses)
            size = _header_size(block[124:136])
            kind = block[156]
            if kind == ord("g"):
                raise _BadOutput("a global PAX header is not allowed")
            if kind in _LONG_HEADER_TYPES and size > HEADER_BYTES:
                raise _BadOutput("a member header is too large")
            if kind == ord("x"):
                body = raw.read(size)
                if len(body) != size:
                    raise _BadOutput("a PAX header is cut short")
                if set(_pax_keys(body)) - _PAX_KEYS:
                    raise _BadOutput("a member carries unexpected headers")
                raw.seek(-size % 512, os.SEEK_CUR)
                continue
            raw.seek((size + 511) // 512 * 512, os.SEEK_CUR)


def _unpack(tar_path: Path, out_dir: Path, max_members: int, stop: threading.Event) -> dict:
    """Copy the accepted members of `tar_path` into `out_dir` (as m000, m001, ...); raise
    _BadOutput at the first member that breaks a rule. Never `tarfile.extract`."""
    _scan_headers(tar_path)
    out_dir.mkdir()
    members: dict[str, Path] = {}
    with (
        open(tar_path, "rb") as raw,
        tarfile.open(fileobj=raw, mode="r|", encoding="utf-8", errors="strict") as tar,
    ):
        for index, member in enumerate(tar):
            if index >= max_members:
                raise _BadOutput(f"more than {max_members} members")
            name = _check_member(member)
            if index == 0 and name != "status.json":
                raise _BadOutput("status.json is not the first member")
            if name in members:
                raise _BadOutput("a member name repeats")
            source = tar.extractfile(member)
            if source is None:
                raise _BadOutput("a member is not a regular file")
            dest = out_dir / f"m{index:03d}"
            copied = 0
            with open(dest, "xb") as target:
                while chunk := source.read(CHUNK):
                    if stop.is_set():
                        raise _BadOutput("stopped")
                    copied += len(chunk)
                    target.write(chunk)
            if copied != member.size:
                raise _BadOutput("a member is shorter than its header says")
            members[name] = dest
        end = tar.offset
    # The archive must end with its end-of-archive blocks and nothing but zeros after them:
    # tarfile quietly stops at a broken header, which would hide a cut or padded stream.
    with open(tar_path, "rb") as raw:
        raw.seek(end)
        rest = raw.read()
    if len(rest) < 1024 or rest.strip(b"\0"):
        raise _BadOutput("the archive does not end cleanly")
    if "status.json" not in members:
        raise _BadOutput("status.json is missing")
    return members


def _read_status(path: Path) -> dict:
    if os.stat(path).st_size > STATUS_BYTES:
        raise _BadOutput("status.json is too large")
    data = path.read_bytes()
    try:
        status = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _BadOutput("status.json is not valid JSON") from None
    if not isinstance(status, dict):
        raise _BadOutput("status.json is not an object")
    code = status.get("exit_code")
    if code is not None and (not isinstance(code, int) or isinstance(code, bool)):
        raise _BadOutput("status.json has a bad exit_code")
    for key in ("timed_out", "stdout_truncated"):
        if not isinstance(status.get(key, False), bool):
            raise _BadOutput(f"status.json has a bad {key}")
    return status


def _read_stdout(path: Path | None, limit: int) -> tuple[str, bool]:
    """The printed output, cut so its UTF-8 form is at most `limit` bytes."""
    if path is None:
        return "", False
    with open(path, "rb") as f:
        data = f.read(limit + 1)
    cut = len(data) > limit
    text = data[:limit].decode("utf-8", "replace")
    encoded = text.encode("utf-8")
    if len(encoded) > limit:  # replacement characters are wider than the bytes they replace
        text = encoded[:limit].decode("utf-8", "ignore")
        cut = True
    return text, cut


async def _read_capped(stream: asyncio.StreamReader | None, cap: int) -> bytes:
    kept = bytearray()
    if stream is None:
        return b""
    while chunk := await stream.read(CHUNK):
        room = cap - len(kept)
        if room > 0:
            kept += chunk[:room]
    return bytes(kept)


async def _drain(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    with contextlib.suppress(Exception):
        while await stream.read(CHUNK):
            pass


async def _wait_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    """Wait up to `timeout` for the process itself to exit (not for its pipes to close: a
    stray child could hold those open)."""
    deadline = time.monotonic() + timeout
    while proc.returncode is None and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return proc.returncode is not None


async def _finish(task: asyncio.Task, timeout: float = 2.0) -> bytes:
    """The task's bytes, or b"" if it fails or takes too long. A cancellation of the caller
    still propagates (asyncio.wait never swallows it)."""
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        return b""
    if task.cancelled() or task.exception() is not None:
        return b""
    return task.result()


class SandboxRunner:
    def __init__(
        self,
        data_dir: Path,
        *,
        image_tag: str,
        docker: Sequence[str] = ("docker",),
        limits: SandboxLimits = DEFAULT_LIMITS,
    ) -> None:
        if not IMAGE_TAG.fullmatch(image_tag):
            raise ValueError("not a sandbox image tag")
        self.data_dir = Path(data_dir)
        self.image_tag = image_tag
        self.docker = tuple(docker)
        self.limits = limits
        self._jobs = self.data_dir / "runtime" / "sandbox" / "jobs"
        self._chats = self.data_dir / "workspace" / "chats"
        self._status: tuple[float, SandboxStatus] | None = None
        self._status_lock = asyncio.Lock()
        self._active: set[str] = set()
        self._runs_lock = asyncio.Lock()  # a run registers, or a sweep runs: never both

    # The container

    def argv(self, run_id: str, job_dir: Path, files_dir: Path, mode: str) -> list[str]:
        """The full `docker run` command for one run. Never `--init` (the entry must be pid 1
        to kill whatever the script leaves behind), never `--gpus`, never the Docker socket."""
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("not a run id")
        if mode not in MODES:
            raise ValueError("not a sandbox mode")
        limits = self.limits
        memory = f"{int(limits.memory_gb)}g"
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
            "--memory", memory,
            "--memory-swap", memory,
            "--cpus", _format_number(limits.cpus),
            "--pids-limit", str(int(limits.pids)),
            "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={TMPFS_BYTES}",
            "--mount", f"type=tmpfs,destination=/out,tmpfs-size={TMPFS_BYTES}",
            "--mount", f"type=bind,source={_mount_source(files_dir)},target=/data,readonly",
            "--mount", f"type=bind,source={_mount_source(job_dir)},target=/job,readonly",
            "--name", f"{NAME_PREFIX}{run_id}",
            "--label", LABEL,
            "--env", "HOME=/tmp",
            self.image_tag,
            mode,
        ]  # fmt: skip

    def _files_dir(self, files_dir: Path) -> Path:
        """Only a chat's own `files` folder is ever mounted. It must exist already (the chat
        creates it through `ChatFileStore.files_dir`): the runner never makes folders there."""
        folder = Path(files_dir)
        if folder.name != "files" or not CHAT_ID.fullmatch(folder.parent.name):
            raise ValueError("not a chat's files folder")
        chats = self._chats.resolve()
        resolved = folder.resolve()
        if resolved.parent.parent != chats or resolved.name != "files":
            raise ValueError("not a chat's files folder")
        for path in (folder.parent, folder):
            if path.is_symlink():
                raise ValueError("a chat's files folder may not be a link")
        if not resolved.is_dir():
            raise FileNotFoundError("the chat's files folder does not exist")
        return resolved

    # Docker itself

    async def _docker(self, *args: str, timeout: float) -> tuple[int | None, bytes]:
        """Run a short docker command; (exit code or None if it failed or hung, stdout)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.docker,
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

    async def status(self) -> SandboxStatus:
        """Docker running, image present: cached for 30 s."""
        async with self._status_lock:
            now = time.monotonic()
            if self._status is not None and now - self._status[0] < STATUS_CACHE_S:
                return self._status[1]
            docker = await asyncio.to_thread(sandbox_image.docker_ok, self.docker)
            present = docker and await asyncio.to_thread(
                sandbox_image.image_present, self.image_tag, self.docker
            )
            result = SandboxStatus(docker=docker, image=bool(present), tag=self.image_tag)
            self._status = (time.monotonic(), result)
            return result

    async def sweep(self) -> int:
        """Remove containers a stopped Hearthkeep left behind, and their job folders. Meant
        for startup; refused (RuntimeError) while any run of this runner is live. Returns how
        many containers were removed."""
        async with self._runs_lock:
            if self._active:
                raise RuntimeError("the sandbox sweep can't run while a run is live")
            return await self._sweep()

    async def _sweep(self) -> int:
        for leftover in self._jobs.iterdir() if self._jobs.is_dir() else ():
            if RUN_ID.fullmatch(leftover.name) and not leftover.is_symlink():
                _remove_tree(leftover)
        code, out = await self._docker(
            "ps", "-aq", "--filter", f"label={LABEL}", timeout=SWEEP_TIMEOUT_S
        )
        if code != 0:
            return 0
        ids = [i for i in out.decode("ascii", "replace").split() if re.fullmatch(r"[0-9a-f]+", i)]
        if not ids:
            return 0
        code, _ = await self._docker("rm", "-f", *ids, timeout=SWEEP_TIMEOUT_S)
        return len(ids) if code == 0 else 0

    async def _stop(self, name: str, proc: asyncio.subprocess.Process) -> None:
        """`docker kill` the container (killing the client alone would leave it running),
        then the `docker run` client too if it is still there after a grace period."""
        drain = asyncio.ensure_future(_drain(proc.stdout))  # so the client never blocks
        try:
            await self._docker("kill", name, timeout=KILL_TIMEOUT_S)
            if not await _wait_exit(proc, KILL_GRACE_S):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await _wait_exit(proc, KILL_GRACE_S)
        finally:
            drain.cancel()

    # A run

    async def run(
        self,
        run_id: str,
        *,
        mode: Literal["script", "inspect", "pages"],
        files_dir: Path,
        manifest: dict,
        script: str | None = None,
        timeout_s: float | None = None,
    ) -> RawRun:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("not a run id")
        if mode not in MODES:
            raise ValueError("not a sandbox mode")
        state = await self.status()
        if not state.docker:
            raise SandboxUnavailable(NO_DOCKER_MESSAGE)
        if not state.image:
            raise SandboxUnavailable(NO_IMAGE_MESSAGE)
        for folder in (self.data_dir, self._chats, self._jobs):
            # A link or junction may lead somewhere Docker can't mount: check where it ends.
            if _UNSAFE_MOUNT.search(str(folder.resolve())):
                raise SandboxUnavailable(DATA_PATH_MESSAGE)
        files = self._files_dir(files_dir)
        limits = self.limits
        timeout = limits.timeout_s if timeout_s is None else float(timeout_s)
        script_timeout = self._script_timeout(timeout)
        name = f"{NAME_PREFIX}{run_id}"

        job = self._jobs / run_id
        async with self._runs_lock:  # never while a sweep is clearing job folders
            self._jobs.mkdir(parents=True, exist_ok=True)
            job.mkdir()  # a run id is used once: an existing folder is an error
            self._active.add(run_id)
        killed = False  # whether the host sent `docker kill`, on any path
        proc: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task | None = None
        handed_over = False
        try:
            job_in = job / "in"
            job_in.mkdir()
            (job_in / "main.py").write_text(script or "", encoding="utf-8", newline="\n")
            body = {**manifest, "script_timeout_s": script_timeout}
            (job_in / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
            argv = self.argv(run_id, job_in, files, mode)
            tar_path = job / "out.tar"

            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_spawn_flags(),
            )
            stderr_task = asyncio.ensure_future(_read_capped(proc.stderr, STDERR_BYTES))
            outcome = await self._collect(proc, tar_path, timeout)
            if outcome != "done" or not await _wait_exit(proc, EXIT_GRACE_S):
                killed = True
                await self._stop(name, proc)
            duration_ms = int((time.monotonic() - started) * 1000)
            stderr = await _finish(stderr_task)

            raw = await self._result(
                outcome, proc.returncode, killed, stderr, duration_ms, job, tar_path, timeout
            )
            if raw.diagnostics:
                _log.warning("sandbox %s %s: %s", name, raw.status, _one_line(raw.diagnostics))
            handed_over = bool(raw.members)
            return raw
        except BaseException:
            if proc is not None and proc.returncode is None:
                killed = True
                with contextlib.suppress(BaseException):
                    await asyncio.shield(self._stop(name, proc))
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
            raise
        finally:
            self._active.discard(run_id)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            if not handed_over:
                _remove_tree(job)

    async def _collect(
        self, proc: asyncio.subprocess.Process, tar_path: Path, timeout: float
    ) -> Literal["done", "timeout", "over"]:
        """Stream the container's stdout into `tar_path`, stopping at `tar_bytes`."""
        over = False

        async def pump() -> None:
            nonlocal over
            total = 0
            assert proc.stdout is not None
            with open(tar_path, "wb") as target:
                while chunk := await proc.stdout.read(CHUNK):
                    total += len(chunk)
                    if total > self.limits.tar_bytes:
                        over = True
                        return
                    target.write(chunk)

        try:
            await asyncio.wait_for(pump(), timeout)
        except TimeoutError:
            return "timeout"
        return "over" if over else "done"

    async def _result(
        self,
        outcome: str,
        returncode: int | None,
        killed: bool,
        stderr: bytes,
        duration_ms: int,
        job: Path,
        tar_path: Path,
        timeout: float,
    ) -> RawRun:
        diagnostics = self._diagnostics(stderr.decode("utf-8", "replace").strip())

        def fail(status: RunStatus, error: str, why: str | None = None) -> RawRun:
            note = "; ".join(x for x in (why, diagnostics) if x) or None
            if note is not None:
                note = self._diagnostics(note)
            return RawRun(status, None, "", False, duration_ms, {}, error, note, job)

        if outcome == "timeout":
            return fail("timeout", f"The run took longer than {timeout:g} seconds and was stopped.")
        if outcome == "over":
            megabytes = self.limits.tar_bytes / (1024 * 1024)
            return fail(
                "too_much_output",
                f"The run produced more than {megabytes:g} MB of output and was stopped.",
            )
        if killed:  # the host had to kill it although its time wasn't up
            return fail("sandbox_error", ERROR_UNEXPECTED, f"killed by the host ({returncode})")
        if returncode in (125, 126, 127):
            self._status = None  # Docker or the image may have gone: look again next time
            message = ERROR_START if returncode == 125 else ERROR_ENTRY
            return fail("sandbox_error", message, f"docker exited {returncode}")
        if returncode == DOCKER_KILLED_EXIT:
            return fail("failed", self._memory_message())
        if returncode != 0:
            return fail("sandbox_error", ERROR_UNEXPECTED, f"docker exited {returncode}")

        stop = threading.Event()
        work = asyncio.ensure_future(
            asyncio.to_thread(_unpack, tar_path, job / "out", self.limits.max_members, stop)
        )
        try:
            members = await asyncio.shield(work)
            status = _read_status(members["status.json"])
            stdout, cut = _read_stdout(members.get("stdout.txt"), self.limits.stdout_bytes)
        except asyncio.CancelledError:
            stop.set()
            with contextlib.suppress(BaseException):
                await work
            raise
        except (_BadOutput, tarfile.TarError, EOFError, UnicodeError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, _BadOutput) else "the archive could not be read"
            return fail("sandbox_error", ERROR_OUTPUT, f"output refused: {reason}")
        with contextlib.suppress(OSError):
            tar_path.unlink()
        _remove_tree(job / "in")

        exit_code = status.get("exit_code")
        truncated = bool(status.get("stdout_truncated")) or cut
        if status.get("timed_out"):
            run_status: RunStatus = "timeout"
            error: str | None = (
                f"The script took longer than {self._script_timeout(timeout)} seconds "
                "and was stopped."
            )
        elif exit_code == SCRIPT_KILLED_EXIT:  # in the container only the OOM killer sends that
            run_status, error = "failed", self._memory_message()
        elif exit_code != 0:
            run_status, error = "failed", None
        else:
            run_status, error = "ok", None
        return RawRun(
            run_status, exit_code, stdout, truncated, duration_ms, members, error, None, job
        )

    def _memory_message(self) -> str:
        memory = _format_number(self.limits.memory_gb)
        return f"The script used more than {memory} GB of memory and was stopped."

    def _diagnostics(self, text: str) -> str:
        """Redacted first, then cut to 2 KB, so no path is left half-replaced."""
        cut = redact(text, self.data_dir).encode("utf-8")[:DIAGNOSTICS_BYTES]
        return cut.decode("utf-8", "ignore")

    def _script_timeout(self, timeout: float) -> int:
        """The script's own limit sits inside the container's, by the same margin."""
        margin = max(self.limits.timeout_s - self.limits.script_timeout_s, 0.0)
        return max(1, int(min(self.limits.script_timeout_s, timeout - margin)))
