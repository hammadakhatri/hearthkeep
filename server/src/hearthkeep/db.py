"""SQLite connection and schema migrations (standard library only)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

BUSY_TIMEOUT_MS = 5000

# Ordered migrations keyed by the schema version they produce. DDL is idempotent so two
# processes racing to migrate the same file cannot fail each other.
MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS credentials (
        id TEXT PRIMARY KEY,
        public_key BLOB,
        sign_count INT,
        device_name TEXT,
        created_at TEXT,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        credential_id TEXT,
        created_at TEXT,
        last_seen_at TEXT,
        revoked_at TEXT
    );
    CREATE TABLE IF NOT EXISTS enrollment_codes (
        code_hash TEXT PRIMARY KEY,
        purpose TEXT,
        expires_at TEXT,
        used_at TEXT
    );
    CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        title TEXT,
        think INT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS chat_grants (
        chat_id TEXT,
        tool TEXT,
        PRIMARY KEY (chat_id, tool)
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        chat_id TEXT,
        role TEXT,
        content TEXT,
        tool_calls TEXT,
        tool_call_id TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS turn_events (
        id INTEGER PRIMARY KEY,
        chat_id TEXT,
        turn_id TEXT,
        seq INT,
        type TEXT,
        data TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        chat_id TEXT,
        turn_id TEXT,
        tool TEXT,
        action_class TEXT,
        summary TEXT,
        detail TEXT,
        requires_step_up INT,
        status TEXT,
        created_at TEXT,
        expires_at TEXT,
        resolved_at TEXT,
        resolved_by_session TEXT
    );
    CREATE TABLE IF NOT EXISTS tool_levels (
        tool TEXT PRIMARY KEY,
        level TEXT
    );
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY,
        at TEXT,
        actor TEXT,
        event TEXT,
        detail TEXT
    );
    """,
    2: """
    CREATE TABLE IF NOT EXISTS images (
        id TEXT PRIMARY KEY,
        chat_id TEXT,
        turn_id TEXT,
        model_id TEXT,
        prompt TEXT,
        width INT,
        height INT,
        seed INT,
        file_name TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY,
        file_name TEXT,
        mime TEXT,
        width INT,
        height INT,
        created_at TEXT
    );
    """,
    3: """
    CREATE TABLE IF NOT EXISTS chat_files (
        id TEXT PRIMARY KEY,
        chat_id TEXT,
        name TEXT,
        kind TEXT,
        mime TEXT,
        size INT,
        status TEXT,
        summary TEXT,
        source TEXT,
        file_name TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS chat_files_chat ON chat_files(chat_id);
    CREATE TABLE IF NOT EXISTS upload_chats (
        upload_id TEXT PRIMARY KEY,
        chat_id TEXT
    );
    """,
    4: """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        chat_id TEXT,
        turn_id TEXT,
        purpose TEXT,
        script TEXT,
        stdout TEXT,
        stdout_truncated INT,
        status TEXT,
        error TEXT,
        duration_ms INT,
        results TEXT,
        problems TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS runs_chat ON runs(chat_id);
    CREATE TABLE IF NOT EXISTS outputs (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        chat_id TEXT,
        kind TEXT,
        name TEXT,
        mime TEXT,
        size INT,
        data TEXT,
        file_name TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS outputs_run ON outputs(run_id);
    """,
    5: """
    CREATE TABLE IF NOT EXISTS number_checks (
        message_id INTEGER PRIMARY KEY,
        chat_id TEXT,
        turn_id TEXT,
        text TEXT,
        marks TEXT,
        created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS number_checks_chat ON number_checks(chat_id);
    """,
    6: """
    CREATE TABLE IF NOT EXISTS allowed_folders (
        id TEXT PRIMARY KEY,
        path TEXT UNIQUE,
        label TEXT UNIQUE,
        created_at TEXT
    );
    """,
    7: """
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        credential_id TEXT PRIMARY KEY,
        endpoint TEXT,
        created_at TEXT
    );
    """,
    8: """
    CREATE TABLE IF NOT EXISTS sent_items (
        id TEXT PRIMARY KEY,
        chat_id TEXT,
        kind TEXT,
        ref_id TEXT,
        title TEXT,
        text TEXT,
        marks TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sent_targets (
        item_id TEXT,
        credential_id TEXT,
        dismissed_at TEXT,
        PRIMARY KEY (item_id, credential_id)
    );
    CREATE INDEX IF NOT EXISTS sent_targets_credential ON sent_targets(credential_id);
    """,
}


def utcnow() -> str:
    """Current time as a UTC ISO-8601 string with fixed microsecond precision.

    Earlier rows may hold timestamps without microseconds; they stay valid and are not rewritten.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version};\nCOMMIT;"
        )


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection (one per process/thread) with WAL, foreign keys and migrations.

    The connection is in autocommit mode; use explicit BEGIN/COMMIT for transactions.
    """
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception (autocommit connections)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
