"""Unified diffs as the model and the approval card see them: never longer than 200 KB."""

from __future__ import annotations

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
