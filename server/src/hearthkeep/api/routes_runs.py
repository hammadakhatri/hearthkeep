"""Saved sandbox runs and their outputs: a run's details, one output, and an output's download."""

from __future__ import annotations

import csv
import dataclasses
import io
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response

from hearthkeep.api.common import ApiError, current_session, services
from hearthkeep.api.routes_files import content_disposition
from hearthkeep.auth.sessions import Session
from hearthkeep.sandbox.runs import OutputRecord, RunStore

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(current_session)]
_NO_STORE = {"Cache-Control": "private, no-store"}
# A cell a spreadsheet would read as a formula (or a number given as text with a sign).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?")


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
            tables.append({"id": out.id, **_table_fields(out)})
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


def _table_fields(out: OutputRecord) -> dict:
    data = out.data or {}
    return {
        "name": out.name,
        "columns": list(data.get("columns") or []),
        "rows": list(data.get("rows") or []),
        "row_numbers": list(data.get("row_numbers") or []),
        "note": data.get("note"),
        "total_rows": data.get("total_rows", 0),
    }


@router.get("/outputs/{output_id}")
async def get_output(output_id: str, session: SessionDep, request: Request) -> dict:
    out = _output(request, output_id)
    if out.kind == "chart":
        data: dict = {"spec": out.data}
    elif out.kind == "table":
        data = _table_fields(out)
    else:
        data = {"mime": out.mime, "size": out.size}
    # The record's own fields last: nothing in the data can replace them.
    return {**data, "id": out.id, "run_id": out.run_id, "chat_id": out.chat_id,
            "kind": out.kind, "name": out.name}  # fmt: skip


def _safe_cell(cell: object) -> object:
    """A text cell that a spreadsheet would run as a formula gets a leading apostrophe; plain
    numbers (also negative ones written as text) are left alone."""
    if cell is None:
        return ""
    if isinstance(cell, str) and cell.startswith(_FORMULA_START) and not _NUMBER.fullmatch(cell):
        return "'" + cell
    return cell


def table_csv(out: OutputRecord) -> bytes:
    """A table as CSV for a spreadsheet: UTF-8 with a BOM, the file's row numbers first."""
    fields = _table_fields(out)
    rows, numbers = fields["rows"], fields["row_numbers"]
    if len(numbers) != len(rows):
        numbers = []
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["row", *(_safe_cell(c) for c in fields["columns"])])
    for index, row in enumerate(rows):
        number = numbers[index] if numbers else ""
        writer.writerow([number, *(_safe_cell(c) for c in row)])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


@router.get("/outputs/{output_id}/download", response_model=None)
async def download_output(
    output_id: str, session: SessionDep, request: Request
) -> FileResponse | Response:
    out = _output(request, output_id)
    if out.kind == "table":
        return Response(
            table_csv(out),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": content_disposition(f"{out.name}.csv", "csv"),
                     **_NO_STORE},  # fmt: skip
        )
    if out.kind != "file":
        raise ApiError(404, "not_found")
    path = _runs(request).output_path(out.id)
    if path is None:
        raise ApiError(404, "not_found")
    ext = out.name.rsplit(".", 1)[-1] if "." in out.name else "bin"
    return FileResponse(
        path,
        media_type=out.mime,
        headers={"Content-Disposition": content_disposition(out.name, ext), **_NO_STORE},
    )
