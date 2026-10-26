import json

from hearthkeep import audit, db
from hearthkeep.db import connect


def test_record_and_list_newest_first(tmp_path, monkeypatch):
    conn = connect(tmp_path / "hk.db")
    times = iter(["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"])
    monkeypatch.setattr(db, "utcnow", lambda: next(times))
    audit.record(conn, "owner", "first", {"a": 1})
    audit.record(conn, "owner", "second", {})
    events = audit.list_events(conn)
    assert [e["event"] for e in events] == ["second", "first"]
    assert events[1]["detail"] == {"a": 1}
    assert events[1]["actor"] == "owner"
    assert events[1]["at"] == "2026-01-01T00:00:00+00:00"
    assert len(audit.list_events(conn, limit=1)) == 1


def test_list_filters_by_query(tmp_path):
    conn = connect(tmp_path / "hk.db")
    audit.record(conn, "owner", "login", {})
    audit.record(conn, "agent", "tool.run", {"path": "needle"})
    assert [e["event"] for e in audit.list_events(conn, query="login")] == ["login"]
    assert [e["event"] for e in audit.list_events(conn, query="needle")] == ["tool.run"]
    assert [e["event"] for e in audit.list_events(conn, query="agent")] == ["tool.run"]
    assert audit.list_events(conn, query="nomatch") == []


def test_record_redacts_secret_keys(tmp_path):
    conn = connect(tmp_path / "hk.db")
    audit.record(conn, "owner", "x", {"token": "abc", "path": "p"})
    raw = conn.execute("SELECT detail FROM audit").fetchone()["detail"]
    assert "abc" not in raw
    assert json.loads(raw) == {"token": "[redacted]", "path": "p"}


def test_redaction_is_case_insensitive_and_recursive(tmp_path):
    conn = connect(tmp_path / "hk.db")
    detail = {"API_KEY": "k1", "n": {"Password": "p1", "items": [{"code": "c1", "ok": 1}]}}
    audit.record(conn, "owner", "x", detail)
    raw = conn.execute("SELECT detail FROM audit").fetchone()["detail"]
    for leaked in ("k1", "p1", "c1"):
        assert leaked not in raw
    assert json.loads(raw)["n"]["items"][0]["ok"] == 1
    assert detail["API_KEY"] == "k1"  # input not mutated


def test_query_treats_wildcards_literally(tmp_path):
    conn = connect(tmp_path / "hk.db")
    audit.record(conn, "owner", "a_b", {})
    audit.record(conn, "owner", "axb", {})
    assert [e["event"] for e in audit.list_events(conn, query="a_b")] == ["a_b"]
