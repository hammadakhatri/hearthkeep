"""Permission engine: pure decision logic plus per-tool level storage."""

import sqlite3
from enum import StrEnum


class ActionClass(StrEnum):
    LOOK = "look"
    CHANGE_LOCAL = "change_local"
    OUTWARD = "outward"


class ToolLevel(StrEnum):
    ALLOWED = "allowed"
    ASK = "ask"
    OFF = "off"


class Decision(StrEnum):
    RUN = "run"
    ASK = "ask"
    ASK_STEP_UP = "ask_step_up"
    DENY = "deny"


DEFAULT_LEVELS: dict[str, ToolLevel] = {
    "wallet": ToolLevel.ALLOWED,
    "news": ToolLevel.ALLOWED,
    "files": ToolLevel.ALLOWED,
    "python": ToolLevel.ALLOWED,
    "images": ToolLevel.ALLOWED,
    "github": ToolLevel.ASK,
    "demo": ToolLevel.ALLOWED,  # Phase 1 demo tools only; removed with them
}

SANDBOXED_TOOLS = {"python", "github_tests"}

_WIDTH = {ToolLevel.OFF: 0, ToolLevel.ASK: 1, ToolLevel.ALLOWED: 2}


def effective_level(tool: str, configured: ToolLevel, docker_available: bool) -> ToolLevel:
    """Sandboxed tools never run unprompted when no sandbox is available."""
    if tool in SANDBOXED_TOOLS and not docker_available and configured == ToolLevel.ALLOWED:
        return ToolLevel.ASK
    return configured


def decide(level: ToolLevel, action: ActionClass, granted_for_chat: bool) -> Decision:
    """Pure decision: no I/O. Outward actions always need a fresh step-up approval."""
    if level == ToolLevel.OFF:
        return Decision.DENY
    if action == ActionClass.OUTWARD:
        return Decision.ASK_STEP_UP
    if level == ToolLevel.ASK:
        return Decision.ASK
    if action == ActionClass.LOOK:
        return Decision.RUN
    return Decision.RUN if granted_for_chat else Decision.ASK


def is_widening(old: ToolLevel, new: ToolLevel) -> bool:
    return _WIDTH[new] > _WIDTH[old]


def get_level(conn: sqlite3.Connection, tool: str) -> ToolLevel:
    row = conn.execute("SELECT level FROM tool_levels WHERE tool = ?", (tool,)).fetchone()
    if row is not None:
        try:
            return ToolLevel(row[0])
        except ValueError:
            return ToolLevel.ASK  # corrupt value: never crash, never widen
    return DEFAULT_LEVELS.get(tool, ToolLevel.ASK)


def set_level(conn: sqlite3.Connection, tool: str, level: ToolLevel) -> None:
    conn.execute(
        "INSERT INTO tool_levels(tool, level) VALUES (?, ?) "
        "ON CONFLICT(tool) DO UPDATE SET level = excluded.level",
        (tool, ToolLevel(level).value),
    )
