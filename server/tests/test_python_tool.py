"""The `python` tool: sandboxed analysis runs, saved and reported to the model."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import asyncio
import json
import subprocess

import pytest
from fakes import FakeSandbox
from test_api import (  # noqa: F401  (harness is a fixture)
    CSRF,
    harness,
    new_chat,
    read_events,
    say,
    signed_in,
)

from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.files.checks import FileCheck
from hearthkeep.files.store import ChatFileStore
from hearthkeep.permissions import ActionClass, ToolLevel, effective_level, get_level
from hearthkeep.providers.chat import StreamDone, TextDelta, ToolCallRequest
from hearthkeep.sandbox.charts import validate_chart_spec
from hearthkeep.sandbox.runs import RunStore
from hearthkeep.tools.base import ToolContext, ToolRegistry
from hearthkeep.tools.python import (
    NO_DOCKER,
    NO_IMAGE,
    TIMEOUT,
    PythonTool,
    format_run_for_model,
)

CHAT = "a" * 32
SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
    "data": {"values": [{"m": "Jan", "v": 2}]},
    "mark": "bar",
    "encoding": {
        "x": {"field": "m", "type": "ordinal"},
        "y": {"field": "v", "type": "quantitative"},
    },
}


def jsonl(*records) -> bytes:
    return b"".join(json.dumps(r).encode() + b"\n" for r in records)


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "hk.db")
    files = ChatFileStore(conn, tmp_path)
    runs = RunStore(conn, tmp_path)
    sandbox = FakeSandbox(tmp_path / "fake")
    settings = Settings(data_dir=tmp_path, docker_available=True)

    class Env:
        pass

    e = Env()
    e.conn, e.files, e.runs, e.sandbox, e.tmp = conn, files, runs, sandbox, tmp_path
    e.tool = PythonTool(sandbox, files, runs)
    e.ctx = ToolContext(CHAT, "turn1", settings, conn)

    def add_file(data=b"date,amount\n2024-03-01,12.50\n", name="sales.csv", kind="csv"):
        staged = files.stage(CHAT, data)
        return files.record(staged, name=name, check=FileCheck(kind, "text/csv", kind),
                            source="upload")  # fmt: skip

    e.add_file = add_file
    yield e
    conn.close()


def args(script="import hk\nprint('hi')\n", purpose="Total sales"):
    return {"script": script, "purpose": purpose}


def test_tool_is_look_with_python_level_key(env):
    tool = env.tool
    assert tool.name == "python" and tool.level_key == "python"
    assert tool.classify(args()) == ActionClass.LOOK
    assert tool.summarize(args()) == "Run Python: Total sales"
    params = tool.parameters
    assert params["required"] == ["script", "purpose"]
    assert params["additionalProperties"] is False
    assert params["properties"]["script"]["minLength"] == 1
    assert params["properties"]["script"]["maxLength"] == 20000
    assert params["properties"]["purpose"]["minLength"] == 1
    assert params["properties"]["purpose"]["maxLength"] == 120


async def test_run_saves_run_and_attaches_it(env):
    saved = env.add_file()
    env.sandbox.queue_run(
        {"stdout.txt": b"hi\n", "results.jsonl": jsonl(
            {"type": "value", "name": "total", "value": 12.5, "unit": "GBP", "kind": "computed"}
        )},
        stdout="hi\n",
    )  # fmt: skip
    result = await env.tool.run(args(), env.ctx)

    assert result.ok is True
    [call] = env.sandbox.calls
    assert call["mode"] == "script"
    assert call["script"] == args()["script"]
    assert call["files_dir"] == env.files.files_dir(CHAT)
    assert call["manifest"]["files"] == [
        {"id": saved.id, "name": "sales.csv", "kind": "csv", "path": f"/data/{saved.id}.csv"}
    ]
    rec = env.runs.get(call["run_id"])
    assert rec is not None
    assert (rec.chat_id, rec.turn_id, rec.purpose, rec.status) == (CHAT, "turn1", "analysis", "ok")
    assert rec.script == args()["script"]
    assert rec.stdout == "hi\n"
    assert [(v.name, v.value, v.unit) for v in rec.values] == [("total", 12.5, "GBP")]
    assert result.data == {"attachments": [{"kind": "run", "id": rec.id}]}
    assert result.content == format_run_for_model(rec, env.runs.outputs(rec.id))
    # The job folder is gone once the run is saved.
    assert not (env.sandbox.root / "jobs" / rec.id).exists()


async def test_runs_with_no_files_create_the_chats_files_folder(env):
    assert not env.files.files_dir(CHAT).exists()
    result = await env.tool.run(args(), env.ctx)
    assert result.ok is True
    assert env.files.files_dir(CHAT).is_dir()
    assert env.sandbox.calls[0]["manifest"]["files"] == []


async def test_content_lists_results_charts_tables_files(env):
    results = jsonl(
        {"type": "section", "name": "glance"},
        {"type": "value", "name": "total", "value": 42, "unit": "GBP", "kind": "computed"},
        {"type": "value", "name": "rows", "value": 3, "kind": "computed"},
        {"type": "value", "name": "opening balance", "value": "1,200.00", "kind": "document",
         "page": 2},
        {"type": "chart", "title": "By month", "spec": SPEC},
        {"type": "table", "name": "Negative amounts", "columns": ["date", "amount"],
         "rows": [["2024-03-02", -3], ["2024-03-04", -1.5]], "row_numbers": [3, 5],
         "total_rows": 2},
        {"type": "file", "name": "clean.csv", "member": "files/clean.csv"},
        {"type": "cleaning", "note": "Dropped 1 blank row."},
        "not an object",
    )  # fmt: skip
    env.sandbox.queue_run(
        {"stdout.txt": b"done\n", "results.jsonl": results, "files/clean.csv": b"a\n1\n"},
        stdout="done\n",
    )
    result = await env.tool.run(args(), env.ctx)
    rec = env.runs.get(env.sandbox.calls[0]["run_id"])
    outputs = {o.kind: o for o in env.runs.outputs(rec.id)}
    chart, table, file = outputs["chart"], outputs["table"], outputs["file"]
    assert result.content == (
        "Run ok in 1.5 s.\n\n"
        "Printed output:\ndone\n\n"
        "Results:\n"
        "- total = 42 GBP\n"
        "- rows = 3\n"
        "- opening balance = 1,200.00 (read from page 2)\n\n"
        f"Charts:\n- chart {chart.id}: By month\n\n"
        f"Tables:\n- table {table.id}: Negative amounts, 2 rows\n"
        "row,date,amount\n3,2024-03-02,-3\n5,2024-03-04,-1.5\n\n"
        f"Files:\n- file {file.id}: clean.csv\n\n"
        "Cleaning notes:\n- Dropped 1 blank row.\n\n"
        "Problems:\n- line 9: not a JSON object"
    )


def test_content_cuts_printed_output_and_tables():
    from hearthkeep.sandbox.runs import OutputRecord, RunRecord

    rec = RunRecord(
        id="r1", chat_id=CHAT, turn_id="t", purpose="analysis", script="x", stdout="x" * 9000,
        stdout_truncated=False, status="ok", error=None, duration_ms=250, values=[],
        cleaning=[], sections=[], problems=[], created_at="now",
    )  # fmt: skip
    rows = [[i] for i in range(30)]
    table = OutputRecord(
        id="o1", run_id="r1", chat_id=CHAT, kind="table", name="Many", mime="application/json",
        size=1, created_at="now",
        data={"name": "Many", "columns": ["n"], "rows": rows, "row_numbers": [],
              "note": None, "total_rows": 400},
    )  # fmt: skip
    text = format_run_for_model(rec, [table])
    assert text.startswith("Run ok in 0.2 s.\n\nPrinted output:\n" + "x" * 8000 + "\n")
    assert "x" * 8001 not in text
    assert "(printed output cut)" in text
    tables = text.split("Tables:\n", 1)[1]
    assert tables.splitlines()[:3] == ["- table o1: Many, 400 rows", "n", "0"]
    assert tables.splitlines()[-1] == "19"
    assert len(tables.splitlines()) == 22  # the line, the header and 20 rows


def _no_spawning(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("nothing may be spawned")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


async def test_without_docker_returns_plain_message_never_runs_on_host(env, monkeypatch):
    _no_spawning(monkeypatch)
    env.sandbox.docker = False
    result = await env.tool.run(args(), env.ctx)
    assert (result.ok, result.content) == (False, NO_DOCKER)
    assert env.sandbox.calls == []
    assert env.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert NO_DOCKER == (
        "Python analysis needs Docker Desktop. Start Docker on the PC and ask again."
    )

    # No sandbox at all (Docker was missing at startup): the same plain message.
    bare = PythonTool(None, env.files, env.runs)
    result = await bare.run(args(), env.ctx)
    assert (result.ok, result.content) == (False, NO_DOCKER)


async def test_without_image_message(env, monkeypatch):
    _no_spawning(monkeypatch)
    env.sandbox.image = False
    result = await env.tool.run(args(), env.ctx)
    assert (result.ok, result.content) == (False, NO_IMAGE)
    assert NO_IMAGE == (
        "The Python sandbox isn't built yet. On the PC, run: hearthkeep sandbox build --data <data>"
    )
    assert env.sandbox.calls == []


async def test_unavailable_during_the_run_is_the_same_message(env):
    from hearthkeep.sandbox.runner import NO_DOCKER_MESSAGE, SandboxUnavailable

    env.sandbox.queue_error(SandboxUnavailable(NO_DOCKER_MESSAGE))
    result = await env.tool.run(args(), env.ctx)
    assert (result.ok, result.content) == (False, NO_DOCKER)


async def test_failed_and_timed_out_runs_are_saved_and_reported(env):
    trace = "Traceback (most recent call last):\nKeyError: 'amount'\n"
    env.sandbox.queue_run({"stdout.txt": trace.encode()}, status="failed", exit_code=1,
                          stdout=trace)  # fmt: skip
    failed = await env.tool.run(args(), env.ctx)
    assert failed.ok is False
    assert failed.content == f"Run failed in 1.5 s.\n\nPrinted output:\n{trace.rstrip()}"
    first = env.runs.get(env.sandbox.calls[0]["run_id"])
    assert first.status == "failed"
    assert failed.data == {"attachments": [{"kind": "run", "id": first.id}]}

    env.sandbox.queue_run(
        {}, status="timeout", exit_code=None, members={}, duration_ms=120000,
        error="The run took longer than 120 seconds and was stopped.",
        diagnostics="docker stderr: diagnostic-only-marker",
    )  # fmt: skip
    timed_out = await env.tool.run(args(), env.ctx)
    assert timed_out.ok is False
    assert timed_out.content == f"Run timeout in 120.0 s.\n\nError:\n{TIMEOUT}"
    assert TIMEOUT == "The script ran for over 120 seconds and was stopped."
    second = env.runs.get(env.sandbox.calls[1]["run_id"])
    assert (second.status, second.error) == ("timeout", TIMEOUT)
    saved_rows = [tuple(r) for r in env.conn.execute("SELECT * FROM runs")]
    assert "diagnostic-only-marker" not in json.dumps(saved_rows)  # diagnostics are never saved


async def test_results_are_read_with_a_size_cap(env, monkeypatch):
    from hearthkeep.sandbox import results as results_module

    monkeypatch.setattr(results_module, "MAX_BYTES", 100)
    line = jsonl({"type": "cleaning", "note": "n" * 40})
    env.sandbox.queue_run({"results.jsonl": line * 50})
    result = await env.tool.run(args(), env.ctx)
    rec = env.runs.get(env.sandbox.calls[0]["run_id"])
    assert len(rec.cleaning) == 1
    assert any("larger than 5 MB" in p for p in rec.problems)
    assert result.ok is True

    from hearthkeep.tools.python import _read_results

    big = env.tmp / "results.jsonl"
    big.write_bytes(line * 50)
    assert len(_read_results(big)) == 102  # never more than the cap (plus the cut marker)


async def test_job_folder_removed_when_saving_fails(env, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.runs, "save", broken)
    env.sandbox.queue_run({"results.jsonl": b""})
    with pytest.raises(RuntimeError):
        await env.tool.run(args(), env.ctx)
    assert not (env.sandbox.root / "jobs" / env.sandbox.calls[0]["run_id"]).exists()


async def test_cancelled_while_reading_results_still_saves_and_cleans_up(env, monkeypatch):
    import threading

    from hearthkeep.tools import python as python_module

    release = threading.Event()
    started = threading.Event()
    real = python_module._read_results

    def slow(path):
        started.set()
        release.wait(5)
        return real(path)

    monkeypatch.setattr(python_module, "_read_results", slow)
    env.sandbox.queue_run({"results.jsonl": jsonl({"type": "cleaning", "note": "kept"})})
    task = asyncio.ensure_future(env.tool.run(args(), env.ctx))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    rec = env.runs.get(env.sandbox.calls[0]["run_id"])
    assert rec is not None and rec.cleaning == ["kept"]
    assert not (env.sandbox.root / "jobs" / rec.id).exists()


def test_effective_level_is_ask_without_docker(env):
    level = get_level(env.conn, "python")
    assert level == ToolLevel.ALLOWED
    assert effective_level("python", level, False) == ToolLevel.ASK
    assert effective_level("python", level, True) == ToolLevel.ALLOWED
    registry = ToolRegistry()
    registry.register(env.tool)
    assert registry.available(env.conn, False) == [env.tool]  # offered, but asks first


def test_python_tool_registered_without_docker(harness):
    client = signed_in(harness, [[TextDelta("Hi."), StreamDone("stop")]])
    chat_id = new_chat(client)
    turn = say(client, chat_id, "hello").json()["turn_id"]
    read_events(client, chat_id, turn)
    tools = {t["function"]["name"] for t in harness.chat.calls[0]["tools"]}
    assert "python" in tools


# ---- the real sandbox -------------------------------------------------------------------------


@pytest.mark.docker
def test_python_tool_end_to_end_real_sandbox(harness, sandbox_image):
    from hearthkeep.sandbox.runner import SandboxRunner

    sandbox = SandboxRunner(harness.data, image_tag=sandbox_image)
    csv = b"date,amount\n2024-03-01,12.50\n2024-03-02,-3.00\n2024-03-03,7.25\n"
    script_for = (
        "import hk\n"
        "df = hk.read({file_id!r})\n"
        "neg = df[df['amount'] < 0]\n"
        "hk.result('negative amounts', len(neg))\n"
        "hk.bar(df, 'date', 'amount', title='Amount by day')\n"
        "hk.table('Negative amounts', neg)\n"
    )
    client = signed_in(harness, sandbox=sandbox)
    chat_id = new_chat(client)
    attached = client.post(
        f"/api/chats/{chat_id}/files", files={"file": ("bank.csv", csv, "text/csv")}, headers=CSRF
    )
    assert attached.status_code == 201, attached.text
    file_id = attached.json()["id"]
    assert attached.json()["status"] == "ready"
    script = script_for.format(file_id=file_id)
    harness.chat.scripts = [
        [ToolCallRequest("c1", "python",
                         json.dumps({"script": script, "purpose": "Find negatives"})),
         StreamDone("tool_calls")],
        [TextDelta("One negative amount."), StreamDone("stop")],
    ]  # fmt: skip
    turn = say(client, chat_id, f"Attached file: {file_id}\nAny negative amounts?").json()
    events = read_events(client, chat_id, turn["turn_id"])

    [row] = harness.query("SELECT id, status, purpose FROM runs")
    run_id, status, purpose = row
    assert (status, purpose) == ("ok", "analysis")
    attachments = [e for e in events if e["event"] == "attachments"]
    assert [a["data"]["items"] for a in attachments] == [[{"kind": "run", "id": run_id}]]

    detail = client.get(f"/api/runs/{run_id}").json()
    assert [(v["name"], v["value"]) for v in detail["values"]] == [("negative amounts", 1)]
    [chart] = detail["charts"]
    assert validate_chart_spec(chart["spec"]) == chart["spec"]
    [table] = detail["tables"]
    assert len(table["rows"]) == 1 and table["rows"][0][1] == -3.0
    assert table["row_numbers"] == [3]
    # The model saw the run, never a host path.
    tool_message = harness.chat.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"].startswith("Run ok in ")
    assert str(harness.data) not in tool_message["content"]
    assert not any((harness.data / "runtime" / "sandbox" / "jobs").glob("*"))
