"""The saved runs and their outputs over HTTP, and runs on the chat's tool messages."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import json

from fakes import FakeSandbox
from test_api import (  # noqa: F401  (harness is a fixture)
    harness,
    new_chat,
    read_events,
    say,
    signed_in,
)

from hearthkeep.providers.chat import StreamDone, TextDelta, ToolCallRequest

SPEC = {
    "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
    "data": {"values": [{"m": "Jan", "v": 2}]},
    "mark": "bar",
    "encoding": {
        "x": {"field": "m", "type": "ordinal"},
        "y": {"field": "v", "type": "quantitative"},
    },
}
SCRIPT = "import hk\nhk.profile('x')\n"
RESULTS = [
    {"type": "section", "name": "glance"},
    {"type": "value", "name": "total", "value": 42.5, "unit": "GBP", "kind": "computed",
     "note": "after cleaning"},
    {"type": "value", "name": "opening", "value": "1,200.00", "kind": "document", "page": 2},
    {"type": "chart", "title": "By month", "spec": SPEC},
    {"type": "table", "name": "Negatives", "columns": ["date", "amount", "memo"],
     "rows": [["2024-03-02", -3, "=HYPERLINK(1)"], ["2024-03-04", -1.5, "café"]],
     "row_numbers": [3, 5], "total_rows": 2, "note": "two rows"},
    {"type": "file", "name": "clean.csv", "member": "files/clean.csv"},
    {"type": "cleaning", "note": "Dropped 1 blank row."},
]  # fmt: skip


def python_turn(text="Done."):
    args = json.dumps({"script": SCRIPT, "purpose": "Profile"})
    return [
        [ToolCallRequest("c1", "python", args), StreamDone("tool_calls")],
        [TextDelta(text), StreamDone("stop")],
    ]


def run_once(harness, tmp_path):
    sandbox = FakeSandbox(tmp_path / "fake")
    results = b"".join(json.dumps(r).encode() + b"\n" for r in RESULTS)
    sandbox.queue_run(
        {"stdout.txt": b"hello\n", "results.jsonl": results, "files/clean.csv": b"a,b\n1,2\n"},
        stdout="hello\n",
    )
    client = signed_in(harness, python_turn(), sandbox=sandbox)
    chat_id = new_chat(client)
    turn_id = say(client, chat_id, "profile it").json()["turn_id"]
    read_events(client, chat_id, turn_id)
    [(run_id,)] = harness.query("SELECT id FROM runs")
    return client, chat_id, turn_id, run_id


def test_run_detail_shape(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    body = response.json()
    outputs = {kind: oid for oid, kind in harness.query("SELECT id, kind FROM outputs")}
    assert body == {
        "id": run_id,
        "chat_id": chat_id,
        "turn_id": turn_id,
        "purpose": "analysis",
        "status": "ok",
        "error": None,
        "duration_ms": 1500,
        "script": SCRIPT,
        "stdout": "hello\n",
        "stdout_truncated": False,
        "values": [
            {"name": "total", "value": 42.5, "unit": "GBP", "note": "after cleaning",
             "is_date": False, "kind": "computed", "page": None},
            {"name": "opening", "value": "1,200.00", "unit": None, "note": None,
             "is_date": False, "kind": "document", "page": 2},
        ],
        "cleaning": ["Dropped 1 blank row."],
        "sections": ["glance"],
        "charts": [{"id": outputs["chart"], "title": "By month", "spec": SPEC}],
        "tables": [{
            "id": outputs["table"], "name": "Negatives", "columns": ["date", "amount", "memo"],
            "rows": [["2024-03-02", -3, "=HYPERLINK(1)"], ["2024-03-04", -1.5, "café"]],
            "row_numbers": [3, 5], "note": "two rows", "total_rows": 2,
        }],
        "files": [{"id": outputs["file"], "name": "clean.csv", "mime": "text/csv", "size": 8}],
        "created_at": body["created_at"],
    }  # fmt: skip

    chart = client.get(f"/api/outputs/{outputs['chart']}").json()
    assert chart == {"id": outputs["chart"], "run_id": run_id, "chat_id": chat_id,
                     "kind": "chart", "name": "By month", "spec": SPEC}  # fmt: skip
    table = client.get(f"/api/outputs/{outputs['table']}").json()
    assert table["kind"] == "table" and table["id"] == outputs["table"]
    assert table["row_numbers"] == [3, 5] and table["total_rows"] == 2
    file = client.get(f"/api/outputs/{outputs['file']}").json()
    assert file == {"id": outputs["file"], "run_id": run_id, "chat_id": chat_id,
                    "kind": "file", "name": "clean.csv", "mime": "text/csv", "size": 8}  # fmt: skip

    download = client.get(f"/api/outputs/{outputs['file']}/download")
    assert download.status_code == 200 and download.content == b"a,b\n1,2\n"
    assert download.headers["content-disposition"].startswith('attachment; filename="clean.csv"')
    assert download.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/api/outputs/{outputs['chart']}/download").status_code == 404


def test_table_download_csv_has_row_numbers(harness, tmp_path):
    client, _, _, _ = run_once(harness, tmp_path)
    [(table_id,)] = harness.query("SELECT id FROM outputs WHERE kind = 'table'")
    response = client.get(f"/api/outputs/{table_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="Negatives.csv"'
    )
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content[3:].decode("utf-8")
    assert text.splitlines() == [
        "row,date,amount,memo",
        "3,2024-03-02,-3,'=HYPERLINK(1)",  # a formula is never live in a spreadsheet
        "5,2024-03-04,-1.5,café",
    ]


def test_outputs_require_session_and_unknown_404(harness, tmp_path):
    client, _, _, run_id = run_once(harness, tmp_path)
    [(output_id,)] = harness.query("SELECT id FROM outputs WHERE kind = 'file'")
    for path in ("/api/runs/nope", "/api/runs/../x", "/api/outputs/nope",
                 "/api/outputs/nope/download", "/api/runs/" + "x" * 40):  # fmt: skip
        assert client.get(path).status_code == 404, path
    client.cookies.clear()
    for path in (f"/api/runs/{run_id}", f"/api/outputs/{output_id}",
                 f"/api/outputs/{output_id}/download"):  # fmt: skip
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"reason": "unauthenticated"}


def test_messages_list_runs_on_tool_message(harness, tmp_path):
    client, chat_id, _, run_id = run_once(harness, tmp_path)
    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    roles = [(m["role"], m["runs"]) for m in messages]
    assert roles == [("user", []), ("assistant", []), ("tool", [run_id]), ("assistant", [])]
    assert all(m["images"] == [] for m in messages)

