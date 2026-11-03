"""The web app Hearthkeep serves from `web/dist`.

A *release checkout* (version `x.y.z`, not `0.0.0`) never builds anything: it uses `web/dist`
when its `.bundle.json` manifest names this version and every file's SHA-256 matches, and
otherwise downloads the release's prebuilt bundle from the GitHub release of the clone's
`origin` remote, checks it against the release's `.sha256`, unpacks it safely and swaps it in.
`main` (a `0.0.0` or `.devN` version) is for developers: it builds the web app with Node, or
uses a build that is already there, or explains where to get a release instead.

The release repository is read from `origin` at run time and is never written in the code."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import stat
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal

import httpx

import hearthkeep
from hearthkeep.host import REPO_ROOT
from hearthkeep.procutil import NO_WINDOW, find_executable

WEB_DIR: Path = REPO_ROOT / "web"
WEB_DIST: Path = WEB_DIR / "dist"
CACHE_DIR: Path = REPO_ROOT / ".cache"
MANIFEST = ".bundle.json"
MAX_BUNDLE_BYTES = 50_000_000
MAX_BUNDLE_FILES = 2000
MAX_CHECKSUM_BYTES = 4096
MAX_REDIRECTS = 5
GIT_TIMEOUT_SECONDS = 10
NPM_TIMEOUT_SECONDS = 1800
HTTP_TIMEOUT_SECONDS = 30.0
# github.com serves the release URL and redirects to one of GitHub's own asset hosts.
ALLOWED_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)

BundleOutcome = Literal["ready", "downloaded", "built", "existing", "failed"]

BUNDLE_READY = "Hearthkeep's web app is ready."
BUNDLE_DOWNLOAD_FAILED = (
    "Hearthkeep couldn't download its web app. Check your internet connection, then "
    "double-click Setup.cmd again."
)
BUNDLE_BAD_CHECKSUM = (
    "The downloaded web app didn't match the release's checksum, so it wasn't used. "
    "Double-click Setup.cmd to try again."
)
BUNDLE_UNUSABLE = (
    "The downloaded web app couldn't be unpacked, so it wasn't used. "
    "Double-click Setup.cmd to try again."
)
NO_BUNDLE_NO_REPO = (
    "This copy of Hearthkeep is missing its web app. Download hearthkeep-{v}.zip from the "
    "Releases page of the Hearthkeep project on GitHub, unzip it, and double-click Setup.cmd "
    "in that folder."
)
BUILDING = "Building the web app with Node.js (developers' version). This takes a minute or two..."
BUILD_FAILED = "The web app didn't build. The messages above say why."
USING_EXISTING_BUILD = (
    "Node.js isn't installed, so the web app that was already built is used as it is."
)
MAIN_IS_FOR_DEVELOPERS = (
    "This copy of Hearthkeep is the developers' version, which needs Node.js to build its web "
    "app. To install Hearthkeep, download the latest release instead:"
)
RELEASES_PAGE_FALLBACK = "the Releases page of the Hearthkeep project on GitHub"

_RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_NAME = r"[A-Za-z0-9_.-]+"
_ORIGIN_FORMS = (
    re.compile(rf"^https://github\.com/({_NAME})/({_NAME}?)(?:\.git)?$"),
    re.compile(rf"^git@github\.com:({_NAME})/({_NAME}?)(?:\.git)?$"),
)
_SHA256 = re.compile(r"^([0-9a-f]{64}) [ *](.+)$")
_CHUNK = 64 * 1024


class BundleError(Exception):
    """The bundle could not be downloaded or unpacked safely."""


class _NotFound(BundleError):
    """The server answered 404: the asset isn't there."""


# ---- versions, names and checksums ----------------------------------------


def release_version(version: str = hearthkeep.__version__) -> str | None:
    """`version` when it is a release (`x.y.z`, not `0.0.0`), else None."""
    if _RELEASE_VERSION.match(version) and version != "0.0.0":
        return version
    return None


def bundle_name(v: str) -> str:
    return f"hearthkeep-web-{v}.zip"


def release_zip_name(v: str) -> str:
    return f"hearthkeep-{v}.zip"


def checksum_line(sha: str, name: str) -> str:
    return f"{sha}  {name}\n"


def parse_checksum(text: str, name: str) -> str | None:
    """The SHA-256 (64 lowercase hex) given for `name` in `sha256sum` format, else None."""
    for line in text.splitlines():
        match = _SHA256.match(line.strip())
        if match and match.group(2) == name:
            return match.group(1)
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---- the manifest ----------------------------------------


def _bundle_files(dist: Path) -> list[Path]:
    return sorted(
        p for p in dist.rglob("*") if p.is_file() and p.relative_to(dist).as_posix() != MANIFEST
    )


def build_manifest(dist: Path, version: str) -> dict:
    """`{"version": v, "files": {posix path: sha256}}` for every file in `dist` but the
    manifest itself, sorted by path."""
    files = {p.relative_to(dist).as_posix(): _sha256_file(p) for p in _bundle_files(dist)}
    return {"version": version, "files": dict(sorted(files.items()))}


def write_manifest(dist: Path, version: str) -> None:
    manifest = build_manifest(dist, version)
    (dist / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")


def verify_dist(dist: Path, version: str) -> bool:
    """True when `dist`'s manifest names `version`, lists exactly the files that are there,
    and every file's SHA-256 matches."""
    try:
        manifest = json.loads((dist / MANIFEST).read_text("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != version:
            return False
        listed = manifest.get("files")
        if not isinstance(listed, dict):
            return False
        actual = {p.relative_to(dist).as_posix(): p for p in _bundle_files(dist)}
        if set(listed) != set(actual):
            return False
        return all(_sha256_file(actual[name]) == listed[name] for name in sorted(actual))
    except (OSError, ValueError):
        return False


# ---- the release repository ----------------------------------------


def github_repo(
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    root: Path = REPO_ROOT,
    find: Callable[[str], Path | None] = find_executable,
) -> tuple[str, str] | None:
    """(owner, repo) of the clone's `origin` remote when it is a GitHub repository, else None."""
    git = find("git")
    if git is None:
        return None
    try:
        done = run(
            [str(git), "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not isinstance(done.stdout, str):
        return None
    url = done.stdout.strip()
    for form in _ORIGIN_FORMS:
        match = form.match(url)
        if match:
            owner, repo = match.group(1), match.group(2)
            if not owner.strip(".") or not repo.strip("."):
                return None
            return owner, repo
    return None


# ---- download ----------------------------------------


def _allowed_url(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.port in (None, 443)
        and not url.userinfo
        and url.host in ALLOWED_HOSTS
    )


def download(url: str, dest: Path, *, client: httpx.Client, limit: int = MAX_BUNDLE_BYTES) -> None:
    """Fetch `url` into `dest`: HTTPS from GitHub (or its asset hosts) only, redirects followed
    one at a time and each checked the same way, streamed, and stopped past `limit` bytes.
    `dest` appears only when the whole body arrived."""
    current = httpx.URL(url)
    part = dest.with_name(dest.name + ".part")
    for _ in range(MAX_REDIRECTS + 1):
        if not _allowed_url(current):
            raise BundleError("refused a URL outside GitHub's HTTPS hosts")
        try:
            with client.stream("GET", current, follow_redirects=False) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise BundleError("a redirect without a location")
                    current = current.join(location)
                    continue
                if response.status_code == 404:
                    raise _NotFound(str(current))
                if response.status_code != 200:
                    raise BundleError(f"HTTP {response.status_code}")
                total = 0
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with part.open("wb") as f:
                        for chunk in response.iter_bytes(_CHUNK):
                            total += len(chunk)
                            if total > limit:
                                raise BundleError("the download is too large")
                            f.write(chunk)
                    part.replace(dest)
                except BaseException:
                    part.unlink(missing_ok=True)
                    raise
                return
        except httpx.HTTPError as exc:
            raise BundleError(f"network error: {type(exc).__name__}") from exc
    raise BundleError("too many redirects")


# ---- safe extraction ----------------------------------------


def _check_entry(info: zipfile.ZipInfo) -> None:
    name = info.filename
    raw = info.orig_filename  # before zipfile turns a Windows os.sep into "/"
    if not name or "\\" in raw or ":" in raw or name.startswith("/"):
        raise BundleError(f"refused entry name {name!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("..", "") for part in parts):
        raise BundleError(f"refused entry name {name!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise BundleError(f"refused symlink entry {name!r}")


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Unpack `zip_path` into `dest`, refusing absolute names, drive letters, `..`, backslashes,
    symlinks, more than `MAX_BUNDLE_FILES` entries or more than `MAX_BUNDLE_BYTES` unpacked.
    Every name is checked before anything is written; sizes are counted as they are written."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_BUNDLE_FILES:
                raise BundleError("too many files in the bundle")
            if sum(info.file_size for info in infos) > MAX_BUNDLE_BYTES:
                raise BundleError("the bundle is too large")
            for info in infos:
                _check_entry(info)
            dest.mkdir(parents=True, exist_ok=True)
            root = dest.resolve()
            written = 0
            for info in infos:
                target = (root / info.filename).resolve()
                if target != root and not target.is_relative_to(root):
                    raise BundleError(f"refused entry name {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    for chunk in iter(lambda: src.read(_CHUNK), b""):
                        written += len(chunk)
                        if written > MAX_BUNDLE_BYTES:
                            raise BundleError("the bundle is too large")
                        out.write(chunk)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as exc:
        raise BundleError("the bundle is not a readable zip") from exc


# ---- putting the bundle in place ----------------------------------------


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _swap_in(new: Path, dist: Path) -> None:
    """Replace `dist` with `new` by renames: `dist` -> `dist.old`, `new` -> `dist`, then remove
    `dist.old`. If the second rename fails, the old `dist` is put back."""
    old = dist.with_name(dist.name + ".old")
    _remove(old)
    had_old = dist.exists()
    if had_old:
        dist.rename(old)
    try:
        new.rename(dist)
    except OSError:
        if had_old:
            old.rename(dist)
        raise
    _remove(old)


def _ensure_release(
    v: str,
    *,
    say: Callable[[str], None],
    dist: Path,
    run: Callable[..., subprocess.CompletedProcess],
    client: httpx.Client | None,
    find: Callable[[str], Path | None],
    cache: Path,
) -> BundleOutcome:
    if verify_dist(dist, v):
        return "ready"
    repo = github_repo(run=run, find=find)
    if repo is None:
        say(NO_BUNDLE_NO_REPO.format(v=v))
        return "failed"
    owner, name = repo
    asset = bundle_name(v)
    base = f"https://github.com/{owner}/{name}/releases/download/v{v}/"
    zip_path = cache / asset
    sum_path = cache / f"{asset}.sha256"
    own_client = client is None
    if client is None:
        client = httpx.Client(trust_env=False, follow_redirects=False, timeout=HTTP_TIMEOUT_SECONDS)
    new = dist.with_name(dist.name + ".new")
    try:
        try:
            download(base + asset, zip_path, client=client)
        except BundleError:
            say(BUNDLE_DOWNLOAD_FAILED)
            return "failed"
        try:
            download(base + asset + ".sha256", sum_path, client=client, limit=MAX_CHECKSUM_BYTES)
            expected = parse_checksum(sum_path.read_text("utf-8", errors="replace"), asset)
        except _NotFound:
            expected = None
        except BundleError:
            say(BUNDLE_DOWNLOAD_FAILED)
            return "failed"
        if expected is None or _sha256_file(zip_path) != expected:
            say(BUNDLE_BAD_CHECKSUM)
            return "failed"
        try:
            _remove(new)
            safe_extract(zip_path, new)
            write_manifest(new, v)
            _swap_in(new, dist)
        except (BundleError, OSError):
            say(BUNDLE_UNUSABLE)
            return "failed"
        say(BUNDLE_READY)
        return "downloaded"
    finally:
        _remove(new)
        _remove(zip_path)
        _remove(sum_path)
        if own_client:
            client.close()


def _ensure_dev(
    *,
    say: Callable[[str], None],
    dist: Path,
    run: Callable[..., subprocess.CompletedProcess],
    find: Callable[[str], Path | None],
) -> BundleOutcome:
    npm = find("npm.cmd") or find("npm")
    if npm is not None:
        say(BUILDING)
        for argv in ([str(npm), "ci"], [str(npm), "run", "build"]):
            try:
                done = run(argv, cwd=WEB_DIR, timeout=NPM_TIMEOUT_SECONDS, check=False)
            except (OSError, subprocess.SubprocessError):
                say(BUILD_FAILED)
                return "failed"
            if done.returncode != 0:
                say(BUILD_FAILED)
                return "failed"
        return "built"
    if (dist / "index.html").is_file():
        say(USING_EXISTING_BUILD)
        return "existing"
    say(MAIN_IS_FOR_DEVELOPERS)
    repo = github_repo(run=run, find=find)
    if repo is None:
        say(RELEASES_PAGE_FALLBACK)
    else:
        say(f"https://github.com/{repo[0]}/{repo[1]}/releases/latest")
    return "failed"


def ensure_web_bundle(
    *,
    say: Callable[[str], None],
    version: str = hearthkeep.__version__,
    dist: Path = WEB_DIST,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    client: httpx.Client | None = None,
    find: Callable[[str], Path | None] = find_executable,
    cache: Path = CACHE_DIR,
) -> BundleOutcome:
    """Make sure `dist` holds the web app this version should serve, saying what happens in
    plain console lines. Never raises for a download, unpack or build problem."""
    v = release_version(version)
    if v is not None:
        return _ensure_release(
            v, say=say, dist=dist, run=run, client=client, find=find, cache=cache
        )
    return _ensure_dev(say=say, dist=dist, run=run, find=find)
