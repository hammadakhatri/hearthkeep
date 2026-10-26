"""The agent loop: model turns, permission-gated tool calls, persisted events.

Every tool call goes through `permissions.decide`. The model cannot resolve approvals, change
levels or read secrets: approvals are resolved only by an owner session (ApprovalStore), and
any stored secret value is replaced with `[redacted]` before text reaches the model, an approval
request, the event log or the message history.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from hearthkeep import audit, db
from hearthkeep.approvals import ApprovalStore
from hearthkeep.config import Settings, detect_docker
from hearthkeep.permissions import (
    ActionClass,
    Decision,
    ToolLevel,
    decide,
    effective_level,
    get_level,
)
from hearthkeep.providers.chat import (
    ProviderError,
    ReasoningDelta,
    TextDelta,
    ToolCallRequest,
)
from hearthkeep.secrets import SecretStore, SecretStoreError
from hearthkeep.tools.base import Tool, ToolContext, ToolRegistry, ToolResult

SYSTEM_PROMPT = (
    "You are Hearthkeep, a private assistant running on its owner's own computer. "
    "Answer helpfully, clearly and concisely. Use the tools you are offered when they help. "
    "Some actions need the owner's approval first; if the owner declines, a request expires or "
    "a tool reports an error, say so plainly instead of retrying the same action."
)

REDACTED = "[redacted]"
MIN_SECRET_LENGTH = 4
PREVIEW_CHARS = 200

DECLINED = "The owner declined this action"
EXPIRED = "The request expired"
TURNED_OFF = "This tool is turned off"
STEP_LIMIT_CALL = "The step limit for this turn was reached, so this call was not run."
STEP_LIMIT_MESSAGE = (
    "I stopped because I hit the step limit for one turn ({limit} tool steps). "
    "Ask me to continue if you want me to keep going."
)
SECRETS_UNREADABLE = (
    "Hearthkeep couldn't read its secret store, so it stopped this turn to keep your secrets safe."
)
UNEXPECTED = "Something went wrong while answering. Please try again."
INVALID_CALL_SUMMARY = "Invalid tool call"
INTERRUPTED = json.dumps({"error": "This step was interrupted"})
_ATTACHMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")

_log = logging.getLogger(__name__)


def redact(value: Any, secrets: list[str]) -> Any:
    """Replace every occurrence of a secret value inside strings (recursing into dicts/lists)."""
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    return value


def load_history(conn: sqlite3.Connection, chat_id: str) -> list[dict]:
    """The chat's persisted messages in OpenAI chat format, oldest first."""
    rows = conn.execute(
        "SELECT role, content, tool_calls, tool_call_id FROM messages"
        " WHERE chat_id = ? ORDER BY id",
        (chat_id,),
    ).fetchall()
    history: list[dict] = []
    unanswered: list[str] = []  # tool_call ids of the latest assistant message

    def answer_rest() -> None:
        history.extend(
            {"role": "tool", "tool_call_id": i, "content": INTERRUPTED} for i in unanswered
        )
        unanswered.clear()

    for row in rows:
        message: dict = {"role": row["role"], "content": row["content"] or ""}
        if row["tool_call_id"]:
            message["tool_call_id"] = row["tool_call_id"]
        if message["role"] == "tool":
            if message.get("tool_call_id") in unanswered:
                unanswered.remove(message["tool_call_id"])
            history.append(message)
            continue
        answer_rest()  # every tool call is answered before the next non-tool message
        if row["tool_calls"]:
            message["tool_calls"] = json.loads(row["tool_calls"])
            unanswered.extend(c.get("id") for c in message["tool_calls"] if c.get("id"))
        history.append(message)
    answer_rest()
    return history


def answer_orphan_tool_calls(conn: sqlite3.Connection, chat_id: str) -> int:
    """Persist the repair `load_history` applies in memory for the chat's trailing tool
    calls: every call of the latest assistant message that has no answer yet gets an
    INTERRUPTED tool message. Earlier gaps are left to `load_history`. Returns the count."""
    rows = conn.execute(
        "SELECT role, tool_calls, tool_call_id FROM messages WHERE chat_id = ? ORDER BY id",
        (chat_id,),
    ).fetchall()
    unanswered: list[str] = []
    for row in rows:
        if row["role"] == "tool":
            if row["tool_call_id"] in unanswered:
                unanswered.remove(row["tool_call_id"])
            continue
        calls = json.loads(row["tool_calls"]) if row["tool_calls"] else []
        unanswered = [c.get("id") for c in calls if c.get("id")]
    for call_id in unanswered:
        save_message(
            conn, chat_id, {"role": "tool", "tool_call_id": call_id, "content": INTERRUPTED}, []
        )
    return len(unanswered)


def record_turn_failure(conn: sqlite3.Connection, exc: BaseException) -> None:
    """Audit a turn-level failure. Only the exception type: its text may carry secrets."""
    try:
        audit.record(conn, "agent", "turn.failed", {"error_type": type(exc).__name__})
    except Exception as audit_exc:
        _log.error(
            "turn failed (%s) and could not be audited (%s)",
            type(exc).__name__,
            type(audit_exc).__name__,
        )


def save_message(conn: sqlite3.Connection, chat_id: str, message: dict, secrets: list[str]) -> None:
    message = redact(message, secrets)
    tool_calls = message.get("tool_calls")
    conn.execute(
        "INSERT INTO messages (chat_id, role, content, tool_calls, tool_call_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            chat_id,
            message["role"],
            message.get("content") or "",
            json.dumps(tool_calls) if tool_calls else None,
            message.get("tool_call_id"),
            db.utcnow(),
        ),
    )


def _error(message: str) -> str:
    return json.dumps({"error": message})


@dataclass
class _Turn:
    chat_id: str
    turn_id: str
    secrets: list[str] = field(default_factory=list)
    seq: int = 0


@dataclass
class _Call:
    id: str
    name: str
    raw_arguments: str
    history_arguments: str  # always a JSON object, safe to replay to the model


@dataclass
class _Outcome:
    content: str = ""
    attachments: list[dict] = field(default_factory=list)  # checked items to show in the chat


class AgentLoop:
    def __init__(
        self,
        client,
        registry: ToolRegistry,
        approvals: ApprovalStore,
        conn: sqlite3.Connection,
        settings: Settings,
        secrets: SecretStore | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._approvals = approvals
        self._conn = conn
        self._settings = settings
        self._secrets = secrets or SecretStore(settings.data_dir)
        self._docker = (
            settings.docker_available if settings.docker_available is not None else detect_docker()
        )

    # -- public -------------------------------------------------------------------------

    async def run_turn(
        self, chat_id: str, turn_id: str, history: list[dict], think: bool
    ) -> AsyncIterator[dict]:
        """Run one turn. Events are persisted to `turn_events` before being yielded; new
        assistant and tool messages are persisted to `messages`. Never raises for model,
        tool or argument failures: the turn ends with `turn_done` or `turn_error`."""
        turn = _Turn(chat_id, turn_id)
        try:
            try:
                turn.secrets = self.secret_values()
            except SecretStoreError:
                yield self._emit(turn, "turn_error", message=SECRETS_UNREADABLE)
                return
            # Closed explicitly so an early stop reaches the provider stream right away.
            async with contextlib.aclosing(self._turn(turn, history, think)) as events:
                async for event in events:
                    yield event
        except Exception as exc:
            record_turn_failure(self._conn, exc)
            yield self._emit(turn, "turn_error", message=UNEXPECTED)

    def secret_values(self) -> list[str]:
        """Current stored secret values worth redacting, longest first."""
        values = {
            v
            for name in self._secrets.names()
            if (v := self._secrets.get(name)) and len(v) >= MIN_SECRET_LENGTH
        }
        return sorted(values, key=len, reverse=True)

    # -- turn ---------------------------------------------------------------------------

    async def _turn(self, turn: _Turn, history: list[dict], think: bool) -> AsyncIterator[dict]:
        limit = self._settings.max_agent_steps
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *redact(history, turn.secrets)]
        steps = 0
        while True:
            tools = self._registry.available(self._conn, self._docker)
            text_parts: list[str] = []
            requests: list[ToolCallRequest] = []
            try:
                # aclosing: a turn that ends early (or a client that goes away) closes the
                # provider stream right here, releasing any GPU lease it holds.
                async with contextlib.aclosing(
                    self._client.stream(messages, self._registry.schemas(tools), think)
                ) as stream:
                    async for event in stream:
                        if isinstance(event, TextDelta):
                            text_parts.append(event.text)
                            yield self._emit(turn, "text_delta", text=event.text)
                        elif isinstance(event, ReasoningDelta):
                            yield self._emit(turn, "reasoning_delta", text=event.text)
                        elif isinstance(event, ToolCallRequest):
                            requests.append(event)
            except ProviderError as exc:
                if text_parts:
                    self._save(turn, {"role": "assistant", "content": "".join(text_parts)})
                yield self._emit(turn, "turn_error", message=exc.user_message)
                return

            text = "".join(text_parts)
            if not requests:
                if text:
                    self._save(turn, {"role": "assistant", "content": text})
                yield self._emit(turn, "turn_done")
                return

            calls = _normalise(requests)
            assistant = redact(
                {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.history_arguments},
                        }
                        for c in calls
                    ],
                },
                turn.secrets,
            )
            messages.append(assistant)
            self._save(turn, assistant)

            hit_limit = False
            answered = 0
            try:
                for call in calls:
                    # Deliberate: `steps` counts tool calls, not model rounds, so one round
                    # asking for many calls cannot run past max_agent_steps.
                    if steps >= limit:
                        hit_limit = True
                        content = _error(STEP_LIMIT_CALL)
                    else:
                        steps += 1
                        outcome = _Outcome()
                        async with contextlib.aclosing(
                            self._handle_call(turn, call, outcome)
                        ) as events:
                            async for event in events:
                                yield event
                        content = outcome.content
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": redact(content, turn.secrets),
                    }
                    messages.append(tool_message)
                    self._save(turn, tool_message)
                    answered += 1
            finally:
                # Cancelled or failed mid-round: answer the rest so the persisted history
                # stays well-formed. Synchronous on purpose (safe during cancellation).
                for call in calls[answered:]:
                    self._save(
                        turn, {"role": "tool", "tool_call_id": call.id, "content": INTERRUPTED}
                    )

            if hit_limit:
                final = STEP_LIMIT_MESSAGE.format(limit=limit)
                yield self._emit(turn, "text_delta", text=final)
                self._save(turn, {"role": "assistant", "content": final})
                yield self._emit(turn, "turn_done")
                return

    # -- one tool call ------------------------------------------------------------------

    async def _handle_call(
        self, turn: _Turn, call: _Call, outcome: _Outcome
    ) -> AsyncIterator[dict]:
        step_id = uuid.uuid4().hex
        tool, args, problem = self._parse(call)
        if problem is not None:
            outcome.content = _error(problem)
            yield self._emit(
                turn, "step_started", step_id=step_id, tool=call.name, summary=INVALID_CALL_SUMMARY
            )
            yield self._step_finished(turn, step_id, False, outcome.content)
            return

        try:
            summary = str(tool.summarize(args))
        except Exception:
            summary = f"Use {tool.name}"
        yield self._emit(turn, "step_started", step_id=step_id, tool=tool.name, summary=summary)

        try:
            action = ActionClass(tool.classify(args))
        except Exception as exc:  # fail closed: an unclassifiable call never runs
            ok, outcome.content = self._failed(tool, None, exc)
            yield self._step_finished(turn, step_id, ok, outcome.content)
            return

        decision = self._decide(turn, tool, action)
        if decision == Decision.RUN:
            ok = await self._run(turn, tool, args, decision, outcome)
        elif decision in (Decision.ASK, Decision.ASK_STEP_UP):
            approval = self._approvals.create(
                turn.chat_id,
                turn.turn_id,
                tool.name,
                action,
                redact(summary, turn.secrets),
                redact({"tool": tool.name, "arguments": args}, turn.secrets),
                requires_step_up=decision == Decision.ASK_STEP_UP,
            )
            yield self._emit(
                turn,
                "approval_required",
                approval_id=approval.id,
                tool=tool.name,
                summary=approval.summary,
                detail=approval.detail,
                requires_step_up=approval.requires_step_up,
            )
            resolved = await self._approvals.wait(approval.id)
            yield self._emit(
                turn, "approval_resolved", approval_id=approval.id, status=resolved.status
            )
            if resolved.status == "approved":
                level = self._level(tool)  # re-read: the owner may have changed it meanwhile
                if level == ToolLevel.OFF:
                    ok, outcome.content = self._deny(tool)
                else:
                    if action == ActionClass.CHANGE_LOCAL and level == ToolLevel.ALLOWED:
                        self._conn.execute(
                            "INSERT OR IGNORE INTO chat_grants (chat_id, tool) VALUES (?, ?)",
                            (turn.chat_id, tool.level_key),
                        )
                    ok = await self._run(turn, tool, args, decision, outcome)
            elif resolved.status == "declined":
                ok, outcome.content = False, _error(DECLINED)
            else:
                ok, outcome.content = False, _error(EXPIRED)
        else:
            ok, outcome.content = self._deny(tool)
        if outcome.attachments:
            yield self._emit(turn, "attachments", step_id=step_id, items=outcome.attachments)
        yield self._step_finished(turn, step_id, ok, outcome.content)

    def _parse(self, call: _Call) -> tuple[Tool | None, dict, str | None]:
        if not call.name:
            return None, {}, "The tool call did not name a tool."
        tool = self._registry.get(call.name)
        if tool is None:
            return None, {}, f"Unknown tool: '{call.name}'."
        try:
            args = json.loads(call.raw_arguments.strip() or "{}")
        except ValueError:
            return None, {}, f"The arguments for '{call.name}' were not valid JSON."
        if not isinstance(args, dict):
            return None, {}, f"The arguments for '{call.name}' must be a JSON object."
        try:
            jsonschema.validate(args, tool.parameters)
        except jsonschema.ValidationError as exc:
            return None, {}, f"Invalid arguments for '{call.name}': {exc.message}"
        except jsonschema.SchemaError:
            return None, {}, f"The tool '{call.name}' is misconfigured and cannot be used."
        return tool, args, None

    def _level(self, tool: Tool) -> ToolLevel:
        return effective_level(tool.level_key, get_level(self._conn, tool.level_key), self._docker)

    def _decide(self, turn: _Turn, tool: Tool, action: ActionClass) -> Decision:
        level = self._level(tool)
        granted = (
            self._conn.execute(
                "SELECT 1 FROM chat_grants WHERE chat_id = ? AND tool = ?",
                (turn.chat_id, tool.level_key),
            ).fetchone()
            is not None
        )
        return decide(level, action, granted)

    def _deny(self, tool: Tool) -> tuple[bool, str]:
        audit.record(
            self._conn,
            "agent",
            "tool.denied",
            {"tool": tool.name, "level_key": tool.level_key, "decision": Decision.DENY.value},
        )
        return False, _error(TURNED_OFF)

    def _failed(self, tool: Tool, decision: Decision | None, exc: Exception) -> tuple[bool, str]:
        # Only the exception type: its message might carry secrets or private data.
        audit.record(
            self._conn,
            "agent",
            "tool.failed",
            {
                "tool": tool.name,
                "level_key": tool.level_key,
                "decision": decision.value if decision else None,
                "ok": False,
                "error_type": type(exc).__name__,
            },
        )
        return False, _error(f"The tool failed: {type(exc).__name__}")

    async def _run(
        self, turn: _Turn, tool: Tool, args: dict, decision: Decision, outcome: _Outcome
    ) -> bool:
        """Run the tool, filling `outcome`. Only `result.content` ever reaches the model;
        `result.data` attachments become an `attachments` event after they are checked."""
        ctx = ToolContext(turn.chat_id, turn.turn_id, self._settings, self._conn)
        try:
            result = await tool.run(args, ctx)
            if not isinstance(result, ToolResult):
                raise TypeError("tool returned something other than a ToolResult")
        except Exception as exc:
            ok, outcome.content = self._failed(tool, decision, exc)
            return ok
        outcome.content = str(result.content)
        outcome.attachments = self._attachments(turn, result.data)
        audit.record(
            self._conn,
            "agent",
            "tool.run",
            {
                "tool": tool.name,
                "level_key": tool.level_key,
                "decision": decision.value,
                "ok": bool(result.ok),
            },
        )
        return bool(result.ok)

    def _attachments(self, turn: _Turn, data: object) -> list[dict]:
        """The valid attachment items of a tool result: images of this chat, each once.
        Anything else is dropped silently."""
        items = data.get("attachments") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        checked: list[dict] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or item.get("kind") != "image":
                continue
            image_id = item.get("id")
            if not isinstance(image_id, str) or not _ATTACHMENT_ID.fullmatch(image_id):
                continue
            if image_id in seen:
                continue
            exists = self._conn.execute(
                "SELECT 1 FROM images WHERE id = ? AND chat_id = ?", (image_id, turn.chat_id)
            ).fetchone()
            if exists is None:
                continue
            seen.add(image_id)
            checked.append({"kind": "image", "id": image_id})
        return checked

    # -- persistence --------------------------------------------------------------------

    def _step_finished(self, turn: _Turn, step_id: str, ok: bool, content: str) -> dict:
        preview = redact(content, turn.secrets)[:PREVIEW_CHARS]
        return self._emit(turn, "step_finished", step_id=step_id, ok=ok, preview=preview)

    def _emit(self, turn: _Turn, type_: str, **data: Any) -> dict:
        turn.seq += 1
        data = redact(data, turn.secrets)
        self._conn.execute(
            "INSERT INTO turn_events (chat_id, turn_id, seq, type, data, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (turn.chat_id, turn.turn_id, turn.seq, type_, json.dumps(data), db.utcnow()),
        )
        return {"type": type_, "seq": turn.seq, **data}

    def _save(self, turn: _Turn, message: dict) -> None:
        save_message(self._conn, turn.chat_id, message, turn.secrets)


def _normalise(requests: list[ToolCallRequest]) -> list[_Call]:
    calls: list[_Call] = []
    seen: set[str] = set()
    for req in requests:
        call_id = req.id if req.id and req.id not in seen else f"call_{uuid.uuid4().hex[:16]}"
        seen.add(call_id)
        raw = req.arguments_json or ""
        try:
            parsed = json.loads(raw.strip() or "{}")
            history_args = raw if isinstance(parsed, dict) and raw.strip() else "{}"
        except ValueError:
            history_args = "{}"
        calls.append(_Call(call_id, req.name or "", raw, history_args))
    return calls
