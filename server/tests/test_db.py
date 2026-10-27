import sqlite3

from hearthkeep.db import connect, utcnow

EXPECTED_TABLES = {
    "credentials", "sessions", "enrollment_codes", "chats", "chat_grants", "messages",
    "turn_events", "approvals", "tool_levels", "audit", "images", "uploads", "chat_files",
    "upload_chats", "runs", "outputs",
}


def test_connect_applies_schema(tmp_path):
    conn = connect(tmp_path / "hk.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_connect_idempotent(tmp_path):
    path = tmp_path / "hk.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()


def test_two_connections_share_data(tmp_path):
    path = tmp_path / "hk.db"
    a, b = connect(path), connect(path)
    a.execute("INSERT INTO tool_levels(tool, level) VALUES ('t', 'ask')")
    a.commit()
    assert b.execute("SELECT level FROM tool_levels WHERE tool='t'").fetchone()["level"] == "ask"


def test_utcnow_is_utc_iso():
    assert utcnow().endswith("+00:00")


def test_migration_2_adds_images_and_uploads(tmp_path):
    conn = connect(tmp_path / "hk.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    img = {r["name"] for r in conn.execute("PRAGMA table_info(images)")}
    assert img == {
        "id", "chat_id", "turn_id", "model_id", "prompt", "width", "height", "seed",
        "file_name", "created_at",
    }
    up = {r["name"] for r in conn.execute("PRAGMA table_info(uploads)")}
    assert up == {"id", "file_name", "mime", "width", "height", "created_at"}
    conn.close()


def test_utcnow_has_microseconds():
    assert len(utcnow().split(".")[1]) >= 6
