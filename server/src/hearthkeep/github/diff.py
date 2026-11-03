"""Unified diffs as the model and the approval card see them: never longer than 200 KB."""

from __future__ import annotations

import difflib

DIFF_CAP = 200 * 1024
CUT_NOTE = "… diff cut at 200 KB"


def cap_diff(text: str) -> str:
    """`text` as is when it fits `DIFF_CAP` bytes, else its first `DIFF_CAP` bytes (never
    half a character) followed by a line saying it was cut."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= DIFF_CAP:
        return text
    kept = raw[:DIFF_CAP].decode("utf-8", "ignore")
    return kept + ("" if kept.endswith("\n") else "\n") + CUT_NOTE


def file_diff(rel: str, old: str | None, new: str | None) -> str:
    """A unified diff of one file: `old` and `new` are its text before and after, None when
    it does not exist (added or deleted). Empty when nothing differs."""

    def lines(text: str | None) -> list[str]:
        out = (text or "").splitlines(keepends=True)
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n\\ No newline at end of file\n"
        return out

    if old == new:
        return ""
    return "".join(
        difflib.unified_diff(
            lines(old),
            lines(new),
            fromfile="/dev/null" if old is None else f"a/{rel}",
            tofile="/dev/null" if new is None else f"b/{rel}",
        )
    )
