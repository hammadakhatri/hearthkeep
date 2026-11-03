"""A stand-in for the docker CLI in sandbox runner tests. Stdlib only.

Every call appends its argv (as one JSON line) to $FAKE_DOCKER_LOG.

- `run`: chosen by $FAKE_DOCKER_MODE:
  - `tar` (default): writes the bytes of $FAKE_DOCKER_TAR to stdout, then exits with
    $FAKE_DOCKER_EXIT (default 0);
  - `tar_then_hang`: writes that tar, closes stdout, then waits like `sleep`;
  - `endless`: writes zeros to stdout until the pipe breaks or the container is killed;
  - `sleep`: waits up to ten minutes for the container to be killed.
  - `held`: writes $FAKE_DOCKER_OUTPUT, starts a helper that keeps stdout open for 30 s (its
    pid in $FAKE_DOCKER_STATE/held.pid), then waits for the kill: the pipe outlives the client.
  Like the real client, a killed run exits 137. It also writes its pid to
  $FAKE_DOCKER_STATE/run.pid and the sorted names in the folder mounted at /job to
  $FAKE_DOCKER_STATE/job.json. $FAKE_DOCKER_STDERR goes to stderr.
- `kill`: marks the container killed ($FAKE_DOCKER_STATE/killed), unless
  $FAKE_DOCKER_KILL_IGNORED is set.
- `ps`: prints the ids in $FAKE_DOCKER_PS (space separated).
- `rm`: exits 0.
- `info`: exits 1 if $FAKE_DOCKER_NO_DAEMON is set, else 0.
- `image inspect`: exits 1 if $FAKE_DOCKER_NO_IMAGE is set, else 0.
- `volume inspect <name>` / `volume rm [-f] <names>` / `volume ls -q --filter name=<part>`:
  answered from $FAKE_DOCKER_STATE/volumes.json, to which every `run` adds the named volumes
  it mounts (as the real `run` creates them). `run` in mode `print` writes
  $FAKE_DOCKER_OUTPUT (or $FAKE_DOCKER_OUTPUT_BYTES zero-free bytes) to stdout and exits with
  $FAKE_DOCKER_EXIT.
"""

from __future__ import annotations

import json
import os
import subprocess
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


def _volumes() -> list[str]:
    state = _state()
    if state is None or not (state / "volumes.json").exists():
        return []
    return json.loads((state / "volumes.json").read_text(encoding="utf-8"))


def _save_volumes(names: list[str]) -> None:
    state = _state()
    if state is not None:
        state.mkdir(parents=True, exist_ok=True)
        (state / "volumes.json").write_text(json.dumps(sorted(set(names))), encoding="utf-8")


def _volume(argv: list[str]) -> int:
    sub = argv[1] if len(argv) > 1 else ""
    names = _volumes()
    if sub == "inspect":
        return 0 if argv[2:] and all(n in names for n in argv[2:]) else 1
    if sub == "rm":
        gone = [a for a in argv[2:] if not a.startswith("-")]
        _save_volumes([n for n in names if n not in gone])
        return 0
    if sub == "ls":
        part = ""
        for i, arg in enumerate(argv):
            if arg == "--filter" and argv[i + 1].startswith("name="):
                part = argv[i + 1][len("name=") :]
        sys.stdout.write("".join(f"{n}\n" for n in names if part in n))
        return 0
    return 2


def _run(argv: list[str]) -> int:
    mounted = []
    for i, arg in enumerate(argv[:-1]):
        if arg == "--mount" and argv[i + 1].startswith("type=volume,"):
            for part in argv[i + 1].split(","):
                if part.startswith("source="):
                    mounted.append(part[len("source=") :])
    if mounted:
        _save_volumes(_volumes() + mounted)
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
    if mode == "print":
        size = os.environ.get("FAKE_DOCKER_OUTPUT_BYTES")
        if size:
            out.write(bytes(65 + i % 26 for i in range(int(size))))
        else:
            out.write(os.environ.get("FAKE_DOCKER_OUTPUT", "").encode("utf-8"))
        out.flush()
        return int(os.environ.get("FAKE_DOCKER_EXIT", "0"))
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
        if mode == "held":
            out.write(os.environ.get("FAKE_DOCKER_OUTPUT", "").encode("utf-8"))
            out.flush()
            helper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=out.fileno(),
                stderr=subprocess.DEVNULL,
            )
            if state is not None:
                (state / "held.pid").write_text(str(helper.pid), encoding="ascii")
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
    if command == "volume":
        return _volume(argv)
    return 2


if __name__ == "__main__":
    sys.exit(main())
