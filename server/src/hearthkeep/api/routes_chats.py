"""Chats, messages and the per-turn Server-Sent Events stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from hearthkeep import db
from hearthkeep.agent.runner import TERMINAL_EVENTS, TurnInProgressError
from hearthkeep.api.common import ApiError, Services, current_session, services
from hearthkeep.auth.sessions import Session
from hearthkeep.files.checks import MAX_FILES_PER_MESSAGE, TOO_MANY
from hearthkeep.files.store import ATTACHED_FILE_LINE
from hearthkeep.images import ATTACHED_PHOTO_LINE, normalize_newlines, without_photo_lines
from hearthkeep.secrets import SecretStoreError

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]
MESSAGE_MAX_CHARS = 20000
TITLE_CHARS = 60
PHOTO_TITLE = "Photo"
SECRETS_UNREADABLE = "Hearthkeep couldn't read its stored secrets. Check the server."
UNKNOWN_FILE = "That file isn't part of this chat. Attach it again."


class NewChatBody(BaseModel):
    think: bool = False
    # Empty: the first message names the chat.
    title: str = Field(default="", max_length=TITLE_CHARS)


class NewMessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)


def _chat_row(hk: Services, chat_id: str):
    row = hk.conn.execute(
        "SELECT id, title, think, created_at FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "not_found")
    return row


def _chat_json(row, active_turn: str | None) -> dict:
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "think": bool(row["think"]),
        "created_at": row["created_at"],
        "active_turn": active_turn,
    }


@router.get("/chats")
async def list_chats(session: SessionDep, request: Request) -> list[dict]:
    hk = services(request)
    rows = hk.conn.execute(
        "SELECT id, title, think, created_at FROM chats ORDER BY created_at DESC, rowid DESC"
    ).fetchall()
    return [_chat_json(r, hk.runner.active_turn(r["id"])) for r in rows]


@router.post("/chats", status_code=201)
async def create_chat(body: NewChatBody, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    chat_id = uuid.uuid4().hex
    hk.conn.execute(
        "INSERT INTO chats (id, title, think, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, " ".join(body.title.split()), int(body.think), db.utcnow()),
    )
    return _chat_json(_chat_row(hk, chat_id), None)


@router.get("/chats/{chat_id}/messages")
async def list_messages(chat_id: str, session: SessionDep, request: Request) -> list[dict]:
    hk = services(request)
    _chat_row(hk, chat_id)
    rows = hk.conn.execute(
        "SELECT id, role, content, tool_calls, tool_call_id, created_at FROM messages"
        " WHERE chat_id = ? ORDER BY id",
        (chat_id,),
    ).fetchall()
    images = hk.conn.execute(
        "SELECT id, created_at FROM images WHERE chat_id = ? ORDER BY created_at, id", (chat_id,)
    ).fetchall()
    runs = hk.conn.execute(
        "SELECT id, created_at FROM runs WHERE chat_id = ? ORDER BY created_at, rowid", (chat_id,)
    ).fetchall()
    checks = {
        c["message_id"]: {"text": c["text"], "marks": json.loads(c["marks"])}
        for c in hk.conn.execute(
            "SELECT message_id, text, marks FROM number_checks WHERE chat_id = ?", (chat_id,)
        )
    }
    out = []
    previous_at = ""
    for r in rows:
        made: list[str] = []
        ran: list[str] = []
        if r["role"] == "tool" and r["created_at"]:
            # Images and runs are saved inside their tool call, before its tool message.
            made = [i["id"] for i in images if previous_at < i["created_at"] <= r["created_at"]]
            ran = [x["id"] for x in runs if previous_at < x["created_at"] <= r["created_at"]]
        out.append(
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"] or "",
                "tool_calls": json.loads(r["tool_calls"]) if r["tool_calls"] else None,
                "tool_call_id": r["tool_call_id"],
                "created_at": r["created_at"],
                "images": made,
                "runs": ran,
                # Only assistant messages carry one (the last of a checked turn); else null.
                "number_check": checks.get(r["id"]) if r["role"] == "assistant" else None,
            }
        )
        previous_at = r["created_at"] or ""
    return out


def _is_photo_line(line: str) -> bool:
    return ATTACHED_PHOTO_LINE.fullmatch(line) is not None


def without_file_lines(text: str) -> str:
    """The text with its `Attached file: <id>` lines left out (exact matches only). CRLF and CR
    line ends count as LF."""
    lines = normalize_newlines(text).split("\n")
    return "\n".join(line for line in lines if not ATTACHED_FILE_LINE.fullmatch(line))


@router.post("/chats/{chat_id}/messages", status_code=202)
async def post_message(
    chat_id: str, body: NewMessageBody, session: SessionDep, request: Request
) -> dict:
    hk = services(request)
    row = _chat_row(hk, chat_id)
    # Line ends are normalised first, so an attached line ending in CR is still counted and
    # checked (and the message is stored with LF line ends).
    text = normalize_newlines(body.text).strip()
    if not text:
        raise ApiError(422, "empty_message")
    lines = text.split("\n")
    file_ids = [m.group(1) for line in lines if (m := ATTACHED_FILE_LINE.fullmatch(line))]
    if len(file_ids) > MAX_FILES_PER_MESSAGE:
        raise ApiError(422, "too_many_files", message=TOO_MANY)
    attached = [hk.files.get(file_id) for file_id in file_ids]
    if any(found is None or found.chat_id != chat_id for found in attached):
        raise ApiError(422, "unknown_file", message=UNKNOWN_FILE)
    photos = [line.removeprefix("Attached photo: ") for line in lines if _is_photo_line(line)]
    try:
        turn_id = hk.runner.start(chat_id, text, think=bool(row["think"]))
    except TurnInProgressError:
        raise ApiError(409, "turn_in_progress") from None
    except SecretStoreError:
        # Nothing was saved: the message is refused rather than stored unredacted.
        raise ApiError(503, "secrets_unreadable", message=SECRETS_UNREADABLE) from None
    for upload_id in photos:
        if hk.images.get_upload(upload_id) is not None:
            hk.conn.execute(
                "INSERT OR IGNORE INTO upload_chats (upload_id, chat_id) VALUES (?, ?)",
                (upload_id, chat_id),
            )
    if not row["title"]:
        # Attached-photo and attached-file lines are not words: a message with only those is
        # titled with its first file's name, else "Photo".
        words = " ".join(without_photo_lines(without_file_lines(text)).split())
        fallback = " ".join(attached[0].name.split()) if attached else PHOTO_TITLE
        title = (words or fallback)[:TITLE_CHARS]
        hk.conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title, chat_id))
    return {"turn_id": turn_id}


def _sse(event: dict) -> str:
    data = {k: v for k, v in event.items() if k not in ("type", "seq")}
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(data)}\n\n"


async def sse_events(
    runner, chat_id: str, turn_id: str, after: int, keepalive: float
) -> AsyncIterator[str]:
    """SSE frames for a turn's events after `after`, with keep-alive comments while idle.

    The pending `__anext__` survives keep-alive timeouts (it is awaited again, never cancelled),
    so no event is lost; it is cancelled only when the stream itself closes.
    """
    events = runner.subscribe(chat_id, turn_id, after_seq=after)
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(events.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=keepalive)
            if not done:
                yield ": keep-alive\n\n"
                continue
            finished, pending = pending, None
            try:
                event = finished.result()
            except StopAsyncIteration:
                return
            yield _sse(event)
            if event["type"] in TERMINAL_EVENTS:
                return
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        with contextlib.suppress(Exception):
            await events.aclose()


@router.get("/chats/{chat_id}/turns/{turn_id}/events")
async def turn_events(
    chat_id: str,
    turn_id: str,
    session: SessionDep,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    hk = services(request)
    _chat_row(hk, chat_id)
    known = hk.runner.active_turn(chat_id) == turn_id or (
        hk.conn.execute(
            "SELECT 1 FROM turn_events WHERE chat_id = ? AND turn_id = ? LIMIT 1",
            (chat_id, turn_id),
        ).fetchone()
        is not None
    )
    if not known:
        raise ApiError(404, "not_found")
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        try:
            after = max(0, int(last_event_id))
        except ValueError:
            raise ApiError(422, "invalid_last_event_id") from None
    return StreamingResponse(
        sse_events(hk.runner, chat_id, turn_id, after, hk.sse_keepalive),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
