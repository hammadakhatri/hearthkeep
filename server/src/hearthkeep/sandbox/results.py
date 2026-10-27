"""Parse the `results.jsonl` a sandbox run wrote (see the `hk` library, `_results.py`).

The file is untrusted. `parse_results` never raises: a bad line is dropped and noted in
`problems` as `line {n}: {reason}` (reasons never repeat the content), and everything
valid is kept.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Literal

from hearthkeep.sandbox.charts import ChartError, validate_chart_spec

MAX_BYTES = 5 * 1024 * 1024
MAX_LINES = 2000
MAX_NAME = 100
MAX_FILE_NAME = 120  # hk.file allows 120 characters
MAX_TEXT = 200
MAX_NOTE = 500
MAX_INT = 2**63
MAX_ROWS = 500
MAX_COLUMNS = 50
NOT_A_NUMBER = "not a number"
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]*")


@dataclass
class Value:
    name: str
    value: float | int | str | None
    unit: str | None
    note: str | None
    is_date: bool
    kind: Literal["computed", "document"]
    page: int | None


@dataclass
class Chart:
    title: str
    spec: dict


@dataclass
class Table:
    name: str
    columns: list[str]
    rows: list[list]
    row_numbers: list[int]  # may be empty, e.g. after reset_index or groupby
    note: str | None
    total_rows: int


@dataclass
class FileRef:
    name: str
    member: str


@dataclass
class ParsedResults:
    values: list[Value] = field(default_factory=list)
    charts: list[Chart] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    files: list[FileRef] = field(default_factory=list)
    cleaning: list[str] = field(default_factory=list)
    sections: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


class _Drop(Exception):
    """This line is not usable; the message is the reason."""


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _text(rec: dict, key: str, limit: int, *, required: bool = True) -> str | None:
    raw = rec.get(key)
    if raw is None and not required:
        return None
    if not isinstance(raw, str):
        raise _Drop(f"{key} must be text")
    if required and not raw.strip():
        raise _Drop(f"{key} is empty")
    if len(raw) > limit:
        raise _Drop(f"{key} is longer than {limit} characters")
    _encodable(raw, key)
    return raw


def _encodable(text: str, what: str) -> None:
    """A lone surrogate (from a JSON \\ud800 escape) cannot be stored or sent as UTF-8."""
    try:
        text.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise _Drop(f"{what} is not valid text") from None


def _non_finite(x: object) -> bool:
    """NaN, infinity, or a whole number too big to be a plain JSON number."""
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return x >= MAX_INT or x < -MAX_INT
    return isinstance(x, float) and not math.isfinite(x)


def _value(rec: dict) -> Value:
    name = _text(rec, "name", MAX_NAME)
    unit = _text(rec, "unit", MAX_NAME, required=False)
    note = _text(rec, "note", MAX_NOTE, required=False)
    kind = rec.get("kind")
    if kind not in ("computed", "document"):
        raise _Drop("kind must be computed or document")
    raw = rec.get("value")
    if raw is None or _non_finite(raw):
        if _non_finite(raw):
            note = NOT_A_NUMBER if note is None else f"{note} ({NOT_A_NUMBER})"
        value: float | int | str | None = None
    elif isinstance(raw, bool):
        value = "true" if raw else "false"
    elif isinstance(raw, int | float):
        value = raw
    elif isinstance(raw, str):
        if len(raw) > MAX_TEXT:
            raise _Drop(f"value is longer than {MAX_TEXT} characters")
        _encodable(raw, "value")
        value = raw
    else:
        raise _Drop("value must be a number, text or null")
    page = rec.get("page")
    if kind == "document":
        if not _is_int(page) or page < 1:
            raise _Drop("a document value needs a page of 1 or more")
    else:
        page = None
    return Value(name, value, unit, note, rec.get("is_date") is True, kind, page)  # type: ignore[arg-type]


def _cell(cell: object) -> float | int | str | None:
    if cell is None or _non_finite(cell):
        return None
    if isinstance(cell, bool):
        return "true" if cell else "false"
    if isinstance(cell, int | float):
        return cell
    if isinstance(cell, str):
        if len(cell) > MAX_TEXT:
            raise _Drop(f"a cell is longer than {MAX_TEXT} characters")
        _encodable(cell, "a cell")
        return cell
    raise _Drop("a cell must be a number, text or null")


def _table(rec: dict) -> Table:
    name = _text(rec, "name", MAX_NAME)
    note = _text(rec, "note", MAX_NOTE, required=False)
    columns = rec.get("columns")
    if not isinstance(columns, list) or not columns or len(columns) > MAX_COLUMNS:
        raise _Drop(f"a table has 1 to {MAX_COLUMNS} columns")
    for column in columns:
        if not isinstance(column, str) or len(column) > MAX_NAME:
            raise _Drop(f"column names are text of at most {MAX_NAME} characters")
        _encodable(column, "a column name")
    rows = rec.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise _Drop(f"a table has at most {MAX_ROWS} rows")
    clean_rows = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise _Drop("every row has one cell per column")
        clean_rows.append([_cell(c) for c in row])
    numbers = rec.get("row_numbers", [])
    if not isinstance(numbers, list) or not all(_is_int(n) for n in numbers):
        raise _Drop("row_numbers must be a list of integers")
    if numbers and len(numbers) != len(rows):
        raise _Drop("row_numbers must be empty or have one number per row")
    total = rec.get("total_rows")
    if not _is_int(total) or total < 0:
        raise _Drop("total_rows must be a whole number")
    return Table(name, list(columns), clean_rows, list(numbers), note, max(total, len(rows)))  # type: ignore[arg-type]


def _chart(rec: dict) -> Chart:
    title = _text(rec, "title", MAX_NAME)
    try:
        spec = validate_chart_spec(rec.get("spec"))
    except ChartError as exc:
        raise _Drop(f"chart rejected: {exc}") from None
    return Chart(title, spec)  # type: ignore[arg-type]


def _file(rec: dict, members: set[str]) -> FileRef:
    name = _text(rec, "name", MAX_FILE_NAME)
    member = rec.get("member")
    if not isinstance(name, str) or not _FILE_NAME.fullmatch(name):
        raise _Drop("file name is not valid")
    if not isinstance(member, str) or member != f"files/{name}" or member not in members:
        raise _Drop("file is not among the run's output files")
    return FileRef(name, member)


def _bounded(raw: bytes) -> tuple[list[bytes], list[str]]:
    """The lines to read, plus problems for what was cut (by size or by line count)."""
    cut: list[tuple[int, str]] = []
    if len(raw) > MAX_BYTES and not (len(raw) == MAX_BYTES + 1 and raw.endswith(b"\n")):
        kept = raw[:MAX_BYTES]
        if raw[MAX_BYTES : MAX_BYTES + 1] == b"\n":
            kept = raw[: MAX_BYTES + 1]  # the 5 MB ends exactly on a line
        else:
            kept = kept[: kept.rfind(b"\n") + 1]  # cut back to a whole line
        cut.append((kept.count(b"\n") + 1, "results are larger than 5 MB, the rest was ignored"))
        raw = kept
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    problems_at = {n: msg for n, msg in cut}
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        problems_at.setdefault(MAX_LINES + 1, "too many lines, the rest was ignored")
    return lines, [f"line {n}: {msg}" for n, msg in sorted(problems_at.items())]


def parse_results(raw: bytes, members: set[str]) -> ParsedResults:
    """Parse results.jsonl. `members` are the names inside the run's output archive."""
    out = ParsedResults()
    lines, cuts = _bounded(raw)
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                raise _Drop("not valid JSON") from None
            if not isinstance(rec, dict):
                raise _Drop("not a JSON object")
            kind = rec.get("type")
            if kind == "value":
                out.values.append(_value(rec))
            elif kind == "chart":
                out.charts.append(_chart(rec))
            elif kind == "table":
                out.tables.append(_table(rec))
            elif kind == "file":
                out.files.append(_file(rec, members))
            elif kind == "cleaning":
                out.cleaning.append(_text(rec, "note", MAX_NOTE))  # type: ignore[arg-type]
            elif kind == "section":
                out.sections.add(_text(rec, "name", MAX_NAME))  # type: ignore[arg-type]
            else:
                raise _Drop("unknown record type")
        except _Drop as drop:
            out.problems.append(f"line {number}: {drop}")
        except Exception:  # untrusted input: whatever goes wrong, drop the line, never raise
            out.problems.append(f"line {number}: could not be read")
    out.problems.extend(cuts)
    return out
