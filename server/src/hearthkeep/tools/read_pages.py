"""The `read_pages` tool: scanned PDF pages are rendered in the sandbox, read one by one by the
chat model's vision, and saved as a vision run. Its numbers are "read from page N" evidence
for the number check, never computed ones.

Everything the model wrote about a page is untrusted. Its reply must be one JSON object of the
exact shape VISION_PROMPT asks for, within MAX_REPLY_BYTES; anything else is a problem line
for that page and nothing is guessed. The reply only ever reaches the agent as this tool's
result (never the system prompt), with the page text on one line inside a fence nothing in it
can close, and tables as CSV lines, all within the python tool's character budget.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import inspect
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from hearthkeep.files.store import ChatFileStore
from hearthkeep.images import image_info
from hearthkeep.permissions import ActionClass
from hearthkeep.providers.chat import ProviderError, TextDelta
from hearthkeep.sandbox.handle import SandboxHandle
from hearthkeep.sandbox.manifest import chat_manifest
from hearthkeep.sandbox.results import (
    MAX_COLUMNS,
    MAX_NAME,
    MAX_ROWS,
    MAX_TEXT,
    ParsedResults,
    Table,
    Value,
)
from hearthkeep.sandbox.runner import RawRun, SandboxRunner, SandboxUnavailable
from hearthkeep.sandbox.runs import RunStore
from hearthkeep.tools.base import ToolContext, ToolResult
from hearthkeep.tools.python import NO_DOCKER, NO_IMAGE, PYTHON_LEVEL_KEY, unavailable_message
from hearthkeep.tools.text import (
    CONTENT_BUDGET,
    DATA_NOTE,
    MORE,
    assemble,
    csv_line,
    fenced,
    one_line,
)

__all__ = ["NO_DOCKER", "NO_IMAGE", "TABLE_NOTE", "VISION_PROMPT", "ReadPagesTool"]

VISION_PROMPT = (
    "You are reading one scanned page for Hearthkeep. Copy what is printed; never calculate, "
    "correct or guess. Reply with JSON only, in this shape: {\"text\": \"the page's text\", "
    '"tables": [{"columns": ["..."], "rows": [["..."]]}]}. Copy every number exactly as '
    'printed, as a string. Use "" for a cell you can\'t read.'
)
TABLE_NOTE = "Read from page {n} by the chat model, not computed"
ENDING = "These numbers were read from the page, not computed."

MAX_PAGES = 10
MAX_PAGE_NUMBER = 2000
DPI = 150
MAX_PAGE_SIDE = 2048  # what image_info must find (the sandbox renders at most 2000)
MAX_PAGE_BYTES = 15 * 1024 * 1024
MAX_REPLY_BYTES = 200 * 1024
MAX_TABLES = 20
MAX_VALUES = 10_000  # read numbers kept as values per run (every cell stays in its table)
PRINTED_MAX = 20000  # the page texts kept as the run's printed output
PAGE_TEXT_SHOWN = 2000
TABLE_ROWS_SHOWN = 50
TABLE_CHARS = 4000
PROBLEMS_SHOWN = 20
PAGE_TIMEOUT_S = 120.0  # wall-clock limit for the model to read one page
STATUS_BYTES = 65_536
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)

NOT_FOUND = "There's no file with that id in this chat."
NOT_PDF = "read_pages reads PDF pages only; that file is not a PDF."
PENDING = "That file is still being checked. Try again in a moment."
NOT_IN_PDF = "Page {n} isn't in this PDF."
RENDER_FAILED = "Hearthkeep couldn't render the pages of this PDF."
PAGE_NOT_RENDERED = "Couldn't render page {n}."
PAGE_RENDER_ERROR = "Couldn't render page {n} ({error})."
PAGE_TOO_SLOW = "Page {n} took too long to read."
PAGE_NOT_READ = "Couldn't read page {n}."
STOPPED = "Reading the pages was stopped."
STOPPED_EARLY = "Stopped before any page was read."
TOO_MANY_VALUES = "Only the first {n} numbers read were kept as values; the tables have them all."
DESCRIPTION = (
    "Read scanned pages (pages that are only an image) of a PDF attached to this chat. "
    "Hearthkeep renders each page in the sandbox and the chat model copies its text and tables. "
    "Numbers read this way are read from the page, not computed: say so when you use them."
)

_log = logging.getLogger(__name__)


# ---- the model's reply --------------------------------------------------------------------------


@dataclass
class PageRead:
    text: str
    tables: list[tuple[list[str], list[list[str]]]]


class _Bad(Exception):
    """The reply is not exactly the JSON asked for."""


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    found: dict = {}
    for key, value in pairs:
        if key in found:
            raise _Bad("duplicate key")
        found[key] = value
    return found


def _no_constant(name: str) -> None:
    raise _Bad("not a JSON number")


def _text(value: object, limit: int | None = None) -> str:
    if not isinstance(value, str) or (limit is not None and len(value) > limit):
        raise _Bad("not text")
    try:
        value.encode("utf-8", "strict")  # a lone surrogate (\\ud800) is not text
    except UnicodeEncodeError:
        raise _Bad("not text") from None
    return value


def _unfenced(reply: str) -> str:
    """The reply without one ``` (or ```json) fence around all of it."""
    text = reply.strip()
    if not text.startswith("```"):
        return text
    first, _, rest = text.partition("\n")
    if first.strip().lower() not in ("```", "```json") or not rest.rstrip().endswith("```"):
        raise _Bad("not one fenced block")
    return rest.rstrip()[:-3]


def _table(raw: object) -> tuple[list[str], list[list[str]]]:
    if not isinstance(raw, dict) or set(raw) != {"columns", "rows"}:
        raise _Bad("a table has exactly columns and rows")
    columns, rows = raw["columns"], raw["rows"]
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise _Bad("columns")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise _Bad("rows")
    clean_columns = [_text(c, MAX_NAME) for c in columns]
    clean_rows = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise _Bad("a row has one cell per column")
        clean_rows.append([_text(c, MAX_TEXT) for c in row])
    return clean_columns, clean_rows


def parse_page(reply: str) -> PageRead | None:
    """`{"text": str, "tables": [{"columns": [str], "rows": [[str]]}]}` exactly ("tables" may
    be left out), or None. Nothing is repaired or guessed."""
    try:
        found = json.loads(
            _unfenced(reply), object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
        if not isinstance(found, dict) or "text" not in found or set(found) - {"text", "tables"}:
            raise _Bad("shape")
        tables = found.get("tables", [])
        if not isinstance(tables, list) or len(tables) > MAX_TABLES:
            raise _Bad("tables")
        return PageRead(_text(found["text"]), [_table(t) for t in tables])
    except (_Bad, ValueError, RecursionError):
        return None


# A number as printed: an optional sign and currency sign, digits with or without thousands
# separators, an optional decimal part, and optionally a currency sign after it.
_CELL_NUMBER = re.compile(
    r"([-+−])?\s*[£$€¥₹]?\s*([-+−])?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)\s*[£$€¥₹]?"
)


def cell_number(cell: str) -> int | float | None:
    """The cell as a number when all of it is one (currency signs and thousands separators
    removed); None otherwise. A decimal comma ("1,5") is not a number here: nothing is
    guessed."""
    m = _CELL_NUMBER.fullmatch(cell.strip())
    if m is None or (m[1] and m[2]):
        return None
    digits = m[3].replace(",", "")
    number: int | float = float(digits) if "." in digits else int(digits)
    if (m[1] or m[2]) in ("-", "−"):
        number = -number
    if not math.isfinite(number) or abs(number) >= 2**63:
        return None
    return number


# ---- what the model is told ---------------------------------------------------------------------


def _page_text(text: str) -> str:
    """The page text with whitespace collapsed, on one line, its first characters only."""
    return one_line(" ".join(text.split()))[:PAGE_TEXT_SHOWN]


def _table_block(index: int, columns: list[str], rows: list[list[str]]) -> str:
    lines: list[str] = []
    size = 0
    for row in [columns, *rows[:TABLE_ROWS_SHOWN]]:
        line = csv_line(row)
        if size + len(line) + 1 > TABLE_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    hidden = len(rows) - max(len(lines) - 1, 0)
    if hidden:
        lines.append(MORE.format(n=hidden))
    return f"- table {index}: {len(rows)} rows\n" + "\n".join(lines)


def format_pages_for_model(read: list[tuple[int, PageRead]], problems: list[str]) -> str:
    """At most CONTENT_BUDGET characters, ending with ENDING whatever is cut."""
    shown = ", ".join(str(n) for n, _ in read) or "none"
    head = f"Pages read by the chat model: {shown}."
    if read:
        head += "\n" + DATA_NOTE
    sections: list[tuple[str, list[str], int]] = []
    if problems:
        lines = [f"- {one_line(p)}" for p in problems]
        sections.append(("Problems", lines[:PROBLEMS_SHOWN], max(len(lines) - PROBLEMS_SHOWN, 0)))
    for n, page in read:
        text = _page_text(page.text)
        items = ["Text:\n" + fenced(text) if text else "Text: (none)"]
        items += [_table_block(t, c, r) for t, (c, r) in enumerate(page.tables, start=1)]
        sections.append((f"Page {n}", items, 0))
    tail = "\n\n" + ENDING
    return assemble(head, sections, CONTENT_BUDGET - len(tail)) + tail


# ---- the run ------------------------------------------------------------------------------------


def _page_image(path: Path | None) -> str | None:
    """The rendered page as a data URL, when it is a PNG with sides of at most MAX_PAGE_SIDE."""
    if path is None:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_PAGE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_PAGE_BYTES:
        return None
    info = image_info(data)
    if info is None or info.mime != "image/png" or info.animated:
        return None
    if max(info.width, info.height) > MAX_PAGE_SIDE:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _value_name(page: int, table: int, row: int, column: str) -> str:
    return one_line(f"Page {page}, table {table}, row {row}: {column}")[:MAX_NAME]


def _results(read: list[tuple[int, PageRead]], problems: list[str]) -> ParsedResults:
    parsed = ParsedResults(problems=list(problems))
    capped = False
    for n, page in read:
        for t, (columns, rows) in enumerate(page.tables, start=1):
            note = TABLE_NOTE.format(n=n)
            parsed.tables.append(Table(f"Page {n}, table {t}", columns, rows, [], note, len(rows)))
            for r, row in enumerate(rows, start=1):
                for column, cell in zip(columns, row, strict=True):
                    number = cell_number(cell)
                    if number is None:
                        continue
                    if len(parsed.values) >= MAX_VALUES:
                        capped = True
                        continue
                    name = _value_name(n, t, r, column)
                    parsed.values.append(Value(name, number, None, None, False, "document", n))
    if capped:
        parsed.problems.append(TOO_MANY_VALUES.format(n=MAX_VALUES))
    return parsed


def _printed(read: list[tuple[int, PageRead]]) -> tuple[str, bool]:
    """Each page's text, as read, at most PRINTED_MAX characters in all."""
    text = "\n\n".join(f"Page {n}\n{page.text}" for n, page in read)
    return text[:PRINTED_MAX], len(text) > PRINTED_MAX


def _render_report(path: Path | None) -> tuple[int | None, dict[int, str]]:
    """The PDF's page count and each failed page's error type name, from status.json.
    Anything unexpected in it is ignored."""
    if path is None:
        return None, {}
    try:
        with open(path, "rb") as f:
            status = json.loads(f.read(STATUS_BYTES).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None, {}
    if not isinstance(status, dict):
        return None, {}
    count = status.get("page_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = None
    errors: dict[int, str] = {}
    found = status.get("page_errors")
    for key, name in found.items() if isinstance(found, dict) else ():
        if not (isinstance(key, str) and key.isascii() and key.isdigit() and len(key) <= 4):
            continue
        if isinstance(name, str) and _ERROR_TYPE.fullmatch(name):
            errors[int(key)] = name
    return count, errors


async def _off_loop(fn, *args):
    """`fn(*args)` in a worker thread. A cancellation first waits for the worker (it may be
    reading the run's job folder, which is removed next), then propagates."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await work
        raise


def _takes_temperature(chat) -> bool:
    try:
        params = inspect.signature(chat.stream).parameters
    except (TypeError, ValueError):
        return False
    return "temperature" in params


class ReadPagesTool:
    name = "read_pages"
    level_key = PYTHON_LEVEL_KEY
    description = DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,32}$",
                "description": "The id of a PDF attached to this chat.",
            },
            "pages": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_NUMBER},
                "minItems": 1,
                "maxItems": MAX_PAGES,
                "uniqueItems": True,
                "description": "The page numbers to read (1 is the first page).",
            },
        },
        "required": ["file", "pages"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        sandbox: SandboxHandle | SandboxRunner | None,
        files: ChatFileStore,
        runs: RunStore,
        chat,
    ) -> None:
        self._sandbox = SandboxHandle.of(sandbox)
        self._files = files
        self._runs = runs
        self._chat = chat

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK

    def summarize(self, args: dict) -> str:
        found = self._files.get(args.get("file"))
        name = one_line(found.name) if found is not None else str(args.get("file", ""))
        pages = ", ".join(str(n) for n in args.get("pages") or [])
        return f"Read scanned pages {pages} of {name}"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        found = self._files.get(args["file"])
        if found is None or found.chat_id != ctx.chat_id:
            return ToolResult(ok=False, content=NOT_FOUND)
        if found.kind != "pdf":
            return ToolResult(ok=False, content=NOT_PDF)
        if found.status == "pending":
            return ToolResult(ok=False, content=PENDING)
        pages = [int(n) for n in args["pages"]][:MAX_PAGES]
        count = (found.summary or {}).get("pages") if isinstance(found.summary, dict) else None
        problems: list[str] = []
        if isinstance(count, int) and not isinstance(count, bool):
            problems = [NOT_IN_PDF.format(n=n) for n in pages if n > count]
            pages = [n for n in pages if n <= count]
        if not pages:
            return ToolResult(ok=False, content=" ".join(problems))

        # Without Docker nothing runs at all: there is no fallback to the host.
        runner = await self._sandbox.get()
        if runner is None:
            return ToolResult(ok=False, content=NO_DOCKER)
        state = await runner.status()
        if not state.docker:
            return ToolResult(ok=False, content=NO_DOCKER)
        if not state.image:
            return ToolResult(ok=False, content=NO_IMAGE)
        folder, manifest = await chat_manifest(
            self._files, ctx.chat_id, {"file": found.id, "pages": pages, "dpi": DPI}
        )
        if found.id not in {f["id"] for f in manifest["files"]}:
            return ToolResult(ok=False, content=NOT_FOUND)
        run_id = self._runs.new_id()
        started = time.monotonic()
        try:
            raw = await runner.run(run_id, mode="pages", files_dir=folder, manifest=manifest)
        except SandboxUnavailable as exc:
            return ToolResult(ok=False, content=unavailable_message(exc))
        except asyncio.CancelledError:
            # Stopped while rendering (the runner removes its own job folder then): the run
            # is still saved, with no pages.
            stopped = RawRun("failed", None, "", False, 0, {}, STOPPED)
            self._save_stopped(stopped, run_id, ctx, [], [*problems, STOPPED_EARLY], started)
            raise
        try:
            return await self._read(raw, run_id, ctx, pages, problems, started)
        finally:
            raw.cleanup()

    def _save(
        self,
        raw: RawRun,
        run_id: str,
        ctx: ToolContext,
        read: list[tuple[int, PageRead]],
        problems: list[str],
        started: float,
        status: str,
        error: str | None,
    ) -> None:
        stdout, cut = _printed(read)
        saved = dataclasses.replace(
            raw,
            status=status,
            error=error,
            stdout=stdout,
            stdout_truncated=cut,
            duration_ms=int((time.monotonic() - started) * 1000),
            members={},
        )
        self._runs.save(
            saved, _results(read, problems), run_id=run_id, chat_id=ctx.chat_id,
            turn_id=ctx.turn_id, purpose="vision", script=None,
        )  # fmt: skip

    def _save_stopped(
        self,
        raw: RawRun,
        run_id: str,
        ctx: ToolContext,
        read: list[tuple[int, PageRead]],
        problems: list[str],
        started: float,
    ) -> None:
        """Every run is saved, a stopped one too; the cancellation matters more than a save
        that fails (only its type is logged)."""
        try:
            self._save(raw, run_id, ctx, read, problems, started, "failed", STOPPED)
        except Exception as exc:
            _log.error("could not save a stopped vision run: %s", type(exc).__name__)

    async def _read(
        self,
        raw: RawRun,
        run_id: str,
        ctx: ToolContext,
        pages: list[int],
        problems: list[str],
        started: float,
    ) -> ToolResult:
        attachments = {"attachments": [{"kind": "run", "id": run_id}]}
        read: list[tuple[int, PageRead]] = []

        def save(status: str, error: str | None) -> None:
            self._save(raw, run_id, ctx, read, problems, started, status, error)

        if raw.status != "ok":
            # The container's own words (raw.stdout) are never kept or shown.
            error = raw.error if raw.status != "failed" and raw.error else RENDER_FAILED
            save(raw.status, error)
            return ToolResult(ok=False, content=error, data=attachments)

        try:
            page_count, page_errors = await _off_loop(
                _render_report, raw.members.get("status.json")
            )
            if page_count is not None:  # the inspect summary didn't know the page count
                problems.extend(NOT_IN_PDF.format(n=n) for n in pages if n > page_count)
                pages = [n for n in pages if n <= page_count]
            hold = getattr(self._chat, "hold", None)
            # One chat-model lease for all the pages of this call, when the client has one.
            async with hold() if callable(hold) else contextlib.nullcontext():
                for n in pages:
                    page = await self._page(n, raw, page_errors, problems)
                    if page is not None:
                        read.append((n, page))
        except ProviderError as exc:
            save("failed", exc.user_message)
            return ToolResult(ok=False, content=exc.user_message, data=attachments)
        except asyncio.CancelledError:
            self._save_stopped(raw, run_id, ctx, read, problems, started)
            raise
        save("ok" if read else "failed", None)
        content = format_pages_for_model(read, problems)
        return ToolResult(ok=bool(read), content=content, data=attachments)

    async def _page(
        self, n: int, raw: RawRun, page_errors: dict[int, str], problems: list[str]
    ) -> PageRead | None:
        """One page read, or None with its problem added."""
        path = raw.members.get(f"pages/{n}.png")
        url = await _off_loop(_page_image, path)
        if url is None:
            error = page_errors.get(n) if path is None else None
            problem = PAGE_RENDER_ERROR.format(n=n, error=error) if error else None
            problems.append(problem or PAGE_NOT_RENDERED.format(n=n))
            return None
        try:
            async with asyncio.timeout(PAGE_TIMEOUT_S):
                reply = await self._ask(n, url)
        except TimeoutError:
            problems.append(PAGE_TOO_SLOW.format(n=n))
            return None
        page = parse_page(reply) if reply is not None else None
        if page is None:
            problems.append(PAGE_NOT_READ.format(n=n))
        return page

    async def _ask(self, n: int, url: str) -> str | None:
        """The model's reply for one page (its text only), or None when it is over
        MAX_REPLY_BYTES. Same chat model as the turn; think off, no tools, temperature 0 when
        the client takes one."""
        messages = [
            {"role": "system", "content": VISION_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Page {n}."},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ]
        extra = {"temperature": 0} if _takes_temperature(self._chat) else {}
        parts: list[str] = []
        size = 0
        # A tool runs after the turn's own stream was closed, so this takes its own GPU lease.
        async with contextlib.aclosing(self._chat.stream(messages, [], False, **extra)) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    size += len(event.text.encode("utf-8"))
                    if size > MAX_REPLY_BYTES:
                        return None
                    parts.append(event.text)
        return "".join(parts)
