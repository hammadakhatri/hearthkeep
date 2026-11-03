"""Copy rules shared by the copy tests: no CLI command and no path placeholder in any
owner-visible or model-visible text. Copy may start with the product name, so only the
commands themselves are forbidden, not the word "Hearthkeep"."""

from __future__ import annotations

import re

FORBIDDEN = (
    re.compile(r"\bhearthkeep (serve|sandbox|secrets|startup|enroll-code)\b", re.IGNORECASE),
    re.compile(r"<data>|<home>|--data\b|\buv run\b"),
)


def forbidden_in(text: str) -> list[str]:
    """The forbidden pieces found in `text` (empty when it follows the rules)."""
    return [m.group(0) for pattern in FORBIDDEN for m in pattern.finditer(text)]
