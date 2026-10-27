"""Deleting a chat with everything it owns, or one file of a chat, and sweeping the folders of
chats that are gone.

Rows go first, in one transaction; files go after the commit, so a failure part way never
leaves rows pointing at missing files. Only paths inside the data folder's own
`workspace/chats/<chat-id>`, images folder or uploads folder are ever removed, and no link or
junction is ever followed: one met on the way is removed itself (its target is untouched) or,
when it stands in for one of those folders, left alone. The audit log is never touched here.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import stat
from pathlib import Path

from hearthkeep import db
from hearthkeep.files.store import CHAT_ID, FILE_ID
from hearthkeep.images import inside

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and other Windows links

_log = logging.getLogger(__name__)


def _is_link(path: Path) -> bool:
    """True for a symlink and, on Windows, any reparse point (a junction, say); also when it
    can't be told (the entry is then left alone)."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _chats_root(data_dir: Path) -> Path:
    return Path(data_dir) / "workspace" / "chats"


def _images_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "workspace" / "output" / "images"


def _uploads_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "workspace" / "uploads"


def _own_folder(folder: Path, data_dir: Path) -> bool:
    """The folder is really where it should be: no link anywhere on the way from the data
    folder (it resolves to the same place as the resolved data folder plus its parts)."""
    expected = Path(data_dir).resolve() / folder.relative_to(data_dir)
    try:
        return folder.is_dir() and not _is_link(folder) and folder.resolve() == expected
    except OSError:
        return False


def _remove_file(folder: Path, file_name: str | None, data_dir: Path) -> None:
    """Remove `file_name` directly inside `folder`, never through a link."""
    if not file_name or not _own_folder(folder, data_dir) or _is_link(folder / file_name):
        return
    path = inside(folder, file_name)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


def _remove_tree(folder: Path) -> None:
    """Remove `folder` and everything in it, looking at each entry without following it: a
    link or junction inside is removed itself (`os.unlink` never touches its target), never
    walked into. Whatever can't be removed is left in place."""
    with os.scandir(folder) as found:
        entries = list(found)
    for entry in entries:
        path = Path(entry.path)
        try:
            if _is_link(path):
                os.unlink(path)
            elif entry.is_dir(follow_symlinks=False):
                _remove_tree(path)
            else:
                os.unlink(path)
        except OSError as exc:
            _log.warning("could not remove a chat's file: %s", type(exc).__name__)
    with contextlib.suppress(OSError):
        os.rmdir(folder)


def _remove_chat_folder(data_dir: Path, chat_id: str) -> bool:
    """Remove `<data>/workspace/chats/<chat_id>` when it really is that folder (not a link
    and resolving there). True if it is gone."""
    if not isinstance(chat_id, str) or not CHAT_ID.fullmatch(chat_id):
        return False
    folder = _chats_root(data_dir) / chat_id
    if not os.path.lexists(folder):
        return True
    if not _own_folder(folder, data_dir):
        return False
    try:
        _remove_tree(folder)
    except OSError as exc:
        _log.warning("could not remove a chat's folder: %s", type(exc).__name__)
    return not os.path.lexists(folder)


def _delete(conn: sqlite3.Connection, counts: dict[str, int], table: str, sql: str, params) -> None:
    counts[table] = conn.execute(f"DELETE FROM {table} WHERE {sql}", params).rowcount


def delete_chat(conn: sqlite3.Connection, data_dir: Path, chat_id: str) -> dict[str, int]:
    """Delete the chat and every row it owns in one transaction, then its files: the image
    PNGs, the photos sent in it and its folder (files and runs). Returns rows removed per
    table. Push subscriptions, allowed folders and the audit log are never touched."""
    sent = "SELECT id FROM sent_items WHERE chat_id = ?"
    photos = "SELECT upload_id FROM upload_chats WHERE chat_id = ?"
    counts: dict[str, int] = {}
    with db.transaction(conn):
        images = [r[0] for r in conn.execute(
            "SELECT file_name FROM images WHERE chat_id = ?", (chat_id,))]  # fmt: skip
        uploads = [r[0] for r in conn.execute(
            f"SELECT file_name FROM uploads WHERE id IN ({photos})", (chat_id,))]  # fmt: skip
        by_chat = (chat_id,)
        _delete(conn, counts, "messages", "chat_id = ?", by_chat)
        _delete(conn, counts, "turn_events", "chat_id = ?", by_chat)
        _delete(conn, counts, "approvals", "chat_id = ?", by_chat)
        _delete(conn, counts, "chat_grants", "chat_id = ?", by_chat)
        _delete(conn, counts, "chat_files", "chat_id = ?", by_chat)
        _delete(conn, counts, "outputs",
                "chat_id = ? OR run_id IN (SELECT id FROM runs WHERE chat_id = ?)",
                (chat_id, chat_id))  # fmt: skip
        _delete(conn, counts, "runs", "chat_id = ?", by_chat)
        _delete(conn, counts, "number_checks", "chat_id = ?", by_chat)
        _delete(conn, counts, "images", "chat_id = ?", by_chat)
        _delete(conn, counts, "uploads", f"id IN ({photos})", by_chat)
        _delete(conn, counts, "upload_chats", "chat_id = ?", by_chat)
        _delete(conn, counts, "sent_targets", f"item_id IN ({sent})", by_chat)
        _delete(conn, counts, "sent_items", "chat_id = ?", by_chat)
        _delete(conn, counts, "chats", "id = ?", by_chat)
    for name in images:
        _remove_file(_images_dir(data_dir), name, data_dir)
    for name in uploads:
        _remove_file(_uploads_dir(data_dir), name, data_dir)
    _remove_chat_folder(data_dir, chat_id)
    return counts


def delete_chat_file(conn: sqlite3.Connection, data_dir: Path, file_id: str) -> str | None:
    """Delete one file of a chat: its row, the sent items that point at it, then its bytes.
    Runs that used it keep their results. Returns the file's chat id, or None if unknown."""
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        return None
    sent = "SELECT id FROM sent_items WHERE chat_id = ? AND kind = 'file' AND ref_id = ?"
    with db.transaction(conn):
        row = conn.execute(
            "SELECT chat_id, file_name FROM chat_files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None:
            return None
        chat_id, file_name = row["chat_id"], row["file_name"]
        conn.execute(f"DELETE FROM sent_targets WHERE item_id IN ({sent})", (chat_id, file_id))
        conn.execute(
            "DELETE FROM sent_items WHERE chat_id = ? AND kind = 'file' AND ref_id = ?",
            (chat_id, file_id),
        )
        conn.execute("DELETE FROM chat_files WHERE id = ?", (file_id,))
    if isinstance(chat_id, str) and CHAT_ID.fullmatch(chat_id):
        _remove_file(_chats_root(data_dir) / chat_id / "files", file_name, data_dir)
    return chat_id


def sweep_orphans(conn: sqlite3.Connection, data_dir: Path) -> int:
    """Remove the `workspace/chats/<id>` folders whose chat no longer exists (a deletion cut
    short by a stop). Only exact chat-id names, never a link or junction. Run at startup,
    before anything can use them. Returns how many folders were removed."""
    root = _chats_root(data_dir)
    if not os.path.lexists(root) or not _own_folder(root, data_dir):
        return 0
    known = {r[0] for r in conn.execute("SELECT id FROM chats")}
    removed = 0
    try:
        with os.scandir(root) as found:
            entries = sorted(found, key=lambda e: e.name)
    except OSError as exc:  # never stops Hearthkeep from starting
        _log.warning("could not sweep the chats folder: %s", type(exc).__name__)
        return 0
    for entry in entries:
        if not CHAT_ID.fullmatch(entry.name) or entry.name in known:
            continue
        folder = Path(entry.path)
        if _is_link(folder) or not entry.is_dir(follow_symlinks=False):
            continue
        if _remove_chat_folder(data_dir, entry.name):
            removed += 1
    return removed
