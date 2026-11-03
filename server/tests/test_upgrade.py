"""The one-time migration for existing installs and the "Hearthkeep was updated" note."""
# ruff: noqa: F811  (the harness and run_cmd_setup fixtures are imported)

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest
from copy_rules import forbidden_in
from test_api import CSRF, harness, signed_in  # noqa: F401  (harness is a fixture)
from test_cli import Serve, write
from test_no_chat_model import copy_owned_models, copy_todays_config
from test_setup_mode import run_cmd_setup  # noqa: F401  (a fixture)

import hearthkeep
from hearthkeep import cli, db, startup
from hearthkeep.api.app import DB_FILE
from hearthkeep.config import init_data_dir
from hearthkeep.db import connect
from hearthkeep.host import UnsupportedHost, WindowsHost
from hearthkeep.shortcut import ShortcutError
from hearthkeep.startup import StartupError
from hearthkeep.upgrade import (
    NOTE_ITEMS,
    UPGRADE_LEVEL,
    UpgradeReport,
    dismiss_update_note,
    run_upgrade,
    update_note,
)

FIXTURES = Path(__file__).parent / "fixtures" / "upgrade"
PYTHONW = Path(r"C:\py\pythonw.exe")
USER = r"HOMEPC\owner"


# ---- helpers ----------------------------------------


def old_task_xml(data: Path) -> str:
    """The task an earlier version made: `serve` with a log file, not the launcher."""
    text = (FIXTURES / "old-task.xml").read_text(encoding="utf-8")
    return text.replace("{pythonw}", escape(str(PYTHONW))).replace("{data}", escape(str(data)))


def schema_9_database(path: Path) -> None:
    """A database as the version before this one left it (migrations 1 to 9)."""
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        for version in range(1, 10):
            conn.executescript(db.MIGRATIONS[version])
        conn.execute("PRAGMA user_version = 9")
    finally:
        conn.close()


def todays_install(tmp_path: Path) -> Path:
    """A data folder as the last version leaves it: its config.toml, models.toml and card, a
    schema-9 database with a credential, a session and levels python=ask, wallet=off,
    news=allowed, demo=ask. Returned resolved, as callers pass it."""
    data = tmp_path / "data"
    copy_todays_config(data)
    copy_owned_models(data, tmp_path)
    schema_9_database(data / DB_FILE)
    conn = sqlite3.connect(data / DB_FILE, isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO credentials (id, public_key, sign_count, device_name, created_at) "
            "VALUES ('cred-1', x'0102', 3, 'laptop', '2026-10-01T10:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO sessions (id, credential_id, created_at, last_seen_at) "
            "VALUES ('sess-1', 'cred-1', '2026-10-01T10:00:00+00:00', "
            "'2026-10-02T10:00:00+00:00')"
        )
        conn.executemany(
            "INSERT INTO tool_levels (tool, level) VALUES (?, ?)",
            [("python", "ask"), ("wallet", "off"), ("news", "allowed"), ("demo", "ask")],
        )
    finally:
        conn.close()
    return data.resolve()


def snapshot(data: Path) -> dict:
    files = {
        str(p.relative_to(data)): p.read_bytes()
        for p in [
            data / "config.toml",
            data / "models.toml",
            *(data / "models" / "cards").iterdir(),
        ]
    }
    conn = connect(data / DB_FILE)
    try:
        rows = {
            table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("credentials", "sessions")
        }
    finally:
        conn.close()
    return {"files": files, "rows": rows}


def levels(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT tool, level FROM tool_levels").fetchall())


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def stored_note(conn):
    return conn.execute("SELECT value FROM app_state WHERE key = 'update_note'").fetchone()


def events(conn, name: str) -> list[dict]:
    rows = conn.execute("SELECT detail FROM audit WHERE event = ? ORDER BY id", (name,))
    return [json.loads(r[0]) for r in rows]


def addons_with(**states: str):
    """Stands in for the add-on registry: `get(id).state`."""
    return SimpleNamespace(get=lambda addon_id: SimpleNamespace(state=states[addon_id]))


class FakeWindowsHost(WindowsHost):
    """Windows, recorded: the task is XML in memory (read with the real parser), turning it on
    writes today's task, the shortcut and the locator are only noted. Nothing real is touched."""

    def __init__(self, task: str | None = None, *, enable_fails: bool = False) -> None:
        self.task = task
        self.enable_fails = enable_fails
        self.shortcut_fails = False
        self.enabled: list[Path] = []
        self.shortcuts: list[Path] = []
        self.location: Path | None = None
        self.calls: list[str] = []

    def drive_type(self, root: str) -> int:
        return 3

    def startup_status(self, *, run=None):
        if self.task is None:
            return startup.StartupStatus(True, False, None)
        return startup._parse(self.task)

    def enable_startup(self, data_dir: Path, *, run=None) -> None:
        if self.enable_fails:
            raise StartupError("Windows refused to create the task: access denied")
        self.enabled.append(Path(data_dir))
        self.task = startup.task_xml(pythonw=PYTHONW, data_dir=Path(data_dir), user=USER)

    def install_shortcut(self, data_dir: Path, *, run=None) -> None:
        if self.shortcut_fails:
            raise ShortcutError("powershell failed")
        self.shortcuts.append(Path(data_dir))

    def read_location(self) -> Path | None:
        return self.location

    def write_location(self, data_dir: Path) -> None:
        self.location = Path(data_dir)


class RecordingUnsupportedHost(UnsupportedHost):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def read_location(self):
        self.calls.append("read_location")
        return super().read_location()

    def write_location(self, data_dir):
        self.calls.append("write_location")
        return super().write_location(data_dir)

    def startup_status(self, *, run=None):
        self.calls.append("startup_status")
        return super().startup_status()

    def enable_startup(self, data_dir, *, run=None):
        self.calls.append("enable_startup")
        return super().enable_startup(data_dir)

    def install_shortcut(self, data_dir, *, run=None):
        self.calls.append("install_shortcut")
        return super().install_shortcut(data_dir)


# ---- run_upgrade ----------------------------------------


@pytest.mark.real_levels
def test_upgrade_moves_task_drops_placeholders_and_keeps_the_rest(tmp_path):
    data = todays_install(tmp_path)
    before = snapshot(data)
    host = FakeWindowsHost(task=old_task_xml(data))
    conn = connect(data / DB_FILE)
    report = run_upgrade(data, conn, host=host)
    assert report == UpgradeReport(True, ("location", "startup", "shortcut", "levels"), ())
    assert host.enabled == [data] and host.shortcuts == [data] and host.location == data
    assert startup._parse(host.task).launcher and startup._parse(host.task).serves(data)
    assert levels(conn) == {"python": "ask"} and snapshot(data) == before
    assert update_note(conn, addons_with(analysis="ready")) == {
        "version": hearthkeep.__version__,
        "items": [],
    }
    assert events(conn, "upgrade.levels_removed") == [{"tools": ["demo", "news", "wallet"]}]
    assert events(conn, "upgrade.ran") == [
        {"level": 1, "changed": ["location", "startup", "shortcut", "levels"], "items": []}
    ]
    level = conn.execute("SELECT value FROM app_state WHERE key = 'upgrade_level'").fetchone()
    assert level[0] == str(UPGRADE_LEVEL)


@pytest.mark.real_levels
def test_upgrade_twice_changes_nothing_the_second_time(tmp_path):
    data = todays_install(tmp_path)
    host = FakeWindowsHost(task=old_task_xml(data))
    conn = connect(data / DB_FILE)
    run_upgrade(data, conn, host=host)
    audit_rows, note = count(conn, "audit"), stored_note(conn)
    second = run_upgrade(data, conn, host=host)
    assert second == UpgradeReport(False, (), ())
    assert len(host.enabled) == 1 and len(host.shortcuts) == 1
    assert count(conn, "audit") == audit_rows and stored_note(conn) == note


@pytest.mark.real_levels
def test_failed_step_retries_next_start(tmp_path):
    data = todays_install(tmp_path)
    host = FakeWindowsHost(task=old_task_xml(data), enable_fails=True)
    conn = connect(data / DB_FILE)
    first = run_upgrade(data, conn, host=host)
    assert first.items == ("startup_not_moved",) and "startup" not in first.changed
    assert update_note(conn, None)["items"] == [
        {"id": "startup_not_moved", "text": NOTE_ITEMS["startup_not_moved"][0], "addon": "startup"}
    ]
    assert conn.execute("SELECT 1 FROM app_state WHERE key = 'upgrade_level'").fetchone() is None
    host.enable_fails = False
    second = run_upgrade(data, conn, host=host)
    assert second.ran and "startup" in second.changed and second.items == ()
    # It was fixed, so the note no longer says it is broken.
    assert update_note(conn, None)["items"] == []
    assert run_upgrade(data, conn, host=host).ran is False


def test_failed_shortcut_is_noted_and_cleared_once_made(tmp_path):
    data = todays_install(tmp_path)
    host = FakeWindowsHost()
    host.shortcut_fails = True
    conn = connect(data / DB_FILE)
    assert run_upgrade(data, conn, host=host).items == ("shortcut_failed",)
    assert [i["id"] for i in update_note(conn, None)["items"]] == ["shortcut_failed"]
    host.shortcut_fails = False
    assert run_upgrade(data, conn, host=host).changed == ("shortcut",)
    assert update_note(conn, None)["items"] == [] and host.shortcuts == [data]


def test_a_failing_step_never_raises(tmp_path, caplog):
    class Broken(FakeWindowsHost):
        def read_location(self):
            raise PermissionError("no")

        def startup_status(self, *, run=None):
            raise RuntimeError("no")

    data = todays_install(tmp_path)
    conn = connect(data / DB_FILE)
    caplog.set_level(logging.WARNING)
    report = run_upgrade(data, conn, host=Broken())
    assert report.ran and report.items == ("startup_not_moved",)
    assert "PermissionError" in caplog.text and "RuntimeError" in caplog.text
    assert str(data) not in caplog.text
    assert conn.execute("SELECT 1 FROM app_state WHERE key = 'upgrade_level'").fetchone() is None


@pytest.mark.parametrize("case", ["another_folder", "launcher", "off", "none"])
def test_task_for_another_folder_or_already_launcher_is_left_alone(tmp_path, case):
    data = todays_install(tmp_path)
    task = {
        "another_folder": old_task_xml(tmp_path / "elsewhere"),
        "launcher": startup.task_xml(pythonw=PYTHONW, data_dir=data, user=USER),
        "off": old_task_xml(data).replace(
            "    <Enabled>true</Enabled>\n  </Settings>",
            "    <Enabled>false</Enabled>\n  </Settings>",
        ),
        "none": None,
    }[case]
    host = FakeWindowsHost(task=task)
    conn = connect(data / DB_FILE)
    report = run_upgrade(data, conn, host=host)
    assert report.ran and "startup" not in report.changed and report.items == ()
    assert host.enabled == [] and host.task == task


def test_fresh_folder_gets_no_note_and_no_shortcut(tmp_path):
    data = (tmp_path / "data").resolve()
    init_data_dir(data)
    host = FakeWindowsHost()
    conn = connect(data / DB_FILE)
    report = run_upgrade(data, conn, host=host)
    assert report == UpgradeReport(True, ("location",), ())
    assert host.shortcuts == [] and update_note(conn, addons_with(analysis="needs_image")) is None


def test_demo_row_kept_while_demo_is_a_default_level(tmp_path):
    data = todays_install(tmp_path)
    conn = connect(data / DB_FILE)
    run_upgrade(data, conn, host=FakeWindowsHost())
    assert levels(conn) == {"python": "ask", "demo": "ask"}
    assert events(conn, "upgrade.levels_removed") == [{"tools": ["news", "wallet"]}]


@pytest.mark.real_levels
def test_other_os_migration_only_touches_the_database(tmp_path):
    data = todays_install(tmp_path)
    host = RecordingUnsupportedHost()
    conn = connect(data / DB_FILE)
    report = run_upgrade(data, conn, host=host)
    assert report == UpgradeReport(True, ("levels",), ())
    assert host.calls == [] and levels(conn) == {"python": "ask"}
    assert update_note(conn, None) == {"version": hearthkeep.__version__, "items": []}


def test_sandbox_item_appears_while_analysis_needs_image(tmp_path):
    data = todays_install(tmp_path)
    conn = connect(data / DB_FILE)
    run_upgrade(data, conn, host=FakeWindowsHost(), version="9.9.9")
    assert update_note(conn, addons_with(analysis="needs_image")) == {
        "version": "9.9.9",
        "items": [
            {"id": "sandbox_image", "text": NOTE_ITEMS["sandbox_image"][0], "addon": "analysis"}
        ],
    }
    assert update_note(conn, addons_with(analysis="ready"))["items"] == []
    assert dismiss_update_note(conn) is True and dismiss_update_note(conn) is False
    assert update_note(conn, addons_with(analysis="needs_image")) is None


def test_unknown_or_broken_note_items_are_not_shown(tmp_path):
    data = (tmp_path / "data").resolve()
    init_data_dir(data)
    conn = connect(data / DB_FILE)
    note = json.dumps({"version": "1.0.0", "items": ["gone_item", "shortcut_failed", 3]})
    conn.execute("INSERT INTO app_state (key, value) VALUES ('update_note', ?)", (note,))
    assert [i["id"] for i in update_note(conn, None)["items"]] == ["shortcut_failed"]
    conn.execute("UPDATE app_state SET value = 'not json' WHERE key = 'update_note'")
    assert update_note(conn, None) is None


@pytest.mark.parametrize("item", sorted(NOTE_ITEMS))
def test_note_copy_follows_the_rules(item):
    text, addon = NOTE_ITEMS[item]
    assert forbidden_in(text) == [] and "\\" not in text and text.endswith(".")
    assert addon in (None, "analysis", "startup")


def test_migration_10_adds_app_state(tmp_path):
    schema_9_database(tmp_path / "old.db")
    conn = connect(tmp_path / "old.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 10
    conn.execute("INSERT INTO app_state (key, value) VALUES ('k', 'v')")
    assert conn.execute("SELECT value FROM app_state WHERE key = 'k'").fetchone()[0] == "v"


# ---- the routes ----------------------------------------


def test_update_note_routes(harness):
    anonymous = harness.start()
    assert anonymous.get("/api/update-note").status_code == 401
    assert anonymous.post("/api/update-note/dismiss", headers=CSRF).status_code == 401
    client = signed_in(harness)
    assert client.get("/api/update-note").json() == {"note": None}
    conn = harness.conn()
    note = json.dumps({"version": "1.2.3", "items": ["startup_not_moved"]})
    conn.execute("INSERT INTO app_state (key, value) VALUES ('update_note', ?)", (note,))
    conn.close()
    assert client.get("/api/update-note").json() == {
        "note": {
            "version": "1.2.3",
            "items": [
                {
                    "id": "startup_not_moved",
                    "text": NOTE_ITEMS["startup_not_moved"][0],
                    "addon": "startup",
                }
            ],
        }
    }
    assert client.post("/api/update-note/dismiss").status_code == 403  # no CSRF header
    assert client.post("/api/update-note/dismiss", headers=CSRF).json() == {"ok": True}
    assert client.get("/api/update-note").json() == {"note": None}
    assert client.post("/api/update-note/dismiss", headers=CSRF).json() == {"ok": True}
    dismissed = [e for e in harness.events() if e["event"] == "update_note.dismissed"]
    assert len(dismissed) == 1 and dismissed[0]["detail"] == {}


# ---- serve and setup ----------------------------------------


@pytest.fixture
def serve(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write(data, "config.toml", "docker_available = false\n")
    return Serve(data, monkeypatch)


def test_serve_runs_the_upgrade_and_still_starts_when_it_fails(serve, monkeypatch, caplog):
    assert serve() == 0
    conn = connect(serve.data / DB_FILE)
    level = conn.execute("SELECT value FROM app_state WHERE key = 'upgrade_level'").fetchone()
    assert level[0] == str(UPGRADE_LEVEL) and len(events(conn, "upgrade.ran")) == 1
    conn.close()

    def broken(*args, **kwargs):
        raise RuntimeError(f"secret detail in {serve.data}")

    monkeypatch.setattr(cli, "run_upgrade", broken)
    caplog.set_level(logging.WARNING)
    assert serve() == 0 and ("running",) in serve.log
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


def test_setup_rerun_upgrades_an_old_install(tmp_path, run_cmd_setup):
    data = todays_install(tmp_path)
    host = run_cmd_setup.host
    host.task = old_task_xml(data)
    opened = []
    opener = lambda settings, h, **kw: opened.append(settings.data_dir) or 0  # noqa: E731
    assert run_cmd_setup(data, opener=opener) == 0
    assert len(opened) == 1 and host.enabled == [data] and host.shortcuts == [data]
    assert run_cmd_setup.run_server.calls == [] and run_cmd_setup.bundle_calls == 0


def test_setup_rerun_makes_a_shortcut_that_failed_at_first(tmp_path, capsys, run_cmd_setup):
    data = tmp_path / "data"
    host = run_cmd_setup.host
    host.shortcut_error = True
    assert run_cmd_setup(data) == 0
    assert cli.SHORTCUT_FAILED in capsys.readouterr().out and host.shortcuts == []
    host.shortcut_error = False
    assert run_cmd_setup(data, opener=lambda s, h, **kw: 0) == 0
    assert host.shortcuts == [data.resolve()]
    # Still failing on the re-run: said once, and the app opens anyway.
    host.shortcut_error = True
    opened = []
    assert run_cmd_setup(data, opener=lambda s, h, **kw: opened.append(1) or 0) == 0
    assert cli.SHORTCUT_FAILED in capsys.readouterr().out and opened == [1]
