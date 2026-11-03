"""Host git, hardened for every call.

Each command runs without a shell, with a minimal environment (no inherited `GIT_*`, proxy or
home settings), an empty global config, no system config, no hooks, no fsmonitor, no
credential helper, no prompts, only the one transport protocol allowed and no submodules.
The token, when a command needs it, goes into the environment as an extra HTTP header for
github.com, never into argv, the remote URL or `.git/config`; every result and error has it
(and its Basic-auth base64 form) replaced by `[redacted]`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hearthkeep.github.api import TOKEN_RE

MIN_GIT = (2, 31)
REDACTED = "[redacted]"
VERSION_TIMEOUT_S = 10.0
KILL_WAIT_S = 5.0
MAX_ERROR_CHARS = 2000
EXTRAHEADER_KEY = "http.https://github.com/.extraheader"

_IS_WINDOWS = sys.platform == "win32"
_AUTH_HEADER = re.compile(r"(authorization:\s*basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE)
_VERSION = re.compile(r"git version (\d+)\.(\d+)")


def github_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def _basic(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def redact(text: str, token: str | None) -> str:
    """`text` with the token, its Basic-auth form and any auth header value replaced."""
    if token:
        text = text.replace(token, REDACTED).replace(_basic(token), REDACTED)
    text = TOKEN_RE.sub(REDACTED, text)
    return _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, text)


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str
    err: str


class GitError(Exception):
    """A git command failed or could not run; `message` is already redacted."""

    def __init__(self, message: str, code: int | None = None) -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def _spawn_flags() -> dict:
    # No console window per git call on Windows; elsewhere its own process group, so a
    # timeout can kill git and everything it started.
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if _IS_WINDOWS:
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_spawn_flags(),
            )  # fmt: skip
            await asyncio.wait_for(killer.wait(), KILL_WAIT_S)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), KILL_WAIT_S)


class GitRunner:
    """Runs host git. `protocol` and `remote_url` exist so tests can use a local bare repo
    over `file`; in Hearthkeep they are always https and github.com."""

    def __init__(
        self,
        data_dir: Path,
        git: str = "git",
        remote_url: Callable[[str], str] = github_url,
        protocol: str = "https",
    ) -> None:
        self._data_dir = Path(data_dir)
        self._git = git
        self._remote_url = remote_url
        self._protocol = protocol
        self._available: bool | None = None

    @property
    def home(self) -> Path:
        return self._data_dir / "runtime" / "git-home"

    @property
    def no_hooks(self) -> Path:
        """An empty folder: the hooks path, and the template a clone starts from."""
        return self.home / "no-hooks"

    def remote_url(self, full_name: str) -> str:
        return self._remote_url(full_name)

    def _executable(self) -> str | None:
        # An absolute path, so Windows never picks up a git.exe from the clone being worked on.
        return shutil.which(self._git)

    def _prepare(self) -> None:
        self.no_hooks.mkdir(parents=True, exist_ok=True)
        empty = self.home / "empty.gitconfig"
        if not empty.exists():
            empty.write_bytes(b"")

    def available(self) -> bool:
        """Whether git ≥ MIN_GIT is installed (checked once)."""
        if self._available is None:
            self._available = self._check_version()
        return self._available

    def _check_version(self) -> bool:
        git = self._executable()
        if git is None:
            return False
        try:
            self._prepare()
            result = subprocess.run(
                [git, "--version"],
                env=self.env(None),
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_S,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        match = _VERSION.search(result.stdout)
        return match is not None and (int(match[1]), int(match[2])) >= MIN_GIT

    def argv(self, *args: str) -> list[str]:
        settings = (
            f"core.hooksPath={self.no_hooks.as_posix()}",
            "core.fsmonitor=false",
            "core.autocrlf=false",
            "core.symlinks=false",
            "core.longpaths=true",
            "protocol.allow=never",
            f"protocol.{self._protocol}.allow=always",
            "submodule.recurse=false",
            "credential.helper=",
            "http.followRedirects=false",
        )
        argv = [self._executable() or self._git]
        for setting in settings:
            argv += ["-c", setting]
        return argv + list(args)

    def env(self, token: str | None, index_file: Path | None = None) -> dict[str, str]:
        home = str(self.home)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": home,
            "USERPROFILE": home,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.home / "empty.gitconfig"),
            "GIT_ASKPASS": "",
        }
        if _IS_WINDOWS:
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
        if index_file is not None:
            env["GIT_INDEX_FILE"] = str(index_file)  # a throwaway index, never the clone's
        if token:
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = EXTRAHEADER_KEY
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {_basic(token)}"
        return env

    async def run(
        self,
        cwd: Path,
        *args: str,
        token: str | None = None,
        timeout: float = 60.0,
        check: bool = True,
        index_file: Path | None = None,
    ) -> GitResult:
        what = args[0] if args else "git"
        if self._executable() is None:
            raise GitError("Git isn't installed on this computer.")
        self._prepare()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(*args),
                cwd=str(cwd),
                env=self.env(token, index_file),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_spawn_flags(),
            )
        except OSError:
            raise GitError("Couldn't start git.") from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            await _kill_tree(proc)
            raise GitError(f"git {what} took longer than {timeout:g} s and was stopped.") from None
        finally:
            # Also on cancellation: git and anything it started never outlive the call.
            if proc.returncode is None:
                await _kill_tree(proc)
        result = GitResult(
            code=proc.returncode if proc.returncode is not None else -1,
            out=redact(out.decode("utf-8", "replace"), token),
            err=redact(err.decode("utf-8", "replace"), token),
        )
        if check and result.code != 0:
            detail = result.err.strip()[-MAX_ERROR_CHARS:] or "no error output"
            raise GitError(f"git {what} failed (exit {result.code}): {detail}", result.code)
        return result
