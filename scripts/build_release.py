"""Build the release assets for one Hearthkeep version.

Run from the repository root after `npm run build` in `web`:

    uv run --project server python scripts/build_release.py --version 0.1.0 --out OUT

It refuses (exit 2) unless the version is a release version equal to `hearthkeep.__version__`
and `web/dist/index.html` exists. It writes the manifest into `web/dist`, then into OUT:

- `hearthkeep-web-<v>.zip`: the files of `web/dist` at the zip root;
- `hearthkeep-<v>.zip`: `git archive` of HEAD under `hearthkeep-<v>/`, plus `web/dist` under
  `hearthkeep-<v>/web/dist/`;

and a `.sha256` beside each. Entries the script adds are sorted, dated 1980-01-01 and mode
0644, so the same tree gives the same bytes."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

import hearthkeep
from hearthkeep.host import REPO_ROOT
from hearthkeep.procutil import find_executable
from hearthkeep.webbundle import (
    WEB_DIST,
    bundle_name,
    checksum_line,
    release_version,
    release_zip_name,
    write_manifest,
)

EXIT_REFUSED = 2
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

GitArchive = Callable[[str, Path], None]


def git_archive(prefix: str, out: Path) -> None:
    """`git archive --format=zip --prefix=<prefix> HEAD` into `out`."""
    git = find_executable("git")
    if git is None:
        raise SystemExit("git was not found")
    subprocess.run(
        [
            str(git),
            "-C",
            str(REPO_ROOT),
            "archive",
            "--format=zip",
            f"--prefix={prefix}",
            "-o",
            str(out),
            "HEAD",
        ],
        check=True,
    )


def _add_tree(zf: zipfile.ZipFile, tree: Path, prefix: str) -> None:
    """Every file under `tree`, sorted, as `prefix + posix path`, with a fixed time and mode."""
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        info = zipfile.ZipInfo(prefix + path.relative_to(tree).as_posix(), FIXED_TIME)
        info.create_system = 3  # Unix, so the mode below is read as one
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, path.read_bytes(), compresslevel=9)


def _write_checksum(path: Path) -> None:
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(f"{path}.sha256").write_text(checksum_line(sha, path.name), "utf-8", newline="\n")


def build(v: str, dist: Path, out: Path, archive: GitArchive) -> None:
    out.mkdir(parents=True, exist_ok=True)
    write_manifest(dist, v)

    web_zip = out / bundle_name(v)
    with zipfile.ZipFile(web_zip, "w") as zf:
        _add_tree(zf, dist, "")

    prefix = f"hearthkeep-{v}/"
    full_zip = out / release_zip_name(v)
    archive(prefix, full_zip)
    with zipfile.ZipFile(full_zip, "a") as zf:
        if any(name.startswith(prefix + "web/dist/") for name in zf.namelist()):
            raise SystemExit("the source archive already holds web/dist")
        _add_tree(zf, dist, prefix + "web/dist/")

    for path in (web_zip, full_zip):
        _write_checksum(path)


def main(
    argv: Sequence[str] | None = None,
    *,
    git_archive: GitArchive = git_archive,
    version: str | None = None,
    dist: Path = WEB_DIST,
) -> int:
    parser = argparse.ArgumentParser(description="Build Hearthkeep's release assets.")
    parser.add_argument("--version", required=True, help="the release version, like 0.1.0")
    parser.add_argument("--out", required=True, type=Path, help="folder for the assets")
    args = parser.parse_args(argv)
    current = hearthkeep.__version__ if version is None else version

    if release_version(args.version) != args.version or args.version != current:
        print(
            f"Refused: {args.version!r} must be a release version (x.y.z, not 0.0.0) equal to "
            f"the code's version {current!r}.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not (dist / "index.html").is_file():
        print(
            f"Refused: {dist / 'index.html'} is missing; run npm run build in web first.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    build(args.version, dist, args.out, git_archive)
    print(f"Release assets for {args.version} are in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
