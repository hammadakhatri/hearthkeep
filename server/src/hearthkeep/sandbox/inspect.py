"""Inspection when a file is attached: one `inspect` run in the sandbox reads what is inside
(sheets, rows, columns, pages, scanned pages) and whether it needs a password.

A file the sandbox finds password-protected or unreadable is refused (`FileRefused`). Anything
else that goes wrong (no Docker, no image, a timeout, a sandbox error, a report that can't be
read) leaves the file "unchecked": it stays attached and the model is told it wasn't checked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from pathlib import Path
from typing import Literal

from hearthkeep.files.checks import FileRefused
from hearthkeep.files.store import ChatFile, ChatFileStore
from hearthkeep.sandbox.handle import SandboxHandle
from hearthkeep.sandbox.manifest import file_manifest
from hearthkeep.sandbox.runner import RawRun, SandboxRunner
from hearthkeep.sandbox.runs import RunStore

INSPECT_TIMEOUT_S = 30
INSPECT_MAX_BYTES = 64 * 1024
MAX_SHEETS = 50
MAX_COLUMNS = 200
MAX_PAGES_LISTED = 1000
MAX_TEXT = 100
_PROBLEMS = {"password": "password_protected", "unreadable": "damaged"}

_log = logging.getLogger(__name__)


class _Unusable(Exception):
    """The inspect report can't be used; the file stays unchecked."""


def _read_report(path: Path | None) -> dict:
    if path is None:
        raise _Unusable("no inspect.json")
    with open(path, "rb") as f:
        data = f.read(INSPECT_MAX_BYTES + 1)
    if len(data) > INSPECT_MAX_BYTES:
        raise _Unusable("inspect.json is too large")
    try:
        report = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Unusable("inspect.json is not JSON") from None
    if not isinstance(report, dict):
        raise _Unusable("inspect.json is not an object")
    return report


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _short(value: object, limit: int = MAX_TEXT) -> str | None:
    """Text cut to `limit`, with control characters and line or paragraph separators as
    spaces (the names reach the model's prompt); None for anything but text."""
    if not isinstance(value, str):
        return None
    cats = ("Cc", "Zl", "Zp")
    return "".join(" " if unicodedata.category(ch) in cats else ch for ch in value[:limit])


def summary_of(report: dict, kind: str) -> dict:
    """The parts of an inspect report worth keeping, each checked (the report is untrusted)."""
    sheets = []
    for sheet in report.get("sheets") or []:
        if not isinstance(sheet, dict) or _short(sheet.get("name")) is None:
            continue
        rows, columns = _count(sheet.get("rows")), _count(sheet.get("columns"))
        if rows is None or columns is None:
            continue
        sheets.append({"name": _short(sheet["name"]), "rows": rows, "columns": columns})
    raw_scanned = report.get("scanned_pages")
    scanned = [n for n in raw_scanned if _count(n)] if isinstance(raw_scanned, list) else []
    raw_columns = report.get("columns")
    columns = raw_columns if isinstance(raw_columns, list) else []
    return {
        "kind": kind,
        "pages": _count(report.get("pages")),
        "scanned_pages": scanned[:MAX_PAGES_LISTED],
        "sheets": sheets[:MAX_SHEETS],
        "rows": _count(report.get("rows")),
        "columns": [c for c in (_short(c) for c in columns) if c is not None][:MAX_COLUMNS],
        "encoding": _short(report.get("encoding"), 40),
        "delimiter": _short(report.get("delimiter"), 4),
    }


class Inspector:
    def __init__(
        self, sandbox: SandboxHandle | SandboxRunner | None, files: ChatFileStore
    ) -> None:
        self._sandbox = SandboxHandle.of(sandbox)
        self._files = files

    async def inspect(self, file: ChatFile) -> Literal["ready", "unchecked"]:
        """Inspect one recorded file and store its summary. Raises FileRefused when the file
        needs a password or can't be read (the caller removes it); otherwise the file's new
        status, "ready", or "unchecked" when it couldn't be checked."""
        status = await self._inspect(file)
        if status == "unchecked":
            self._files.set_status(file.id, "unchecked", None)
        return status

    async def _inspect(self, file: ChatFile) -> Literal["ready", "unchecked"]:
        try:
            runner = await self._sandbox.get()
            if runner is None:
                return "unchecked"
            folder, manifest = await file_manifest(self._files, file, {"file": file.id})
            if not manifest["files"]:
                return "unchecked"
            raw = await runner.run(
                RunStore.new_id(),
                mode="inspect",
                files_dir=folder,
                manifest=manifest,
                timeout_s=INSPECT_TIMEOUT_S,
            )
        except Exception as exc:  # SandboxUnavailable among them: never refused for this
            _log.info("file not inspected: %s", type(exc).__name__)
            return "unchecked"
        try:
            report = await self._report(raw)
        finally:
            raw.cleanup()
        if report is None:
            return "unchecked"
        if report.get("ok") is not True:
            reason = _PROBLEMS.get(report.get("problem"))  # type: ignore[arg-type]
            if reason is None:
                return "unchecked"
            raise FileRefused(reason)
        self._files.set_status(file.id, "ready", summary_of(report, file.kind))
        return "ready"

    async def _report(self, raw: RawRun) -> dict | None:
        if raw.status != "ok":
            return None
        try:
            return await asyncio.to_thread(_read_report, raw.members.get("inspect.json"))
        except (_Unusable, OSError) as exc:
            _log.info("file not inspected: %s", exc if isinstance(exc, _Unusable) else "OSError")
            return None


async def inspect_recorded(
    inspector: Inspector | None, store: ChatFileStore, saved: ChatFile
) -> ChatFile:
    """Inspect a file just recorded as "pending" (while pending, no turn lists or mounts it).
    Shared by every way a file reaches a chat (attached, or brought from a folder).

    - Checked: "ready", with its summary.
    - Couldn't be checked (no Docker, a timeout, a sandbox error): "unchecked", still attached.
    - Password-protected or unreadable: removed (row and bytes), and FileRefused is raised.
    - Cancelled part way (the request or the server stopping): removed too, so nothing is
      left half-checked; the owner never got its id and attaches it again.
    """
    if inspector is None:
        store.set_status(saved.id, "unchecked", None)
        return store.get(saved.id) or saved
    try:
        await inspector.inspect(saved)
    except BaseException:
        store.remove(saved.id)  # refused, or cancelled part way: keep nothing half-checked
        raise
    return store.get(saved.id) or saved
