"""Approval store: pending tool-call approvals resolved only by an authenticated owner session.

The agent creates and awaits approvals; it has no way to resolve one. Resolution is a single
atomic conditional UPDATE so concurrent resolvers cannot both win.
"""

import asyncio
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from hearthkeep import audit, db
from hearthkeep.permissions import ActionClass

Status = Literal["pending", "approved", "declined", "expired"]
Reason = Literal["not_found", "already_resolved", "expired", "step_up_required"]


class ApprovalError(Exception):
    def __init__(self, reason: Reason):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Approval:
    id: str
    chat_id: str
    turn_id: str
    tool: str
    action_class: str
    summary: str
    detail: dict
    requires_step_up: bool
    status: Status
    created_at: str
    expires_at: str


_COLUMNS = (
    "id, chat_id, turn_id, tool, action_class, summary, detail, requires_step_up, status,"
    " created_at, expires_at"
)


def _from_row(row: sqlite3.Row) -> Approval:
    return Approval(
        id=row["id"],
        chat_id=row["chat_id"],
        turn_id=row["turn_id"],
        tool=row["tool"],
        action_class=row["action_class"],
        summary=row["summary"],
        detail=json.loads(row["detail"]),
        requires_step_up=bool(row["requires_step_up"]),
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


class ApprovalStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        ttl_minutes: int,
        clock: Callable[[], datetime],
        poll_interval: float = 0.5,
    ):
        self._conn = conn
        self._ttl = timedelta(minutes=ttl_minutes)
        self._clock = clock
        self._poll_interval = poll_interval

    def create(
        self,
        chat_id: str,
        turn_id: str,
        tool: str,
        action_class: ActionClass,
        summary: str,
        detail: dict,
        requires_step_up: bool,
    ) -> Approval:
        now = self._clock()
        approval = Approval(
            id=secrets.token_urlsafe(16),
            chat_id=chat_id,
            turn_id=turn_id,
            tool=tool,
            action_class=str(ActionClass(action_class)),
            summary=summary,
            detail=detail,
            requires_step_up=requires_step_up,
            status="pending",
            created_at=now.isoformat(),
            expires_at=(now + self._ttl).isoformat(),
        )
        with db.transaction(self._conn):
            self._conn.execute(
                f"INSERT INTO approvals ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval.id,
                    chat_id,
                    turn_id,
                    tool,
                    approval.action_class,
                    summary,
                    json.dumps(detail, default=str),
                    int(requires_step_up),
                    "pending",
                    approval.created_at,
                    approval.expires_at,
                ),
            )
            audit.record(
                self._conn,
                "agent",
                "approval.created",
                {
                    "approval_id": approval.id,
                    "tool": tool,
                    "action_class": approval.action_class,
                    "requires_step_up": requires_step_up,
                },
            )
        return approval

    def _row(self, approval_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise ApprovalError("not_found")
        return row

    def _expire_if_due(self, row: sqlite3.Row) -> sqlite3.Row:
        if row["status"] == "pending" and self._clock() >= datetime.fromisoformat(
            row["expires_at"]
        ):
            self._conn.execute(
                "UPDATE approvals SET status = 'expired', resolved_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (self._clock().isoformat(), row["id"]),
            )
            return self._row(row["id"])
        return row

    def get(self, approval_id: str) -> Approval:
        return _from_row(self._expire_if_due(self._row(approval_id)))

    def pending(self) -> list[Approval]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        live = [self._expire_if_due(r) for r in rows]
        return [_from_row(r) for r in live if r["status"] == "pending"]

    def resolve(
        self, approval_id: str, session_id: str, approve: bool, step_up_verified: bool
    ) -> Approval:
        status = "approved" if approve else "declined"
        error: Reason | None = None
        # Errors are raised after COMMIT so an expiry transition persists.
        with db.transaction(self._conn):
            row = self._expire_if_due(self._row(approval_id))
            if row["status"] == "expired":
                error = "expired"
            elif row["status"] != "pending":
                error = "already_resolved"
            elif approve and row["requires_step_up"] and not step_up_verified:
                error = "step_up_required"
            else:
                now = self._clock().isoformat()
                cur = self._conn.execute(
                    "UPDATE approvals SET status = ?, resolved_at = ?, resolved_by_session = ?"
                    " WHERE id = ? AND status = 'pending' AND expires_at > ?",
                    (status, now, session_id, approval_id, now),
                )
                if cur.rowcount == 1:
                    audit.record(
                        self._conn,
                        f"session:{session_id}",
                        "approval.resolved",
                        {"approval_id": approval_id, "status": status, "tool": row["tool"]},
                    )
                else:
                    current = self._expire_if_due(self._row(approval_id))
                    error = "expired" if current["status"] == "expired" else "already_resolved"
        if error:
            raise ApprovalError(error)
        return self.get(approval_id)

    async def wait(self, approval_id: str) -> Approval:
        while True:
            approval = self.get(approval_id)
            if approval.status != "pending":
                return approval
            await asyncio.sleep(self._poll_interval)
