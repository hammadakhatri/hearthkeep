"""Sending to the owner's devices from the web, and each device's "Sent to you" tray.

"This device" is the session's passkey: a device sees, dismisses and downloads only what was
sent to it. A summary's text always comes from the server (the reply's number-checked text,
or the message itself), never from the client. Pushes run in the background.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from hearthkeep.api.common import ApiError, Services, actor, current_session, services
from hearthkeep.api.downloads import chat_file_download, output_download, text_download
from hearthkeep.auth.sessions import Session
from hearthkeep.sent import SUMMARY_TITLE, SendRefused, SentItem, SentStore, question_before

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]
MAX_DEVICES = 32
_STATUS_FOR_REASON = {"not_found": 404}


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no text, title or chat from the client

    kind: Literal["chart", "table", "file", "summary"]
    ref_id: str = Field(min_length=1, max_length=32)
    devices: (
        Literal["all"]
        | Annotated[
            list[Annotated[str, Field(min_length=1, max_length=1024)]],
            Field(min_length=1, max_length=MAX_DEVICES),
        ]
    )


def _sent(hk: Services) -> SentStore:
    if hk.sent is None:
        raise ApiError(404, "not_found")
    return hk.sent


def _chart_spec(hk: Services, item: SentItem) -> dict | None:
    if item.kind != "chart" or hk.runs is None or item.ref_id is None:
        return None
    out = hk.runs.output(item.ref_id)
    if out is None or out.kind != "chart" or out.chat_id != item.chat_id:
        return None
    return out.data


def item_json(hk: Services, item: SentItem) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "title": item.title,
        "text": item.text,
        "marks": item.marks,
        "chart": _chart_spec(hk, item),
        "created_at": item.created_at,
        "dismissed": item.dismissed,
    }


def _summary_source(hk: Services, ref_id: str) -> tuple[str, str, list[dict] | None] | None:
    """(chat id, text, marks) of an assistant message that was number-checked: the check's
    stored text and marks, as they are. None when it has no check (the caller checks the
    message's own content then). 404 for anything but an assistant message."""
    if not ref_id.isascii() or not ref_id.isdigit():
        raise ApiError(404, "not_found")
    message = _assistant_message(hk, ref_id)
    checked = hk.conn.execute(
        "SELECT text, marks FROM number_checks WHERE message_id = ? AND chat_id = ?",
        (message["id"], message["chat_id"]),
    ).fetchone()
    if checked is None:
        return None
    return message["chat_id"], checked["text"] or "", json.loads(checked["marks"] or "[]")


def _assistant_message(hk: Services, ref_id: str):
    message = hk.conn.execute(
        "SELECT id, chat_id, role, content FROM messages WHERE id = ?", (int(ref_id),)
    ).fetchone()
    if message is None or message["role"] != "assistant":
        raise ApiError(404, "not_found")
    return message


def _ref_chat(hk: Services, ref_id: str) -> str:
    """The chat an output or chat file belongs to; `send` then checks its kind."""
    out = hk.runs.output(ref_id) if hk.runs is not None else None
    if out is not None:
        return out.chat_id
    found = hk.files.get(ref_id)
    if found is not None:
        return found.chat_id
    raise ApiError(404, "not_found")


@router.post("/sent", status_code=201)
async def send(body: SendBody, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    sent = _sent(hk)
    try:
        if body.kind != "summary":
            item = sent.send(
                chat_id=_ref_chat(hk, body.ref_id),
                kind=body.kind,
                ref_id=body.ref_id,
                text=None,
                devices=body.devices,
                actor=actor(session),
            )
        elif (checked := _summary_source(hk, body.ref_id)) is not None:
            chat_id, text, marks = checked
            item = sent.send_checked_summary(
                chat_id=chat_id,
                ref_id=body.ref_id,
                text=text,
                marks=marks,
                devices=body.devices,
                actor=actor(session),
            )
        else:
            # Never checked (a chat without files then): check the message now. Its turn
            # isn't known, so no analysis run is evidence.
            message = _assistant_message(hk, body.ref_id)
            item = sent.send(
                chat_id=message["chat_id"],
                kind="summary",
                ref_id=body.ref_id,
                text=message["content"] or "",
                devices=body.devices,
                turn_id=None,
                question=question_before(hk.conn, message["chat_id"], message["id"]),
                actor=actor(session),
            )
    except SendRefused as refused:
        raise ApiError(_STATUS_FOR_REASON.get(refused.reason, 422), refused.reason) from None
    sent.notify_later(item)  # never waited for: the push service may be slow or down
    return JSONResponse(item_json(hk, item), status_code=201)


@router.get("/sent")
async def inbox(session: SessionDep, request: Request) -> dict:
    hk = services(request)
    sent = _sent(hk)
    items = sent.inbox(session.credential_id)
    return {
        "items": [item_json(hk, i) for i in items],
        "unread": sent.unread_count(session.credential_id),
    }


@router.get("/sent/count")
async def unread(session: SessionDep, request: Request) -> dict:
    return {"unread": _sent(services(request)).unread_count(session.credential_id)}


@router.post("/sent/{item_id}/dismiss")
async def dismiss(item_id: str, session: SessionDep, request: Request) -> dict:
    if not _sent(services(request)).dismiss(item_id, session.credential_id):
        raise ApiError(404, "not_found")
    return {"dismissed": True}


@router.get("/sent/{item_id}/download", response_model=None)
async def download(item_id: str, session: SessionDep, request: Request) -> Response:
    """A table as CSV, a file's bytes or a summary as text; 404 for a chart (saved as PNG on
    the device) or for a thing deleted since it was sent."""
    hk = services(request)
    item = _sent(hk).item_for(item_id, session.credential_id)
    if item is None:
        raise ApiError(404, "not_found")
    if item.kind == "summary":
        return text_download(item.text or "", f"{SUMMARY_TITLE}.txt")
    if item.ref_id is None or item.kind == "chart":
        raise ApiError(404, "not_found")
    out = hk.runs.output(item.ref_id) if hk.runs is not None else None
    if out is not None and out.chat_id == item.chat_id and out.kind == item.kind:
        return output_download(hk.runs, out)
    found = hk.files.get(item.ref_id) if item.kind == "file" else None
    if found is None or found.chat_id != item.chat_id:
        raise ApiError(404, "not_found")
    return chat_file_download(hk.files, found)
