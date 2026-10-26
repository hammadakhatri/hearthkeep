"""Background turn runner: one asyncio task per turn, subscribers replay then follow live.

Turns are owned by the runner, not by whoever is listening: a subscriber that goes away (a
closed browser tab, a cancelled SSE stream) never cancels the turn. Events are persisted by the
agent loop before they are broadcast, so `subscribe(after_seq=n)` can resume from the log.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from hearthkeep import audit, db
from hearthkeep.agent.loop import (
    UNEXPECTED,
    AgentLoop,
    answer_orphan_tool_calls,
    load_history,
    record_turn_failure,
    save_message,
)

TERMINAL_EVENTS = frozenset({"turn_done", "turn_error"})
RESTARTED = "This reply was interrupted when Hearthkeep restarted."


def recover_after_restart(conn: sqlite3.Connection) -> None:
    """Startup sweep: no turn survives a restart. Expire every pending approval (nothing is
    left to run it), end every turn whose last event is not terminal with a turn_error, and
    answer the tool calls those turns left open so the chat history stays well-formed."""
    now = db.utcnow()
    with db.transaction(conn):
        expired = conn.execute(
            "UPDATE approvals SET status = 'expired', resolved_at = ? WHERE status = 'pending'",
            (now,),
        ).rowcount
        open_turns = conn.execute(
            "SELECT e.chat_id, e.turn_id, e.seq FROM turn_events e JOIN ("
            "  SELECT chat_id, turn_id, MAX(seq) AS last FROM turn_events"
            "  GROUP BY chat_id, turn_id"
            ") l ON e.chat_id = l.chat_id AND e.turn_id = l.turn_id AND e.seq = l.last"
            " WHERE e.type NOT IN ('turn_done', 'turn_error')"
        ).fetchall()
        for row in open_turns:
            conn.execute(
                "INSERT INTO turn_events (chat_id, turn_id, seq, type, data, created_at)"
                " VALUES (?, ?, ?, 'turn_error', ?, ?)",
                (
                    row["chat_id"],
                    row["turn_id"],
                    row["seq"] + 1,
                    json.dumps({"message": RESTARTED}),
                    now,
                ),
            )
        for chat_id in {row["chat_id"] for row in open_turns}:
            answer_orphan_tool_calls(conn, chat_id)
        if expired or open_turns:
            audit.record(
                conn,
                "system",
                "turns.recovered",
                {"approvals_expired": expired, "turns_closed": len(open_turns)},
            )


class TurnInProgressError(Exception):
    """A turn is already running on this chat; only one turn per chat at a time."""

    def __init__(self, chat_id: str, turn_id: str) -> None:
        super().__init__(f"a turn is already running on chat '{chat_id}' (turn '{turn_id}')")
        self.chat_id = chat_id
        self.turn_id = turn_id


@dataclass
class _Live:
    chat_id: str
    turn_id: str
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()


class TurnRunner:
    def __init__(self, conn: sqlite3.Connection, loop: AgentLoop) -> None:
        self._conn = conn
        self._loop = loop
        self._live: dict[str, _Live] = {}  # turn_id -> running turn
        self._by_chat: dict[str, str] = {}  # chat_id -> running turn_id

    def active_turn(self, chat_id: str) -> str | None:
        return self._by_chat.get(chat_id)

    def start(self, chat_id: str, user_text: str, think: bool) -> str:
        """Persist the user's message and start the turn in the background. Must be called
        from within the running event loop. Raises TurnInProgressError if the chat is busy."""
        running = self._by_chat.get(chat_id)
        if running is not None:
            raise TurnInProgressError(chat_id, running)
        secrets = self._loop.secret_values()
        save_message(self._conn, chat_id, {"role": "user", "content": user_text}, secrets)
        history = load_history(self._conn, chat_id)
        turn_id = uuid.uuid4().hex
        live = _Live(chat_id, turn_id)
        live.task = asyncio.create_task(self._drive(live, history, think))
        # Registered only once the task exists; the task cannot run before we return.
        self._live[turn_id] = live
        self._by_chat[chat_id] = turn_id
        return turn_id

    def cancel(self, turn_id: str) -> bool:
        """Stop a running turn. Returns False if it is not running."""
        live = self._live.get(turn_id)
        if live is None or live.task is None or live.task.done():
            return False
        live.task.cancel()
        return True

    async def shutdown(self) -> None:
        """Cancel every running turn and wait for them to close (server shutdown only)."""
        for live in list(self._live.values()):
            if live.task is not None:
                live.task.cancel()
        await self.wait_idle()

    async def wait_idle(self) -> None:
        """Wait until every running turn has finished (mainly for tests and shutdown)."""
        while self._live:
            tasks = [live.task for live in self._live.values() if live.task is not None]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def subscribe(
        self, chat_id: str, turn_id: str, after_seq: int = 0
    ) -> AsyncIterator[dict]:
        """Persisted events with seq > after_seq, then live ones until the turn ends."""
        last = after_seq
        while True:
            live = self._live.get(turn_id)
            waiter = live.changed if live is not None else None
            rows = self._conn.execute(
                "SELECT seq, type, data FROM turn_events"
                " WHERE chat_id = ? AND turn_id = ? AND seq > ? ORDER BY seq",
                (chat_id, turn_id, last),
            ).fetchall()
            for row in rows:
                event = {"type": row["type"], "seq": row["seq"], **json.loads(row["data"])}
                last = row["seq"]
                yield event
                if event["type"] in TERMINAL_EVENTS:
                    return
            if rows:
                continue  # re-check: more may have arrived while we were yielding
            if waiter is None:
                return  # finished (or unknown) turn, everything replayed
            await waiter.wait()

    async def _drive(self, live: _Live, history: list[dict], think: bool) -> None:
        last_type = None
        try:
            async for event in self._loop.run_turn(live.chat_id, live.turn_id, history, think):
                last_type = event["type"]
                live.notify()
        except Exception as exc:
            last_type = None  # fall through: close the turn below
            record_turn_failure(self._conn, exc)
        finally:
            if last_type not in TERMINAL_EVENTS:
                self._close_unfinished(live)
            self._live.pop(live.turn_id, None)
            if self._by_chat.get(live.chat_id) == live.turn_id:
                del self._by_chat[live.chat_id]
            live.notify()

    def _close_unfinished(self, live: _Live) -> None:
        """A turn that ended without a terminal event (crashed or cancelled): expire its
        pending approvals and persist a last-resort turn_error so subscribers never wait
        forever."""
        try:
            now = db.utcnow()
            self._conn.execute(
                "UPDATE approvals SET status = 'expired', resolved_at = ?"
                " WHERE chat_id = ? AND turn_id = ? AND status = 'pending'",
                (now, live.chat_id, live.turn_id),
            )
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM turn_events WHERE chat_id = ? AND turn_id = ?",
                (live.chat_id, live.turn_id),
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO turn_events (chat_id, turn_id, seq, type, data, created_at)"
                " VALUES (?, ?, ?, 'turn_error', ?, ?)",
                (live.chat_id, live.turn_id, seq + 1, json.dumps({"message": UNEXPECTED}), now),
            )
        except Exception as exc:
            record_turn_failure(self._conn, exc)
