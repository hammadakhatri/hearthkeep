import json
from pathlib import Path

import pytest

from hearthkeep.db import connect
from hearthkeep.sandbox.results import Chart, FileRef, ParsedResults, Table, Value
from hearthkeep.sandbox.runner import RawRun
from hearthkeep.sandbox.runs import OutputRecord, RunRecord, RunStore

CHAT = "c" * 32
OTHER_CHAT = "d" * 32
SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
    "data": {"values": [{"a": 1, "b": 2}]},
    "mark": "bar",
    "encoding": {
        "x": {"field": "a", "type": "ordinal"},
        "y": {"field": "b", "type": "quantitative"},
    },
}


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path / "hk.db")
    yield RunStore(conn, tmp_path)
    conn.close()


def raw_run(tmp_path: Path, files: dict[str, bytes], **fields) -> RawRun:
    job = tmp_path / "runtime" / "sandbox" / "jobs" / "job1"
    out = job / "out"
    out.mkdir(parents=True, exist_ok=True)
    members = {}
    for index, (name, data) in enumerate(files.items()):
        path = out / f"m{index:03d}"
        path.write_bytes(data)
        members[name] = path
    values = dict(
        status="ok", exit_code=0, stdout="done\n", stdout_truncated=False, duration_ms=1234,
        members=members, error=None, job_dir=job,
    )  # fmt: skip
    values.update(fields)
    return RawRun(**values)


def parsed(**fields) -> ParsedResults:
    return ParsedResults(**fields)


def test_migration_4_creates_runs_and_outputs(tmp_path):
    conn = connect(tmp_path / "hk.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 4
        runs = [r["name"] for r in conn.execute("PRAGMA table_info(runs)")]
        assert runs == ["id", "chat_id", "turn_id", "purpose", "script", "stdout",
                        "stdout_truncated", "status", "error", "duration_ms", "results",
                        "problems", "created_at"]  # fmt: skip
        outputs = [r["name"] for r in conn.execute("PRAGMA table_info(outputs)")]
        assert outputs == ["id", "run_id", "chat_id", "kind", "name", "mime", "size", "data",
                           "file_name", "created_at"]  # fmt: skip
    finally:
        conn.close()


def test_save_writes_rows_and_output_files(store, tmp_path):
    raw = raw_run(tmp_path, {
        "status.json": b"{}",
        "files/summary.csv": b"a,b\n1,2\n",
        "files/notes.md": b"# hi\n",
        "files/blob.parquet": b"PAR1",
    })  # fmt: skip
    results = parsed(
        values=[
            Value("total", 3, "GBP", None, False, "computed", None),
            Value("opening balance", "1,200.00", None, "as printed", False, "document", 2),
        ],
        charts=[Chart("By month", SPEC)],
        tables=[Table("Top rows", ["a", "b"], [[1, 2]], [5], None, 10)],
        files=[FileRef("summary.csv", "files/summary.csv"), FileRef("notes.md", "files/notes.md"),
               FileRef("blob.parquet", "files/blob.parquet")],
        cleaning=["dropped 2 blank rows"],
        sections={"Totals", "By month"},
        problems=["line 7: unknown record type"],
    )  # fmt: skip
    run_id = store.new_id()
    record = store.save(
        raw, results, run_id=run_id, chat_id=CHAT, turn_id="turn1", purpose="analysis",
        script="print('done')\n",
    )  # fmt: skip
    assert isinstance(record, RunRecord)
    assert record.id == run_id and record.chat_id == CHAT and record.turn_id == "turn1"
    assert (record.status, record.stdout, record.duration_ms) == ("ok", "done\n", 1234)
    assert record.script == "print('done')\n"
    assert record.values == results.values
    assert record.cleaning == ["dropped 2 blank rows"]
    assert record.sections == ["By month", "Totals"]
    assert record.problems == ["line 7: unknown record type"]
    assert store.get(run_id) == record

    outputs = store.outputs(run_id)
    assert [o.kind for o in outputs] == ["chart", "table", "file", "file", "file"]
    chart, table, csv, md, blob = outputs
    assert isinstance(chart, OutputRecord)
    assert (chart.name, chart.data) == ("By month", SPEC)
    assert table.data["columns"] == ["a", "b"] and table.data["total_rows"] == 10
    assert (csv.name, csv.mime, csv.size, csv.data) == ("summary.csv", "text/csv", 8, None)
    assert md.mime == "text/markdown"
    assert blob.mime == "application/octet-stream"

    run_dir = tmp_path / "workspace" / "chats" / CHAT / "runs" / run_id / "files"
    assert store.output_path(csv.id) == (run_dir / f"{csv.id}.csv").resolve()
    assert store.output_path(csv.id).read_bytes() == b"a,b\n1,2\n"
    assert store.output_path(md.id).name == f"{md.id}.md"
    assert store.output_path(blob.id).name == f"{blob.id}.bin"
    assert store.output(csv.id) == csv
    assert store.output_path(chart.id) is None  # charts and tables are rows, not files
    # The files were moved out of the job folder.
    assert not raw.members["files/summary.csv"].exists()


def test_save_keeps_a_failed_run(store, tmp_path):
    raw = raw_run(tmp_path, {}, status="timeout", exit_code=None, stdout="x" * 10,
                  stdout_truncated=True, error="The run took too long.")  # fmt: skip
    record = store.save(raw, parsed(), run_id=store.new_id(), chat_id=CHAT, turn_id="t",
                        purpose="vision", script=None)  # fmt: skip
    again = store.get(record.id)
    assert (again.status, again.stdout_truncated, again.error) == (
        "timeout", True, "The run took too long."
    )  # fmt: skip
    assert again.script is None and again.purpose == "vision"
    assert store.outputs(record.id) == []


def test_save_refuses_bad_ids(store, tmp_path):
    raw = raw_run(tmp_path, {})
    for kwargs in (
        dict(run_id="../x", chat_id=CHAT),
        dict(run_id="r1", chat_id="not-a-chat"),
    ):
        with pytest.raises(ValueError):
            store.save(raw, parsed(), turn_id="t", purpose="analysis", script=None, **kwargs)
    with pytest.raises(ValueError):
        store.save(raw, parsed(), run_id="r1", chat_id=CHAT, turn_id="t", purpose="other",
                   script=None)  # fmt: skip


def test_output_path_rejects_unknown_and_traversal(store, tmp_path):
    raw = raw_run(tmp_path, {"files/a.csv": b"x"})
    record = store.save(
        raw, parsed(files=[FileRef("a.csv", "files/a.csv")]), run_id=store.new_id(),
        chat_id=CHAT, turn_id="t", purpose="analysis", script="",
    )  # fmt: skip
    (output,) = store.outputs(record.id)
    assert store.output_path(output.id) is not None
    for bad in ("nope", "../" + output.id, "", "a/b", "x" * 40, None, 5):
        assert store.output_path(bad) is None
        assert store.output(bad) is None
    # A row tampered to lead elsewhere is never followed.
    secret = tmp_path / "secret.txt"
    secret.write_text("s", encoding="utf-8")
    store._conn.execute("UPDATE outputs SET file_name = ? WHERE id = ?",
                        ("../../../../../secret.txt", output.id))  # fmt: skip
    assert store.output_path(output.id) is None
    store._conn.execute("UPDATE outputs SET file_name = ?, run_id = ? WHERE id = ?",
                        (f"{output.id}.csv", "../..", output.id))  # fmt: skip
    assert store.output_path(output.id) is None


def test_runs_for_chat_newest_first(store, tmp_path):
    ids = []
    for n in range(3):
        raw = raw_run(tmp_path, {})
        ids.append(store.save(raw, parsed(), run_id=f"run{n}", chat_id=CHAT, turn_id="t",
                              purpose="analysis", script=str(n)).id)  # fmt: skip
    store.save(raw_run(tmp_path, {}), parsed(), run_id="other", chat_id=OTHER_CHAT,
               turn_id="t", purpose="analysis", script="")  # fmt: skip
    assert [r.id for r in store.runs_for_chat(CHAT)] == list(reversed(ids))
    assert [r.id for r in store.runs_for_chat(CHAT, limit=2)] == ["run2", "run1"]
    assert [r.id for r in store.runs_for_chat(OTHER_CHAT)] == ["other"]
    assert store.get("missing") is None
    assert store.outputs("missing") == []


def test_results_column_is_json(store, tmp_path):
    record = store.save(
        raw_run(tmp_path, {}), parsed(values=[Value("n", 1.5, None, None, True, "computed", None)]),
        run_id="r1", chat_id=CHAT, turn_id="t", purpose="analysis", script="",
    )  # fmt: skip
    row = store._conn.execute("SELECT results, problems FROM runs WHERE id = ?", (record.id,))
    results, problems = row.fetchone()
    assert json.loads(results)["values"][0]["is_date"] is True
    assert json.loads(problems) == []


def test_duplicate_file_records_make_one_output(store, tmp_path):
    raw = raw_run(tmp_path, {"files/a.csv": b"a\n"})
    ref = FileRef("a.csv", "files/a.csv")
    record = store.save(
        raw, parsed(files=[ref, FileRef("a.csv", "files/a.csv")]), run_id="r1", chat_id=CHAT,
        turn_id="t", purpose="analysis", script="",
    )  # fmt: skip
    assert store.get(record.id) is not None
    (output,) = store.outputs(record.id)
    assert store.output_path(output.id).read_bytes() == b"a\n"


def test_failed_save_leaves_no_run_folder(store, tmp_path, monkeypatch):
    from hearthkeep.sandbox import runs as runs_mod

    raw = raw_run(tmp_path, {"files/a.csv": b"a\n", "files/b.csv": b"b\n"})
    moved = []
    real_move = runs_mod._move

    def flaky(source, target):
        if moved:
            target.write_bytes(b"partial")  # a copy cut short
            raise OSError("disk full")
        moved.append(target)
        real_move(source, target)

    monkeypatch.setattr(runs_mod, "_move", flaky)
    refs = [FileRef("a.csv", "files/a.csv"), FileRef("b.csv", "files/b.csv")]
    with pytest.raises(OSError):
        store.save(raw, parsed(files=refs), run_id="r1", chat_id=CHAT, turn_id="t",
                   purpose="analysis", script="")  # fmt: skip
    assert not (tmp_path / "workspace" / "chats" / CHAT / "runs" / "r1").exists()
    assert store.get("r1") is None
    assert store._conn.execute("SELECT COUNT(*) FROM outputs").fetchone()[0] == 0
