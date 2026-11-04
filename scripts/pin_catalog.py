"""Check (or rewrite) the sizes and SHA-256 values pinned in the model catalog.

    python scripts/pin_catalog.py --check    # one line per file: ok, or differs: ...
    python scripts/pin_catalog.py --write    # rewrite the size / sha256 lines in place

Values come from the GitHub releases API (the asset's `digest`) and the Hugging Face tree API
at the pinned commit (`lfs.oid`); no file is downloaded. Run it by hand when the catalog
changes. It is never run in CI.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

CATALOG = (
    Path(__file__).resolve().parents[1]
    / "server" / "src" / "hearthkeep" / "models" / "catalog.toml"
)  # fmt: skip
# An api maps a file URL to its (size, lowercase sha256), or raises LookupError.
Api = Callable[[str], tuple[int, str]]

_GITHUB = re.compile(r"^/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)$")
_HF = re.compile(r"^/([^/]+/[^/]+)/resolve/([0-9a-f]{40})/(.+)$")
_TIMEOUT = httpx.Timeout(30.0)


def real_api(url: str) -> tuple[int, str]:
    parts = urlsplit(url)
    path = unquote(parts.path)
    if parts.hostname == "github.com":
        m = _GITHUB.match(path)
        if not m:
            raise LookupError("not a GitHub release asset url")
        owner, repo, tag, name = m.groups()
        r = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT,
            follow_redirects=True,  # a renamed repository answers 301 to its numeric id
        )
        r.raise_for_status()
        for asset in r.json().get("assets", []):
            if asset.get("name") == name:
                digest = asset.get("digest") or ""
                if not digest.startswith("sha256:"):
                    raise LookupError("the asset has no sha256 digest")
                return int(asset["size"]), digest.removeprefix("sha256:")
        raise LookupError(f"no asset named {name} in release {tag}")
    if parts.hostname == "huggingface.co":
        m = _HF.match(path)
        if not m:
            raise LookupError("not a pinned Hugging Face url")
        repo, commit, file_path = m.groups()
        folder, _, name = file_path.rpartition("/")
        tree = f"https://huggingface.co/api/models/{repo}/tree/{commit}"
        r = httpx.get(f"{tree}/{folder}" if folder else tree, timeout=_TIMEOUT)
        r.raise_for_status()
        for entry in r.json():
            if entry.get("path") == file_path:
                lfs = entry.get("lfs") or {}
                if not lfs.get("oid"):
                    raise LookupError("the file is not stored with Git LFS")
                return int(lfs.get("size", entry["size"])), lfs["oid"]
        raise LookupError(f"no file {file_path} at commit {commit}")
    raise LookupError("unsupported host")


def _files(text: str) -> list[dict]:
    raw = tomllib.loads(text)
    return [f for item in raw.get("item", []) for f in item.get("file", [])]


def _lookup(api: Api, url: str) -> tuple[int, str] | str:
    try:
        size, sha = api(url)
    except (LookupError, httpx.HTTPError, KeyError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return size, sha


def check(text: str, api: Api) -> tuple[list[str], bool]:
    lines: list[str] = []
    all_ok = True
    for f in _files(text):
        found = _lookup(api, f["url"])
        if isinstance(found, str):
            lines.append(f"differs: lookup failed ({found}) {f['url']}")
            all_ok = False
        elif found == (f["size"], f["sha256"]):
            lines.append(f"ok {f['url']}")
        else:
            lines.append(f"differs: size {found[0]} sha256 {found[1]} {f['url']}")
            all_ok = False
    return lines, all_ok


def rewrite(text: str, api: Api) -> tuple[str, list[str]]:
    """Replace the size / sha256 lines of each file with the API's values."""
    problems: list[str] = []
    out: list[str] = []
    current: tuple[int, str] | None = None
    for line in text.splitlines(keepends=True):
        url = re.match(r'url\s*=\s*"([^"]+)"', line)
        if line.startswith("["):
            current = None
        elif url:
            found = _lookup(api, url.group(1))
            if isinstance(found, str):
                problems.append(f"{url.group(1)}: {found}")
            else:
                current = found
        elif current and re.match(r"size\s*=", line):
            line = re.sub(r"=.*", f"= {current[0]}", line.rstrip("\r\n")) + _eol(line)
        elif current and re.match(r"sha256\s*=", line):
            line = re.sub(r"=.*", f'= "{current[1]}"', line.rstrip("\r\n")) + _eol(line)
        out.append(line)
    return "".join(out), problems


def _eol(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def main(argv: list[str] | None = None, api: Api | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report ok or differs per file")
    mode.add_argument("--write", action="store_true", help="rewrite size and sha256 in place")
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    args = parser.parse_args(argv)
    lookup = api or real_api

    text = args.catalog.read_text(encoding="utf-8")
    if args.check:
        lines, all_ok = check(text, lookup)
        print("\n".join(lines))
        return 0 if all_ok else 1
    new_text, problems = rewrite(text, lookup)
    for problem in problems:
        print(f"could not look up {problem}", file=sys.stderr)
    if problems:
        return 1
    if new_text != text:
        args.catalog.write_text(new_text, encoding="utf-8", newline="")
    print("catalog written" if new_text != text else "catalog already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
