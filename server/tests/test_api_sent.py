"""Sending to devices over HTTP: the per-device tray, downloads and background pushes."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import asyncio
import json
import threading
import time

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from test_api import (  # noqa: F401  (harness is a fixture)
    CSRF,
    harness,
    new_chat,
    read_events,
    say,
    signed_in,
)
from test_api_push import add_second_device, subscribe
from test_api_runs import SPEC, run_once

from hearthkeep.providers.chat import StreamDone, TextDelta, ToolCallRequest
from hearthkeep.push import PushSender, VapidKeys

FCM = "https://fcm.googleapis.com/fcm/send/device-one:APA91b-secret"
NO_STORE = "private, no-store"


def post_sent(client, **body):
    return client.post("/api/sent", json=body, headers=CSRF)


def outputs(harness):
    return {kind: oid for oid, kind in harness.query("SELECT id, kind FROM outputs")}


def wait_until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def reply_turn(text):
    return [[TextDelta(text), StreamDone("stop")]]


def chat_with_reply(harness, text, **start):
    client = signed_in(harness, reply_turn(text), **start)
    chat_id = new_chat(client)
    turn_id = say(client, chat_id, "how many?").json()["turn_id"]
    read_events(client, chat_id, turn_id)
    [(message_id,)] = harness.query(
        "SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant'", (chat_id,)
    )
    return client, chat_id, turn_id, message_id


def test_sent_requires_session(harness):
    client = harness.start()
    assert client.get("/api/sent").status_code == 401
    assert client.get("/api/sent/count").status_code == 401
    assert post_sent(client, kind="summary", ref_id="1", devices="all").status_code == 401
    assert client.post("/api/sent/abc/dismiss", headers=CSRF).status_code == 401
    assert client.get("/api/sent/abc/download").status_code == 401
    assert harness.query("SELECT * FROM sent_items") == []


def test_sent_changes_need_csrf(harness):
    client = signed_in(harness)
    response = client.post("/api/sent", json={"kind": "summary", "ref_id": "1", "devices": "all"})
    assert response.status_code == 403
    assert client.post("/api/sent/abc/dismiss").status_code == 403


def test_web_summary_text_comes_from_server(harness):
    client, chat_id, turn_id, message_id = chat_with_reply(harness, "There are 5 rows.")
    # Text from the client is refused outright, never used.
    smuggled = post_sent(client, kind="summary", ref_id=str(message_id), devices="all",
                         text="Wire money to me")  # fmt: skip
    assert smuggled.status_code == 422
    response = post_sent(client, kind="summary", ref_id=str(message_id), devices="all")
    assert response.status_code == 201, response.text
    item = response.json()
    assert (item["kind"], item["title"]) == ("summary", "Summary")
    assert item["text"] == "There are 5 rows."
    assert item["marks"] is None and item["chart"] is None and item["dismissed"] is False

    # When the reply was number-checked, the checked text is what is sent.
    c = harness.conn()
    c.execute(
        "INSERT INTO number_checks (message_id, chat_id, turn_id, text, marks, created_at)"
        " VALUES (?, ?, ?, 'Checked: 5 rows in all.', '[]', 'now')",
        (message_id, chat_id, turn_id),
    )
    c.close()
    checked = post_sent(client, kind="summary", ref_id=str(message_id), devices="all").json()
    assert checked["text"] == "Checked: 5 rows in all."

    [(user_id,)] = harness.query("SELECT id FROM messages WHERE role = 'user'")
    for ref, status in ((str(user_id), 404), ("999999", 404), ("abc", 404), ("1e3", 404),
                        ("", 422)):  # fmt: skip
        refused = post_sent(client, kind="summary", ref_id=ref, devices="all")
        assert refused.status_code == status, ref
    assert len(harness.query("SELECT id FROM sent_items")) == 2


def add_chat_file(harness, chat_id, file_id="f1", name="a.csv"):
    c = harness.conn()
    c.execute(
        "INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source, file_name,"
        " created_at) VALUES (?, ?, ?, 'csv', 'text/csv', 1, 'ready', 'upload', ?, 'now')",
        (file_id, chat_id, name, f"{file_id}.csv"),
    )
    c.close()


def test_web_summary_reuses_the_stored_check(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)  # a run that reported 42.5
    [(message_id,)] = harness.query(
        "SELECT id FROM messages WHERE role = 'assistant' AND content = 'Done.'"
    )
    add_chat_file(harness, chat_id)
    stored = [{"start": 6, "end": 10, "text": "42.5", "kind": "computed", "page": None,
               "note": None}]  # fmt: skip
    c = harness.conn()
    c.execute(
        "INSERT INTO number_checks (message_id, chat_id, turn_id, text, marks, created_at)"
        " VALUES (?, ?, ?, 'Total 42.5, 7 more.', ?, 'now')",
        (message_id, chat_id, turn_id, json.dumps(stored)),
    )
    c.close()
    item = post_sent(client, kind="summary", ref_id=str(message_id), devices="all").json()
    assert (item["text"], item["marks"]) == ("Total 42.5, 7 more.", stored)  # as stored


def test_web_summary_without_a_check_is_checked_without_a_turn(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)  # a run that reported 42.5
    add_chat_file(harness, chat_id)
    c = harness.conn()
    message_id = c.execute(
        "INSERT INTO messages (chat_id, role, content, created_at)"
        " VALUES (?, 'assistant', 'Total 42.5.', 'now')",
        (chat_id,),
    ).lastrowid
    c.close()
    item = post_sent(client, kind="summary", ref_id=str(message_id), devices="all").json()
    # No stored check, so no turn: the run's results are no evidence.
    assert [(m["text"], m["kind"]) for m in item["marks"]] == [("42.5", "unverified")]


def test_step_limit_message_stays_unmarked_in_a_sent_summary(harness):
    rounds = [[TextDelta("Found 5 rows."), ToolCallRequest("c0", "clock", "{}"),
               StreamDone("tool_calls")]]  # fmt: skip
    rounds += [[ToolCallRequest(f"c{i}", "clock", "{}"), StreamDone("tool_calls")]
               for i in range(1, 12)]  # fmt: skip
    client = signed_in(harness, rounds)
    chat_id = new_chat(client)
    add_chat_file(harness, chat_id)  # a chat with files: its replies are number-checked
    turn_id = say(client, chat_id, "go").json()["turn_id"]
    read_events(client, chat_id, turn_id)
    [(message_id, text, marks)] = harness.query(
        "SELECT message_id, text, marks FROM number_checks WHERE chat_id = ?", (chat_id,)
    )
    assert "step limit" in text and "(8 tool steps)" in text
    item = post_sent(client, kind="summary", ref_id=str(message_id), devices="all").json()
    assert item["text"] == text
    assert item["marks"] == json.loads(marks)
    assert [m["text"] for m in item["marks"]] == ["5"]  # never the 8 of Hearthkeep's own words


def test_tray_lists_this_devices_items_with_chart_spec(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)
    ids = outputs(harness)
    chart = post_sent(client, kind="chart", ref_id=ids["chart"], devices="all")
    assert chart.status_code == 201, chart.text
    table = post_sent(client, kind="table", ref_id=ids["table"], devices=["cred-1"]).json()
    body = client.get("/api/sent").json()
    assert body["unread"] == 2
    assert [i["id"] for i in body["items"]] == [table["id"], chart.json()["id"]]
    first = body["items"][1]
    assert first == {
        "id": chart.json()["id"],
        "kind": "chart",
        "title": "By month",
        "text": None,
        "marks": None,
        "chart": SPEC,
        "created_at": first["created_at"],
        "dismissed": False,
    }
    assert body["items"][0]["chart"] is None
    assert client.get("/api/sent/count").json() == {"unread": 2}

    dismissed = client.post(f"/api/sent/{table['id']}/dismiss", headers=CSRF)
    assert dismissed.status_code == 200
    assert client.get("/api/sent/count").json() == {"unread": 1}
    assert [(i["id"], i["dismissed"]) for i in client.get("/api/sent").json()["items"]] == [
        (chart.json()["id"], False),
        (table["id"], True),
    ]


def test_send_refusals(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)
    ids = outputs(harness)
    assert post_sent(client, kind="table", ref_id=ids["chart"], devices="all").status_code == 404
    unknown = post_sent(client, kind="chart", ref_id=ids["chart"], devices=["cred-9"])
    assert (unknown.status_code, unknown.json()["reason"]) == (422, "unknown_device")
    for kind, ref in (("chart", ids["table"]), ("file", ids["chart"]), ("chart", "missing"),
                      ("table", ids["file"])):  # fmt: skip
        refused = post_sent(client, kind=kind, ref_id=ref, devices="all")
        assert (refused.status_code, refused.json()["reason"]) == (404, "not_found"), kind
    add_chat_file(harness, chat_id)
    as_table = post_sent(client, kind="table", ref_id="f1", devices="all")  # a file, as a table
    assert (as_table.status_code, as_table.json()["reason"]) == (404, "not_found")
    other = new_chat(client)
    add_chat_file(harness, other, file_id="f2")
    assert post_sent(client, kind="file", ref_id="f2", devices="all").status_code == 201
    assert post_sent(client, kind="photo", ref_id=ids["chart"], devices="all").status_code == 422
    assert post_sent(client, kind="chart", ref_id="x" * 40, devices="all").status_code == 422
    assert post_sent(client, kind="chart", ref_id=ids["chart"], devices="some").status_code == 422
    assert len(harness.query("SELECT * FROM sent_items")) == 1


def test_download_after_the_thing_was_deleted_is_404(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)
    ids = outputs(harness)
    add_chat_file(harness, chat_id)
    folder = tmp_path / "workspace" / "chats" / chat_id / "files"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "f1.csv").write_bytes(b"x,y\n")
    sent = {
        ref: post_sent(client, kind=kind, ref_id=ref, devices="all").json()["id"]
        for kind, ref in (("table", ids["table"]), ("file", ids["file"]), ("file", "f1"))
    }
    for item_id in sent.values():
        assert client.get(f"/api/sent/{item_id}/download").status_code == 200
    c = harness.conn()
    c.execute("DELETE FROM outputs WHERE id = ?", (ids["table"],))
    c.execute("DELETE FROM chat_files WHERE id = 'f1'")
    c.close()
    for path in (tmp_path / "workspace" / "chats" / chat_id / "runs").rglob("*.csv"):
        path.unlink()  # the run's output file is gone from disk, its row stays
    for item_id in sent.values():
        got = client.get(f"/api/sent/{item_id}/download")
        assert (got.status_code, got.json()) == (404, {"reason": "not_found"})


def test_download_table_csv_and_file(harness, tmp_path):
    client, chat_id, turn_id, run_id = run_once(harness, tmp_path)
    ids = outputs(harness)
    sent = {
        kind: post_sent(client, kind=kind, ref_id=ids[kind], devices="all").json()["id"]
        for kind in ("chart", "table", "file")
    }
    table = client.get(f"/api/sent/{sent['table']}/download")
    assert table.status_code == 200
    assert table.headers["content-type"] == "text/csv; charset=utf-8"
    assert table.headers["content-disposition"].startswith('attachment; filename="Negatives.csv"')
    assert table.headers["cache-control"] == NO_STORE
    lines = table.content.decode("utf-8-sig").splitlines()
    assert lines[0] == "row,date,amount,memo"
    assert lines[1] == "3,2024-03-02,-3,'=HYPERLINK(1)"  # never a live formula

    out_file = client.get(f"/api/sent/{sent['file']}/download")
    assert out_file.status_code == 200
    assert out_file.content == b"a,b\n1,2\n"
    assert out_file.headers["content-disposition"].startswith('attachment; filename="clean.csv"')
    assert out_file.headers["cache-control"] == NO_STORE

    # A chat file sent as a file: its own bytes, its original name kept safe in the header.
    folder = tmp_path / "workspace" / "chats" / chat_id / "files"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "f1.csv").write_bytes(b"x,y\n")
    c = harness.conn()
    c.execute(
        "INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source, file_name,"
        " created_at) VALUES ('f1', ?, 'relevé \"mai\".csv', 'csv', 'text/csv', 4, 'ready',"
        " 'upload', 'f1.csv', 'now')",
        (chat_id,),
    )
    c.close()
    chat_file = post_sent(client, kind="file", ref_id="f1", devices="all").json()
    got = client.get(f"/api/sent/{chat_file['id']}/download")
    assert got.status_code == 200 and got.content == b"x,y\n"
    assert got.headers["content-disposition"] == (
        "attachment; filename=\"releve mai.csv\"; filename*=UTF-8''relev%C3%A9%20%22mai%22.csv"
    )
    assert got.headers["cache-control"] == NO_STORE

    # Charts are saved as PNG on the device: no download here.
    assert client.get(f"/api/sent/{sent['chart']}/download").status_code == 404

    events = [e for e in harness.events() if e["event"] == "sent.created"]
    assert {e["detail"]["kind"] for e in events} == {"chart", "table", "file"}
    for e in events:
        assert set(e["detail"]) == {"id", "kind", "targets"}
        assert e["detail"]["targets"] == ["cred-1"]
    everything = json.dumps(harness.events())
    assert "Negatives" not in everything and "clean.csv" not in everything
    assert "relev" not in everything and "By month" not in everything


def test_download_summary_text(harness):
    client, chat_id, turn_id, message_id = chat_with_reply(harness, "All 5 rows are fine.")
    item = post_sent(client, kind="summary", ref_id=str(message_id), devices="all").json()
    got = client.get(f"/api/sent/{item['id']}/download")
    assert got.status_code == 200
    assert got.text == "All 5 rows are fine."
    assert got.headers["content-type"] == "text/plain; charset=utf-8"
    assert got.headers["content-disposition"].startswith('attachment; filename="Summary.txt"')
    assert got.headers["cache-control"] == NO_STORE


def test_second_device_sees_only_its_own_items(harness):
    client, chat_id, turn_id, message_id = chat_with_reply(harness, "Five rows.")
    mine = post_sent(client, kind="summary", ref_id=str(message_id), devices=["cred-1"]).json()
    add_second_device(harness, client)  # now signed in as cred-2
    assert client.get("/api/sent").json() == {"items": [], "unread": 0}
    assert client.get(f"/api/sent/{mine['id']}/download").status_code == 404
    assert client.post(f"/api/sent/{mine['id']}/dismiss", headers=CSRF).status_code == 404
    both = post_sent(client, kind="summary", ref_id=str(message_id), devices="all").json()
    assert [i["id"] for i in client.get("/api/sent").json()["items"]] == [both["id"]]
    assert harness.query(
        "SELECT credential_id FROM sent_targets WHERE item_id = ? ORDER BY credential_id",
        (both["id"],),
    ) == [("cred-1",), ("cred-2",)]


class BlockingSender:
    """A push service that never answers until released; records endpoints only."""

    public_key = "test-key"

    def __init__(self):
        self.calls: list[str] = []
        self.release = threading.Event()
        self.cancelled = False
        self.closed = False

    async def notify(self, endpoint):
        self.calls.append(endpoint)
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "sent"

    async def aclose(self):
        self.closed = True


def test_post_never_waits_for_the_push_and_shutdown_ends_it(harness):
    sender = BlockingSender()
    client, chat_id, turn_id, message_id = chat_with_reply(harness, "Done.", push_sender=sender)
    assert subscribe(client, FCM).status_code == 200
    started = time.monotonic()
    response = post_sent(client, kind="summary", ref_id=str(message_id), devices="all")
    assert response.status_code == 201
    assert time.monotonic() - started < 2.0
    wait_until(lambda: sender.calls == [FCM])  # the push is running, and still blocked
    harness.stop(client)
    assert sender.cancelled and sender.closed


def test_push_sends_nothing_but_the_endpoint_and_vapid_headers(harness):
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(410)  # the device unsubscribed

    sender = PushSender(
        VapidKeys(ec.generate_private_key(ec.SECP256R1())),
        transport=httpx.MockTransport(handler),
    )
    client, chat_id, turn_id, message_id = chat_with_reply(
        harness, "Secret summary 42.", push_sender=sender
    )
    subscribe(client, FCM)
    post_sent(client, kind="summary", ref_id=str(message_id), devices="all")
    wait_until(lambda: harness.query("SELECT * FROM push_subscriptions") == [])  # gone: removed
    [request] = requests
    assert str(request.url) == FCM
    assert request.content == b""
    assert set(request.headers) == {
        "host", "accept", "accept-encoding", "connection", "user-agent",
        "ttl", "urgency", "authorization", "content-length",
    }  # fmt: skip
    assert "Secret" not in json.dumps(dict(request.headers))
    wait_until(lambda: any(e["event"] == "push.sent" for e in harness.events()))
    pushes = [e["detail"] for e in harness.events() if e["event"] == "push.sent"]
    assert pushes == [{"status": "gone"}]
