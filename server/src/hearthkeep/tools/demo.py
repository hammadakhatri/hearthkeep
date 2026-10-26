"""Phase 1 demo tools, exercising the RUN and ASK paths end to end. Replaced in Phase 2."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from hearthkeep.permissions import ActionClass
from hearthkeep.tools.base import ToolContext, ToolResult

DEMO_LEVEL_KEY = "demo"
NOTE_MAX_CHARS = 2000


class ClockTool:
    name = "clock"
    level_key = DEMO_LEVEL_KEY
    description = "Get the current local date and time."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        return "Check the time"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            ok=True, content=datetime.now().astimezone().isoformat(timespec="seconds")
        )


class NoteTool:
    name = "note"
    level_key = DEMO_LEVEL_KEY
    description = "Save a short note for the owner to read later."
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": NOTE_MAX_CHARS,
                "description": "The note to save.",
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.CHANGE_LOCAL

    def summarize(self, args: dict) -> str:
        text = _one_line(args.get("text", ""))
        return f"Save a note: {text[:80]}{'...' if len(text) > 80 else ''}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        path = Path(ctx.settings.data_dir) / "workspace" / "output" / "notes.txt"
        await asyncio.to_thread(_append_line, path, _one_line(args["text"]))
        return ToolResult(ok=True, content="Note saved.")


def _one_line(text: str) -> str:
    return " ".join(str(text).splitlines()).strip()


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
