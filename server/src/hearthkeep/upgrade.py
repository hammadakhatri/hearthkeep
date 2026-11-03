"""The one-time migration an existing install gets on its first start after an update, and the
"Hearthkeep was updated" note it leaves for the owner.

Each step is idempotent and runs in its own `try`: a step that fails is logged by type, adds
its note item (when it has one) and is tried again on the next start, because the level is
recorded only once every step succeeded. Config files, cards, credentials, sessions and the
owner's own tool levels are never touched."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import hearthkeep
from hearthkeep import audit, db
from hearthkeep.permissions import DEFAULT_LEVELS
from hearthkeep.shortcut import SHORTCUT_FAILED

if TYPE_CHECKING:
    from hearthkeep.addons import Addons
    from hearthkeep.host import Host

log = logging.getLogger(__name__)

UPGRADE_LEVEL = 1
# Tool levels earlier versions stored for tools that never shipped.
PLACEHOLDER_LEVELS = ("wallet", "news", "demo")
LEVEL_KEY = "upgrade_level"
NOTE_KEY = "update_note"
# Kept per upgrade level (`_key`), so a later level starts with fresh retries and decides
# again whether the install was in use:
ATTEMPTS_KEY = "upgrade_attempts"  # starts that tried the upgrade without finishing it
ITEMS_KEY = "upgrade_items"  # the note items the last attempt left
EXISTING_KEY = "upgrade_existing"  # "1" or "0", decided at the first attempt
MAX_ATTEMPTS = 3  # after this many starts the level is set anyway; the note keeps the items
ACTOR = "system"

# Note item id -> (text, the add-on whose card the note's button opens, if any).
NOTE_ITEMS: dict[str, tuple[str, str | None]] = {
    "sandbox_image": ("The analysis sandbox needs rebuilding.", "analysis"),
    "startup_not_moved": (
        "Start at login couldn't be moved to the new way Hearthkeep starts. Check it in "
        "Settings › Add-ons.",
        "startup",
    ),
    "shortcut_failed": (SHORTCUT_FAILED, None),
}

# The note item each step leaves when it fails (steps without one only log).
_STEP_ITEMS = {"startup": "startup_not_moved", "shortcut": "shortcut_failed"}


@dataclass(frozen=True)
class UpgradeReport:
    ran: bool
    changed: tuple[str, ...]  # the steps that changed something, in step order
    items: tuple[str, ...]  # the note items this run left (failed steps)


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _level(conn: sqlite3.Connection) -> int:
    try:
        return int(_get(conn, LEVEL_KEY) or 0)
    except ValueError:
        return 0


def _stored_note(conn: sqlite3.Connection) -> dict | None:
    raw = _get(conn, NOTE_KEY)
    if raw is None:
        return None
    try:
        note = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(note, dict):
        return None
    items = note.get("items")
    return {
        "version": str(note.get("version") or ""),
        "items": [i for i in items if isinstance(i, str)] if isinstance(items, list) else [],
    }


def _existing(conn: sqlite3.Connection) -> bool:
    """Whether this database belongs to an install that was already in use."""
    for table in ("credentials", "tool_levels"):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:  # noqa: S608
            return True
    return False


def _remove_placeholder_levels(conn: sqlite3.Connection) -> list[str]:
    tools = [t for t in PLACEHOLDER_LEVELS if t not in DEFAULT_LEVELS]
    if not tools:
        return []
    marks = ", ".join("?" * len(tools))
    with db.transaction(conn):
        removed = [
            row[0]
            for row in conn.execute(
                f"SELECT tool FROM tool_levels WHERE tool IN ({marks})",  # noqa: S608
                tools,
            )
        ]
        if removed:
            conn.execute(f"DELETE FROM tool_levels WHERE tool IN ({marks})", tools)  # noqa: S608
            audit.record(conn, ACTOR, "upgrade.levels_removed", {"tools": sorted(removed)})
    return removed


def _key(name: str) -> str:
    """`name` for the current upgrade level, e.g. `upgrade_attempts:1`."""
    return f"{name}:{UPGRADE_LEVEL}"


def _json_list(conn: sqlite3.Connection, key: str) -> list[str] | None:
    raw = _get(conn, key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return [i for i in value if isinstance(i, str)] if isinstance(value, list) else None


def _was_existing(conn: sqlite3.Connection) -> bool:
    """Whether this is an install that was in use before the upgrade, decided at the first
    attempt and kept, so that removing placeholder rows can't change the answer on a retry."""
    stored = _get(conn, _key(EXISTING_KEY))
    if stored is not None:
        return stored == "1"
    existing = _existing(conn)
    _put(conn, _key(EXISTING_KEY), "1" if existing else "0")
    return existing


def run_upgrade(
    data_dir: Path,
    conn: sqlite3.Connection,
    *,
    host: Host,
    run: Callable = subprocess.run,
    version: str = hearthkeep.__version__,
) -> UpgradeReport:
    """Bring an install up to UPGRADE_LEVEL. `data_dir` is the resolved data folder.

    A step with a note item (start at login, the shortcut) that fails leaves the level unset,
    so the next start tries again, up to MAX_ATTEMPTS starts; then the level is set anyway
    and the note keeps the item. A failing step without one (the locator, the levels) only
    logs: it never blocks the level."""
    if _level(conn) >= UPGRADE_LEVEL:
        return UpgradeReport(False, (), ())
    data_dir = Path(data_dir)
    existing = _was_existing(conn)
    changed: list[str] = []
    items: list[str] = []
    succeeded: set[str] = set()  # steps that ran to the end this time

    def failure(step: str, exc: Exception) -> None:
        log.warning("upgrade step %s failed: %s", step, type(exc).__name__)
        if step in _STEP_ITEMS:
            items.append(_STEP_ITEMS[step])

    if host.supported:
        try:
            recorded = host.read_location()
            if recorded is None or not Path(recorded).is_dir():
                host.write_location(data_dir)
                changed.append("location")
            succeeded.add("location")
        except Exception as exc:
            failure("location", exc)

        try:
            # Strict: a task that can't be read now is a failure to retry, not "no task".
            state = host.startup_status(run=run, strict=True)
            if state.enabled and state.serves(data_dir.resolve()) and not state.launcher:
                host.enable_startup(data_dir, run=run)
                changed.append("startup")
            succeeded.add("startup")
        except Exception as exc:  # StartupError or anything else: never stop the start
            failure("startup", exc)

        if existing:
            try:
                host.install_shortcut(data_dir, run=run)
                changed.append("shortcut")
                succeeded.add("shortcut")
            except Exception as exc:  # ShortcutError or anything else
                failure("shortcut", exc)

    try:
        if _remove_placeholder_levels(conn):
            changed.append("levels")
    except Exception as exc:
        failure("levels", exc)

    with db.transaction(conn):
        if _level(conn) >= UPGRADE_LEVEL:
            # Another start finished the upgrade meanwhile; its note and audit row stand.
            return UpgradeReport(False, (), ())
        first_attempt = _get(conn, _key(ATTEMPTS_KEY)) is None
        last_items = _json_list(conn, _key(ITEMS_KEY)) or []
        attempts = int(_get(conn, _key(ATTEMPTS_KEY)) or 0) + 1
        _put(conn, _key(ATTEMPTS_KEY), str(attempts))
        _put(conn, _key(ITEMS_KEY), json.dumps(sorted(items)))
        gave_up = bool(items) and attempts >= MAX_ATTEMPTS
        if not items or gave_up:
            _put(conn, LEVEL_KEY, str(UPGRADE_LEVEL))
        if existing:
            stored = _stored_note(conn)
            new_items = set(items) - set(last_items)
            # A dismissed note stays dismissed unless something new went wrong.
            if first_attempt or stored is not None or new_items:
                kept = set(stored["items"]) if stored else set()
                # A step that worked this time takes its earlier failure off the note.
                kept -= {_STEP_ITEMS[s] for s in succeeded if s in _STEP_ITEMS}
                note = {"version": version, "items": sorted(kept | set(items))}
                _put(conn, NOTE_KEY, json.dumps(note))
        # Only a run that did or found something new, or gave up, is audited.
        if changed or sorted(items) != sorted(last_items) or gave_up:
            audit.record(
                conn,
                ACTOR,
                "upgrade.ran",
                {"level": UPGRADE_LEVEL, "changed": changed, "items": items},
            )
    return UpgradeReport(True, tuple(changed), tuple(items))


def update_note(conn: sqlite3.Connection, addons: Addons | None) -> dict | None:
    """The note for the owner, or None when there is none to show."""
    stored = _stored_note(conn)
    if stored is None:
        return None
    ids = [i for i in stored["items"] if i in NOTE_ITEMS]
    if (
        addons is not None
        and "sandbox_image" not in ids
        and addons.get("analysis").state == "needs_image"
    ):
        ids.append("sandbox_image")
    return {
        "version": stored["version"],
        "items": [{"id": i, "text": NOTE_ITEMS[i][0], "addon": NOTE_ITEMS[i][1]} for i in ids],
    }


def dismiss_update_note(conn: sqlite3.Connection) -> bool:
    """Forget the note; whether there was one."""
    return conn.execute("DELETE FROM app_state WHERE key = ?", (NOTE_KEY,)).rowcount > 0
