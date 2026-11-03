"""`hearthkeep launch` keeps the server running in the background, and `hearthkeep open` opens
it in the browser, starting the launcher first when nothing answers.

The launcher runs `serve` as a child process and reads only its exit code (see
`lifecycle`): restart starts a new child, stop and a configuration error end the launcher, and
anything else is a crash, started again after a pause unless it keeps crashing. A lock file
makes it single-instance per data folder. On Windows every child is put in a kill-on-close job
held by the launcher, so ending the launcher (Task Manager, ending the start-at-login task,
signing out) ends the server and the model servers it started, never leaving an orphan that
holds the port. The launcher runs only on Windows (`UnsupportedHost` elsewhere), so there is
no job on other systems. Its own log holds times, events and exit codes,
never a path or an argument."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from hearthkeep import startup
from hearthkeep.lifecycle import EXIT_RESTART, EXIT_STOPPED, EXIT_USAGE, SUPERVISED_ENV
from hearthkeep.procutil import NO_WINDOW

if TYPE_CHECKING:
    from hearthkeep.config import Settings
    from hearthkeep.host import Host

LAUNCHER_LOG = Path("logs") / "launcher.log"
LOCK_FILE = Path("runtime") / "launcher.lock"
CRASH_LIMIT = 3
CRASH_WINDOW_S = 600.0
CRASH_PAUSE_S = 5.0
HEALTH_TIMEOUT_S = 1.0
START_WAIT_S = 60.0
HEALTH_POLL_S = 0.5


def serve_argv(data_dir: Path) -> list[str]:
    """The child the launcher runs (under pythonw, `sys.executable` is pythonw: no window)."""
    return [
        sys.executable, "-m", "hearthkeep", "serve", "--data", str(data_dir),
        "--log-file", str(startup.log_path(data_dir)),
    ]  # fmt: skip


def launcher_argv(data_dir: Path) -> list[str]:
    return [str(startup.pythonw_path()), "-m", "hearthkeep", "launch", "--data", str(data_dir)]


def acquire_lock(path: Path) -> object | None:
    """An exclusive, non-blocking lock on `path`: the open file that holds it (close it to
    release), or None when another launcher holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")  # noqa: SIM115 (held open as the lock)
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def new_job() -> object | None:
    """A kill-on-close job for the launcher's children on Windows; None elsewhere, or when
    Windows refuses one (the launcher still runs, as before)."""
    if sys.platform != "win32":
        return None
    from hearthkeep.runtime.winjob import KillOnCloseJob

    try:
        return KillOnCloseJob()
    except OSError:
        return None


def _note(log: Path, event: str) -> None:
    """One line in the launcher log: UTC time and the event. Never fails the launcher."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with contextlib.suppress(OSError):
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {event}\n")


def run_launcher(
    data_dir: Path,
    *,
    popen=subprocess.Popen,
    sleep=time.sleep,
    clock=time.monotonic,
    lock=acquire_lock,
    make_job=None,
) -> int:
    """Run `serve` until it stops: 0 when it stopped (or another launcher already runs),
    EXIT_USAGE on a configuration error, 1 after CRASH_LIMIT crashes within CRASH_WINDOW_S."""
    data_dir = Path(data_dir)
    held = lock(data_dir / LOCK_FILE)
    if held is None:
        return 0
    log = data_dir / LAUNCHER_LOG
    env = {**os.environ, SUPERVISED_ENV: "1"}
    crashes: list[float] = []
    job = None
    try:
        job = (make_job or new_job)()
        if job is None:
            _note(log, "no_job")
        while True:
            _note(log, "start")
            try:
                child = popen(serve_argv(data_dir), env=env, creationflags=NO_WINDOW)
            except OSError:
                code = None
            else:
                if job is not None:
                    try:
                        job.assign(child.pid)
                    except OSError:
                        _note(log, "job_assign_failed")
                code = child.wait()
            if code == EXIT_RESTART:
                _note(log, f"exit {code} restart")
                continue
            if code in (EXIT_STOPPED, EXIT_USAGE):
                _note(log, f"exit {code} {'stop' if code == EXIT_STOPPED else 'config_error'}")
                return code
            _note(log, "start_failed crash" if code is None else f"exit {code} crash")
            now = clock()
            crashes = [t for t in crashes if now - t < CRASH_WINDOW_S]
            crashes.append(now)
            if len(crashes) >= CRASH_LIMIT:
                _note(log, f"give_up after {len(crashes)} crashes")
                return 1
            sleep(CRASH_PAUSE_S)
    finally:
        if job is not None:
            job.close()  # kills a child still running (the launcher is ending abnormally)
        close = getattr(held, "close", None)
        if close is not None:
            close()


def health(base_url: str, *, timeout: float = HEALTH_TIMEOUT_S) -> dict | None:
    """Hearthkeep's `/api/health` when it answers as itself (200, `status` "ok" and a
    `setup_mode` key), else None. Never raises."""
    import httpx

    try:
        response = httpx.get(f"{base_url}/api/health", timeout=timeout, trust_env=False)
        if response.status_code != 200:
            return None
        body = response.json()
    except Exception:  # anything at all means "not answering"
        return None
    if isinstance(body, dict) and body.get("status") == "ok" and "setup_mode" in body:
        return body
    return None


def wait_for_health(
    base_url: str,
    timeout_s: float = START_WAIT_S,
    *,
    probe=health,
    sleep=time.sleep,
    clock=time.monotonic,
    want_setup_mode: bool | None = None,
) -> bool:
    """Whether `probe` answers (with `setup_mode == want_setup_mode`, when given) within
    `timeout_s`."""
    deadline = clock() + timeout_s
    while True:
        body = probe(base_url)
        if body is not None and (want_setup_mode is None or body["setup_mode"] == want_setup_mode):
            return True
        if clock() >= deadline:
            return False
        sleep(HEALTH_POLL_S)


def open_app(
    settings: Settings,
    host: Host,
    *,
    probe=health,
    open_url=webbrowser.open,
    wait=wait_for_health,
) -> int:
    """Start the launcher when Hearthkeep isn't answering, then open it in the browser either
    way (the app shows "Can't reach Hearthkeep" with Retry if it's still starting)."""
    base = f"http://127.0.0.1:{settings.port}"
    if probe(base) is None:
        host.spawn_detached(launcher_argv(Path(settings.data_dir)))
        wait(base)
    open_url(settings.public_origin)
    return 0
