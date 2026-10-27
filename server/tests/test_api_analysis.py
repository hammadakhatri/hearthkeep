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
        seed(conn, A, "Old bank", files=[("fa", "ready", "2026-03-01T10:00:00.000000+00:00")])
        seed(
            conn, B, "New sales",
            files=[("fb1", "unchecked", "2026-03-02T10:00:00.000000+00:00"),
                   ("fb2", "pending", "2026-03-02T10:30:00.000000+00:00")],
            runs=[("rb", "2026-03-05T10:00:00.000000+00:00")],
            charts=[("ob1", "rb", "By month", "2026-03-05T10:00:01.000000+00:00"),
                    ("ob2", "rb", "By shop", "2026-03-05T10:00:02.000000+00:00")],
        )  # fmt: skip
        # No file at all, only a pending one, and only a run: none of these are listed.
        seed(conn, C, "No files", runs=[("rc", "2026-03-09T10:00:00.000000+00:00")])
        seed(conn, D, "Pending only", files=[("fd", "pending", "2026-03-08T10:00:00.000000+00:00")])
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
    assert sales["updated_at"] == "2026-03-05T10:00:00.000000+00:00"
    assert body["chats"][1]["updated_at"] == "2026-03-01T10:00:00.000000+00:00"
    # A pending file is not shown.
    assert [f["id"] for f in sales["files"]] == ["fb1"]
    assert sales["files"][0] == {
        "id": "fb1", "chat_id": B, "name": "fb1.csv", "kind": "csv", "size": 10,
        "status": "unchecked", "created_at": "2026-03-02T10:00:00.000000+00:00",
    }  # fmt: skip


def test_includes_charts_per_chat(harness):
    client = seeded(harness)
    chats = {c["chat_id"]: c for c in client.get("/api/analysis").json()["chats"]}
    assert chats[B]["charts"] == [
        {"id": "ob1", "run_id": "rb", "title": "By month",
         "created_at": "2026-03-05T10:00:01.000000+00:00"},
        {"id": "ob2", "run_id": "rb", "title": "By shop",
         "created_at": "2026-03-05T10:00:02.000000+00:00"},
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
    assert end == {"chats": [], "more": False}
    assert first["more"] is True and second["more"] is False
    # A cursor in another notation (Z, or no zone) is read as the same time.
    again = client.get(
        "/api/analysis", params={"limit": 1, "before": cursor.replace("+00:00", "Z")}
    ).json()
    assert [c["chat_id"] for c in again["chats"]] == [A]
    for bad in ({"limit": 0}, {"limit": 51}, {"before": "not a date"}):
        assert client.get("/api/analysis", params=bad).status_code == 422, bad


def test_default_limit_is_twenty_and_more_says_so(harness):
    client = signed_in(harness)
    conn = harness.conn()
    try:
        for n in range(21):
            at = f"2026-04-01T10:{n:02d}:00.000000+00:00"
            seed(conn, f"{n:032x}", f"Chat {n}", files=[(f"f{n}", "ready", at)])
        conn.commit()
    finally:
        conn.close()
    page = client.get("/api/analysis").json()
    assert len(page["chats"]) == 20 and page["more"] is True
    rest = client.get("/api/analysis", params={"before": page["chats"][-1]["updated_at"]}).json()
    assert [c["title"] for c in rest["chats"]] == ["Chat 0"] and rest["more"] is False


def test_analysis_requires_session(harness):
    client = harness.start()
    assert client.get("/api/analysis").status_code == 401


def test_output_includes_turn_headline(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)
    [(chart_id,)] = harness.query("SELECT id FROM outputs WHERE kind = 'chart'")
    chart = client.get(f"/api/outputs/{chart_id}").json()
    assert chart["turn_id"] == turn_id and chart["headline"] is None

    assistants = harness.query(
        "SELECT id, content FROM messages WHERE role = 'assistant' ORDER BY id"
    )
    final_id = assistants[-1][0]
    assert assistants[-1][1] == "Done." and len(assistants) >= 2
    earlier_id = assistants[0][0]
    marks = [{"start": 4, "end": 8, "text": "42.5", "kind": "computed", "page": None, "note": None}]

    def check(message_id, text, shown, turn="t"):
        conn = harness.conn()
        try:
            conn.execute(
                "INSERT INTO number_checks (message_id, chat_id, turn_id, text, marks, created_at)"
                " VALUES (?, ?, ?, ?, ?, '2026-03-05T10:00:00+00:00')",
                (message_id, chat_id, turn, text, json.dumps(shown)),
            )
            conn.commit()
        finally:
            conn.close()

    # An earlier step's check is never the headline when the final reply has none.
    check(earlier_id, "Early 1.0", [], turn_id)
    check(999, "Other chat message", [], "other")
    assert client.get(f"/api/outputs/{chart_id}").json()["headline"] is None
    check(final_id, "Total 42.5 in all", marks, turn_id)
    chart = client.get(f"/api/outputs/{chart_id}").json()
    assert chart["turn_id"] == turn_id
    assert chart["headline"] == {"text": "Total 42.5 in all", "marks": marks}
    assert chart["spec"] == SPEC
