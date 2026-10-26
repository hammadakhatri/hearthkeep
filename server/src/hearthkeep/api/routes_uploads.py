"""The owner's own photos: upload one (it stays on this PC) and fetch it back."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from hearthkeep.api.common import ApiError, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.images import MAX_UPLOAD_BYTES, MAX_UPLOAD_SIDE, image_info

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

_UPLOAD_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_CACHE = "private, max-age=86400"
# Room for the multipart boundaries and part headers around the photo itself.
_ENVELOPE_BYTES = 16 * 1024
_MAX_BODY = MAX_UPLOAD_BYTES + _ENVELOPE_BYTES
_BOUNDARY = re.compile(r'boundary="?([^";]{1,200})"?', re.IGNORECASE)
_FIELD_NAME = re.compile(rb'(?:^|;)\s*name="([^"]*)"', re.IGNORECASE)

TOO_BIG = "That photo is too big. Photos can be up to 15 MB."
WRONG_TYPE = "That file isn't a photo Hearthkeep can use. Pick a PNG, JPEG or WebP image."
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
    """The content of the multipart field named `file`, or None. Other fields are ignored and
    the part's own file name and type are never looked at."""
    delimiter = b"--" + boundary
    for part in body.split(delimiter)[1:]:
        if part.startswith(b"--"):  # the closing delimiter
            break
        part = part.removeprefix(b"\r\n")
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-disposition":
                match = _FIELD_NAME.search(value)
                if match and match.group(1) == b"file":
                    return content.removesuffix(b"\r\n")
    return None


@router.post("/uploads", status_code=201)
async def upload_photo(session: SessionDep, request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    match = _BOUNDARY.search(content_type)
    if not content_type.lower().startswith("multipart/form-data") or match is None:
        raise ApiError(415, "unsupported_type", message=WRONG_TYPE)
    body = await _read_body(request)
    data = _file_field(body, match.group(1).encode("latin-1"))
    if data is None:
        raise ApiError(422, "invalid_request", message=NO_PHOTO)
    if len(data) > MAX_UPLOAD_BYTES:
        raise _too_big()
    info = image_info(data)
    if info is None:
        raise ApiError(415, "unsupported_type", message=WRONG_TYPE)
    if info.width > MAX_UPLOAD_SIDE or info.height > MAX_UPLOAD_SIDE:
        raise ApiError(422, "too_many_pixels", message=TOO_MANY_PIXELS)

    hk = services(request)
    saved = hk.images.save_upload(
        data, mime=info.mime, ext=info.ext, width=info.width, height=info.height
    )
    record(hk, actor(session), "upload", {"id": saved.id, "type": info.mime, "bytes": len(data)})
    return JSONResponse({"id": saved.id}, status_code=201)


@router.get("/uploads/{upload_id}/file")
async def upload_file(session: SessionDep, request: Request, upload_id: str) -> Response:
    # The id is checked before the store is asked, and the store builds the path itself.
    if not _UPLOAD_ID.fullmatch(upload_id):
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
