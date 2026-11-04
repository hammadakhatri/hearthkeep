"""The sandbox build job: builds the two images `hearthkeep sandbox build` builds, one after
the other, in a worker thread, one build at a time.

Docker's output is read line by line in that thread into a 40-line ring and written to
`<data>/logs/sandbox-build.log` when the build ends, pass or fail. It never goes anywhere else:
not in the state the API shows, not in the audit log. `shutdown()` kills the `docker build`
child, so a server stop never leaves a build running."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Literal

from hearthkeep.sandbox import image

_log = logging.getLogger(__name__)

LOG_LINES = 40
STOP_TIMEOUT_S = 10  # how long a server stop waits for the build to end
LOG_FILE = "sandbox-build.log"
STEPS = ("sandbox", "repo")  # the order build_targets() gives the images in

State = Literal["idle", "building", "done", "failed"]
Reason = Literal["docker_missing", "docker_stopped", "build_failed"]


class BuildBusy(Exception):
    """A build is already running."""


class SandboxBuilder:
    def __init__(
        self,
        data_dir: Path,
        *,
        docker: Sequence[str] = image.DOCKER,
        run_build: Callable[..., int] = image.build_image,
        record: Callable[[str, str, dict], None] | None = None,
        refresh: Callable[[], Awaitable[object]] | None = None,
        docker_ok: Callable[[Sequence[str]], bool] = image.docker_ok,
        docker_installed: Callable[[], bool] | None = None,
        targets: Callable[[], list[tuple[str, str, Path]]] = image.build_targets,
        stop_timeout: float = STOP_TIMEOUT_S,
    ) -> None:
        self._data = Path(data_dir)
        self._docker = tuple(docker)
        self._run_build = run_build
        self._record = record
        self._refresh = refresh
        self._docker_ok = docker_ok
        self._docker_installed = docker_installed
        self._targets = targets
        self._stop_timeout = stop_timeout
        self._finishing = False
        self._state: State = "idle"
        self._step: str | None = None
        self._reason: Reason | None = None
        self._actor = "system"
        self._lines: deque[str] = deque(maxlen=LOG_LINES)
        self._proc: image.BuildProcess | None = None
        self._stopping = False
        self._task: asyncio.Task | None = None

    def busy(self) -> bool:
        """Images are building right now (not the add-on check that follows the last one)."""
        return self._state == "building" and not self._finishing

    def state(self) -> dict:
        """Where the build is. Only these three plain values: never Docker's output."""
        return {"state": self._state, "step": self._step, "reason": self._reason}

    async def start(self, actor: str = "system") -> dict:
        """Check Docker, then build in the background. Raises BuildBusy while a build runs. If
        Docker is missing or stopped the state says which and nothing is built."""
        if self._state == "building":
            raise BuildBusy
        self._state, self._step, self._reason = "building", None, None
        self._actor, self._stopping, self._finishing = actor, False, False
        self._lines.clear()
        try:
            reason = await asyncio.to_thread(self._docker_problem)
        except Exception as exc:  # never stay at "building" with nothing running
            _log.warning("could not check Docker: %s", type(exc).__name__)
            reason = "docker_missing"
        if reason is None and self._stopping:  # the server began to stop meanwhile
            reason = "build_failed"
        if reason is not None:
            self._state, self._reason = "failed", reason
            return self.state()
        self._audit("sandbox.build_started", {})
        self._task = asyncio.create_task(self._work())
        return self.state()

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.wait({self._task})

    async def shutdown(self) -> None:
        """Kill the `docker build` in progress (if any, and what it started) and wait for the
        job to end, for at most `stop_timeout`: a stop is never held up by a stuck build."""
        self._stopping = True
        proc = self._proc
        if proc is not None:
            self._kill(proc)
        try:
            await asyncio.wait_for(self.wait(), self._stop_timeout)
        except Exception as exc:
            _log.warning("the sandbox build did not stop in time: %s", type(exc).__name__)

    # ---- the job ---------------------------------------------------------------------------

    def _docker_problem(self) -> Reason | None:
        if self._docker_ok(self._docker):
            return None
        return "docker_stopped" if self._installed() else "docker_missing"

    def _installed(self) -> bool:
        probe = self._docker_installed
        if probe is None:
            from hearthkeep.addons import docker_installed  # addons imports the sandbox package

            probe = docker_installed
        return probe()

    async def _work(self) -> None:
        ok = False
        try:
            ok = await asyncio.to_thread(self._run)
            self._finishing = True
            if ok and not self._stopping and self._refresh is not None:
                try:
                    await self._refresh()  # so "ready" is true by the time "done" is shown
                except Exception as exc:
                    _log.warning("could not check the add-ons: %s", type(exc).__name__)
        finally:
            reason: Reason | None = None if ok else "build_failed"
            self._state, self._step, self._reason = ("done" if ok else "failed"), None, reason
            self._proc = None
            self._audit("sandbox.build_finished", {"ok": ok, "reason": reason})

    def _run(self) -> bool:
        """In the worker thread: each image in order, stopping at the first that fails."""
        try:
            for step, (_label, tag, folder) in zip(STEPS, self._targets(), strict=True):
                if self._stopping:
                    return False
                self._step = step
                code = self._run_build(
                    tag,
                    self._docker,
                    folder,
                    on_line=self._lines.append,
                    on_proc=self._on_proc,
                )
                if code != 0 or self._stopping:
                    return False
            return True
        except Exception as exc:  # a bug or a vanished folder: a failed build, not a crash
            _log.warning("the sandbox build stopped unexpectedly: %s", type(exc).__name__)
            return False
        finally:
            self._write_log()

    def _on_proc(self, proc: image.BuildProcess) -> None:
        self._proc = proc
        if self._stopping:  # shutdown came between two images
            self._kill(proc)

    @staticmethod
    def _kill(proc: image.BuildProcess) -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            proc.kill()

    def _write_log(self) -> None:
        text = "\n".join(self._lines)
        try:
            logs = self._data / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / LOG_FILE).write_text(text + "\n" if text else "", encoding="utf-8")
        except OSError as exc:
            _log.warning("could not write the sandbox build log: %s", type(exc).__name__)

    def _audit(self, event: str, detail: dict) -> None:
        if self._record is None:
            return
        try:
            self._record(self._actor, event, detail)
        except Exception as exc:
            _log.warning("could not record %s: %s", event, type(exc).__name__)
