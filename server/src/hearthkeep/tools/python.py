"""The `python` tool: the model's analysis scripts run in the Docker sandbox (never on the
host), every run is saved, and the model is told what the run reported.

The model sees only what `format_run_for_model` writes: the run's status and time, its
printed output, results, outputs, cleaning notes, problems and its fixed error message. Never
Docker's own words (`RawRun.diagnostics`), host paths, or where the owner's files came from.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import logging
import unicodedata
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
# What the model is told about one run is bounded: items per section, and characters overall.
CONTENT_BUDGET = 20000
CAPS = {"values": 50, "tables": 5, "charts": 20, "files": 20, "cleaning": 20, "problems": 20}
MORE = "… {n} more (see the run)."
_MORE_ROOM = len(MORE.format(n=10**9)) + 1

ANALYSIS_PROMPT = (
    "This chat has files the owner attached:\n{files}\n\nTo answer questions about them, use "
    "the python tool. Scripts run in a sandbox with no internet, and the files are read-only. "
    "Use the hk library: hk.read(file_id) loads a file (it handles encodings, separators, "
    "header rows and sheets); hk.result(name, value, unit=None) reports a number, date or "
    "count; hk.bar, hk.line and hk.scatter (or hk.chart with a Vega-Lite spec) make charts, "
    "with highlight= for problem areas; hk.table(name, rows) reports the rows behind a "
    "finding; hk.file(name, data) saves a cleaned file; hk.cleaned(note) records anything you "
    "cleaned or assumed; hk.pdf_text and hk.pdf_tables read PDFs that have text.\nRules: never "
    "compute, estimate or round a number yourself. Every number, date and count in your reply "
    "must come from a result of a run in this chat, written as reported or rounded. Start "
    "with two or three plain sentences that answer the question. Then say what you had to "
    "clean or assume. If a script fails or the data can't answer the question, say what went "
    "wrong and what the owner could do; never estimate. When files arrive without a question, "
    "run hk.profile(file_id) once and reply with a short 'at a glance' summary that ends by "
    "inviting a question."
)
KIND_LABELS = {
    "csv": "CSV",
    "xlsx": "Excel workbook",
    "xls": "Excel 97-2003 workbook",
    "pdf": "PDF",
    "txt": "Text",
    "json": "JSON",
}
DESCRIPTION = (
    "Run a Python script in a locked-down sandbox to analyse the files attached to this chat. "
    "There is no internet and the files are read-only. Use the hk library (hk.read, "
    "hk.result, hk.bar/line/scatter/chart, hk.table, hk.file, hk.cleaned, hk.profile, "
    "hk.pdf_text, hk.pdf_tables) to report what you found; only what hk reports and what the "
    "script prints comes back."
)


# ---- the chat's files, for the model ------------------------------------------------------------

_log = logging.getLogger(__name__)


def one_line(text: str) -> str:
    """Control characters (line breaks included) and line/paragraph separators as spaces."""
    return "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in text)


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


def analysis_context(files: ChatFileStore, chat_id: str) -> str:
    """The analysis prompt listing the chat's files, or "" for a chat without files. A file
    still being inspected is left out until it is settled."""
    found = [f for f in files.list(chat_id) if f.status != "pending"]
    if not found:
        return ""
    lines = [
        f"- {f.id}: {_quoted(f.name)} "
        f"({KIND_LABELS.get(f.kind, f.kind)}, {size_text(f.size)}){_details(f)}"
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


def _fenced(text: str) -> str:
    """`text` in a backtick fence longer than any run of backticks inside it, so nothing the
    script printed can close the block."""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _csv_line(cells: list) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow(
        ["" if c is None else one_line(c) if isinstance(c, str) else c for c in cells]
    )
    return out.getvalue()


def _csv_rows(columns: list, rows: list, numbers: list) -> str:
    """The header and rows as CSV, whole lines only, stopping at TABLE_CHARS."""
    lines: list[str] = []
    size = 0
    header = (["row"] if numbers else []) + [str(c) for c in columns]
    for index, row in enumerate([header, *rows]):
        if index > 0 and numbers:
            row = [numbers[index - 1], *row]
        line = _csv_line(row)
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


def _assemble(head: str, sections: list[tuple[str, list[str], int]], budget: int) -> str:
    """The sections after `head`, within `budget` characters. A section cut short (by its cap
    or by the budget) ends with a "more" line; once the budget is hit, nothing more is added."""
    text = head
    for header, items, hidden in sections:
        if not items:
            continue
        start = f"\n\n{header}:"
        if len(text) + len(start) + 1 + _MORE_ROOM > budget:
            break
        size = len(text) + len(start)
        kept: list[str] = []
        for item in items:
            if size + 1 + len(item) + 1 + _MORE_ROOM > budget:
                break
            kept.append(item)
            size += 1 + len(item)
        more = hidden + len(items) - len(kept)
        lines = kept + ([MORE.format(n=more)] if more else [])
        text += start + "\n" + "\n".join(lines)
        if len(kept) < len(items):
            break
    return text


def format_run_for_model(rec: RunRecord, outputs: list[OutputRecord]) -> str:
    """What the model is told about a run: at most CONTENT_BUDGET characters, each section
    capped, and nothing a script wrote can pass for the prompt's own text."""
    head = f"Run {rec.status} in {rec.duration_ms / 1000:.1f} s."
    sections: list[tuple[str, list[str], int]] = []
    if rec.stdout:
        printed = _fenced(rec.stdout[:PRINTED_CHARS].rstrip("\n"))
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
    return _assemble(head, sections, CONTENT_BUDGET - len(tail)) + tail


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
                "type": "string",
                "minLength": 1,
                "maxLength": PURPOSE_MAX_CHARS,
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
        return f"Run Python: {args.get('purpose', '')}"

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
        content = format_run_for_model(rec, self._runs.outputs(rec.id))
        return ToolResult(
            ok=rec.status == "ok",
            content=content,
            data={"attachments": [{"kind": "run", "id": rec.id}]},
        )

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
