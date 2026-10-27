"""The sandbox image: its content-hash tag, and checking for and building it with Docker."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

IMAGE_DIR = Path(__file__).resolve().parent / "image"
IMAGE_NAME = "hearthkeep-sandbox"
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


def image_tag(image_dir: Path = IMAGE_DIR) -> str:
    """`hearthkeep-sandbox:<12 hex>`: a hash of every file's relative path and bytes, so any
    change to the image's files names a new image."""
    digest = hashlib.sha256()
    for rel, path in _image_files(Path(image_dir)):
        data = path.read_bytes()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(str(len(data)).encode("ascii") + b"\0")
        digest.update(data)
    return f"{IMAGE_NAME}:{digest.hexdigest()[:12]}"


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


def build_image(tag: str, docker: Sequence[str] = DOCKER) -> int:
    """`docker build -t <tag> <IMAGE_DIR>` with its output shown as it runs."""
    try:
        return subprocess.run(
            [*docker, "build", "-t", tag, str(IMAGE_DIR)],
            stdin=subprocess.DEVNULL,
            check=False,
            creationflags=_NO_WINDOW,
        ).returncode
    except OSError:
        return 127
