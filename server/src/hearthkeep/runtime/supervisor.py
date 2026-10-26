"""Start, health-check and stop the local model servers (llama-server and ComfyUI).

Every server is bound to 127.0.0.1. Output is appended to `<data>/logs/<name>.log`.
Children never outlive Hearthkeep: on Windows each one runs in a kill-on-close Job Object; on
POSIX each one leads its own session and is stopped as a process group.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shlex
import signal
import socket
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from hearthkeep.models.cards import LAUNCH_PLACEHOLDER_RE, ModelCard
from hearthkeep.models.runtime import RuntimeConfig
from hearthkeep.secrets import SecretStore

_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    from hearthkeep.runtime.winjob import KillOnCloseJob

LOOPBACK = "127.0.0.1"
API_KEY_FILE = Path("runtime") / "llama-api-key"
COMFYUI_PATHS_FILE = Path("runtime") / "comfyui-paths.yaml"
COMFYUI_SUBFOLDERS = ("diffusion_models", "text_encoders", "vae", "loras")
_POLL_INTERVAL = 0.2
_TIMEOUT_STOP = 5.0
# Windows answers a connection to a closed loopback port only after ~2 s of SYN retries, so the
# connect phase is capped well below the overall 2 s probe timeout.
_HEALTH_TIMEOUT = httpx.Timeout(2.0, connect=0.5)
_PORT_PROBE_TIMEOUT = 0.5


log = logging.getLogger(__name__)


class ProcessError(Exception):
    """A model server could not be started or configured. `user_message` is safe to show."""

    def __init__(self, user_message: str, detail: str | None = None) -> None:
        super().__init__(detail or user_message)
        self.user_message = user_message


async def _cancel(task: asyncio.Future) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _finish(future: asyncio.Future) -> None:
    """Await `future` to the end even if we are cancelled meanwhile; re-raise that afterwards."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _port_answers(port: int) -> bool:
    """True if anything accepts a TCP connection on 127.0.0.1:port. Sends no data."""
    try:
        with socket.create_connection((LOOPBACK, port), timeout=_PORT_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


class ManagedProcess:
    """One child server process that Hearthkeep starts, health-checks and stops."""

    def __init__(
        self,
        name: str,
        argv: list[str],
        health_url: str,
        log_path: Path,
        *,
        ready_timeout: float = 120.0,
        cwd: Path | None = None,
        env: dict | None = None,
        data_dir: Path | None = None,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.health_url = health_url
        self.port = urlparse(health_url).port
        self.log_path = Path(log_path)
        self.ready_timeout = ready_timeout
        self.cwd = cwd
        self.env = env
        self.data_dir = data_dir
        self.prepare = prepare  # runs before every spawn: re-writes the key file or the YAML
        self._proc: asyncio.subprocess.Process | None = None
        self._exit_task: asyncio.Task | None = None  # logs the exit line, resolves on exit
        self._job = None  # Windows KillOnCloseJob for the live child
        self._lock = asyncio.Lock()
        self._stop_requested = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _log_hint(self) -> str:
        """The log location for messages: relative to the data folder, never absolute."""
        if self.data_dir is not None:
            with suppress(ValueError):
                rel = self.log_path.relative_to(self.data_dir)
                return f"<data>/{rel.as_posix()}"
        return f"the log file {self.log_path.name}"

    def _start_failed(self, detail: str | None = None) -> ProcessError:
        return ProcessError(f"{self.name} couldn't start. See {self._log_hint()}.", detail)

    def _append_log(self, line: str) -> None:
        with suppress(OSError), open(self.log_path, "ab") as log:
            log.write(f"{line}\n".encode())

    async def is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=_HEALTH_TIMEOUT) as client:
                response = await client.get(self.health_url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def start(self) -> None:
        """Spawn the server (unless our own child is already healthy) and wait until it answers.

        A port that something else is listening on is refused, never adopted. A concurrent
        `stop()` interrupts the wait. A failed or cancelled `start()` stops and reaps the child
        only if this call spawned it; an earlier child it merely waited on is left alone.
        """
        async with self._lock:
            if self._stop_requested.is_set():
                raise ProcessError(f"{self.name} is being stopped.")
            spawned = False
            if self.running:
                if await self.is_healthy():
                    return
            else:
                await self._reap_exited()
                if self.port is not None and await asyncio.to_thread(_port_answers, self.port):
                    raise ProcessError(
                        f"Port {self.port} is already in use by another program. Change "
                        "llama_port (or comfyui_port) in <data>/models.toml, or stop that program."
                    )
                await self._spawn()
                spawned = True
            try:
                await self._wait_ready()
            except BaseException:
                if spawned:
                    await self._stop(_TIMEOUT_STOP)
                raise

    async def stop(self, timeout: float = 10.0) -> None:
        """Stop the child (interrupting a start that is still waiting) and reap it."""
        self._stop_requested.set()
        try:
            async with self._lock:
                await self._stop(timeout)
        finally:
            self._stop_requested.clear()

    async def _reap_exited(self) -> None:
        """Forget an earlier child that exited on its own (its exit line is already logged)."""
        if self._proc is not None:
            await self._stop(_TIMEOUT_STOP)

    async def _spawn(self) -> None:
        if self.prepare is not None:
            try:
                await asyncio.to_thread(self.prepare)
            except OSError as exc:
                raise ProcessError(
                    f"{self.name} couldn't start: Hearthkeep could not write its runtime files.",
                    f"prepare failed: {exc.strerror or type(exc).__name__}",
                ) from exc
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        flags: dict = {}
        if _IS_WINDOWS:
            flags["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            flags["start_new_session"] = True  # own process group, stopped with killpg
        # The child gets its own copy of the handle; ours is closed as soon as it has started.
        with open(self.log_path, "ab") as log:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            log.write(f"--- start {stamp} ---\nargv: {shlex.join(self.argv)}\n".encode())
            log.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=self.cwd,
                    env=self.env,
                    **flags,
                )
            except OSError as exc:
                log.write(f"--- could not start: {exc.strerror or exc} ---\n".encode())
                raise self._start_failed(f"spawn failed: {type(exc).__name__}") from exc
        self._proc = proc
        self._exit_task = asyncio.ensure_future(self._log_exit(proc))
        if _IS_WINDOWS:
            job = None
            try:
                job = KillOnCloseJob()
                job.assign(proc.pid)
            except OSError as exc:
                if job is not None:
                    job.close()  # the child is not in it; _stop below ends the child itself
                await self._stop(_TIMEOUT_STOP)
                raise ProcessError(
                    f"{self.name} couldn't start: Hearthkeep could not supervise it.",
                    f"job object failed: {exc}",
                ) from exc
            self._job = job

    async def _log_exit(self, proc: asyncio.subprocess.Process) -> None:
        code = await proc.wait()
        self._append_log(f"--- exited with code {code} ---")

    async def _wait_ready(self) -> None:
        """Poll health until 200. Each probe races the child's exit and a stop request, so a
        crash or a stop ends the wait at once."""
        assert self._exit_task is not None
        exited = self._exit_task
        stopping = asyncio.ensure_future(self._stop_requested.wait())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_timeout
        try:
            while True:
                probe = asyncio.ensure_future(self.is_healthy())
                await asyncio.wait(
                    {probe, exited, stopping},
                    timeout=max(deadline - loop.time(), 0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                healthy = probe.done() and probe.result()
                await _cancel(probe)
                if exited.done():
                    code = self._proc.returncode if self._proc else None
                    raise self._start_failed(f"exited with code {code} during startup")
                if stopping.done():
                    raise ProcessError(f"{self.name} was stopped before it was ready.")
                if healthy:
                    return
                if loop.time() >= deadline:
                    raise ProcessError(
                        f"{self.name} didn't become ready within {self.ready_timeout:g} seconds. "
                        f"See {self._log_hint()}."
                    )
                await asyncio.wait({exited, stopping}, timeout=_POLL_INTERVAL)
        finally:
            await _cancel(stopping)

    async def _stop(self, timeout: float) -> None:
        """End the child and always reap it, even if we are cancelled while doing so.

        Windows: TerminateJobObject at once (the child and anything it started); `timeout` is
        unused, and CTRL_BREAK is not sent because it cannot reach a CREATE_NO_WINDOW child.
        POSIX: SIGTERM to the process group, then SIGKILL after `timeout`.
        """
        proc = self._proc
        if proc is None:
            return
        exit_task = self._exit_task
        assert exit_task is not None
        try:
            if not _IS_WINDOWS and not exit_task.done():
                with suppress(OSError):
                    os.killpg(proc.pid, signal.SIGTERM)
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(exit_task), timeout)
        finally:
            if not exit_task.done():
                self._kill(proc)
            try:
                await _finish(exit_task)
            finally:
                if self._job is not None:
                    self._job.close()
                    self._job = None
                self._proc = None
                self._exit_task = None

    def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the child now. If TerminateJobObject fails, closing the job (kill-on-close) and
        killing the child directly follow, so `_stop` never waits on a child left running."""
        if self._job is not None:
            try:
                self._job.terminate()
                return
            except OSError:
                with suppress(OSError):
                    self._job.close()
        with suppress(OSError):  # includes ProcessLookupError: it already exited
            if _IS_WINDOWS:
                proc.kill()  # TerminateProcess
            else:
                os.killpg(proc.pid, signal.SIGKILL)


# --- private files --------------------------------------------------------------------------


def _write_atomic(path: Path, text: str, before_replace: Callable[[Path], None] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:  # mkstemp: mode 0600
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if before_replace is not None:
            before_replace(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_ACL_FAILED = "Hearthkeep couldn't make its runtime folder private, so the chat key wasn't written."


def _run_icacls(argv: list[str]) -> None:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessError(_ACL_FAILED, f"icacls failed: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise ProcessError(_ACL_FAILED, f"icacls exited with code {result.returncode}")


def _icacls_exe() -> str:
    """icacls from the Windows folder, never looked up on PATH; no SystemRoot → refuse."""
    root = os.environ.get("SystemRoot")
    if not root:
        raise ProcessError(_ACL_FAILED, "SystemRoot is not set")
    return str(Path(root) / "System32" / "icacls.exe")


def _owner_only(path: Path, *, folder: bool) -> None:
    """Restrict `path` to the current user. POSIX: 0700/0600. Windows: an explicit DACL with
    inheritance removed and full control for the current user only."""
    if not _IS_WINDOWS:
        os.chmod(path, 0o700 if folder else 0o600)
        return
    domain, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    if not user:
        raise ProcessError(_ACL_FAILED, "USERNAME is not set")
    account = f"{domain}\\{user}" if domain else user
    rights = "(OI)(CI)F" if folder else "F"
    _run_icacls([_icacls_exe(), str(path), "/inheritance:r", "/grant:r", f"{account}:{rights}"])


def write_private_file(path: Path, text: str) -> None:
    """Atomically write `text` readable by the current user only.

    The folder is restricted first; if that fails nothing is written (`ProcessError`). The
    file's own permissions are set before it is moved into place, so it never exists with
    wider access. Windows: owner-only DACL via icacls; POSIX: 0700 folder, 0600 file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _owner_only(path.parent, folder=True)
    _write_atomic(path, text, before_replace=lambda tmp: _owner_only(tmp, folder=False))


def write_api_key_file(data_dir: Path, value: str) -> Path:
    """Write the chat API key to `<data>/runtime/llama-api-key` for llama-server to read."""
    path = Path(data_dir) / API_KEY_FILE
    write_private_file(path, value)
    return path


def ensure_chat_api_key(store: SecretStore, name: str = "chat_api_key") -> str:
    """The stored chat API key; a random one is generated and stored if there is none."""
    value = store.get(name)
    if not value:
        value = secrets.token_urlsafe(32)
        store.put(name, value)
    return value


# --- builders ------------------------------------------------------------------------------


def _require(value: Path | None, key: str) -> Path:
    if value is None:
        raise ProcessError(f"Set {key} in <data>/models.toml first.")
    return Path(value).absolute()


def _option_values(args: list[str], flag: str) -> list[str | None]:
    """Values given for `flag` as `flag value` or `flag=value`; None if the value is missing."""
    values: list[str | None] = []
    for i, arg in enumerate(args):
        if arg == flag:
            values.append(args[i + 1] if i + 1 < len(args) else None)
        elif arg.startswith(flag + "="):
            values.append(arg[len(flag) + 1 :])
    return values


def _secure_llama_args(card: ModelCard, args: list[str], port: str, key_file: str) -> list[str]:
    """Loopback host, Hearthkeep's API key file and port: refused if changed, added if absent."""
    if _option_values(args, "--api-key"):
        raise ProcessError(
            f"The {card.name} model card sets --api-key. Remove it: Hearthkeep supplies the key."
        )
    required = (
        ("--host", LOOPBACK, f"--host must be {LOOPBACK}: model servers stay on this computer"),
        ("--api-key-file", key_file, "--api-key-file must be {api_key_file}"),
        ("--port", port, "--port must be {port}, which comes from models.toml"),
    )
    args = list(args)
    for flag, value, rule in required:
        given = _option_values(args, flag)
        if any(v != value for v in given):
            raise ProcessError(f"The {card.name} model card is not allowed: {rule}.")
        if not given:
            args += [flag, value]
    return args


def _llama_env() -> dict[str, str]:
    """Our environment minus llama.cpp's own settings, which could add a host or a key."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("LLAMA_ARG_") and k.upper() != "LLAMA_API_KEY"
    }


WINDOWS_MAX_PATH = 260
_PATH_TOO_LONG = (
    "Hearthkeep's data folder path is too long for llama-server on Windows (it must stay "
    "under 260 characters). Move the data folder to a shorter path."
)


def _on_windows() -> bool:
    return os.name == "nt"


def _check_windows_path_lengths(paths: list[str]) -> None:
    """llama-server can't open a path of 260 characters or more on Windows. Fail before
    spawning, with a message that never contains the path (only its length is logged)."""
    longest = max(len(p) for p in paths)
    if _on_windows() and longest >= WINDOWS_MAX_PATH:
        log.warning("a llama-server path is %d characters long (limit %d)", longest, 259)
        raise ProcessError(_PATH_TOO_LONG, f"path of {longest} characters")


def build_llama_process(
    card: ModelCard,
    rt: RuntimeConfig,
    data_dir: Path,
    api_key_file: Path,
    *,
    api_key: Callable[[], str],
) -> ManagedProcess:
    """llama-server for a chat card. Before every start the key file is re-written (owner-only)
    from `api_key()`, e.g. `lambda: ensure_chat_api_key(store)`."""
    if card.kind != "chat":
        raise ProcessError(f"{card.name} is not a chat model.")
    exe = _require(rt.llama_server_exe, "llama_server_exe")
    folder = rt.model_dirs.get(card.id)
    if folder is None:
        raise ProcessError(
            f'Set model_dirs."{card.id}" in <data>/models.toml to the folder with the '
            f"{card.name} files."
        )
    folder = Path(folder).absolute()
    key_file = Path(api_key_file).absolute()
    values = {"port": str(rt.llama_port), "api_key_file": str(key_file)}
    values.update({key: str(folder / name) for key, name in card.files.items()})
    _check_windows_path_lengths(
        [values["api_key_file"], *(str(folder / name) for name in card.files.values())]
    )

    def fill(match) -> str:
        if match.group(1) not in values:
            raise ProcessError(
                f"The {card.name} model card uses {match.group(0)} but has no file for it."
            )
        return values[match.group(1)]

    args = [LAUNCH_PLACEHOLDER_RE.sub(fill, arg) for arg in card.launch_args]
    args = _secure_llama_args(card, args, values["port"], values["api_key_file"])

    def prepare() -> None:
        write_private_file(key_file, api_key())

    data_dir = Path(data_dir)
    return ManagedProcess(
        "llama-server",
        [str(exe), *args],
        f"http://{LOOPBACK}:{rt.llama_port}/health",
        data_dir / "logs" / "llama-server.log",
        env=_llama_env(),
        data_dir=data_dir,
        prepare=prepare,
    )


def _yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def comfyui_paths_yaml(model_root: Path) -> str:
    lines = ["hearthkeep:", f"  base_path: {_yaml_quote(str(model_root))}"]
    lines += [f"  {sub}: {sub}/" for sub in COMFYUI_SUBFOLDERS]
    return "\n".join(lines) + "\n"


def build_comfyui_process(rt: RuntimeConfig, data_dir: Path) -> ManagedProcess:
    """ComfyUI on 127.0.0.1, reading image models from `comfyui_model_root` only. The paths
    YAML is (re-)written on every start."""
    python = _require(rt.comfyui_python, "comfyui_python")
    main = _require(rt.comfyui_main, "comfyui_main")
    root = _require(rt.comfyui_model_root, "comfyui_model_root")
    data_dir = Path(data_dir)
    paths_file = data_dir / COMFYUI_PATHS_FILE
    argv = [
        str(python), "-s", str(main),
        "--listen", LOOPBACK, "--port", str(rt.comfyui_port), "--disable-auto-launch",
        "--extra-model-paths-config", str(paths_file),
    ]  # fmt: skip
    return ManagedProcess(
        "ComfyUI",
        argv,
        f"http://{LOOPBACK}:{rt.comfyui_port}/system_stats",
        data_dir / "logs" / "comfyui.log",
        cwd=main.parent,
        data_dir=data_dir,
        prepare=lambda: _write_atomic(paths_file, comfyui_paths_yaml(root)),
    )
