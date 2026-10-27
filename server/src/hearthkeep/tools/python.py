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
from pathlib import Path

from hearthkeep.files.store import ChatFile, ChatFileStore
from hearthkeep.images import inside
from hearthkeep.permissions import ActionClass
from hearthkeep.sandbox import results as results_format
from hearthkeep.sandbox.results import ParsedResults, parse_results
from hearthkeep.sandbox.runner import (
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

NO_DOCKER = "Python analysis needs Docker Desktop. Start Docker on the PC and ask again."
NO_IMAGE = (
    "The Python sandbox isn't built yet. On the PC, run: hearthkeep sandbox build --data <data>"
)
TIMEOUT = "The script ran for over 120 seconds and was stopped."
RESULTS_UNREADABLE = "results.jsonl could not be read"
PRINTED_CUT = "(printed output cut)"

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


# ---- the chat's files, for the sandbox and for the model ----------------------------------------


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
        parts = [f"{s['name']} {s['rows']} rows × {s['columns']} columns" for s in sheets]
        return ", sheets: " + "; ".join(parts)
    if found.kind == "pdf" and summary.get("pages") is not None:
        text = f", {summary['pages']} pages"
        scanned = summary.get("scanned_pages") or []
        if scanned:
            text += f", scanned pages: {', '.join(str(n) for n in scanned)} (use read_pages)"
        return text
    return ""


def analysis_context(files: ChatFileStore, chat_id: str) -> str:
    """The analysis prompt listing the chat's files, or "" for a chat without files."""
    found = files.list(chat_id)
    if not found:
        return ""
    lines = [
        f"- {f.id}: {json.dumps(f.name, ensure_ascii=False)} "
        f"({KIND_LABELS.get(f.kind, f.kind)}, {size_text(f.size)}){_details(f)}"
        for f in found
    ]
    return ANALYSIS_PROMPT.format(files="\n".join(lines))


def _manifest_entries(files: ChatFileStore, chat_id: str) -> list[tuple[dict, str]]:
    """(entry, stored name) for each of the chat's files; the names are checked on disk later."""
    entries = []
    for found in files.list(chat_id):
        name = files.stored_name(found.id)
        if name is None:
            continue
        entry = {"id": found.id, "name": found.name, "kind": found.kind, "path": f"/data/{name}"}
        entries.append((entry, name))
    return entries


def _prepare_folder(folder: Path, entries: list[tuple[dict, str]]) -> list[dict]:
    """Create the chat's files folder; keep only the files that are really in it."""
    folder.mkdir(parents=True, exist_ok=True)
    return [entry for entry, name in entries if inside(folder, name) is not None]


async def chat_manifest(files: ChatFileStore, chat_id: str, args: dict | None = None):
    """(the chat's files folder, a sandbox manifest of its files). File checks run off the
    event loop."""
    folder = files.files_dir(chat_id)
    entries = _manifest_entries(files, chat_id)
    kept = await asyncio.to_thread(_prepare_folder, folder, entries)
    return folder, {"files": kept, "args": dict(args or {})}


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
    return "no value" if value is None else str(value)


def _csv_rows(columns: list, rows: list, numbers: list) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow((["row"] if numbers else []) + [str(c) for c in columns])
    for index, row in enumerate(rows):
        cells = ["" if cell is None else cell for cell in row]
        writer.writerow(([numbers[index]] if numbers else []) + cells)
    return out.getvalue().rstrip("\n")


def format_run_for_model(rec: RunRecord, outputs: list[OutputRecord]) -> str:
    sections = [f"Run {rec.status} in {rec.duration_ms / 1000:.1f} s."]

    if rec.stdout:
        printed = rec.stdout[:PRINTED_CHARS]
        if len(rec.stdout) > PRINTED_CHARS or rec.stdout_truncated:
            printed = printed.rstrip("\n") + "\n" + PRINTED_CUT
        sections.append("Printed output:\n" + printed.rstrip("\n"))

    if rec.values:
        lines = []
        for v in rec.values:
            line = f"- {v.name} = {_value_text(v.value)}"
            if v.unit:
                line += f" {v.unit}"
            if v.kind == "document":
                line += f" (read from page {v.page})"
            if v.note:
                line += f" ({v.note})"
            lines.append(line)
        sections.append("Results:\n" + "\n".join(lines))

    charts = [o for o in outputs if o.kind == "chart"]
    if charts:
        sections.append("Charts:\n" + "\n".join(f"- chart {o.id}: {o.name}" for o in charts))

    tables = [o for o in outputs if o.kind == "table" and isinstance(o.data, dict)]
    if tables:
        blocks = []
        for o in tables:
            data = o.data or {}
            rows = list(data.get("rows") or [])[:TABLE_ROWS_SHOWN]
            numbers = list(data.get("row_numbers") or [])[: len(rows)]
            if len(numbers) != len(rows):
                numbers = []
            head = f"- table {o.id}: {o.name}, {data.get('total_rows', len(rows))} rows"
            blocks.append(head + "\n" + _csv_rows(data.get("columns") or [], rows, numbers))
        sections.append("Tables:\n" + "\n".join(blocks))

    files = [o for o in outputs if o.kind == "file"]
    if files:
        sections.append("Files:\n" + "\n".join(f"- file {o.id}: {o.name}" for o in files))
    if rec.cleaning:
        sections.append("Cleaning notes:\n" + "\n".join(f"- {n}" for n in rec.cleaning))
    if rec.problems:
        sections.append("Problems:\n" + "\n".join(f"- {p}" for p in rec.problems))
    if rec.error:
        sections.append("Error:\n" + rec.error)
    return "\n\n".join(sections)


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
        self, sandbox: SandboxRunner | None, files: ChatFileStore, runs: RunStore
    ) -> None:
        self._sandbox = sandbox
        self._files = files
        self._runs = runs

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        return f"Run Python: {args.get('purpose', '')}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        # Without Docker nothing runs at all: there is no fallback to the host.
        if self._sandbox is None:
            return ToolResult(ok=False, content=NO_DOCKER)
        state = await self._sandbox.status()
        if not state.docker:
            return ToolResult(ok=False, content=NO_DOCKER)
        if not state.image:
            return ToolResult(ok=False, content=NO_IMAGE)
        script = args["script"]
        folder, manifest = await chat_manifest(self._files, ctx.chat_id)
        run_id = self._runs.new_id()
        try:
            raw = await self._sandbox.run(
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
            save(_settled(work))
            raise
        return save(parsed)
