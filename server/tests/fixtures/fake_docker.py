"""A stand-in for the docker CLI in sandbox runner tests. Stdlib only.

Every call appends its argv (as one JSON line) to $FAKE_DOCKER_LOG.

- `run`: chosen by $FAKE_DOCKER_MODE:
  - `tar` (default): writes the bytes of $FAKE_DOCKER_TAR to stdout, then exits with
    $FAKE_DOCKER_EXIT (default 0);
  - `tar_then_hang`: writes that tar, closes stdout, then waits like `sleep`;
  - `endless`: writes zeros to stdout until the pipe breaks or the container is killed;
  - `sleep`: waits up to ten minutes for the container to be killed.
  Like the real client, a killed run exits 137. It also writes its pid to
  $FAKE_DOCKER_STATE/run.pid and the sorted names in the folder mounted at /job to
  $FAKE_DOCKER_STATE/job.json. $FAKE_DOCKER_STDERR goes to stderr.
- `kill`: marks the container killed ($FAKE_DOCKER_STATE/killed), unless
  $FAKE_DOCKER_KILL_IGNORED is set.
- `ps`: prints the ids in $FAKE_DOCKER_PS (space separated).
- `rm`: exits 0.
- `info`: exits 1 if $FAKE_DOCKER_NO_DAEMON is set, else 0.
- `image inspect`: exits 1 if $FAKE_DOCKER_NO_IMAGE is set, else 0.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

KILLED_EXIT = 137


def _state() -> Path | None:
    folder = os.environ.get("FAKE_DOCKER_STATE")
    return Path(folder) if folder else None


def _killed() -> bool:
    state = _state()
    return state is not None and (state / "killed").exists()


def _record(argv: list[str]) -> None:
    log = os.environ.get("FAKE_DOCKER_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\n")


def _job_source(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv[:-1]):
        if arg == "--mount" and "target=/job" in argv[i + 1]:
            for part in argv[i + 1].split(","):
                if part.startswith("source="):
                    return part[len("source=") :]
    return None


def _wait_for_kill(seconds: float) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _killed():
            return KILLED_EXIT
        time.sleep(0.05)
    return 0


def _run(argv: list[str]) -> int:
    state = _state()
    if state is not None:
        state.mkdir(parents=True, exist_ok=True)
        (state / "killed").unlink(missing_ok=True)
        (state / "run.pid").write_text(str(os.getpid()), encoding="ascii")
        source = _job_source(argv)
        listing = sorted(os.listdir(source)) if source else None
        (state / "job.json").write_text(json.dumps(listing), encoding="utf-8")
    if os.environ.get("FAKE_DOCKER_STDERR"):
        sys.stderr.write(os.environ["FAKE_DOCKER_STDERR"])
        sys.stderr.flush()
    mode = os.environ.get("FAKE_DOCKER_MODE", "tar")
    out = sys.stdout.buffer
    try:
        if mode in ("tar", "tar_then_hang"):
            out.write(Path(os.environ["FAKE_DOCKER_TAR"]).read_bytes())
            out.flush()
            if mode == "tar":
                return int(os.environ.get("FAKE_DOCKER_EXIT", "0"))
            os.close(1)  # stdout ends, but the client goes on running
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115
            return _wait_for_kill(600)
        if mode == "endless":
            chunk = b"\0" * 65536
            while not _killed():
                out.write(chunk)
            return KILLED_EXIT
        if mode == "sleep":
            return _wait_for_kill(600)
    except OSError:  # the reader went away
        return 1
    return 2


def _kill() -> int:
    state = _state()
    if os.environ.get("FAKE_DOCKER_KILL_IGNORED") or state is None:
        return 0
    (state / "killed").write_text("1", encoding="ascii")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    _record(argv)
    command = argv[0] if argv else ""
    if command == "run":
        return _run(argv)
    if command == "kill":
        return _kill()
    if command == "ps":
        ids = os.environ.get("FAKE_DOCKER_PS", "").split()
        sys.stdout.write("".join(f"{i}\n" for i in ids))
        return 0
    if command == "rm":
        return 0
    if command == "info":
        return 1 if os.environ.get("FAKE_DOCKER_NO_DAEMON") else 0
    if command == "image":
        return 1 if os.environ.get("FAKE_DOCKER_NO_IMAGE") else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
