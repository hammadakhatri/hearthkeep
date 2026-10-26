"""The files attached to each chat: rows in `chat_files`, bytes under the chat's own folder,
`<data>/workspace/chats/<chat-id>/files/<file-id>.<ext>`."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal

from hearthkeep import db
from hearthkeep.files.checks import FILE_KINDS, MAX_FILE_BYTES, FileCheck, FileRefused
from hearthkeep.images import UPLOAD_ID, _inside

CHAT_ID = re.compile(r"[0-9a-f]{32}")
FILE_ID = UPLOAD_ID
ATTACHED_FILE_LINE = re.compile(r"Attached file: ([A-Za-z0-9_-]{1,32})")
STATUSES = ("ready", "unchecked")
SOURCES = ("upload", "folder")
_COPY_CHUNK = 1024 * 1024
_COLUMNS = "id, chat_id, name, kind, mime, size, status, summary, source, created_at"


@dataclass
class ChatFile:
    id: str
    chat_id: str
    name: str
    kind: str
    mime: str
    size: int
    status: Literal["ready", "unchecked"]
    summary: dict | None
    source: Literal["upload", "folder"]
    created_at: str


@dataclass(frozen=True)
class StagedFile:
    id: str
    chat_id: str
    tmp: Path


def _chat_file(row: sqlite3.Row) -> ChatFile:
    values = dict(row)
    values["summary"] = json.loads(values["summary"]) if values["summary"] else None
    return ChatFile(**values)


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


class ChatFileStore:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path) -> None:
        self._conn = conn
        self._chats = Path(data_dir) / "workspace" / "chats"

    def files_dir(self, chat_id: str) -> Path:
        """The chat's files folder (not created here); ValueError for anything but a chat id."""
        if not isinstance(chat_id, str) or not CHAT_ID.fullmatch(chat_id):
            raise ValueError("not a chat id")
        return self._chats / chat_id / "files"

    def _new_tmp(self, chat_id: str) -> StagedFile:
        folder = self.files_dir(chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        file_id = token_urlsafe(12)
        return StagedFile(file_id, chat_id, folder / f"{file_id}.tmp")

    # Staging touches no database, so it may run on a worker thread (the connection may not).

    def stage(self, chat_id: str, data: bytes) -> StagedFile:
        """Step 1 of keeping a file: write it to a temporary file under a new random id."""
        staged = self._new_tmp(chat_id)
        try:
            staged.tmp.write_bytes(data)
        except BaseException:
            _unlink(staged.tmp)
            raise
        return staged

    def stage_copy(self, chat_id: str, src: Path, max_bytes: int = MAX_FILE_BYTES) -> StagedFile:
        """Step 1 for a file copied from disk, stopped (FileRefused "too_large") as soon as it
        is over `max_bytes`."""
        staged = self._new_tmp(chat_id)
        copied = 0
        try:
            with open(src, "rb") as source, open(staged.tmp, "wb") as target:
                while chunk := source.read(_COPY_CHUNK):
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise FileRefused("too_large")
                    target.write(chunk)
        except BaseException:
            _unlink(staged.tmp)
            raise
        return staged

    def discard(self, staged: StagedFile) -> None:
        _unlink(staged.tmp)

    def record(
        self,
        staged: StagedFile,
        *,
        name: str,
        check: FileCheck,
        source: Literal["upload", "folder"],
        status: Literal["ready", "unchecked"] = "unchecked",
    ) -> ChatFile:
        """Step 2: the row, and the file moved into place, together; the temporary file is
        removed if either fails."""
        final: Path | None = None
        placed = False
        try:
            if check.kind not in FILE_KINDS or check.ext != check.kind:
                raise ValueError("unsupported file kind")
            if source not in SOURCES or status not in STATUSES:
                raise ValueError("bad source or status")
            if staged.tmp.parent != self.files_dir(staged.chat_id):
                raise ValueError("staged file is outside its chat's folder")
            file_name = f"{staged.id}.{check.ext}"
            final = staged.tmp.parent / file_name
            saved = ChatFile(
                id=staged.id,
                chat_id=staged.chat_id,
                name=name,
                kind=check.kind,
                mime=check.mime,
                size=staged.tmp.stat().st_size,
                status=status,
                summary=None,
                source=source,
                created_at=db.utcnow(),
            )
            with db.transaction(self._conn):
                self._conn.execute(
                    f"INSERT INTO chat_files ({_COLUMNS}, file_name) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (saved.id, saved.chat_id, saved.name, saved.kind, saved.mime, saved.size,
                     saved.status, None, saved.source, saved.created_at, file_name),
                )  # fmt: skip
                os.replace(staged.tmp, final)
                placed = True
        except BaseException:
            _unlink(staged.tmp)
            if placed and final is not None:
                _unlink(final)
            raise
        return saved

    def set_status(
        self, file_id: str, status: Literal["ready", "unchecked"], summary: dict | None
    ) -> None:
        if status not in STATUSES:
            raise ValueError("bad status")
        self._conn.execute(
            "UPDATE chat_files SET status = ?, summary = ? WHERE id = ?",
            (status, json.dumps(summary) if summary is not None else None, file_id),
        )

    def list(self, chat_id: str) -> list[ChatFile]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM chat_files WHERE chat_id = ? ORDER BY created_at, rowid",
            (chat_id,),
        )
        return [_chat_file(r) for r in rows]

    def get(self, file_id: str) -> ChatFile | None:
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            return None
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM chat_files WHERE id = ?", (file_id,)
        ).fetchone()
        return _chat_file(row) if row else None

    def path(self, file_id: str) -> Path | None:
        """The file, only ever inside its chat's files folder; None if unknown or gone."""
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            return None
        row = self._conn.execute(
            "SELECT chat_id, file_name FROM chat_files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None or not CHAT_ID.fullmatch(row["chat_id"] or ""):
            return None
        return _inside(self.files_dir(row["chat_id"]), row["file_name"] or "")
