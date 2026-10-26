"""Files attached to a chat: attach one, list them, read one's details, download it."""

from __future__ import annotations

import unicodedata
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from hearthkeep.api.common import ApiError, actor, current_session, record, services
from hearthkeep.api.multipart import multipart_boundary, parse_file_part, read_limited_body
from hearthkeep.auth.sessions import Session
from hearthkeep.files.checks import (
    MAX_FILE_BYTES,
    MESSAGES,
    MIME,
    TOO_BIG,
    FileCheck,
    FileRefused,
    check_file,
    clean_file_name,
)
from hearthkeep.files.store import CHAT_ID, ChatFile, ChatFileStore, StagedFile

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

# Room for the multipart boundaries and part headers around the file itself.
_ENVELOPE_BYTES = 16 * 1024
_MAX_BODY = MAX_FILE_BYTES + _ENVELOPE_BYTES
NO_FILE = "No file was sent."
_STATUS_FOR_REASON = {
    "too_large": 413,
    "too_large_to_open": 413,
    "unsupported": 415,
    "mismatch": 415,
    "macros": 415,
    "password_protected": 422,
    "invalid_json": 422,
    "damaged": 422,
}


def _too_big() -> ApiError:
    return ApiError(413, "too_large", message=TOO_BIG)


def _refusal(error: FileRefused) -> ApiError:
    return ApiError(_STATUS_FOR_REASON.get(error.reason, 422), error.reason, message=error.message)


def file_json(found: ChatFile) -> dict:
    return {
        "id": found.id,
        "chat_id": found.chat_id,
        "name": found.name,
        "kind": found.kind,
        "size": found.size,
        "status": found.status,
        "created_at": found.created_at,
    }


def _require_chat(request: Request, chat_id: str) -> None:
    if not CHAT_ID.fullmatch(chat_id):
        raise ApiError(404, "not_found")
    row = services(request).conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,))
    if row.fetchone() is None:
        raise ApiError(404, "not_found")


def prepare_upload(
    store: ChatFileStore, chat_id: str, body: bytes, boundary: bytes
) -> tuple[StagedFile, FileCheck, str]:
    """Find the file in the body, write it to a temporary file and check what it is. Runs on a
    worker thread (no database here); the temporary file is removed when the file is refused."""
    part = parse_file_part(body, boundary)
    if part is None:
        raise ApiError(422, "invalid_request", message=NO_FILE)
    if len(part.data) > MAX_FILE_BYTES:
        raise _too_big()
    staged = store.stage(chat_id, part.data)
    try:
        check = check_file(staged.tmp, part.filename or "")
    except BaseException:
        store.discard(staged)
        raise
    return staged, check, clean_file_name(part.filename, check.ext)


@router.post("/chats/{chat_id}/files", status_code=201)
async def attach_file(chat_id: str, session: SessionDep, request: Request) -> JSONResponse:
    _require_chat(request, chat_id)
    boundary = multipart_boundary(request.headers.get("content-type", ""))
    if boundary is None:
        raise ApiError(415, "unsupported", message=MESSAGES["unsupported"])
    body = await read_limited_body(request, _MAX_BODY, _too_big())
    hk = services(request)
    try:
        staged, check, name = await run_in_threadpool(
            prepare_upload, hk.files, chat_id, body, boundary
        )
    except FileRefused as error:
        raise _refusal(error) from None
    # The row is added here: the SQLite connection belongs to the event loop's thread.
    saved = hk.files.record(staged, name=name, check=check, source="upload")
    # Never the name: audit records the id, type and size only.
    record(hk, actor(session), "file.attached",
           {"id": saved.id, "type": saved.mime, "bytes": saved.size})  # fmt: skip
    return JSONResponse(file_json(saved), status_code=201)


@router.get("/chats/{chat_id}/files")
async def list_files(chat_id: str, session: SessionDep, request: Request) -> dict:
    _require_chat(request, chat_id)
    return {"files": [file_json(f) for f in services(request).files.list(chat_id)]}


def _found(request: Request, file_id: str) -> ChatFile:
    found = services(request).files.get(file_id)
    if found is None:
        raise ApiError(404, "not_found")
    return found


@router.get("/files/{file_id}")
async def get_file(file_id: str, session: SessionDep, request: Request) -> dict:
    return file_json(_found(request, file_id))


def _ascii_name(name: str, kind: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    plain = "".join(c for c in plain if c.isprintable() and c not in '"\\')
    return plain.strip() or f"file.{kind}"


def content_disposition(name: str, kind: str) -> str:
    return (
        f'attachment; filename="{_ascii_name(name, kind)}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


@router.get("/files/{file_id}/download")
async def download_file(file_id: str, session: SessionDep, request: Request) -> FileResponse:
    found = _found(request, file_id)
    path = services(request).files.path(file_id)
    if path is None:
        raise ApiError(404, "not_found")
    return FileResponse(
        path,
        media_type=MIME[found.kind],
        headers={
            "Content-Disposition": content_disposition(found.name, found.kind),
            "Cache-Control": "private, no-store",
        },
    )
