"""Sending to the owner's devices from the web, and each device's "Sent to you" tray.

"This device" is the session's passkey: a device sees, dismisses and downloads only what was
sent to it. A summary's text always comes from the server (the reply's number-checked text,
or the message itself), never from the client. Pushes run in the background.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from hearthkeep.api.common import ApiError, Services, actor, current_session, services
from hearthkeep.api.routes_files import content_disposition
from hearthkeep.api.routes_runs import table_csv
from hearthkeep.auth.sessions import Session
from hearthkeep.files.checks import MIME
from hearthkeep.sent import SUMMARY_TITLE, SendRefused, SentItem, SentStore, question_before

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]
_NO_STORE = {"Cache-Control": "private, no-store"}
MAX_DEVICES = 32
_STATUS_FOR_REASON = {"not_found": 404}


class SendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")  # no text, title or chat from the client

    kind: Literal["chart", "table", "file", "summary"]
    ref_id: str = Field(min_length=1, max_length=32)
    devices: Literal["all"] | Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1024)]],
        Field(min_length=1, max_length=MAX_DEVICES),
    ]


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


def _summary_source(hk: Services, ref_id: str) -> tuple[str, str, str | None, str]:
    """(chat id, text, turn id, question) of an assistant message: the text its number check
    stored (with that check's turn), else the message's own content."""
    if not ref_id.isascii() or not ref_id.isdigit():
        raise ApiError(404, "not_found")
    message = hk.conn.execute(
        "SELECT id, chat_id, role, content FROM messages WHERE id = ?", (int(ref_id),)
    ).fetchone()
    if message is None or message["role"] != "assistant":
        raise ApiError(404, "not_found")
    chat_id = message["chat_id"]
    checked = hk.conn.execute(
        "SELECT text, turn_id FROM number_checks WHERE message_id = ? AND chat_id = ?",
        (message["id"], chat_id),
    ).fetchone()
    question = question_before(hk.conn, chat_id, message["id"])
    if checked is not None:
        return chat_id, checked["text"] or "", checked["turn_id"], question
    return chat_id, message["content"] or "", None, question


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
    turn_id: str | None = None
    text: str | None = None
    question = ""
    if body.kind == "summary":
        chat_id, text, turn_id, question = _summary_source(hk, body.ref_id)
    else:
        chat_id = _ref_chat(hk, body.ref_id)
    try:
        item = sent.send(
            chat_id=chat_id,
            kind=body.kind,
            ref_id=body.ref_id,
            text=text,
            devices=body.devices,
            turn_id=turn_id,
            question=question,
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
    hk = services(request)
    item = _sent(hk).item_for(item_id, session.credential_id)
    if item is None:
        raise ApiError(404, "not_found")
    if item.kind == "summary":
        return Response(
            (item.text or "").encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": content_disposition(f"{SUMMARY_TITLE}.txt", "txt"),
                     **_NO_STORE},  # fmt: skip
        )
    if item.ref_id is None or item.kind == "chart":  # charts are saved as PNG on the device
        raise ApiError(404, "not_found")
    out = hk.runs.output(item.ref_id) if hk.runs is not None else None
    if out is not None and out.chat_id == item.chat_id and out.kind == item.kind:
        if out.kind == "table":
            return Response(
                table_csv(out),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": content_disposition(f"{out.name}.csv", "csv"),
                         **_NO_STORE},  # fmt: skip
            )
        path = hk.runs.output_path(out.id)
        if path is None:
            raise ApiError(404, "not_found")
        ext = out.name.rsplit(".", 1)[-1] if "." in out.name else "bin"
        return FileResponse(
            path,
            media_type=out.mime,
            headers={"Content-Disposition": content_disposition(out.name, ext), **_NO_STORE},
        )
    found = hk.files.get(item.ref_id) if item.kind == "file" else None
    path = hk.files.path(item.ref_id) if found is not None else None
    if found is None or found.chat_id != item.chat_id or path is None:
        raise ApiError(404, "not_found")
    return FileResponse(
        path,
        media_type=MIME[found.kind],
        headers={"Content-Disposition": content_disposition(found.name, found.kind), **_NO_STORE},
    )
