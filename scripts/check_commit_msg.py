"""Commit-message guard: no co-author trailers and no AI attribution.

Usage:
    python scripts/check_commit_msg.py <message-file>     (commit-msg hook)
    git log --format=%B <range> | python scripts/check_commit_msg.py -    (CI)

Exits 1 if any forbidden text is present, 0 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN: list[tuple[str, re.Pattern[str]]] = [
    ("Co-Authored-By trailer", re.compile(r"co-authored[\s-]*by", re.IGNORECASE)),
    (
        "AI attribution line",
        # "Generated with/by ... Claude", also across a markdown link or line break
        re.compile(r"generated\s+(?:with|by)\b.{0,120}?claude", re.IGNORECASE | re.DOTALL),
    ),
    ("AI vendor noreply address", re.compile(r"noreply@anthropic\.com", re.IGNORECASE)),
]


def find_violations(message: str) -> list[str]:
    return [name for name, pattern in FORBIDDEN if pattern.search(message)]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_commit_msg.py <message-file | ->", file=sys.stderr)
        return 2
    if args[0] == "-":
        message = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    else:
        message = Path(args[0]).read_text(encoding="utf-8", errors="replace")
    violations = find_violations(message)
    for name in violations:
        print(f"commit message rejected: contains {name} (see CONTRIBUTING.md)", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
