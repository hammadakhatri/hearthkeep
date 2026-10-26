"""Generated images: the gallery list and the image files."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from hearthkeep.api.common import ApiError, current_session, services
from hearthkeep.auth.sessions import Session

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]

_IMAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_CACHE = "private, max-age=86400"


def _timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        problem = {"loc": ["query", "before"], "msg": "must be an ISO timestamp"}
        raise ApiError(422, "invalid_request", problems=[problem]) from None
    return value


@router.get("/images")
async def list_images(
    session: SessionDep,
    request: Request,
    before: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 60,
) -> dict:
    records = services(request).images.list(limit=limit, before=_timestamp(before))
    return {
        "images": [
            {
                "id": r.id,
                "model_id": r.model_id,
                "prompt": r.prompt,
                "width": r.width,
                "height": r.height,
                "created_at": r.created_at,
            }
            for r in records
        ]
    }


@router.get("/images/{image_id}/file")
async def image_file(session: SessionDep, request: Request, image_id: str) -> Response:
    # The id is checked before the store is asked, so nothing path-like ever reaches it.
    if not _IMAGE_ID.fullmatch(image_id):
        raise ApiError(404, "not_found")
    path = services(request).images.path(image_id)
    if path is None:
        raise ApiError(404, "not_found")
    try:
        data = path.read_bytes()
    except FileNotFoundError:  # removed after path() found it
        raise ApiError(404, "not_found") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": _CACHE})
