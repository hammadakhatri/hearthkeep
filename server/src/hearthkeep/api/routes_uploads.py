"""The owner's own photos: upload one (it stays on this PC) and fetch it back."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from hearthkeep.api.common import ApiError, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.images import MAX_UPLOAD_BYTES, MAX_UPLOAD_SIDE, UPLOAD_ID, ImageInfo, image_info

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

_CACHE = "private, max-age=86400"
# Room for the multipart boundaries and part headers around the photo itself.
_ENVELOPE_BYTES = 16 * 1024
_MAX_BODY = MAX_UPLOAD_BYTES + _ENVELOPE_BYTES
_BOUNDARY = re.compile(r'boundary="?([^";]{1,70})"?', re.IGNORECASE)
_FIELD_NAME = re.compile(rb'(?:^|;)\s*name="([^"]*)"', re.IGNORECASE)

TOO_BIG = "That photo is too big. Photos can be up to 15 MB."
WRONG_TYPE = "That file isn't a photo Hearthkeep can use. Pick a PNG, JPEG or WebP image."
STILL_ONLY = "Hearthkeep can only use still photos."
TOO_MANY_PIXELS = "That photo is too large. Photos can be up to 4096 pixels on each side."
NO_PHOTO = "No photo was sent."


def _too_big() -> ApiError:
    return ApiError(413, "too_large", message=TOO_BIG)


async def _read_body(request: Request) -> bytes:
    """The request body, read in chunks and abandoned as soon as it is over the limit."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _MAX_BODY:
        raise _too_big()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_BODY:
            raise _too_big()
    return bytes(body)


def _file_field(body: bytes, boundary: bytes) -> bytes | None:
    """The content of the multipart field named `file`, or None (RFC 7578 / RFC 2046).

    The first delimiter is `--boundary` at the very start (or after a preamble); every later
    one is CRLF + `--boundary`, so boundary text inside the photo without a CRLF in front is
    content. The part's own file name and type are never looked at."""
    dash = b"--" + boundary
    if body.startswith(dash):
        rest = body[len(dash) :]
    else:
        start = body.find(b"\r\n" + dash)
        if start < 0:
            return None
        rest = body[start + 2 + len(dash) :]
    for part in rest.split(b"\r\n" + dash):
        if part.startswith(b"--"):  # the closing delimiter
            break
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-disposition":
                match = _FIELD_NAME.search(value)
                if match and match.group(1) == b"file":
                    return content
    return None


def parse_upload(body: bytes, boundary: bytes) -> tuple[bytes, ImageInfo | None] | None:
    """The photo and what its headers say (None for a photo over the size limit), or None
    when no `file` field was sent. Runs on a worker thread: splitting and scanning up to
    15 MB shouldn't hold up the event loop."""
    data = _file_field(body, boundary)
    if data is None:
        return None
    return data, image_info(data) if len(data) <= MAX_UPLOAD_BYTES else None


@router.post("/uploads", status_code=201)
async def upload_photo(session: SessionDep, request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    match = _BOUNDARY.search(content_type)
    if not content_type.lower().startswith("multipart/form-data") or match is None:
        raise ApiError(415, "unsupported_type", message=WRONG_TYPE)
    body = await _read_body(request)
    parsed = await run_in_threadpool(parse_upload, body, match.group(1).encode("latin-1"))
    if parsed is None:
        raise ApiError(422, "invalid_request", message=NO_PHOTO)
    data, info = parsed
    if len(data) > MAX_UPLOAD_BYTES:
        raise _too_big()
    if info is None:
        raise ApiError(415, "unsupported_type", message=WRONG_TYPE)
    if info.animated:
        raise ApiError(415, "animated", message=STILL_ONLY)
    if info.width > MAX_UPLOAD_SIDE or info.height > MAX_UPLOAD_SIDE:
        raise ApiError(422, "too_many_pixels", message=TOO_MANY_PIXELS)

    hk = services(request)
    # The file is written on a worker thread. The row is added here: the SQLite connection
    # belongs to the event loop's thread and may not be used from another one.
    staged = await run_in_threadpool(hk.images.write_upload_file, data, info.ext)
    saved = hk.images.record_upload(staged, mime=info.mime, width=info.width, height=info.height)
    record(hk, actor(session), "upload", {"id": saved.id, "type": info.mime, "bytes": len(data)})
    return JSONResponse(
        {"id": saved.id, "width": saved.width, "height": saved.height}, status_code=201
    )


@router.get("/uploads/{upload_id}/file")
async def upload_file(session: SessionDep, request: Request, upload_id: str) -> Response:
    # The id is checked before the store is asked, and the store builds the path itself.
    if not UPLOAD_ID.fullmatch(upload_id):
        raise ApiError(404, "not_found")
    store = services(request).images
    found = store.get_upload(upload_id)
    path = store.upload_path(upload_id) if found else None
    if found is None or path is None:
        raise ApiError(404, "not_found")
    try:
        data = path.read_bytes()
    except FileNotFoundError:  # removed after upload_path() found it
        raise ApiError(404, "not_found") from None
    return Response(data, media_type=found.mime, headers={"Cache-Control": _CACHE})
