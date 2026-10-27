"""The owner's allowed folders: list them (with suggestions), add one (passkey step-up) and
remove one (session only). Folder paths are shown to the owner's session only: they never go to
the audit log, the server log or the model."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from hearthkeep.api.common import ApiError, Services, actor, current_session, record, services
from hearthkeep.auth.sessions import Session
from hearthkeep.folders import MAX_PATH_CHARS, AllowedFolder, FolderError, FolderStore

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


class AddFolderBody(BaseModel):
    path: str = Field(max_length=MAX_PATH_CHARS)
    step_up_token: str = Field(max_length=256)


def _folder_json(folder: AllowedFolder) -> dict:
    return {"id": folder.id, "label": folder.label, "path": str(folder.path)}


def _store(hk: Services) -> FolderStore:
    if hk.folders is None:
        raise ApiError(503, "unavailable")
    return hk.folders


@router.get("/folders")
async def list_folders(session: SessionDep, request: Request) -> dict:
    store = _store(services(request))
    added = store.list()
    # The disk checks run on a worker thread; the database is read here, on the loop's thread.
    suggestions = await run_in_threadpool(store.suggest, added)
    return {"folders": [_folder_json(f) for f in added], "suggestions": suggestions}


@router.post("/folders", status_code=201)
async def add_folder(body: AddFolderBody, session: SessionDep, request: Request) -> JSONResponse:
    hk = services(request)
    store = _store(hk)
    if not hk.step_up.check(body.step_up_token, session.id, None, "add_folder"):
        record(hk, actor(session), "folders.add_denied", {"reason": "step_up_required"})
        raise ApiError(403, "step_up_required")
    try:
        real = await run_in_threadpool(store.check_new, body.path)
        # The row is added here: the SQLite connection belongs to the event loop's thread.
        folder = store.add_checked(real)
    except FolderError as error:
        raise ApiError(422, "folder_refused", message=error.message) from None
    record(hk, actor(session), "folders.added", {"id": folder.id, "label": folder.label})
    return JSONResponse(_folder_json(folder), status_code=201)


@router.delete("/folders/{folder_id}")
async def remove_folder(folder_id: str, session: SessionDep, request: Request) -> dict:
    hk = services(request)
    if len(folder_id) > 64 or not _store(hk).remove(folder_id):
        raise ApiError(404, "not_found")
    record(hk, actor(session), "folders.removed", {"id": folder_id})
    return {"ok": True}
