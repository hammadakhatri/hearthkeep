"""The sandbox runner as the app holds it: ready at startup when Docker was running, or made
later, the first time Docker is seen up (checked at most every 30 s), when it wasn't. With
Docker configured off it never exists, and nothing ever runs on the host."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from hearthkeep.sandbox.runner import SandboxRunner

DOCKER_CHECK_S = 30.0

_log = logging.getLogger(__name__)


class SandboxHandle:
    def __init__(
        self,
        runner: SandboxRunner | None = None,
        *,
        factory: Callable[[], Awaitable[SandboxRunner | None]] | None = None,
        docker_check: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`runner`: ready to use. `factory` and `docker_check`: Docker was missing at startup,
        so `docker_check` (blocking, run in a thread) is retried and the factory builds the
        runner once it passes. Neither: there is never a sandbox."""
        self._runner = runner
        self._factory = factory
        self._check = docker_check
        self._clock = clock
        self._checked_at: float | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def of(cls, sandbox: SandboxHandle | SandboxRunner | None) -> SandboxHandle:
        if isinstance(sandbox, SandboxHandle):
            return sandbox
        return cls(sandbox)

    @property
    def runner(self) -> SandboxRunner | None:
        """The runner if it exists yet (no check is made)."""
        return self._runner

    async def get(self) -> SandboxRunner | None:
        """The runner, made now if Docker has come up since the last check; else None."""
        if self._runner is not None or self._factory is None or self._check is None:
            return self._runner
        async with self._lock:
            if self._runner is not None:
                return self._runner
            now = self._clock()
            if self._checked_at is not None and now - self._checked_at < DOCKER_CHECK_S:
                return None
            self._checked_at = now
            try:
                up = bool(await asyncio.to_thread(self._check))
                if up:
                    self._runner = await self._factory()
            except Exception as exc:
                _log.warning("could not start the sandbox: %s", type(exc).__name__)
        return self._runner

    async def docker_available(self) -> bool:
        """Whether Docker is running now (the runner's own status, also cached for 30 s)."""
        runner = await self.get()
        if runner is None:
            return False
        try:
            return bool((await runner.status()).docker)
        except Exception:
            return False
