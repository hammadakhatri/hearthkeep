"""The Analysis gallery over HTTP: files and charts grouped by chat, and a chart's headline."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import json

from test_api import harness, signed_in  # noqa: F401  (harness is a fixture)
from test_api_runs import SPEC, run_once

A, B, C, D = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)


def seed(conn, chat_id, title, *, files=(), runs=(), charts=()):
    """files: (id, status, created_at); runs: (id, created_at); charts: (id, run_id, title, at)."""
    conn.execute(
        "INSERT INTO chats (id, title, think, created_at) VALUES (?, ?, 0, '2026-01-01T00:00:00')",
        (chat_id, title),
    )
    for file_id, status, at in files:
        conn.execute(
            "INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source,"
            " file_name, created_at) VALUES (?, ?, ?, 'csv', 'text/csv', 10, ?, 'upload', ?, ?)",
            (file_id, chat_id, f"{file_id}.csv", status, f"{file_id}.csv", at),
        )
    for run_id, at in runs:
        conn.execute(
            "INSERT INTO runs (id, chat_id, turn_id, purpose, status, created_at)"
            " VALUES (?, ?, 't', 'analysis', 'ok', ?)",
            (run_id, chat_id, at),
        )
    for out_id, run_id, name, at in charts:
        conn.execute(
            "INSERT INTO outputs (id, run_id, chat_id, kind, name, mime, size, data, created_at)"
            " VALUES (?, ?, ?, 'chart', ?, 'application/json', 1, ?, ?)",
            (out_id, run_id, chat_id, name, json.dumps(SPEC), at),
        )


def seeded(harness):
    client = signed_in(harness)
    conn = harness.conn()
    try:
        seed(conn, A, "Old bank", files=[("fa", "ready", "2026-03-01T10:00:00.000000Z")])
        seed(
            conn, B, "New sales",
            files=[("fb1", "unchecked", "2026-03-02T10:00:00.000000Z"),
                   ("fb2", "pending", "2026-03-02T10:30:00.000000Z")],
            runs=[("rb", "2026-03-05T10:00:00.000000Z")],
            charts=[("ob1", "rb", "By month", "2026-03-05T10:00:01.000000Z"),
                    ("ob2", "rb", "By shop", "2026-03-05T10:00:02.000000Z")],
        )  # fmt: skip
        # No file at all, only a pending one, and only a run: none of these are listed.
        seed(conn, C, "No files", runs=[("rc", "2026-03-09T10:00:00.000000Z")])
        seed(conn, D, "Pending only", files=[("fd", "pending", "2026-03-08T10:00:00.000000Z")])
        conn.commit()
    finally:
        conn.close()
    return client


def test_lists_only_chats_with_files_newest_first(harness):
    client = seeded(harness)
    body = client.get("/api/analysis").json()
    assert [c["chat_id"] for c in body["chats"]] == [B, A]
    sales = body["chats"][0]
    assert sales["title"] == "New sales"
    # The newest of the chat's files and runs.
    assert sales["updated_at"] == "2026-03-05T10:00:00.000000Z"
    assert body["chats"][1]["updated_at"] == "2026-03-01T10:00:00.000000Z"
    # A pending file is not shown.
    assert [f["id"] for f in sales["files"]] == ["fb1"]
    assert sales["files"][0] == {
        "id": "fb1", "chat_id": B, "name": "fb1.csv", "kind": "csv", "size": 10,
        "status": "unchecked", "created_at": "2026-03-02T10:00:00.000000Z",
    }  # fmt: skip


def test_includes_charts_per_chat(harness):
    client = seeded(harness)
    chats = {c["chat_id"]: c for c in client.get("/api/analysis").json()["chats"]}
    assert chats[B]["charts"] == [
        {"id": "ob1", "run_id": "rb", "title": "By month",
         "created_at": "2026-03-05T10:00:01.000000Z"},
        {"id": "ob2", "run_id": "rb", "title": "By shop",
         "created_at": "2026-03-05T10:00:02.000000Z"},
    ]  # fmt: skip
    assert chats[A]["charts"] == []
    # The list stays small: no spec in it.
    assert "spec" not in json.dumps(chats[B]["charts"])


def test_paginates_with_before(harness):
    client = seeded(harness)
    first = client.get("/api/analysis", params={"limit": 1}).json()
    assert [c["chat_id"] for c in first["chats"]] == [B]
    cursor = first["chats"][-1]["updated_at"]
    second = client.get("/api/analysis", params={"limit": 1, "before": cursor}).json()
    assert [c["chat_id"] for c in second["chats"]] == [A]
    end = client.get(
        "/api/analysis", params={"limit": 1, "before": second["chats"][-1]["updated_at"]}
    ).json()
    assert end == {"chats": []}
    for bad in ({"limit": 0}, {"limit": 51}, {"before": "not a date"}):
        assert client.get("/api/analysis", params=bad).status_code == 422, bad


def test_analysis_requires_session(harness):
    client = harness.start()
    assert client.get("/api/analysis").status_code == 401


def test_output_includes_turn_headline(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)
    [(chart_id,)] = harness.query("SELECT id FROM outputs WHERE kind = 'chart'")
    chart = client.get(f"/api/outputs/{chart_id}").json()
    assert chart["turn_id"] == turn_id and chart["headline"] is None

    marks = [{"start": 4, "end": 8, "text": "42.5", "kind": "computed", "page": None, "note": None}]
    conn = harness.conn()
    try:
        for message_id, text, shown in ((900, "Early 1.0", []), (901, "Total 42.5 in all", marks)):
            conn.execute(
                "INSERT INTO number_checks (message_id, chat_id, turn_id, text, marks, created_at)"
                " VALUES (?, ?, ?, ?, ?, '2026-03-05T10:00:00Z')",
                (message_id, chat_id, turn_id, text, json.dumps(shown)),
            )
        # Another turn's check never leaks in.
        conn.execute(
            "INSERT INTO number_checks (message_id, chat_id, turn_id, text, marks, created_at)"
            " VALUES (999, ?, 'other', 'Other', '[]', '2026-03-05T10:00:00Z')",
            (chat_id,),
        )
        conn.commit()
    finally:
        conn.close()
    chart = client.get(f"/api/outputs/{chart_id}").json()
    assert chart["turn_id"] == turn_id
    assert chart["headline"] == {"text": "Total 42.5 in all", "marks": marks}
    assert chart["spec"] == SPEC
