"""Deleting a chat with everything it owns, or one file of a chat, and sweeping what a
deletion left behind on disk.

Rows go first, in one transaction; files go after the commit, so a failure part way never
leaves rows pointing at missing files. A file that can't be removed then (locked, say) is
logged by error type and removed by the next startup's sweep, which also removes what a stop
cut short. Only paths inside the data folder's own `workspace/chats/<chat-id>`, images folder
or uploads folder are ever removed, and no link or junction is ever followed: one met on the
way is removed itself (its target is untouched) or, when it stands in for one of those folders,
left in place. The audit log is never touched here. Logs never carry a path or a name.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path

from hearthkeep import db
from hearthkeep.files.store import CHAT_ID, FILE_ID
from hearthkeep.images import ATTACHED_PHOTO_LINE, inside, normalize_newlines

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: junctions and other Windows links
# A stored file's name: `<id>.<ext>` (temporary files end in `.tmp` and are swept elsewhere).
STORED_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\.[A-Za-z0-9]{1,8}")
# The chat title made from a first file's name (routes_chats.TITLE_CHARS) is reset to this
# when that file is deleted.
TITLE_CHARS = 60
FILE_TITLE_RESET = "Chat"

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


def _own_folder(folder: Path, data_dir: Path, what: str) -> bool:
    """The folder is really where it should be: no link anywhere on the way from the data
    folder (it resolves to the same place as the resolved data folder plus its parts). A
    folder that exists but isn't is left in place, with a warning naming only `what`."""
    expected = Path(data_dir).resolve() / folder.relative_to(data_dir)
    try:
        if not os.path.lexists(folder):
            return False
        if folder.is_dir() and not _is_link(folder) and folder.resolve() == expected:
            return True
    except OSError:
        pass
    _log.warning("left in place: %s is not Hearthkeep's own folder", what)
    return False


def _remove_file(folder: Path, file_name: str | None, data_dir: Path, what: str) -> bool:
    """Remove `file_name` directly inside `folder`, never through a link. True if it is gone
    (or was never there); a failure is logged by type and left to the startup sweep."""
    if not file_name or not _own_folder(folder, data_dir, what):
        return False
    entry = folder / file_name
    if not os.path.lexists(entry):
        return True
    path = None if _is_link(entry) else inside(folder, file_name)
    if path is None:
        _log.warning("left in place: a %s that is not a plain file", what)
        return False
    try:
        path.unlink()
    except OSError as exc:
        _log.warning("could not remove a %s: %s", what, type(exc).__name__)
        return False
    return True


def _remove_tree(folder: Path) -> None:
    """Remove `folder` and everything in it, looking at each entry without following it: a
    link or junction inside is removed itself (`os.unlink` never touches its target), never
    walked into. Whatever can't be removed is left in place (and logged by type)."""
    if _is_link(folder):  # checked again on entry: it may have changed since it was looked at
        _log.warning("left in place: a chat folder that is a link")
        return
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
    try:
        os.rmdir(folder)
    except OSError as exc:
        _log.warning("could not remove a chat's folder: %s", type(exc).__name__)


def _remove_chat_folder(data_dir: Path, chat_id: str) -> bool:
    """Remove `<data>/workspace/chats/<chat_id>` when it really is that folder (not a link
    and resolving there). True if it is gone."""
    if not isinstance(chat_id, str) or not CHAT_ID.fullmatch(chat_id):
        return False
    folder = _chats_root(data_dir) / chat_id
    if not os.path.lexists(folder):
        return True
    if not _own_folder(folder, data_dir, "chat folder"):
        return False
    try:
        _remove_tree(folder)
    except OSError as exc:
        _log.warning("could not remove a chat's folder: %s", type(exc).__name__)
    return not os.path.lexists(folder)


def _delete(conn: sqlite3.Connection, counts: dict[str, int], table: str, sql: str, params) -> None:
    counts[table] = conn.execute(f"DELETE FROM {table} WHERE {sql}", params).rowcount


@dataclass(frozen=True)
class RemovalPlan:
    """The files a committed chat deletion leaves to remove from disk."""

    chat_id: str
    images: list[str] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)


def _photo_kept_by(conn: sqlite3.Connection, chat_id: str, upload_id: str) -> str | None:
    """Another chat whose owner's message carries this photo's `Attached photo:` line."""
    line = f"Attached photo: {upload_id}"
    rows = conn.execute(
        "SELECT chat_id, content FROM messages WHERE chat_id != ? AND role = 'user'"
        " AND instr(content, ?) > 0 ORDER BY id",
        (chat_id, line),
    )
    for row in rows:
        lines = normalize_newlines(row["content"] or "").split("\n")
        if any(ATTACHED_PHOTO_LINE.fullmatch(x) and x == line for x in lines):
            return row["chat_id"]
    return None


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the write-ahead log back into the database and empty it, so deleted rows don't
    linger there. A failure (another reader, say) is only logged by type."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.Error as exc:
        _log.warning("could not checkpoint the database: %s", type(exc).__name__)


def commit_chat_delete(
    conn: sqlite3.Connection, chat_id: str
) -> tuple[dict[str, int], RemovalPlan]:
    """Delete the chat and every row it owns in one transaction. Returns the rows removed per
    table and the files left to remove (`remove_planned`). A photo also sent in another chat
    stays, linked to that chat. Push subscriptions, allowed folders and the audit log are
    never touched."""
    sent = "SELECT id FROM sent_items WHERE chat_id = ?"
    counts: dict[str, int] = {}
    with db.transaction(conn):
        images = [r[0] for r in conn.execute(
            "SELECT file_name FROM images WHERE chat_id = ?", (chat_id,))]  # fmt: skip
        linked = [r[0] for r in conn.execute(
            "SELECT upload_id FROM upload_chats WHERE chat_id = ?", (chat_id,))]  # fmt: skip
        gone: list[str] = []
        for upload_id in linked:
            other = _photo_kept_by(conn, chat_id, upload_id)
            if other is None:
                gone.append(upload_id)
            else:
                conn.execute("UPDATE upload_chats SET chat_id = ? WHERE upload_id = ?",
                             (other, upload_id))  # fmt: skip
        marks = ",".join("?" * len(gone))
        uploads = [r[0] for r in conn.execute(
            f"SELECT file_name FROM uploads WHERE id IN ({marks})", gone)]  # fmt: skip
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
        _delete(conn, counts, "uploads", f"id IN ({marks})", gone)
        _delete(conn, counts, "upload_chats", "chat_id = ?", by_chat)
        _delete(conn, counts, "sent_targets", f"item_id IN ({sent})", by_chat)
        _delete(conn, counts, "sent_items", "chat_id = ?", by_chat)
        _delete(conn, counts, "chats", "id = ?", by_chat)
    checkpoint(conn)
    return counts, RemovalPlan(chat_id, images, uploads)


def remove_planned(data_dir: Path, plan: RemovalPlan) -> None:
    """Remove a committed deletion's files: no database here, so it may run on a worker
    thread. What can't be removed now is left to the startup sweep."""
    for name in plan.images:
        _remove_file(_images_dir(data_dir), name, data_dir, "image")
    for name in plan.uploads:
        _remove_file(_uploads_dir(data_dir), name, data_dir, "photo")
    _remove_chat_folder(data_dir, plan.chat_id)


def delete_chat(conn: sqlite3.Connection, data_dir: Path, chat_id: str) -> dict[str, int]:
    """`commit_chat_delete` then `remove_planned`, in one call. Returns rows removed per
    table."""
    counts, plan = commit_chat_delete(conn, chat_id)
    remove_planned(data_dir, plan)
    return counts


def delete_chat_file(conn: sqlite3.Connection, data_dir: Path, file_id: str) -> str | None:
    """Delete one file of a chat: its row, the sent items that point at it, then its bytes.
    Runs that used it keep their results. A chat titled with the file's name is retitled
    "Chat". Returns the file's chat id, or None if unknown."""
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        return None
    sent = "SELECT id FROM sent_items WHERE chat_id = ? AND kind = 'file' AND ref_id = ?"
    with db.transaction(conn):
        row = conn.execute(
            "SELECT chat_id, name, file_name FROM chat_files WHERE id = ?", (file_id,)
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
        from_name = " ".join((row["name"] or "").split())[:TITLE_CHARS]
        if from_name:
            conn.execute(
                "UPDATE chats SET title = ? WHERE id = ? AND title = ?",
                (FILE_TITLE_RESET, chat_id, from_name),
            )
    if isinstance(chat_id, str) and CHAT_ID.fullmatch(chat_id):
        _remove_file(_chats_root(data_dir) / chat_id / "files", file_name, data_dir, "chat file")
    return chat_id


def _stored_names(folder: Path) -> list[str]:
    """Plain `<id>.<ext>` files directly in `folder` (no `.tmp`, no links)."""
    try:
        with os.scandir(folder) as found:
            names = sorted(e.name for e in found)
    except OSError as exc:
        _log.warning("could not sweep a folder: %s", type(exc).__name__)
        return []
    return [
        n for n in names
        if STORED_NAME.fullmatch(n) and not n.endswith(".tmp") and not _is_link(folder / n)
    ]  # fmt: skip


def _sweep_files(folder: Path, data_dir: Path, kept: set[str], what: str) -> int:
    if not _own_folder(folder, data_dir, what + " folder"):
        return 0
    removed = 0
    for name in _stored_names(folder):
        if name not in kept and _remove_file(folder, name, data_dir, what):
            removed += 1
    return removed


def sweep_orphans(conn: sqlite3.Connection, data_dir: Path) -> int:
    """Finish deletions on disk (run at startup, before anything can use these folders):
    remove the `workspace/chats/<id>` folders whose chat no longer exists, the images and
    photos without a row, and the files in each chat's `files/` folder without a row. Only
    exact chat-id and `<id>.<ext>` names, never a link or junction. Returns how many folders
    and files were removed."""
    removed = 0

    def rows(sql: str) -> set[str]:
        return {r[0] for r in conn.execute(sql) if r[0]}

    removed += _sweep_files(_images_dir(data_dir), data_dir,
                            rows("SELECT file_name FROM images"), "image")  # fmt: skip
    removed += _sweep_files(_uploads_dir(data_dir), data_dir,
                            rows("SELECT file_name FROM uploads"), "photo")  # fmt: skip
    root = _chats_root(data_dir)
    if not _own_folder(root, data_dir, "chats folder"):
        return removed
    known = rows("SELECT id FROM chats")
    try:
        with os.scandir(root) as found:
            entries = sorted(found, key=lambda e: e.name)
    except OSError as exc:  # never stops Hearthkeep from starting
        _log.warning("could not sweep the chats folder: %s", type(exc).__name__)
        return removed
    for entry in entries:
        if not CHAT_ID.fullmatch(entry.name):
            continue
        folder = Path(entry.path)
        if _is_link(folder) or not entry.is_dir(follow_symlinks=False):
            continue
        if entry.name not in known:
            if _remove_chat_folder(data_dir, entry.name):
                removed += 1
            continue
        files = folder / "files"
        if os.path.lexists(files):
            sql = "SELECT file_name FROM chat_files WHERE chat_id = ?"
            kept = {r[0] for r in conn.execute(sql, (entry.name,)) if r[0]}
            removed += _sweep_files(files, data_dir, kept, "chat file")
    return removed
