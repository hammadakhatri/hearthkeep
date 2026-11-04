"""Shared HTTPS fetching: the host allow-list rules, a resumable verified download, and safe
unzipping. Used by the web-bundle download and the model and llama.cpp downloads.

`FetchError.detail` is for logs and tests only; it is never shown to the owner."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import re
import stat
import time
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Literal

import httpx

MAX_REDIRECTS = 5
CHUNK = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 0.25
_HASH_CHUNK = 64 * 1024
_WINERROR_DISK_FULL = 112

MODEL_HOSTS = frozenset({"github.com", "huggingface.co"})
MODEL_HOST_SUFFIXES = (".githubusercontent.com", ".hf.co")

_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")

FetchReason = Literal[
    "network", "not_found", "too_big", "checksum", "no_space", "cancelled", "unpack"
]


class FetchError(Exception):
    """A fetch or unpack failed. `reason` says why; `detail` is for logs, never for the owner."""

    def __init__(self, reason: FetchReason, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason: FetchReason = reason
        self.detail = detail


@dataclass
class Progress:
    done: int
    total: int


_now = time.monotonic


def allowed_url(url: httpx.URL, *, exact: frozenset[str], suffixes: tuple[str, ...]) -> bool:
    """True for `https`, port 443 or none, no userinfo, and a host that is in `exact` or ends
    with one of `suffixes`."""
    if url.scheme != "https" or url.port not in (None, 443) or url.userinfo:
        return False
    host = url.host
    if host in exact:
        return True
    if not host or host.startswith(".") or ".." in host:
        return False
    return any(host.endswith(suffix) and len(host) > len(suffix) for suffix in suffixes)


def sha256_file(path: Path) -> str:
    """Lowercase hex SHA-256 of the file."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_client() -> httpx.AsyncClient:
    """An async client that ignores proxy settings and never follows redirects on its own."""
    return httpx.AsyncClient(
        trust_env=False, follow_redirects=False, timeout=httpx.Timeout(30, read=60)
    )


# ---- download ----------------------------------------


def _open_part(path: Path, resume: bool) -> IO[bytes]:
    return path.open("ab" if resume else "wb")


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _disk_error(exc: OSError) -> FetchError:
    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == _WINERROR_DISK_FULL:
        return FetchError("no_space", f"{type(exc).__name__}: {exc}")
    return FetchError("network", f"could not write the file: {type(exc).__name__}: {exc}")


async def download_file(
    url: str,
    part: Path,
    *,
    size: int,
    sha256: str,
    client: httpx.AsyncClient,
    on_progress: Callable[[Progress], None],
    cancelled: Callable[[], bool],
    hosts: frozenset[str] = MODEL_HOSTS,
    suffixes: tuple[str, ...] = MODEL_HOST_SUFFIXES,
) -> None:
    """Download `url` into `part`, resuming a shorter `part`, then check its SHA-256.

    Raises `FetchError`: `network`, `not_found`, `too_big` (part deleted), `cancelled` and
    `no_space` (part kept), `checksum` (part deleted). On success `part` holds the verified
    file for the caller to move into place."""
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            _remove(part)
            have = 0
    except OSError as exc:
        raise _disk_error(exc) from exc
    if have < size:
        await _fetch_body(url, part, have, size, client, on_progress, cancelled, hosts, suffixes)
    else:
        on_progress(Progress(size, size))
    try:
        digest = await asyncio.to_thread(sha256_file, part)
    except OSError as exc:
        raise FetchError("network", f"could not read the file to check it: {exc}") from exc
    if digest != sha256.lower():
        _remove(part)
        raise FetchError("checksum", f"expected {sha256}, got {digest}")


async def _fetch_body(
    url: str,
    part: Path,
    have: int,
    size: int,
    client: httpx.AsyncClient,
    on_progress: Callable[[Progress], None],
    cancelled: Callable[[], bool],
    hosts: frozenset[str],
    suffixes: tuple[str, ...],
) -> None:
    try:
        current = httpx.URL(url)
        for _ in range(MAX_REDIRECTS + 1):
            if not allowed_url(current, exact=hosts, suffixes=suffixes):
                raise FetchError("network", "refused a URL outside the allowed HTTPS hosts")
            # Raw bytes only, so sizes, Range offsets and the hash all mean the file itself.
            headers = {"Accept-Encoding": "identity"}
            if have:
                headers["Range"] = f"bytes={have}-"
            # Never let the client follow redirects: every hop must pass allowed_url.
            async with client.stream(
                "GET", current, headers=headers, follow_redirects=False
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("network", "a redirect without a location")
                    current = current.join(location)
                    continue
                if response.status_code == 404:
                    raise FetchError("not_found", str(current))
                resume = _accepts_status(response, have, size)
                start = have if resume else 0
                await _write_body(response, part, start, resume, size, on_progress, cancelled)
                return
        raise FetchError("network", "too many redirects")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise FetchError("network", f"{type(exc).__name__}: {exc}") from exc


def _accepts_status(response: httpx.Response, have: int, size: int) -> bool:
    """True to append to the existing part (a matching 206); False to start over (a 200)."""
    code = response.status_code
    if code == 200:
        return False
    if code == 206 and have:
        match = _CONTENT_RANGE.match(response.headers.get("content-range", ""))
        if match and int(match.group(1)) == have and match.group(3) in (str(size), "*"):
            return True
        raise FetchError("network", "the Content-Range did not match the resume point")
    raise FetchError("network", f"HTTP {code}")


async def _write_body(
    response: httpx.Response,
    part: Path,
    start: int,
    resume: bool,
    size: int,
    on_progress: Callable[[Progress], None],
    cancelled: Callable[[], bool],
) -> None:
    done = start
    last = _now()
    try:
        with _open_part(part, resume) as f:
            async for chunk in response.aiter_bytes(CHUNK):
                if done + len(chunk) > size:
                    raise FetchError("too_big", f"more than {size} bytes arrived")
                f.write(chunk)
                done += len(chunk)
                if cancelled():
                    raise FetchError("cancelled")
                now = _now()
                if now - last >= PROGRESS_INTERVAL_SECONDS:
                    last = now
                    on_progress(Progress(done, size))
    except FetchError as exc:
        if exc.reason == "too_big":
            _remove(part)
        raise
    except OSError as exc:
        raise _disk_error(exc) from exc
    if done != size:
        raise FetchError("network", f"the body ended at {done} of {size} bytes")
    on_progress(Progress(size, size))


# ---- safe extraction ----------------------------------------


def _check_entry(info: zipfile.ZipInfo) -> None:
    name = info.filename
    raw = info.orig_filename  # before zipfile turns a Windows os.sep into "/"
    if not name or "\\" in raw or ":" in raw or name.startswith("/"):
        raise FetchError("unpack", f"refused entry name {name!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("..", "") for part in parts):
        raise FetchError("unpack", f"refused entry name {name!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise FetchError("unpack", f"refused symlink entry {name!r}")


def safe_extract_zip(zip_path: Path, dest: Path, *, max_files: int, max_bytes: int) -> None:
    """Unpack `zip_path` into `dest`, refusing absolute names, drive letters, `..`, backslashes,
    symlinks, more than `max_files` entries or more than `max_bytes` unpacked. Every name is
    checked before anything is written; sizes are counted as they are written."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > max_files:
                raise FetchError("unpack", "too many files in the archive")
            if sum(info.file_size for info in infos) > max_bytes:
                raise FetchError("unpack", "the archive is too large")
            for info in infos:
                _check_entry(info)
            dest.mkdir(parents=True, exist_ok=True)
            root = dest.resolve()
            written = 0
            for info in infos:
                target = (root / info.filename).resolve()
                if target != root and not target.is_relative_to(root):
                    raise FetchError("unpack", f"refused entry name {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    for chunk in iter(lambda: src.read(_HASH_CHUNK), b""):
                        written += len(chunk)
                        if written > max_bytes:
                            raise FetchError("unpack", "the archive is too large")
                        out.write(chunk)
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        zlib.error,  # corrupt compressed data
        RuntimeError,  # an encrypted entry
        NotImplementedError,  # a compression method zipfile cannot read
        ValueError,  # anything else zipfile finds wrong with an entry
    ) as exc:
        raise FetchError("unpack", "not a readable zip") from exc
