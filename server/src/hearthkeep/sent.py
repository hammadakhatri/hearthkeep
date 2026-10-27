"""Things sent to the owner's devices: a chart, table or file of a chat, or a summary. Each
item has its targets (passkey credentials); each device sees only its own "Sent to you" tray.

A device with push turned on is told only that something is ready: the push carries nothing
at all (see `push`). Titles, names and text never reach a push, the audit log or the logs; the
audit records the item's id, kind and its target credential ids.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Literal

from hearthkeep import audit, db
from hearthkeep.auth.credentials import CredentialStore
from hearthkeep.files.store import ChatFileStore
from hearthkeep.images import UPLOAD_ID
from hearthkeep.numbercheck import NumberChecker, web_marks
from hearthkeep.push import PushSender, PushSubscriptions
from hearthkeep.sandbox.runs import RunStore

Kind = Literal["chart", "table", "file", "summary"]
KINDS: tuple[Kind, ...] = ("chart", "table", "file", "summary")
ITEM_ID = UPLOAD_ID
MAX_SUMMARY_CHARS = 4000
SUMMARY_TITLE = "Summary"
INBOX_LIMIT = 50
# At shutdown, pushes still going get this long to finish before they are cancelled.
SHUTDOWN_GRACE_SECONDS = 1.0
_COLUMNS = "i.id, i.chat_id, i.kind, i.ref_id, i.title, i.text, i.marks, i.created_at"

_log = logging.getLogger(__name__)


class SendRefused(ValueError):
    """Nothing was sent. `reason`: not_found (the reference isn't that kind of thing in that
    chat), unknown_device, no_text or too_long. The message never holds a name or title."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class SentItem:
    id: str
    chat_id: str
    kind: Kind
    ref_id: str | None
    title: str
    text: str | None
    marks: list[dict] | None
    created_at: str
    dismissed: bool = False


def _item(row: sqlite3.Row, dismissed: bool = False) -> SentItem:
    return SentItem(
        id=row["id"],
        chat_id=row["chat_id"],
        kind=row["kind"],
        ref_id=row["ref_id"],
        title=row["title"] or "",
        text=row["text"],
        marks=json.loads(row["marks"]) if row["marks"] else None,
        created_at=row["created_at"],
        dismissed=dismissed,
    )


def _summary_text(text: object) -> str:
    if not isinstance(text, str) or not text.strip():
        raise SendRefused("no_text")
    if len(text) > MAX_SUMMARY_CHARS:
        raise SendRefused("too_long")
    return text


def question_before(conn: sqlite3.Connection, chat_id: str, message_id: int | None) -> str:
    """The owner's last message in the chat (before `message_id` when given): its numbers are
    exempt from the number check, as they were for the reply."""
    sql = "SELECT content FROM messages WHERE chat_id = ? AND role = 'user'"
    params: list = [chat_id]
    if message_id is not None:
        sql += " AND id < ?"
        params.append(int(message_id))
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return (row["content"] or "") if row is not None else ""


class SentStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        runs: RunStore,
        files: ChatFileStore,
        credentials: CredentialStore,
        checker: NumberChecker,
        push: PushSender | None,
        subscriptions: PushSubscriptions | None,
    ) -> None:
        self._conn = conn
        self._runs = runs
        self._files = files
        self._credentials = credentials
        self._checker = checker
        self._push = push
        self._subscriptions = subscriptions
        self._tasks: set[asyncio.Task] = set()
        self._closing = False

    # ---- sending ---------------------------------------------------------------------------

    def _title(self, chat_id: str, kind: Kind, ref_id: str | None) -> str:
        """The thing's title, after checking it is an output (or for `file` also a chat file)
        of this chat of this kind; SendRefused("not_found") otherwise."""
        if kind == "summary":
            return SUMMARY_TITLE
        if kind not in KINDS or not isinstance(ref_id, str):
            raise SendRefused("not_found")
        out = self._runs.output(ref_id)
        if out is not None and out.chat_id == chat_id and out.kind == kind:
            return out.name
        if kind == "file":
            found = self._files.get(ref_id)
            if found is not None and found.chat_id == chat_id:
                return found.name
        raise SendRefused("not_found")

    def _targets(self, devices: Literal["all"] | list[str]) -> list[str]:
        known = [c.id for c in self._credentials.unrevoked()]
        if devices == "all":
            targets = known
        elif isinstance(devices, list) and all(isinstance(d, str) for d in devices):
            targets = list(dict.fromkeys(devices))
            if any(d not in known for d in targets):
                raise SendRefused("unknown_device")
        else:
            raise SendRefused("unknown_device")
        if not targets:
            raise SendRefused("unknown_device")
        return targets

    def send(
        self,
        *,
        chat_id: str,
        kind: Kind,
        ref_id: str | None,
        text: str | None,
        devices: Literal["all"] | list[str],
        turn_id: str | None = None,
        question: str = "",
        actor: str = "owner",
    ) -> SentItem:
        """Record an item for the target devices (`all` or credential ids, each unrevoked).
        A summary's text (at most MAX_SUMMARY_CHARS) is number-checked with `turn_id`, the
        turn whose runs are its evidence, and its marks (UTF-16 offsets) are stored."""
        title = self._title(chat_id, kind, ref_id)
        marks: list[dict] | None = None
        if kind == "summary":
            text = _summary_text(text)
            found = self._checker.marks_for(chat_id, text, turn_id=turn_id, question=question)
            marks = web_marks(text, found) if found is not None else None
        else:
            text = None
        return self._record(chat_id, kind, ref_id, title, text, marks, devices, actor)

    def send_checked_summary(
        self,
        *,
        chat_id: str,
        ref_id: str,
        text: str,
        marks: list[dict] | None,
        devices: Literal["all"] | list[str],
        actor: str = "owner",
    ) -> SentItem:
        """A reply already number-checked when it was written: its stored text and marks are
        kept as they are (the check knew the turn, the question and Hearthkeep's own words)."""
        text = _summary_text(text)
        return self._record(chat_id, "summary", ref_id, SUMMARY_TITLE, text, marks, devices, actor)

    def _record(
        self,
        chat_id: str,
        kind: Kind,
        ref_id: str | None,
        title: str,
        text: str | None,
        marks: list[dict] | None,
        devices: Literal["all"] | list[str],
        actor: str,
    ) -> SentItem:
        targets = self._targets(devices)
        item = SentItem(
            id=token_urlsafe(12),
            chat_id=chat_id,
            kind=kind,
            ref_id=ref_id,
            title=title,
            text=text,
            marks=marks,
            created_at=db.utcnow(),
        )
        with db.transaction(self._conn):
            self._conn.execute(
                "INSERT INTO sent_items (id, chat_id, kind, ref_id, title, text, marks, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (item.id, chat_id, kind, ref_id, title, text,
                 json.dumps(marks) if marks is not None else None, item.created_at),
            )  # fmt: skip
            self._conn.executemany(
                "INSERT INTO sent_targets (item_id, credential_id, dismissed_at)"
                " VALUES (?, ?, NULL)",
                [(item.id, target) for target in targets],
            )
            audit.record(
                self._conn, actor, "sent.created", {"id": item.id, "kind": kind, "targets": targets}
            )
        return item

    def targets(self, item_id: str) -> list[str]:
        """The credential ids the item was sent to."""
        rows = self._conn.execute(
            "SELECT credential_id FROM sent_targets WHERE item_id = ? ORDER BY rowid", (item_id,)
        )
        return [r["credential_id"] for r in rows]

    # ---- pushes ----------------------------------------------------------------------------

    async def notify(self, item: SentItem) -> None:
        """One payload-free push to each target device that is still enrolled and has push
        on: only endpoints stored for a target are ever used. A gone subscription is removed.
        Never raises: any error is logged by its type only."""
        if self._push is None or self._subscriptions is None:
            return
        try:
            targets = self.targets(item.id)
        except Exception as exc:
            _log.warning("push for a sent item failed (%s)", type(exc).__name__)
            return
        for credential_id in targets:
            try:
                if self._credentials.get(credential_id) is None:
                    continue
                endpoint = self._subscriptions.endpoint_for(credential_id)
                if endpoint is None:
                    continue
                status = await self._push.notify(endpoint)
                if status == "gone":
                    self._subscriptions.remove_endpoint(endpoint)
                audit.record(self._conn, "owner", "push.sent", {"status": status})
            except Exception as exc:
                _log.warning("push for a sent item failed (%s)", type(exc).__name__)

    def notify_later(self, item: SentItem) -> None:
        """`notify` as a tracked background task: the caller never waits on a push service.
        `aclose` finishes or cancels what is still running; once it has begun, no new task
        is made."""
        if self._push is None or self._subscriptions is None or self._closing:
            return
        task = asyncio.get_running_loop().create_task(self.notify(item))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def aclose(self, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """At shutdown: wait up to `grace` seconds for running pushes, then cancel the rest.
        From here on `notify_later` makes no task."""
        self._closing = True
        tasks = list(self._tasks)
        if not tasks:
            return
        _done, running = await asyncio.wait(tasks, timeout=grace)
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---- the tray --------------------------------------------------------------------------

    def _enrolled(self, credential_id: str) -> bool:
        return self._credentials.get(credential_id) is not None

    def inbox(self, credential_id: str, limit: int = INBOX_LIMIT) -> list[SentItem]:
        """This device's items: not yet dismissed first, newest first."""
        if not self._enrolled(credential_id):
            return []
        rows = self._conn.execute(
            f"SELECT {_COLUMNS}, t.dismissed_at FROM sent_items i"
            " JOIN sent_targets t ON t.item_id = i.id WHERE t.credential_id = ?"
            " ORDER BY t.dismissed_at IS NOT NULL, i.created_at DESC, i.rowid DESC LIMIT ?",
            (credential_id, int(limit)),
        ).fetchall()
        return [_item(r, r["dismissed_at"] is not None) for r in rows]

    def unread_count(self, credential_id: str) -> int:
        if not self._enrolled(credential_id):
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sent_targets WHERE credential_id = ? AND dismissed_at IS NULL",
            (credential_id,),
        ).fetchone()
        return int(row[0])

    def item_for(self, item_id: str, credential_id: str) -> SentItem | None:
        """The item, only for an enrolled device it was sent to."""
        if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
            return None
        if not self._enrolled(credential_id):
            return None
        row = self._conn.execute(
            f"SELECT {_COLUMNS}, t.dismissed_at FROM sent_items i"
            " JOIN sent_targets t ON t.item_id = i.id WHERE i.id = ? AND t.credential_id = ?",
            (item_id, credential_id),
        ).fetchone()
        return _item(row, row["dismissed_at"] is not None) if row is not None else None

    def dismiss(self, item_id: str, credential_id: str) -> bool:
        """Hide the item from this device's unread ones; False when it isn't this device's."""
        if self.item_for(item_id, credential_id) is None:
            return False
        self._conn.execute(
            "UPDATE sent_targets SET dismissed_at = ?"
            " WHERE item_id = ? AND credential_id = ? AND dismissed_at IS NULL",
            (db.utcnow(), item_id, credential_id),
        )
        return True
