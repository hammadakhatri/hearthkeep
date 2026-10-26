"""Commit-message guard: no co-author trailers, no AI attribution and no personal data.

Personal data uses the privacy scanner's rules (absolute user paths, private links, tokens)
and the owner's markers file. Git comment lines and a verbose diff are not scanned for it.

Usage:
    python scripts/check_commit_msg.py <message-file>     (commit-msg hook)
    git log --format=%B <range> | python scripts/check_commit_msg.py -    (CI)

Exits 1 if any forbidden text is present, 0 otherwise.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

from privacy_scan import REPO_ROOT, read_markers, scan_text, warn_markers_problem

FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    ("Co-Authored-By trailer", re.compile(r"co-authored[\s-]*by", re.IGNORECASE)),
    (
        "AI attribution line",
        # "Generated with/by ... Claude", also across a markdown link or line break
        re.compile(r"generated\s+(?:with|by)\b.{0,120}?claude", re.IGNORECASE | re.DOTALL),
    ),
    ("AI vendor noreply address", re.compile(r"noreply@anthropic\.com", re.IGNORECASE)),
]


SCISSORS = "# ------------------------ >8 ------------------------"
PERSONAL_RULES = {
    "abs-path": "an absolute user path",
    "private-link": "a private link",
    "token": "a token or private key",
    "personal-marker": "a personal marker",
}


def _message_body(message: str) -> str:
    """The text git keeps: no comment lines, nothing below the verbose-diff scissors."""
    message = message.split(SCISSORS, 1)[0]
    return "\n".join(ln for ln in message.splitlines() if not ln.startswith("#"))


def find_violations(message: str, markers: Iterable[str] = ()) -> list[str]:
    found = [name for name, pattern in FORBIDDEN if pattern.search(message)]
    rules = {f.rule for f in scan_text(_message_body(message), list(markers))}
    # Names only: never echo the matched text (it may be the personal data itself).
    found += [label for rule, label in PERSONAL_RULES.items() if rule in rules]
    return found


def main(argv: list[str] | None = None, root: Path = REPO_ROOT) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_commit_msg.py <message-file | ->", file=sys.stderr)
        return 2
    if args[0] == "-":
        message = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    else:
        message = Path(args[0]).read_text(encoding="utf-8", errors="replace")
    markers, problem = read_markers(root)
    if problem:
        warn_markers_problem(problem)
        print("commit message rejected: fix the markers file (or delete it)", file=sys.stderr)
        return 1
    violations = find_violations(message, markers)
    for name in violations:
        print(f"commit message rejected: contains {name} (see CONTRIBUTING.md)", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
