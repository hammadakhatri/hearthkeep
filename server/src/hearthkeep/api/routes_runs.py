"""Saved sandbox runs and their outputs: a run's details, one output, and an output's download."""

from __future__ import annotations

import dataclasses
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response

from hearthkeep.api.common import ApiError, current_session, services
from hearthkeep.api.downloads import output_download, table_fields
from hearthkeep.auth.sessions import Session
from hearthkeep.sandbox.runs import OutputRecord, RunRecord, RunStore

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]


def _runs(request: Request) -> RunStore:
    runs = services(request).runs
    if runs is None:
        raise ApiError(404, "not_found")
    return runs


def _output(request: Request, output_id: str) -> OutputRecord:
    found = _runs(request).output(output_id)
    if found is None:
        raise ApiError(404, "not_found")
    return found


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: SessionDep, request: Request) -> dict:
    runs = _runs(request)
    rec = runs.get(run_id)
    if rec is None:
        raise ApiError(404, "not_found")
    charts, tables, files = [], [], []
    for out in runs.outputs(rec.id):
        if out.kind == "chart":
            charts.append({"id": out.id, "title": out.name, "spec": out.data})
        elif out.kind == "table":
            tables.append({"id": out.id, **table_fields(out)})
        elif out.kind == "file":
            files.append({"id": out.id, "name": out.name, "mime": out.mime, "size": out.size})
    return {
        "id": rec.id,
        "chat_id": rec.chat_id,
        "turn_id": rec.turn_id,
        "purpose": rec.purpose,
        "status": rec.status,
        "error": rec.error,
        "duration_ms": rec.duration_ms,
        "script": rec.script,
        "stdout": rec.stdout,
        "stdout_truncated": rec.stdout_truncated,
        "values": [dataclasses.asdict(v) for v in rec.values],
        "cleaning": rec.cleaning,
        "sections": rec.sections,
        "charts": charts,
        "tables": tables,
        "files": files,
        "created_at": rec.created_at,
    }


def _headline(request: Request, run: RunRecord | None) -> dict | None:
    """The number-checked final reply of the turn that made a run: the turn is the messages
    between the owner's message before the run and the next one, and the reply is its latest
    assistant message with words. That message's check, or None: an earlier step's check is
    never used."""
    if run is None:
        return None
    conn = services(request).conn
    chat_id = run.chat_id
    start = conn.execute(
        "SELECT id FROM messages WHERE chat_id = ? AND role = 'user' AND created_at <= ?"
        " ORDER BY id DESC LIMIT 1",
        (chat_id, run.created_at),
    ).fetchone()
    first = start["id"] if start else 0
    end = conn.execute(
        "SELECT MIN(id) AS id FROM messages WHERE chat_id = ? AND role = 'user' AND id > ?",
        (chat_id, first),
    ).fetchone()["id"]
    final = conn.execute(
        "SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant' AND id > ?"
        " AND (? IS NULL OR id < ?) AND TRIM(COALESCE(content, '')) != ''"
        " ORDER BY id DESC LIMIT 1",
        (chat_id, first, end, end),
    ).fetchone()
    if final is None:
        return None
    row = conn.execute(
        "SELECT text, marks FROM number_checks WHERE message_id = ? AND chat_id = ?",
        (final["id"], chat_id),
    ).fetchone()
    if row is None:
        return None
    return {"text": row["text"] or "", "marks": json.loads(row["marks"] or "[]")}


@router.get("/outputs/{output_id}")
async def get_output(output_id: str, session: SessionDep, request: Request) -> dict:
    out = _output(request, output_id)
    if out.kind == "chart":
        data: dict = {"spec": out.data}
    elif out.kind == "table":
        data = table_fields(out)
    else:
        data = {"mime": out.mime, "size": out.size}
    run = _runs(request).get(out.run_id)
    turn_id = run.turn_id if run is not None else None
    if out.kind == "chart":
        data["headline"] = _headline(request, run)
    # The record's own fields last: nothing in the data can replace them.
    return {**data, "id": out.id, "run_id": out.run_id, "chat_id": out.chat_id,
            "kind": out.kind, "name": out.name, "turn_id": turn_id}  # fmt: skip


@router.get("/outputs/{output_id}/download", response_model=None)
async def download_output(
    output_id: str, session: SessionDep, request: Request
) -> FileResponse | Response:
    out = _output(request, output_id)
    return output_download(_runs(request), out)
