"""Privacy scanner: fail if text contains personal markers, private links or secrets.

Usage:
    python scripts/privacy_scan.py [paths...]
    git log -p | python scripts/privacy_scan.py -

A path of ``-`` reads standard input. Exits 1 if anything is found, 0 otherwise.
Extra, owner-specific identifiers are read (one per line, ``#`` comments allowed)
from ``.hearthkeep-personal-markers`` in the repository root. That file is gitignored.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

MARKERS_FILE = ".hearthkeep-personal-markers"
REPO_ROOT = Path(__file__).resolve().parent.parent

RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "abs-path",
        re.compile(
            r"[A-Za-z]:\\Users\\"  # Windows profile folder
            r"|/home/[A-Za-z0-9._-]+/"
            r"|/Users/[A-Za-z0-9._-]+/",
            re.IGNORECASE,
        ),
    ),
    (
        "private-link",
        re.compile(r"claude\.ai/(?:code/)?artifact/", re.IGNORECASE),
    ),
    (
        "token",
        re.compile(
            r"ghp_[A-Za-z0-9]{36}"
            r"|github_pat_[A-Za-z0-9_]{40,}"
            r"|sk-[A-Za-z0-9-]{20,}"
            r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        ),
    ),
]


@dataclass(frozen=True)
class Finding:
    rule: str
    line: int
    excerpt: str


def _excerpt(line: str) -> str:
    line = line.strip()
    return line if len(line) <= 120 else line[:117] + "..."


def scan_text(text: str, extra_markers: list[str]) -> list[Finding]:
    """Return one finding per (rule, line) for everything suspicious in ``text``."""
    markers = [m.strip().lower() for m in extra_markers if m.strip()]
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in RULES:
            if pattern.search(line):
                findings.append(Finding(rule, number, _excerpt(line)))
        lowered = line.lower()
        if any(marker in lowered for marker in markers):
            findings.append(Finding("personal-marker", number, _excerpt(line)))
    return findings


def load_markers(root: Path = REPO_ROOT) -> list[str]:
    path = root / MARKERS_FILE
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _read_file(path: Path) -> str | None:
    """File text, or None if the path is missing or looks binary."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    markers = load_markers()
    total = 0
    for arg in args:
        if arg == "-":
            label, text = "<stdin>", sys.stdin.buffer.read().decode("utf-8", errors="replace")
        else:
            label, text = arg, _read_file(Path(arg))
        if text is None:
            continue
        for f in scan_text(text, markers):
            total += 1
            print(f"{label}:{f.line}: [{f.rule}] {f.excerpt}")
    if total:
        print(f"privacy scan: {total} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    sys.exit(main())
