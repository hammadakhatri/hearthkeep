"""Downloads shared by the routes: a run's table as CSV, a run's output file and a chat file.

Every download is an attachment with a safe file name (an ASCII fallback plus the RFC 5987
UTF-8 name) and is never cached (`private, no-store`). Table cells a spreadsheet would run as
formulas are defused.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from urllib.parse import quote

from fastapi.responses import FileResponse, Response

from hearthkeep.api.common import ApiError
from hearthkeep.files.checks import MIME
from hearthkeep.files.store import ChatFile, ChatFileStore
from hearthkeep.sandbox.runs import OutputRecord, RunStore

NO_STORE = {"Cache-Control": "private, no-store"}
# A cell a spreadsheet would read as a formula (or a number given as text with a sign).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?")


def _ascii_name(name: str, kind: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    plain = "".join(c for c in plain if c.isprintable() and c not in '"\\')
    return plain.strip() or f"file.{kind}"


def content_disposition(name: str, kind: str) -> str:
    return (
        f'attachment; filename="{_ascii_name(name, kind)}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )


def _headers(name: str, kind: str) -> dict:
    return {"Content-Disposition": content_disposition(name, kind), **NO_STORE}


def table_fields(out: OutputRecord) -> dict:
    data = out.data or {}
    return {
        "name": out.name,
        "columns": list(data.get("columns") or []),
        "rows": list(data.get("rows") or []),
        "row_numbers": list(data.get("row_numbers") or []),
        "note": data.get("note"),
        "total_rows": data.get("total_rows", 0),
    }


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
    fields = table_fields(out)
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


def output_download(runs: RunStore, out: OutputRecord) -> Response:
    """A table output as CSV, or a file output's bytes; 404 for anything else or a file that
    is gone."""
    if out.kind == "table":
        return Response(
            table_csv(out),
            media_type="text/csv; charset=utf-8",
            headers=_headers(f"{out.name}.csv", "csv"),
        )
    if out.kind != "file":
        raise ApiError(404, "not_found")
    path = runs.output_path(out.id)
    if path is None:
        raise ApiError(404, "not_found")
    ext = out.name.rsplit(".", 1)[-1] if "." in out.name else "bin"
    return FileResponse(path, media_type=out.mime, headers=_headers(out.name, ext))


def chat_file_download(files: ChatFileStore, found: ChatFile) -> FileResponse:
    """A chat file's bytes under its original name; 404 when the file is gone."""
    path = files.path(found.id)
    if path is None:
        raise ApiError(404, "not_found")
    return FileResponse(path, media_type=MIME[found.kind], headers=_headers(found.name, found.kind))


def text_download(text: str, name: str) -> Response:
    return Response(
        text.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers=_headers(name, "txt"),
    )
