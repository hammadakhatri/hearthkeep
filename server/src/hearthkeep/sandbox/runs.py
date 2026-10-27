"""Saved sandbox runs: a `runs` row for every run (script, printed output, results, status,
run time) and an `outputs` row for each chart, table and file it made. Files live in
`<data>/workspace/chats/<chat-id>/runs/<run-id>/files/<output-id>.<ext>`."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal

from hearthkeep import db
from hearthkeep.files.store import CHAT_ID
from hearthkeep.images import UPLOAD_ID, inside
from hearthkeep.sandbox.results import ParsedResults, Value
from hearthkeep.sandbox.runner import RawRun

RUN_ID = UPLOAD_ID
OUTPUT_ID = UPLOAD_ID
PURPOSES = ("analysis", "vision")
CHART_MIME = "application/vnd.vegalite.v6+json"
TABLE_MIME = "application/json"
FILE_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
    "json": "application/json",
    "md": "text/markdown",
}
OTHER_EXT, OTHER_MIME = "bin", "application/octet-stream"
_RUN_COLUMNS = (
    "id, chat_id, turn_id, purpose, script, stdout, stdout_truncated, status, error, "
    "duration_ms, results, problems, created_at"
)
_OUTPUT_COLUMNS = "id, run_id, chat_id, kind, name, mime, size, data, created_at"


@dataclass
class RunRecord:
    id: str
    chat_id: str
    turn_id: str
    purpose: Literal["analysis", "vision"]
    script: str | None
    stdout: str
    stdout_truncated: bool
    status: str
    error: str | None
    duration_ms: int
    values: list[Value]
    cleaning: list[str]
    sections: list[str]
    problems: list[str]
    created_at: str


@dataclass
class OutputRecord:
    id: str
    run_id: str
    chat_id: str
    kind: Literal["chart", "table", "file"]
    name: str
    mime: str
    size: int
    data: dict | None
    created_at: str


def _file_type(name: str) -> tuple[str, str]:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in FILE_TYPES:
        return ext, FILE_TYPES[ext]
    return OTHER_EXT, OTHER_MIME


def _run(row: sqlite3.Row) -> RunRecord:
    results = json.loads(row["results"] or "{}")
    return RunRecord(
        id=row["id"],
        chat_id=row["chat_id"],
        turn_id=row["turn_id"],
        purpose=row["purpose"],
        script=row["script"],
        stdout=row["stdout"] or "",
        stdout_truncated=bool(row["stdout_truncated"]),
        status=row["status"],
        error=row["error"],
        duration_ms=int(row["duration_ms"] or 0),
        values=[Value(**v) for v in results.get("values", [])],
        cleaning=list(results.get("cleaning", [])),
        sections=list(results.get("sections", [])),
        problems=json.loads(row["problems"] or "[]"),
        created_at=row["created_at"],
    )


def _output(row: sqlite3.Row) -> OutputRecord:
    values = dict(row)
    values["data"] = json.loads(values["data"]) if values["data"] else None
    return OutputRecord(**values)


def _move(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError:  # another volume: copy, then drop the original
        shutil.copyfile(source, target)
        with contextlib.suppress(OSError):
            source.unlink()


class RunStore:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path) -> None:
        self._conn = conn
        self._chats = Path(data_dir) / "workspace" / "chats"

    @staticmethod
    def new_id() -> str:
        return token_urlsafe(12)

    def _files_dir(self, chat_id: str, run_id: str) -> Path:
        if not isinstance(chat_id, str) or not CHAT_ID.fullmatch(chat_id):
            raise ValueError("not a chat id")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("not a run id")
        return self._chats / chat_id / "runs" / run_id / "files"

    def save(
        self,
        raw: RawRun,
        parsed: ParsedResults,
        *,
        run_id: str,
        chat_id: str,
        turn_id: str,
        purpose: Literal["analysis", "vision"],
        script: str | None,
    ) -> RunRecord:
        """The run's row, plus one output per chart, table and file, in one transaction.
        Output files are moved out of the run's job folder into the chat's run folder."""
        files_dir = self._files_dir(chat_id, run_id)
        if purpose not in PURPOSES:
            raise ValueError("bad purpose")
        now = db.utcnow()
        record = RunRecord(
            id=run_id,
            chat_id=chat_id,
            turn_id=turn_id,
            purpose=purpose,
            script=script,
            stdout=raw.stdout,
            stdout_truncated=raw.stdout_truncated,
            status=raw.status,
            error=raw.error,
            duration_ms=raw.duration_ms,
            values=list(parsed.values),
            cleaning=list(parsed.cleaning),
            sections=sorted(parsed.sections),
            problems=list(parsed.problems),
            created_at=now,
        )
        results = {
            "values": [dataclasses.asdict(v) for v in record.values],
            "cleaning": record.cleaning,
            "sections": record.sections,
        }
        rows: list[tuple] = []
        for chart in parsed.charts:
            body = json.dumps(chart.spec)
            rows.append((self.new_id(), "chart", chart.title, CHART_MIME, len(body), body, None))
        for table in parsed.tables:
            body = json.dumps(dataclasses.asdict(table))
            rows.append((self.new_id(), "table", table.name, TABLE_MIME, len(body), body, None))
        moves: list[tuple[Path, Path]] = []
        seen: set[str] = set()
        for ref in parsed.files:
            if ref.member in seen:  # the same file reported twice: one output
                continue
            seen.add(ref.member)
            source = raw.members.get(ref.member)
            if source is None or not source.is_file():
                record.problems.append(f"file {ref.name}: not among the run's output files")
                continue
            output_id = self.new_id()
            ext, mime = _file_type(ref.name)
            file_name = f"{output_id}.{ext}"
            moves.append((source, files_dir / file_name))
            rows.append((output_id, "file", ref.name, mime, source.stat().st_size, None, file_name))

        run_dir = files_dir.parent
        made_run_dir = not run_dir.exists()
        placed: list[Path] = []
        try:
            with db.transaction(self._conn):
                self._conn.execute(
                    f"INSERT INTO runs ({_RUN_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, chat_id, turn_id, purpose, script, record.stdout,
                     int(record.stdout_truncated), record.status, record.error,
                     record.duration_ms, json.dumps(results), json.dumps(record.problems), now),
                )  # fmt: skip
                for output_id, kind, name, mime, size, data, file_name in rows:
                    self._conn.execute(
                        f"INSERT INTO outputs ({_OUTPUT_COLUMNS}, file_name) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (output_id, run_id, chat_id, kind, name, mime, size, data, now,
                         file_name),
                    )  # fmt: skip
                if moves:
                    files_dir.mkdir(parents=True, exist_ok=True)
                for source, target in moves:
                    placed.append(target)  # before the move: a cut copy is removed too
                    _move(source, target)
        except BaseException:
            for target in placed:
                with contextlib.suppress(OSError):
                    target.unlink(missing_ok=True)
            if made_run_dir:
                shutil.rmtree(run_dir, ignore_errors=True)
            raise
        return record

    def get(self, run_id: str) -> RunRecord | None:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            return None
        row = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return _run(row) if row else None

    def outputs(self, run_id: str) -> list[OutputRecord]:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            return []
        rows = self._conn.execute(
            f"SELECT {_OUTPUT_COLUMNS} FROM outputs WHERE run_id = ? ORDER BY rowid", (run_id,)
        )
        return [_output(r) for r in rows]

    def output(self, output_id: str) -> OutputRecord | None:
        if not isinstance(output_id, str) or not OUTPUT_ID.fullmatch(output_id):
            return None
        row = self._conn.execute(
            f"SELECT {_OUTPUT_COLUMNS} FROM outputs WHERE id = ?", (output_id,)
        ).fetchone()
        return _output(row) if row else None

    def output_path(self, output_id: str) -> Path | None:
        """An output's file, only ever inside its own run's files folder; None otherwise."""
        if not isinstance(output_id, str) or not OUTPUT_ID.fullmatch(output_id):
            return None
        row = self._conn.execute(
            "SELECT run_id, chat_id, kind, file_name FROM outputs WHERE id = ?", (output_id,)
        ).fetchone()
        if row is None or row["kind"] != "file" or not row["file_name"]:
            return None
        try:
            folder = self._files_dir(row["chat_id"], row["run_id"])
        except ValueError:
            return None
        return inside(folder, row["file_name"])

    def runs_for_chat(self, chat_id: str, limit: int = 200) -> list[RunRecord]:
        """The chat's runs, newest first."""
        rows = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE chat_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (chat_id, int(limit)),
        )
        return [_run(r) for r in rows]
