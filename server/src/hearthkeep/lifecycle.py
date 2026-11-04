"""How a running server is asked to restart or stop, and the exit codes that carry the answer.

The launcher (`hearthkeep launch`) runs `serve` as a child and reads only its exit code: no
port, no other channel. `ServerControl` is the server's side of that: a request records the
code and tells uvicorn to shut down; `run_server` returns the code as the process's own."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

EXIT_STOPPED = 0  # stop: the launcher ends too
EXIT_USAGE = 2  # a configuration or usage error: starting again would fail the same way
EXIT_RESTART = 75  # restart: the launcher starts a new server
# Setup only: the setup page chose another data folder, so `cmd_setup` starts setup again
# there. Only the setup server returns it, and the launcher never runs that server, so the
# launcher needs no case for it (it would count it as a crash).
EXIT_RELOCATE = 76
SUPERVISED_ENV = "HEARTHKEEP_SUPERVISED"  # "1" when the launcher started this server


class ServerControl:
    """Restart and stop requests for one server run. The first request wins: it sets
    `exit_code` and calls the attached shutdown once; later requests are ignored. Before
    `attach` a request is only recorded."""

    def __init__(
        self,
        *,
        supervised: bool,
        setup_mode: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.supervised = supervised
        self.setup_mode = setup_mode
        self.started_at: datetime = clock()
        self.exit_code: int | None = None
        self.restart_needed: set[str] = set()
        self._shutdown: Callable[[], None] | None = None

    def attach(self, shutdown: Callable[[], None]) -> None:
        self._shutdown = shutdown

    def request(self, exit_code: int) -> None:
        if self.exit_code is not None:
            return
        self.exit_code = exit_code
        if self._shutdown is not None:
            self._shutdown()

    def mark_restart_needed(self, reason: str) -> None:
        self.restart_needed.add(reason)
