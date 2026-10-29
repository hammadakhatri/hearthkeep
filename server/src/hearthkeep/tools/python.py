"""The `python` tool: the model's analysis scripts run in the Docker sandbox (never on the
host), every run is saved, and the model is told what the run reported.

The model sees only what `format_run_for_model` writes: the run's status and time, its
printed output, results, outputs, cleaning notes, problems and its fixed error message. Never
Docker's own words (`RawRun.diagnostics`), host paths, or where the owner's files came from.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path

from hearthkeep.files.store import ChatFile, ChatFileStore
from hearthkeep.permissions import ActionClass
from hearthkeep.sandbox import results as results_format
from hearthkeep.sandbox.handle import SandboxHandle
from hearthkeep.sandbox.manifest import chat_manifest
from hearthkeep.sandbox.results import ParsedResults, parse_results
from hearthkeep.sandbox.runner import (
    ERROR_OUTPUT,
    NO_DOCKER_MESSAGE,
    NO_IMAGE_MESSAGE,
    RawRun,
    SandboxRunner,
    SandboxUnavailable,
)
from hearthkeep.sandbox.runs import OutputRecord, RunRecord, RunStore
from hearthkeep.tools.base import ToolContext, ToolResult
from hearthkeep.tools.text import (
    CONTENT_BUDGET,
    PRINTED_NOTE,
    assemble,
    csv_line,
    fenced,
    one_line,
)

PYTHON_LEVEL_KEY = "python"
SCRIPT_MAX_CHARS = 20000
PURPOSE_MAX_CHARS = 120
PRINTED_CHARS = 8000
TABLE_ROWS_SHOWN = 20
TABLE_CHARS = 4000  # a wide table shows fewer rows (its line still gives the total)

NO_DOCKER = "Python analysis needs Docker Desktop. Start Docker on the PC and ask again."
NO_IMAGE = (
    "The Python sandbox isn't built yet. On the PC, run: hearthkeep sandbox build --data <data>"
)
TIMEOUT = "The script ran for over 120 seconds and was stopped."
RESULTS_UNREADABLE = "results.jsonl could not be read"
RESULTS_FAILED = "Hearthkeep couldn't read the results."
PRINTED_CUT = "(printed output cut)"
# What the model is told about one run is bounded: items per section, and characters overall
# (CONTENT_BUDGET, in tools.text).
COLUMNS_SHOWN = 30  # column names listed per file (the rest are counted)
COLUMN_NAME_CHARS = 100  # a column name is cut to this length wherever it is listed
KEY_ERROR_FILES = 3  # files whose columns follow a KeyError
KEY_ERROR_CHARS = 2000  # and at most about this many characters of them
RETRY = "The script failed. Fix it and run python again before you answer."
# The last line of a traceback for a missing key, e.g. KeyError: 'Region'.
_KEY_ERROR = re.compile(r"KeyError: (?:\(|\[)?['\"]")
_READ_CALL = re.compile(r"""hk\.read\(\s*(?:file\s*=\s*)?(['"])(.+?)\1""")
CAPS = {"values": 50, "tables": 5, "charts": 20, "files": 20, "cleaning": 20, "problems": 20}

ANALYSIS_PROMPT = """\
This chat has files the owner attached:
{files}

How to answer questions about them:
1. Work everything out in a python tool call. Scripts run in a sandbox with no internet and \
the files are read-only. Only what the script reports with hk, and what it prints, comes back \
to you.
2. Load a file with df = hk.read("<file id>"). Number and date columns come back parsed \
(YYYY-MM-DD and day/month dates included). Never call pd.to_datetime with dayfirst=True: it \
reads 2025-03-12 as 3 December. If a date column is still text, use \
df["col"] = hk.dates(df["col"]).
3. In the script, call hk.result("<name>", value) once for every number, date or count you \
will state, name first, for example hk.result("Total spent", df["amount"].sum(), unit="GBP"). \
For a value read off a PDF page use hk.document("<name>", value, page=N).
4. Show the rows behind a finding with hk.table("<name>", rows), gaps in dated data with \
hk.gaps(df, "<date column>", by="<group column>"), charts with hk.bar, hk.line or hk.scatter, \
and anything you cleaned or assumed with hk.cleaned("<note>"). Save files only with \
hk.file(name, data).
Example:
    import hk
    df = hk.read("<file id>")
    negative = df[df["amount"] < 0]
    hk.result("Negative amounts", len(negative))
    hk.table("Rows with a negative amount", negative)
5. Then reply in plain words. hk.result is Python: it never appears in your reply. Write the \
value itself, for example "There are 3 negative amounts." Use only numbers a run reported \
with hk.result or hk.document, as reported or rounded to fewer digits; any other number shows \
as unverified. Never estimate.
Start with two or three plain sentences that answer the question, then say what you cleaned \
or assumed. If a script fails, fix it and run it again; if the data can't answer the \
question, say what went wrong and what the owner could do. When files arrive without a \
question, run hk.profile("<file id>") once and reply with a short 'at a glance' summary that \
ends by inviting a question."""
KIND_LABELS = {
    "csv": "CSV",
    "xlsx": "Excel workbook",
    "xls": "Excel 97-2003 workbook",
    "pdf": "PDF",
    "txt": "Text",
    "json": "JSON",
}
DESCRIPTION = (
    "Run a Python script in a locked-down sandbox (no internet, read-only files) to analyse "
    "this chat's files. Put the whole analysis in script and a few words in purpose. In the "
    "script: df = hk.read(file_id); hk.result(name, value) for each number you will state; "
    "hk.document(name, value, page=N) for a value read off a PDF page; hk.table(name, rows); "
    "hk.gaps(df, date_col, by=col); hk.bar/line/scatter/chart; hk.file(name, data) to save "
    "a file; hk.cleaned(note); hk.profile(file_id); hk.pdf_text/hk.pdf_tables. Only what hk "
    "reports and what the script prints comes back."
)
NO_NUMBERS = (
    "No numbers were reported with hk.result, so any number you state from the printed "
    'output will show as unverified. Run the script again with hk.result("<name>", value) '
    "for each number you will mention."
)


# ---- the chat's files, for the model ------------------------------------------------------------

_log = logging.getLogger(__name__)


def _quoted(text: object) -> str:
    """A JSON string: one token, on one line, whatever the text holds."""
    return json.dumps(str(text), ensure_ascii=False)


def size_text(size: int) -> str:
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _details(found: ChatFile) -> str:
    if found.status != "ready" or not isinstance(found.summary, dict):
        return ", not checked yet"
    summary = found.summary
    sheets = summary.get("sheets") or []
    if found.kind in ("xlsx", "xls") and sheets:
        parts = [
            f"{_quoted(s['name'])} {s['rows']} rows × {s['columns']} columns" for s in sheets
        ]
        return ", sheets: " + "; ".join(parts)
    if found.kind == "pdf" and summary.get("pages") is not None:
        text = f", {summary['pages']} pages"
        scanned = summary.get("scanned_pages") or []
        if scanned:
            text += f", scanned pages: {', '.join(str(n) for n in scanned)} (use read_pages)"
        return text
    return ""


def column_list(columns: list) -> str:
    """Column names as JSON strings (owner data: one token each, on one line), the first 30
    and then how many more."""
    names = [_quoted(str(c)[:COLUMN_NAME_CHARS]) for c in columns[:COLUMNS_SHOWN]]
    extra = len(columns) - COLUMNS_SHOWN
    if extra > 0:
        names.append(f"…and {extra} more")
    return ", ".join(names)


def file_columns(found: ChatFile) -> list[str]:
    """The columns the attach-time check found (CSV, text, JSON, a workbook's first sheet);
    none for a PDF or a file that wasn't checked."""
    if found.kind == "pdf" or found.status != "ready" or not isinstance(found.summary, dict):
        return []
    columns = found.summary.get("columns")
    return [str(c) for c in columns] if isinstance(columns, list) else []


def _columns_text(found: ChatFile) -> str:
    columns = file_columns(found)
    if not columns:
        return ""
    where = "columns of the first sheet" if found.kind in ("xlsx", "xls") else "columns"
    return f"; {where}: {column_list(columns)}"


def analysis_context(files: ChatFileStore, chat_id: str) -> str:
    """The analysis prompt listing the chat's files, or "" for a chat without files. A file
    still being inspected is left out until it is settled."""
    found = [f for f in files.list(chat_id) if f.status != "pending"]
    if not found:
        return ""
    lines = [
        f"- {f.id}: {_quoted(f.name)} "
        f"({KIND_LABELS.get(f.kind, f.kind)}, {size_text(f.size)}){_details(f)}"
        f"{_columns_text(f)}"
        for f in found
    ]
    return ANALYSIS_PROMPT.format(files="\n".join(lines))


# ---- reading a run's results --------------------------------------------------------------------


def _read_results(path: Path | None) -> bytes:
    """results.jsonl, at most a little over 5 MB of it (parse_results notes the cut)."""
    if path is None:
        return b""
    with open(path, "rb") as f:
        return f.read(results_format.MAX_BYTES + 2)


def _parse(path: Path | None, members: set[str]) -> ParsedResults:
    try:
        raw = _read_results(path)
    except OSError:
        return ParsedResults(problems=[RESULTS_UNREADABLE])
    return parse_results(raw, members)


def _settled(work: asyncio.Future) -> ParsedResults:
    if work.done() and not work.cancelled() and work.exception() is None:
        return work.result()
    return ParsedResults(problems=[RESULTS_UNREADABLE])


# ---- what the model is told ---------------------------------------------------------------------


def _value_text(value: object) -> str:
    return "no value" if value is None else one_line(str(value))


def _csv_rows(columns: list, rows: list, numbers: list) -> str:
    """The header and rows as CSV, whole lines only, stopping at TABLE_CHARS."""
    lines: list[str] = []
    size = 0
    header = (["row"] if numbers else []) + [str(c) for c in columns]
    for index, row in enumerate([header, *rows]):
        if index > 0 and numbers:
            row = [numbers[index - 1], *row]
        line = csv_line(row)
        if size + len(line) + 1 > TABLE_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def _value_line(v) -> str:
    line = f"- {one_line(v.name)} = {_value_text(v.value)}"
    if v.unit:
        line += f" {one_line(v.unit)}"
    if v.kind == "document":
        line += f" (read from page {v.page})"
    if v.note:
        line += f" ({one_line(v.note)})"
    return line


def _table_block(o: OutputRecord) -> str:
    data = o.data or {}
    rows = list(data.get("rows") or [])[:TABLE_ROWS_SHOWN]
    numbers = list(data.get("row_numbers") or [])[: len(rows)]
    if len(numbers) != len(rows):
        numbers = []
    head = f"- table {o.id}: {one_line(o.name)}, {data.get('total_rows', len(rows))} rows"
    body = _csv_rows(data.get("columns") or [], rows, numbers)
    return f"{head}\n{body}" if body else head


def _section(header: str, items: list[str], cap: int) -> tuple[str, list[str], int]:
    """(header, the items shown at most, how many more there are)."""
    return header, items[:cap], max(len(items) - cap, 0)


def _exception_line(stdout: str) -> str:
    """The last traceback's exception line (the first unindented line after its frames), so
    notes printed after it don't hide it; without a traceback, the last printed line."""
    lines = stdout.splitlines()
    start = max((i for i, ln in enumerate(lines) if ln.startswith("Traceback (")), default=None)
    if start is not None:
        for line in lines[start + 1 :]:
            if line.strip() and not line[:1].isspace():
                return line.strip()
    filled = [ln for ln in lines if ln.strip()]
    return filled[-1].strip() if filled else ""


def _key_error(stdout: str) -> bool:
    """The script stopped on a KeyError (a column it guessed, usually)."""
    return bool(_KEY_ERROR.match(_exception_line(stdout)))


def _listed_columns(columns: dict[str, list[str]]) -> str:
    """The columns of at most 3 files and about 2000 characters, then how many more files."""
    if len(columns) == 1:
        chunks = [column_list(next(iter(columns.values())))]
    else:
        chunks = [f"in {_quoted(n)}: {column_list(c)}" for n, c in columns.items()]
    shown: list[str] = []
    size = 0
    for chunk in chunks[:KEY_ERROR_FILES]:
        if shown and size + len(chunk) > KEY_ERROR_CHARS:
            break
        if len(chunk) > KEY_ERROR_CHARS:  # one file alone is too wide: cut at a name
            chunk = chunk[:KEY_ERROR_CHARS].rsplit(", ", 1)[0] + ", …"
        shown.append(chunk)
        size += len(chunk) + 2
    text = "; ".join(shown)
    more = len(chunks) - len(shown)
    if more:
        text += f"; and {more} more file{'' if more == 1 else 's'}"
    return text


def _retry_text(rec: RunRecord, columns: dict[str, list[str]] | None) -> str:
    text = RETRY
    if columns and _key_error(rec.stdout or ""):
        text += f" The columns are: {_listed_columns(columns)}."
    return text


def format_run_for_model(
    rec: RunRecord,
    outputs: list[OutputRecord],
    *,
    columns: dict[str, list[str]] | None = None,
) -> str:
    """What the model is told about a run: at most CONTENT_BUDGET characters, each section
    capped, and nothing a script wrote can pass for the prompt's own text. A failed run ends
    by asking for a fixed run; after a KeyError it lists `columns` (file name -> the column
    names the attach-time check found), never file contents."""
    head = f"Run {rec.status} in {rec.duration_ms / 1000:.1f} s."
    sections: list[tuple[str, list[str], int]] = []
    if rec.stdout:
        printed = PRINTED_NOTE + "\n" + fenced(rec.stdout[:PRINTED_CHARS].rstrip("\n"))
        if len(rec.stdout) > PRINTED_CHARS or rec.stdout_truncated:
            printed += "\n" + PRINTED_CUT
        sections.append(("Printed output", [printed], 0))
    sections.append(_section("Results", [_value_line(v) for v in rec.values], CAPS["values"]))
    charts = [f"- chart {o.id}: {one_line(o.name)}" for o in outputs if o.kind == "chart"]
    sections.append(_section("Charts", charts, CAPS["charts"]))
    tables = [o for o in outputs if o.kind == "table" and isinstance(o.data, dict)]
    shown = tables[: CAPS["tables"]]
    sections.append(("Tables", [_table_block(o) for o in shown], len(tables) - len(shown)))
    files = [f"- file {o.id}: {one_line(o.name)}" for o in outputs if o.kind == "file"]
    sections.append(_section("Files", files, CAPS["files"]))
    cleaning = [f"- {one_line(n)}" for n in rec.cleaning]
    sections.append(_section("Cleaning notes", cleaning, CAPS["cleaning"]))
    problems = [f"- {one_line(p)}" for p in rec.problems]
    sections.append(_section("Problems", problems, CAPS["problems"]))
    # The error is a fixed, short message: room is kept for it whatever else is cut.
    tail = f"\n\nError:\n{one_line(rec.error)}" if rec.error else ""
    if rec.status == "failed":  # the script raised (or ran out of memory): never answer yet
        tail += "\n\n" + _retry_text(rec, columns)
    if rec.status == "ok" and not any(v.kind in ("computed", "document") for v in rec.values):
        tail += f"\n\nNote:\n{NO_NUMBERS}"  # e.g. the model wrote hk.result in its reply
    return assemble(head, sections, CONTENT_BUDGET - len(tail)) + tail


def unavailable_message(exc: SandboxUnavailable) -> str:
    """The plain message for a sandbox that can't run (its own text is already safe)."""
    if exc.user_message == NO_DOCKER_MESSAGE:
        return NO_DOCKER
    if exc.user_message == NO_IMAGE_MESSAGE:
        return NO_IMAGE
    return exc.user_message


# ---- the tool -----------------------------------------------------------------------------------


class PythonTool:
    name = "python"
    level_key = PYTHON_LEVEL_KEY
    description = DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "minLength": 1,
                "maxLength": SCRIPT_MAX_CHARS,
                "description": "The Python script to run (it can import hk).",
            },
            "purpose": {
                # No maxLength: a long purpose is cut in the summary, never refused.
                "type": "string",
                "minLength": 1,
                "description": "A few words on what the script works out, for the owner.",
            },
        },
        "required": ["script", "purpose"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        sandbox: SandboxHandle | SandboxRunner | None,
        files: ChatFileStore,
        runs: RunStore,
    ) -> None:
        self._sandbox = SandboxHandle.of(sandbox)
        self._files = files
        self._runs = runs

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        purpose = args.get("purpose")
        shown = one_line(purpose)[:PURPOSE_MAX_CHARS] if isinstance(purpose, str) else ""
        return f"Run Python: {shown}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        # Without Docker nothing runs at all: there is no fallback to the host.
        runner = await self._sandbox.get()
        if runner is None:
            return ToolResult(ok=False, content=NO_DOCKER)
        state = await runner.status()
        if not state.docker:
            return ToolResult(ok=False, content=NO_DOCKER)
        if not state.image:
            return ToolResult(ok=False, content=NO_IMAGE)
        script = args["script"]
        folder, manifest = await chat_manifest(self._files, ctx.chat_id)
        run_id = self._runs.new_id()
        try:
            raw = await runner.run(
                run_id, mode="script", files_dir=folder, manifest=manifest, script=script
            )
        except SandboxUnavailable as exc:
            return ToolResult(ok=False, content=unavailable_message(exc))
        try:
            rec = await self._save(raw, run_id, ctx, script)
        finally:
            raw.cleanup()
        columns = self._columns(ctx.chat_id, script) if rec.status == "failed" else None
        content = format_run_for_model(rec, self._runs.outputs(rec.id), columns=columns)
        return ToolResult(
            ok=rec.status == "ok",
            content=content,
            data={"attachments": [{"kind": "run", "id": rec.id}]},
        )

    def _columns(self, chat_id: str, script: str) -> dict[str, list[str]]:
        """Column names of the files the script reads with hk.read (by id or name), else of
        every file of the chat that has them."""
        found = [f for f in self._files.list(chat_id) if file_columns(f)]
        wanted = {m.group(2) for m in _READ_CALL.finditer(script)}
        read = [f for f in found if f.id in wanted or f.name in wanted]
        return {f.name: file_columns(f) for f in (read or found)}

    async def _save(self, raw: RawRun, run_id: str, ctx: ToolContext, script: str) -> RunRecord:
        """Parse the results off the event loop, then save the run. A cancellation while the
        results are read still saves the run first (every run is saved), then propagates."""
        if raw.status == "timeout":
            raw.error = TIMEOUT
        members = set(raw.members)
        work = asyncio.ensure_future(
            asyncio.to_thread(_parse, raw.members.get("results.jsonl"), members)
        )

        def save(parsed: ParsedResults) -> RunRecord:
            return self._runs.save(
                raw,
                parsed,
                run_id=run_id,
                chat_id=ctx.chat_id,
                turn_id=ctx.turn_id,
                purpose="analysis",
                script=script,
            )

        try:
            parsed = await asyncio.shield(work)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await work  # let the reader finish with the job folder before it goes
            try:
                save(_settled(work))
            except Exception as exc:  # the cancellation matters more; only the type is logged
                _log.error("could not save a cancelled run: %s", type(exc).__name__)
            raise
        except Exception as exc:
            # Parsing never raises by design; if it does, the run is saved as a sandbox error.
            _log.error("could not read a run's results: %s", type(exc).__name__)
            raw.status, raw.error = "sandbox_error", ERROR_OUTPUT
            parsed = ParsedResults(problems=[RESULTS_FAILED])
        return save(parsed)
