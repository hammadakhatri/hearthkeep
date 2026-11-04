"""The sandbox images, each named by a content hash of its folder, and checking for and
building them with Docker: the Python sandbox (`hearthkeep-sandbox`) and the image repo
dependencies are installed and repo tests run in (`hearthkeep-repo`)."""

from __future__ import annotations

import contextlib
import hashlib
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

IMAGE_DIR = Path(__file__).resolve().parent / "image"
IMAGE_NAME = "hearthkeep-sandbox"
REPO_IMAGE_DIR = Path(__file__).resolve().parent / "repo_image"
REPO_IMAGE_NAME = "hearthkeep-repo"
DOCKER: tuple[str, ...] = ("docker",)
DOCKER_INFO_TIMEOUT_S = 5
DOCKER_INSPECT_TIMEOUT_S = 30
# Bytecode and test caches are never part of the image (see .dockerignore).
_IGNORED_DIRS = frozenset({"__pycache__", ".pytest_cache"})
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})
# No console window flashes up for each docker command on Windows (0 elsewhere).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _image_files(image_dir: Path) -> list[tuple[str, Path]]:
    found = []
    for path in image_dir.rglob("*"):
        rel = path.relative_to(image_dir)
        if any(part in _IGNORED_DIRS for part in rel.parts) or path.suffix in _IGNORED_SUFFIXES:
            continue
        if path.is_file():
            found.append((rel.as_posix(), path))
    return sorted(found)


def image_tag(image_dir: Path = IMAGE_DIR, name: str = IMAGE_NAME) -> str:
    """`<name>:<12 hex>`: a hash of every file's relative path and bytes, so any change to
    the image's files names a new image."""
    digest = hashlib.sha256()
    for rel, path in _image_files(Path(image_dir)):
        data = path.read_bytes()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return f"{name}:{digest.hexdigest()[:12]}"


def repo_image_tag() -> str:
    """`hearthkeep-repo:<12 hex>`, the image repo dependencies are installed and tested in."""
    return image_tag(REPO_IMAGE_DIR, REPO_IMAGE_NAME)


def _quiet(command: list[str], timeout: float) -> bool:
    try:
        proc = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def docker_ok(docker: Sequence[str] = DOCKER) -> bool:
    """Docker is installed and its engine answers (`docker info`, 5 s)."""
    return _quiet([*docker, "info"], DOCKER_INFO_TIMEOUT_S)


def image_present(tag: str, docker: Sequence[str] = DOCKER) -> bool:
    return _quiet([*docker, "image", "inspect", tag], DOCKER_INSPECT_TIMEOUT_S)


def build_targets() -> list[tuple[str, str, Path]]:
    """(label, tag, folder) for each image a build makes, in order: the sandbox, then the
    image repo dependencies are installed and tested in."""
    return [
        ("Sandbox image", image_tag(), IMAGE_DIR),
        ("Repo image", repo_image_tag(), REPO_IMAGE_DIR),
    ]


def build_image(
    tag: str,
    docker: Sequence[str] = DOCKER,
    image_dir: Path = IMAGE_DIR,
    *,
    on_line: Callable[[str], None] | None = None,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    """`docker build -t <tag> <image_dir>`. Without `on_line` its output goes to the console as
    it runs. With it, output is piped and each line (no line ending) is passed to `on_line`;
    `on_proc` is handed the child process first, so a caller can kill it."""
    command = [*docker, "build", "-t", tag, str(image_dir)]
    if on_line is None:
        try:
            return subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                check=False,
                creationflags=_NO_WINDOW,
            ).returncode
        except OSError:
            return 127
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except OSError:
        return 127
    try:
        if on_proc is not None:
            on_proc(proc)
        for line in proc.stdout or ():
            on_line(line.rstrip("\r\n"))
        return proc.wait()
    finally:
        if proc.poll() is None:  # on_line or on_proc raised: don't leave the build running
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc.kill()
                proc.wait(timeout=5)
