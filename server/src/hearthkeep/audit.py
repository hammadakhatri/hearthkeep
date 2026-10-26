"""Append-only audit log with secret redaction."""

import json
import sqlite3
from typing import Any

from hearthkeep import db

REDACTED_KEYS = frozenset({"token", "secret", "api_key", "password", "code"})
REDACTED = "[redacted]"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and k.lower() in REDACTED_KEYS else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


def record(conn: sqlite3.Connection, actor: str, event: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit (at, actor, event, detail) VALUES (?, ?, ?, ?)",
        (db.utcnow(), actor, event, json.dumps(_redact(detail), default=str)),
    )


def list_events(conn: sqlite3.Connection, limit: int = 100, query: str | None = None) -> list[dict]:
    sql = "SELECT id, at, actor, event, detail FROM audit"
    params: list[Any] = []
    if query:
        escaped = query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        sql += (
            " WHERE actor LIKE ? ESCAPE '!' OR event LIKE ? ESCAPE '!'"
            " OR detail LIKE ? ESCAPE '!'"
        )
        params += [f"%{escaped}%"] * 3
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in conn.execute(sql, params)]
