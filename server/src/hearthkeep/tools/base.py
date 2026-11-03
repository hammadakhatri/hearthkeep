"""Tool framework: the Tool protocol and the registry the agent loop draws from."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from hearthkeep.config import Settings
from hearthkeep.permissions import ActionClass, ToolLevel, effective_level, get_level


@dataclass
class ToolResult:
    ok: bool
    content: str
    data: dict | None = None


@dataclass
class ToolContext:
    chat_id: str
    turn_id: str
    settings: Settings
    conn: sqlite3.Connection
    # What the owner was shown when they approved this call (the tool's `approval_detail`,
    # redacted); None when the call needed no approval or the tool adds no detail.
    approved_detail: dict | None = None
    # True only for the run that follows an approved card that needed a passkey step-up
    # (an Outward action); False after a plain card and for every run without a card.
    approved_step_up: bool = False


class AddonGate(Protocol):
    """Says whether the add-ons a tool needs are ready (`Addons` is one)."""

    def tool_ready(self, tool_name: str) -> bool: ...


@runtime_checkable
class Tool(Protocol):
    name: str
    level_key: str  # permission key; several tools can share one
    description: str
    parameters: dict  # JSON Schema for the arguments object

    def classify(self, args: dict) -> ActionClass: ...

    def summarize(self, args: dict) -> str: ...

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...

    # Optional, looked up with getattr so tools without them keep working:
    #   async def approval_detail(self, args: dict, ctx: ToolContext) -> dict | None
    #       extra fields (a diff, a head sha) for the approval card, built before it is shown;
    #   def is_available(self) -> bool
    #       False while the tool cannot work (a missing program or token); it is then not offered.


def _is_available(tool: Tool) -> bool:
    check = getattr(tool, "is_available", None)
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception:  # a tool that cannot say it is available is not offered
        return False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.addons: AddonGate | None = None  # set by the app; None means no gate

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        """Every registered tool, in registration order."""
        return list(self._tools.values())

    def is_available(self, tool: Tool) -> bool:
        """The tool's own check, and its add-on being ready when a gate is set. Read at every
        use, so a token removed after the tools were offered is seen at call time. A gate that
        raises counts as not available."""
        if not _is_available(tool):
            return False
        if self.addons is None:
            return True
        try:
            return bool(self.addons.tool_ready(tool.name))
        except Exception:
            return False

    def available(self, conn: sqlite3.Connection, docker_available: bool) -> list[Tool]:
        """Registered tools whose effective level is not OFF and that say they are available
        (a tool without `is_available` always is), in registration order."""
        return [
            t
            for t in self._tools.values()
            if effective_level(t.level_key, get_level(conn, t.level_key), docker_available)
            != ToolLevel.OFF
            and self.is_available(t)
        ]

    def schemas(self, tools: list[Tool]) -> list[dict]:
        """OpenAI `tools` entries for the given tools."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
