"""Start, health-check and stop the local model servers (llama-server and ComfyUI).

Every server is bound to 127.0.0.1. Output is appended to `<data>/logs/<name>.log`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shlex
import signal
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from functools import partial
from pathlib import Path

import httpx

from hearthkeep.models.cards import LAUNCH_PLACEHOLDER_RE, ModelCard
from hearthkeep.models.runtime import RuntimeConfig
from hearthkeep.secrets import SecretStore

LOOPBACK = "127.0.0.1"
API_KEY_FILE = Path("runtime") / "llama-api-key"
COMFYUI_PATHS_FILE = Path("runtime") / "comfyui-paths.yaml"
COMFYUI_SUBFOLDERS = ("diffusion_models", "text_encoders", "vae", "loras")
_POLL_INTERVAL = 0.2
# Windows: CTRL_BREAK_EVENT only reaches a child that shares our console, and a child started
# with CREATE_NO_WINDOW has its own hidden console, so the grace period for it is kept short.
_CTRL_BREAK_GRACE = 1.0
_TIMEOUT_STOP = 5.0
# Windows answers a connection to a closed loopback port only after ~2 s of SYN retries, so the
# connect phase is capped well below the overall 2 s probe timeout.
_HEALTH_TIMEOUT = httpx.Timeout(2.0, connect=0.5)


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


async def _exited_within(proc: asyncio.subprocess.Process, seconds: float) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), seconds)
    except TimeoutError:
        return False
    return True


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
        self.log_path = Path(log_path)
        self.ready_timeout = ready_timeout
        self.cwd = cwd
        self.env = env
        self.data_dir = data_dir
        self.prepare = prepare  # runs before every spawn, e.g. to re-write the API key file
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

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

    async def is_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=_HEALTH_TIMEOUT) as client:
                response = await client.get(self.health_url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def start(self) -> None:
        """Spawn the server unless it is already healthy, then wait until it answers."""
        async with self._lock:
            if await self.is_healthy():
                return
            if not self.running:
                await self._spawn()
            await self._wait_ready()

    async def stop(self, timeout: float = 10.0) -> None:
        async with self._lock:
            await self._stop(timeout)

    async def _spawn(self) -> None:
        if self._proc is not None:  # an earlier child that exited on its own: reap it first
            await self._proc.wait()
            self._proc = None
        if self.prepare is not None:
            try:
                self.prepare()
            except OSError as exc:
                raise ProcessError(
                    f"{self.name} couldn't start: Hearthkeep could not write its runtime files.",
                    f"prepare failed: {exc.strerror or type(exc).__name__}",
                ) from exc
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        flags: dict = {}
        if os.name == "nt":
            flags["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            flags["start_new_session"] = True
        # The child gets its own copy of the handle; ours is closed as soon as it has started.
        with open(self.log_path, "ab") as log:
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            log.write(f"--- start {stamp} ---\nargv: {shlex.join(self.argv)}\n".encode())
            log.flush()
            try:
                self._proc = await asyncio.create_subprocess_exec(
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

    async def _wait_ready(self) -> None:
        """Poll health until 200. Each probe races the child's exit, so a crash fails fast
        (a refused connection on Windows can take the full probe timeout)."""
        proc = self._proc
        assert proc is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_timeout
        exited = asyncio.ensure_future(proc.wait())
        try:
            while True:
                probe = asyncio.ensure_future(self.is_healthy())
                remaining = max(deadline - loop.time(), 0)
                await asyncio.wait(
                    {probe, exited}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                healthy = probe.done() and probe.result()
                await _cancel(probe)
                if exited.done():
                    self._proc = None
                    raise self._start_failed(f"exited with code {proc.returncode} during startup")
                if healthy:
                    return
                if loop.time() >= deadline:
                    await self._stop(_TIMEOUT_STOP)
                    raise ProcessError(
                        f"{self.name} didn't become ready within {self.ready_timeout:g} seconds. "
                        f"See {self._log_hint()}."
                    )
                await asyncio.wait({exited}, timeout=_POLL_INTERVAL)
        finally:
            await _cancel(exited)

    async def _stop(self, timeout: float) -> None:
        """Ask the child to exit, then terminate, then kill; always reap it."""
        proc = self._proc
        if proc is None:
            return
        if os.name == "nt":
            ctrl_break = partial(proc.send_signal, signal.CTRL_BREAK_EVENT)  # process group
            steps = [
                (ctrl_break, min(_CTRL_BREAK_GRACE, timeout / 2)),
                (proc.terminate, timeout / 2),
            ]
        else:
            steps = [(proc.terminate, timeout)]  # SIGTERM
        try:
            for action, grace in steps:
                if proc.returncode is not None:
                    break
                with suppress(OSError):  # includes ProcessLookupError: it already exited
                    action()
                if await _exited_within(proc, grace):
                    break
            else:
                with suppress(ProcessLookupError):
                    proc.kill()  # SIGKILL / TerminateProcess
        except BaseException:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
            raise
        await proc.wait()
        self._proc = None


# --- builders ------------------------------------------------------------------------------


def _require(value: Path | None, key: str) -> Path:
    if value is None:
        raise ProcessError(f"Set {key} in <data>/models.toml first.")
    return Path(value).absolute()


def write_private_file(path: Path, text: str) -> None:
    """Atomically write `text`; owner-only (0600) on POSIX.

    On Windows no ACL work is done: the file inherits the data folder's permissions, which
    live in the owner's profile.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:  # mkstemp: mode 0600
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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


def build_llama_process(
    card: ModelCard,
    rt: RuntimeConfig,
    data_dir: Path,
    api_key_file: Path,
    *,
    api_key: Callable[[], str] | None = None,
) -> ManagedProcess:
    """llama-server for a chat card. With `api_key`, the key file is re-written on every start."""
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

    def fill(match) -> str:
        if match.group(1) not in values:
            raise ProcessError(
                f"The {card.name} model card uses {match.group(0)} but has no file for it."
            )
        return values[match.group(1)]

    args = [LAUNCH_PLACEHOLDER_RE.sub(fill, arg) for arg in card.launch_args]
    args = _secure_llama_args(card, args, values["port"], values["api_key_file"])

    prepare = None
    if api_key is not None:

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


def build_comfyui_process(rt: RuntimeConfig, data_dir: Path) -> ManagedProcess:
    """ComfyUI on 127.0.0.1, reading image models from `comfyui_model_root` only."""
    python = _require(rt.comfyui_python, "comfyui_python")
    main = _require(rt.comfyui_main, "comfyui_main")
    root = _require(rt.comfyui_model_root, "comfyui_model_root")
    data_dir = Path(data_dir)
    paths_file = data_dir / COMFYUI_PATHS_FILE
    lines = ["hearthkeep:", f"  base_path: {_yaml_quote(str(root))}"]
    lines += [f"  {sub}: {sub}/" for sub in COMFYUI_SUBFOLDERS]
    write_private_file(paths_file, "\n".join(lines) + "\n")
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
    )
