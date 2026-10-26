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


@runtime_checkable
class Tool(Protocol):
    name: str
    level_key: str  # permission key; several tools can share one
    description: str
    parameters: dict  # JSON Schema for the arguments object

    def classify(self, args: dict) -> ActionClass: ...

    def summarize(self, args: dict) -> str: ...

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def available(self, conn: sqlite3.Connection, docker_available: bool) -> list[Tool]:
        """Registered tools whose effective level is not OFF, in registration order."""
        return [
            t
            for t in self._tools.values()
            if effective_level(t.level_key, get_level(conn, t.level_key), docker_available)
            != ToolLevel.OFF
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
