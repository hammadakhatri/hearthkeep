"""The sandbox manifest of a chat's files: `{"files": [{id, name, kind, path}], "args"}`, with
each path `/data/<stored name>` inside the chat's read-only files folder."""

from __future__ import annotations

import asyncio
from pathlib import Path

from hearthkeep.files.store import ChatFile, ChatFileStore
from hearthkeep.images import inside


def _entry(files: ChatFileStore, found: ChatFile) -> tuple[dict, str] | None:
    name = files.stored_name(found.id)
    if name is None:
        return None
    entry = {"id": found.id, "name": found.name, "kind": found.kind, "path": f"/data/{name}"}
    return entry, name


def manifest_entries(files: ChatFileStore, chat_id: str) -> list[tuple[dict, str]]:
    """(entry, stored name) for each of the chat's files that is not still being inspected.
    Database only: the names are checked on disk later, off the event loop."""
    entries = []
    for found in files.list(chat_id):
        if found.status == "pending":
            continue
        entry = _entry(files, found)
        if entry is not None:
            entries.append(entry)
    return entries


def _prepare_folder(folder: Path, entries: list[tuple[dict, str]]) -> list[dict]:
    """Create the chat's files folder; keep only the files that are really in it."""
    folder.mkdir(parents=True, exist_ok=True)
    return [entry for entry, name in entries if inside(folder, name) is not None]


async def chat_manifest(
    files: ChatFileStore, chat_id: str, args: dict | None = None
) -> tuple[Path, dict]:
    """(the chat's files folder, a manifest of its settled files). Disk checks run in a thread."""
    folder = files.files_dir(chat_id)
    entries = manifest_entries(files, chat_id)
    kept = await asyncio.to_thread(_prepare_folder, folder, entries)
    return folder, {"files": kept, "args": dict(args or {})}


async def file_manifest(files: ChatFileStore, found: ChatFile, args: dict) -> tuple[Path, dict]:
    """(the file's chat folder, a manifest of that one file, whatever its status)."""
    folder = files.files_dir(found.chat_id)
    entry = _entry(files, found)
    kept = await asyncio.to_thread(_prepare_folder, folder, [entry] if entry else [])
    return folder, {"files": kept, "args": dict(args)}
