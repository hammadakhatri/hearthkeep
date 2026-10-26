"""Privacy scanner: fail if text contains personal markers, private links or secrets.

Usage:
    python scripts/privacy_scan.py [paths...]
    python scripts/privacy_scan.py --staged        (scan the staged content, for hooks)
    git log -p | python scripts/privacy_scan.py -

A path of ``-`` reads standard input. Exits 1 if anything is found, 0 otherwise.
Extra, owner-specific identifiers are read (one per line, ``#`` comments allowed)
from ``.hearthkeep-personal-markers`` in the repository root. That file is gitignored.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MARKERS_FILE = ".hearthkeep-personal-markers"
REPO_ROOT = Path(__file__).resolve().parent.parent

RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "abs-path",
        re.compile(
            # Windows profile folder, with one or more backslashes (escaped source) or slashes
            r"[A-Za-z]:[\\/]+Users[\\/]+"
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
            r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9-]{20,}"
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
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def decode_bytes(data: bytes) -> str | None:
    """Decode file content to text, or None if it is a real binary.

    Handles UTF-8 (with or without BOM) and UTF-16 (BOM, or BOM-less with the
    tell-tale alternating NUL bytes, as written by PowerShell 5 redirection).
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    sample = data[:8192]
    if b"\x00" not in sample:
        return data.decode("utf-8-sig", errors="replace")
    even, odd = sample[0::2], sample[1::2]
    if odd and odd.count(0) > 0.8 * len(odd) and even.count(0) < 0.1 * len(even):
        return data.decode("utf-16-le", errors="replace")
    if even and even.count(0) > 0.8 * len(even) and odd.count(0) < 0.1 * len(odd):
        return data.decode("utf-16-be", errors="replace")
    return None


def _report(label: str, text: str | None, markers: list[str]) -> int:
    if text is None:
        print(f"privacy scan: skipped binary file {label}", file=sys.stderr)
        return 0
    findings = scan_text(text, markers)
    for f in findings:
        print(f"{label}:{f.line}: [{f.rule}] {f.excerpt}")
    return len(findings)


def _staged_files() -> list[tuple[str, bytes]]:
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    files = []
    for raw in filter(None, names):
        name = raw.decode("utf-8", errors="replace")
        blob = subprocess.run(["git", "show", ":" + name], capture_output=True, check=True)
        files.append((name, blob.stdout))
    return files


def main(argv: list[str] | None = None, root: Path = REPO_ROOT) -> int:
    args = sys.argv[1:] if argv is None else argv
    markers = load_markers(root)
    if (root / MARKERS_FILE).is_file() and not markers:
        print(f"privacy scan: warning: {MARKERS_FILE} exists but has no markers", file=sys.stderr)
    total = 0
    for arg in args:
        if arg == "--staged":
            for name, data in _staged_files():
                total += _report(name, decode_bytes(data), markers)
        elif arg == "-":
            total += _report("<stdin>", decode_bytes(sys.stdin.buffer.read()), markers)
        else:
            try:
                data = Path(arg).read_bytes()
            except OSError:
                continue  # missing (e.g. deleted) or unreadable: nothing to scan
            total += _report(arg, decode_bytes(data), markers)
    if total:
        print(f"privacy scan: {total} finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    sys.exit(main())
