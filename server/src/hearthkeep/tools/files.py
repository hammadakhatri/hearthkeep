"""The `files` tool: the model looks in the folders the owner shared. It can search them, read
the start of a text file, and bring a file into the chat for analysis. Nothing writes, moves
or deletes anything in the owner's folders.

The model sees `{folder label}/{relative path}` only, never an absolute path. Names and labels
are put on one line; text read from a file is untrusted, so it follows DATA_NOTE inside a fence
nothing in it can close. All path rules live in `hearthkeep.folders`.
"""

from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
from datetime import date

from hearthkeep import audit
from hearthkeep.files.checks import (
    FILE_KINDS,
    MAX_FILE_BYTES,
    FileCheck,
    FileRefused,
    check_file,
    clean_file_name,
)
from hearthkeep.files.store import ChatFile, ChatFileStore, StagedFile
from hearthkeep.folders import (
    FOLDER_MOVED,
    MAX_MATCHES,
    NO_FILE,
    ONLINE_SKIPPED,
    UNKNOWN_FOLDER,
    AllowedFolder,
    FolderError,
    FolderStore,
)
from hearthkeep.permissions import ActionClass
from hearthkeep.sandbox.inspect import Inspector, inspect_recorded
from hearthkeep.tools.base import ToolContext, ToolResult
from hearthkeep.tools.python import size_text
from hearthkeep.tools.text import DATA_NOTE, fenced, one_line

FILES_LEVEL_KEY = "files"
ACTIONS = ("search", "read", "bring")
READ_CHARS = 20_000
READ_BYTES = 1024 * 1024  # never more than this is read from disk for `read`
READ_EXTS = ("txt", "csv", "json", "md", "log")
QUERY_MAX_CHARS = 200
LABEL_MAX_CHARS = 200
PATH_MAX_CHARS = 1024
SHOWN_CHARS = 300  # a name or path shown back to the model or the owner, at most

USE_BRING = "Use bring to analyse this file."
NO_FOLDERS = "No folders are shared with Hearthkeep yet. The owner can add one in Settings."
NO_MATCHES = "No files matched."
NEEDS_PATH = "Give the file's path as shown by search: folder/path/to/file."
BAD_DATE = "Dates must be written YYYY-MM-DD."
CUT = "The search stopped early (too many files); narrow it with a query, folder or kinds."
NOT_TEXT = "Hearthkeep couldn't read that file as text. " + USE_BRING
DESCRIPTION = (
    "Look in the folders the owner shared with Hearthkeep. action=search lists files "
    "(optionally by query words, folder, kinds and modified dates, YYYY-MM-DD, inclusive), "
    "newest first, as folder/path. action=read returns the start of a text file (txt, csv, "
    "json, md, log). action=bring copies a file into this chat so the python tool can "
    "analyse it. Give folder and path, or path as folder/path exactly as search showed it. "
    "Nothing in the owner's folders is ever changed."
)


@dataclass(frozen=True)
class _Target:
    folder: AllowedFolder
    rel: str

    @property
    def shown(self) -> str:
        return _shown(f"{self.folder.label}/{self.rel}")


def _shown(text: str) -> str:
    return one_line(text)[:SHOWN_CHARS]


def _date(value: object) -> date | None:
    """A YYYY-MM-DD date, or ValueError."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(value)
    return date.fromisoformat(value)


def _decode(data: bytes, complete: bool) -> str | None:
    """Text in UTF-8 (with or without a BOM), UTF-16 with a BOM, or else cp1252; None for
    something that isn't text."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates = ["utf-16"]
    elif b"\x00" in data:
        return None
    elif data.startswith(codecs.BOM_UTF8):
        candidates = ["utf-8-sig"]
    else:
        candidates = ["utf-8", "cp1252"]
    for encoding in candidates:
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            return decoder.decode(data, final=complete)
        except UnicodeDecodeError:
            continue
    return None


class FilesTool:
    name = "files"
    level_key = FILES_LEVEL_KEY
    description = DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "query": {
                "type": "string",
                "maxLength": QUERY_MAX_CHARS,
                "description": "Words that must all appear in the file's folder/path.",
            },
            "folder": {
                "type": "string",
                "maxLength": LABEL_MAX_CHARS,
                "description": "A shared folder's name, as search shows it.",
            },
            "path": {
                "type": "string",
                "maxLength": PATH_MAX_CHARS,
                "description": "The file's path inside the folder (or folder/path).",
            },
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": list(FILE_KINDS)},
                "maxItems": len(FILE_KINDS),
            },
            "modified_after": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "modified_before": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(
        self, folders: FolderStore, files: ChatFileStore, inspector: Inspector | None
    ) -> None:
        self._folders = folders
        self._files = files
        self._inspector = inspector

    def classify(self, args: dict) -> ActionClass:
        return ActionClass.LOOK  # nothing in the owner's folders is ever changed

    def summarize(self, args: dict) -> str:
        action = args.get("action")
        path = _shown(str(args.get("path") or ""))
        if action == "read":
            return f"Read {path} from a shared folder"
        if action == "bring":
            return f"Bring {path} from a shared folder into this chat"
        query = _shown(str(args.get("query") or ""))
        return f"Search the shared folders for {query}" if query else "Search the shared folders"

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        action = args.get("action")
        try:
            if action == "search":
                return await self._search(args)
            if action in ("read", "bring"):
                target = self._target(args)
                if action == "read":
                    return await self._read(target)
                return await self._bring(target, ctx)
        except FolderError as error:
            return ToolResult(ok=False, content=error.message)
        return ToolResult(ok=False, content="Use action search, read or bring.")

    # ---- search -------------------------------------------------------------------------------

    async def _search(self, args: dict) -> ToolResult:
        try:
            after, before = _date(args.get("modified_after")), _date(args.get("modified_before"))
        except ValueError:
            return ToolResult(ok=False, content=BAD_DATE)
        kinds = args.get("kinds")
        if kinds is not None and (
            not isinstance(kinds, list) or any(k not in FILE_KINDS for k in kinds)
        ):
            return ToolResult(ok=False, content="Kinds must be among: " + ", ".join(FILE_KINDS))
        query = args.get("query")
        label = args.get("folder")
        if label is not None:
            chosen = self._folders.by_label(str(label))
            if chosen is None:
                return ToolResult(ok=False, content=UNKNOWN_FOLDER)
            targets = [chosen]
        else:
            targets = self._folders.list()
            if not targets:
                return ToolResult(ok=True, content=NO_FOLDERS)
        outcome = await asyncio.to_thread(
            self._folders.search_in,
            targets,
            query=str(query) if query is not None else None,
            kinds=kinds,
            modified_after=after,
            modified_before=before,
        )
        if label is not None and outcome.moved:
            return ToolResult(ok=False, content=FOLDER_MOVED)
        lines = [
            f"{_shown(f.folder)}/{_shown(f.path)} · {f.kind} · {size_text(f.size)} · "
            f"modified {f.modified[:10]}"
            for f in outcome.found
        ]
        shown = "the newest " if len(lines) == MAX_MATCHES else ""
        parts = [f"Shared folders: {', '.join(_shown(f.label) for f in targets)}"]
        if lines:
            parts.append(f"Found {shown}{len(lines)} files (newest first):\n" + "\n".join(lines))
        else:
            parts.append(NO_MATCHES)
        if outcome.cut:
            parts.append(CUT)
        if outcome.online_only > 0:
            parts.append(ONLINE_SKIPPED.format(n=outcome.online_only))
        for moved in outcome.moved:
            parts.append(f"{_shown(moved)}: {FOLDER_MOVED}")
        return ToolResult(ok=True, content="\n\n".join(parts))

    # ---- read and bring -------------------------------------------------------------------------

    def _target(self, args: dict) -> _Target:
        raw = args.get("path")
        if not isinstance(raw, str) or not raw:
            raise FolderError(NEEDS_PATH)
        label = args.get("folder")
        if label is None:
            # "folder/path" exactly as search shows it.
            label, sep, raw = raw.replace("\\", "/").partition("/")
            if not sep:
                raise FolderError(NEEDS_PATH)
        folder = self._folders.by_label(str(label))
        if folder is None:
            raise FolderError(UNKNOWN_FOLDER)
        return _Target(folder, raw)

    async def _read(self, target: _Target) -> ToolResult:
        ext = target.rel.rpartition(".")[2].lower() if "." in target.rel else ""
        text = await asyncio.to_thread(self._read_head, target, ext in READ_EXTS)
        if text is None:
            return ToolResult(ok=True, content=USE_BRING)
        cut = len(text) > READ_CHARS
        head = f"{target.shown}" + (f" (the first {READ_CHARS:,} characters)" if cut else "")
        return ToolResult(ok=True, content=f"{head}\n{DATA_NOTE}\n{fenced(text[:READ_CHARS])}")

    def _read_head(self, target: _Target, readable: bool) -> str | None:
        """The start of the file as text, read from the checked handle (at most READ_BYTES);
        None when it isn't a text kind. Runs on a worker thread."""
        handle, _ = self._folders.open_checked(target.folder, target.rel)
        with handle:
            if not readable:
                return None
            data = handle.read(READ_BYTES)
            complete = len(data) < READ_BYTES
        text = _decode(data, complete)
        if text is None:
            raise FolderError(NOT_TEXT)
        return text

    def _copy(self, target: _Target, chat_id: str) -> tuple[StagedFile, str, FileCheck]:
        """Copy from the checked handle into a staged file, then check what it is. Runs on a
        worker thread (no database)."""
        handle, path = self._folders.open_checked(target.folder, target.rel)
        with handle:
            staged = self._files.stage_stream(chat_id, handle, MAX_FILE_BYTES)
        try:
            check = check_file(staged.tmp, path.name)
        except BaseException:
            self._files.discard(staged)
            raise
        return staged, clean_file_name(path.name, check.ext), check

    async def _discard_when_copied(self, copying: asyncio.Future) -> None:
        """After a cancel: wait for the copy to finish (even through further cancels), then
        remove what it staged, so nothing half-brought is left in the chat's folder."""
        while not copying.done():
            try:
                await asyncio.shield(copying)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if copying.done() and not copying.cancelled() and copying.exception() is None:
            self._files.discard(copying.result()[0])

    async def _bring(self, target: _Target, ctx: ToolContext) -> ToolResult:
        copying = asyncio.ensure_future(asyncio.to_thread(self._copy, target, ctx.chat_id))
        try:
            # Shielded: a worker thread can't be stopped part way.
            staged, name, check = await asyncio.shield(copying)
        except asyncio.CancelledError:
            await self._discard_when_copied(copying)
            raise
        except FileRefused as error:
            return ToolResult(ok=False, content=error.message)
        except ValueError:  # not a chat id
            return ToolResult(ok=False, content=NO_FILE)
        # The row is added here: the SQLite connection belongs to the event loop's thread.
        saved: ChatFile = self._files.record(
            staged,
            name=name,
            check=check,
            source="folder",
            status="pending",
        )
        try:
            saved = await inspect_recorded(self._inspector, self._files, saved)
        except FileRefused as error:
            return ToolResult(ok=False, content=error.message)
        # Like an attached file: the id, type and size only (never the name or the path).
        audit.record(ctx.conn, "agent", "file.brought",
                     {"id": saved.id, "type": saved.mime, "bytes": saved.size})  # fmt: skip
        return ToolResult(
            ok=True,
            content=f"Brought {one_line(saved.name)} into this chat as file {saved.id}.",
            data={"attachments": [{"kind": "file", "id": saved.id}]},
        )
