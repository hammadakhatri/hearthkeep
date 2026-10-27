"""Sent items: targets, the per-device tray, summaries' number check and payload-free pushes."""

import asyncio
import json
import logging

import pytest

from hearthkeep import audit
from hearthkeep.auth.credentials import CredentialStore
from hearthkeep.auth.webauthn import NewCredential
from hearthkeep.db import connect
from hearthkeep.files.store import ChatFileStore
from hearthkeep.numbercheck import UNVERIFIED_NOTE, NumberChecker
from hearthkeep.push import PushSubscriptions
from hearthkeep.sandbox.runs import RunStore
from hearthkeep.sent import MAX_SUMMARY_CHARS, SendRefused, SentStore

CHAT = "a" * 32
OTHER = "b" * 32
FCM_1 = "https://fcm.googleapis.com/fcm/send/one-secret"
FCM_2 = "https://fcm.googleapis.com/fcm/send/two-secret"
FCM_3 = "https://fcm.googleapis.com/fcm/send/three-secret"
TOTAL_5 = {"name": "total", "value": 5, "unit": None, "note": None, "is_date": False,
           "kind": "computed", "page": None}  # fmt: skip
SPEC = {"mark": "bar", "data": {"values": [{"m": "Jan", "v": 2}]}}


class FakeSender:
    """Takes nothing but an endpoint: anything else passed to a push fails the call."""

    def __init__(self, outcomes=None):
        self.calls: list[str] = []
        self.outcomes = outcomes or {}

    async def notify(self, endpoint):
        self.calls.append(endpoint)
        return self.outcomes.get(endpoint, "sent")


class Env:
    def __init__(self, tmp_path, sender):
        self.data = tmp_path
        self.conn = connect(tmp_path / "hk.db")
        self.runs = RunStore(self.conn, tmp_path)
        self.files = ChatFileStore(self.conn, tmp_path)
        self.credentials = CredentialStore(self.conn)
        self.subscriptions = PushSubscriptions(self.conn)
        self.sender = sender
        self.checker = NumberChecker(self.conn, self.runs, self.files)
        self.sent = SentStore(
            self.conn, self.runs, self.files, self.credentials, self.checker,
            sender, self.subscriptions,
        )  # fmt: skip
        self._n = 0

    def device(self, cred_id, name="laptop"):
        self.credentials.add_in(self.conn, NewCredential(cred_id, b"pk", 0), name)

    def revoke(self, cred_id):
        self.conn.execute("UPDATE credentials SET revoked_at = 'now' WHERE id = ?", (cred_id,))

    def run(self, chat_id=CHAT, turn_id="t1", values=()):
        self._n += 1
        run_id = f"run{self._n}"
        results = {"values": list(values), "cleaning": [], "sections": []}
        self.conn.execute(
            "INSERT INTO runs (id, chat_id, turn_id, purpose, script, stdout, stdout_truncated,"
            " status, error, duration_ms, results, problems, created_at)"
            " VALUES (?, ?, ?, 'analysis', 'x', '', 0, 'ok', NULL, 1, ?, '[]', ?)",
            (run_id, chat_id, turn_id, json.dumps(results), f"2026-01-01T00:00:{self._n:02d}"),
        )
        return run_id

    def output(self, kind, name, chat_id=CHAT, data=None, body=None):
        run_id = self.run(chat_id)
        self._n += 1
        out_id = f"out{self._n}"
        file_name = f"{out_id}.csv" if kind == "file" else None
        self.conn.execute(
            "INSERT INTO outputs (id, run_id, chat_id, kind, name, mime, size, data, file_name,"
            " created_at) VALUES (?, ?, ?, ?, ?, 'text/csv', 1, ?, ?, '2026-01-01')",
            (out_id, run_id, chat_id, kind, name, json.dumps(data) if data else None, file_name),
        )
        if file_name:
            folder = self.data / "workspace" / "chats" / chat_id / "runs" / run_id / "files"
            folder.mkdir(parents=True)
            (folder / file_name).write_bytes(body or b"a,b\n")
        return out_id

    def chat_file(self, name, chat_id=CHAT):
        self._n += 1
        file_id = f"file{self._n}"
        self.conn.execute(
            "INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source,"
            " file_name, created_at) VALUES (?, ?, ?, 'csv', 'text/csv', 1, 'ready', 'upload',"
            " ?, ?)",
            (file_id, chat_id, name, f"{file_id}.csv", f"2026-01-01T00:00:{self._n:02d}"),
        )
        return file_id

    def send(self, **fields):
        args = {"chat_id": CHAT, "ref_id": None, "text": None, "devices": "all", **fields}
        return self.sent.send(**args)

    def events(self, prefix):
        found = audit.list_events(self.conn, limit=1000)
        return [(e["event"], e["detail"]) for e in reversed(found) if e["event"].startswith(prefix)]

    def targets(self, item_id):
        rows = self.conn.execute(
            "SELECT credential_id FROM sent_targets WHERE item_id = ? ORDER BY credential_id",
            (item_id,),
        )
        return [r[0] for r in rows]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, FakeSender())
    e.device("cred-1", "Laptop")
    e.device("cred-2", "Phone")
    yield e
    e.conn.close()


def test_send_to_all_targets_every_device(env):
    env.device("cred-3", "Old tablet")
    env.revoke("cred-3")
    chart = env.output("chart", "Spending by month", data=SPEC)
    item = env.send(kind="chart", ref_id=chart)
    assert (item.kind, item.ref_id, item.title, item.text, item.marks) == (
        "chart", chart, "Spending by month", None, None,
    )  # fmt: skip
    assert env.targets(item.id) == ["cred-1", "cred-2"]  # never a revoked device
    assert env.events("sent.") == [
        ("sent.created", {"id": item.id, "kind": "chart", "targets": ["cred-1", "cred-2"]})
    ]
    assert "Spending" not in json.dumps(audit.list_events(env.conn, limit=1000))


def test_send_to_named_device_only(env):
    table = env.output("table", "Negatives", data={"columns": ["a"], "rows": [[1]]})
    item = env.send(kind="table", ref_id=table, devices=["cred-2"])
    assert item.title == "Negatives"
    assert env.targets(item.id) == ["cred-2"]
    assert [i.id for i in env.sent.inbox("cred-2")] == [item.id]
    assert env.sent.inbox("cred-1") == []

    env.device("cred-3")
    env.revoke("cred-3")
    for devices in (["nope"], ["cred-2", "nope"], ["cred-3"], [], "some"):
        with pytest.raises(ValueError):
            env.send(kind="table", ref_id=table, devices=devices)
    assert env.conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0] == 1


def test_ref_must_belong_to_chat(env):
    chart = env.output("chart", "C", data=SPEC)
    table = env.output("table", "T", data={"columns": [], "rows": []})
    out_file = env.output("file", "clean.csv")
    chat_file = env.chat_file("bank statement.csv")
    elsewhere = env.output("chart", "Other chat", chat_id=OTHER, data=SPEC)
    other_file = env.chat_file("theirs.csv", chat_id=OTHER)

    assert env.send(kind="file", ref_id=out_file).title == "clean.csv"
    assert env.send(kind="file", ref_id=chat_file).title == "bank statement.csv"
    for kind, ref in (
        ("chart", table),  # a table is not a chart
        ("table", chart),
        ("file", chart),
        ("chart", chat_file),  # a chat file can only be sent as a file
        ("chart", elsewhere),  # another chat's output
        ("file", other_file),  # another chat's file
        ("chart", "missing"),
        ("chart", None),
        ("chart", "../../x"),
        ("picture", chart),
    ):
        with pytest.raises(SendRefused) as refused:
            env.send(kind=kind, ref_id=ref)
        assert refused.value.reason == "not_found"
    assert env.conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0] == 2


def test_summary_is_number_checked(env):
    env.chat_file("sales.csv")
    env.run(turn_id="t1", values=[TOTAL_5])
    item = env.send(kind="summary", text="📈 5 rows and 6 columns", turn_id="t1")
    assert (item.title, item.text) == ("Summary", "📈 5 rows and 6 columns")
    assert item.marks == [
        {"start": 3, "end": 4, "text": "5", "kind": "computed", "page": None, "note": None},
        {"start": 14, "end": 15, "text": "6", "kind": "unverified", "page": None,
         "note": UNVERIFIED_NOTE},
    ]  # fmt: skip  (UTF-16 offsets: the emoji is two units)
    stored = env.sent.item_for(item.id, "cred-1")
    assert stored.marks == item.marks and stored.text == item.text

    # Another turn's runs are not evidence; nor is anything without a turn.
    for turn_id in ("t2", None):
        other = env.send(kind="summary", text="5 rows", turn_id=turn_id)
        assert [m["kind"] for m in other.marks] == ["unverified"]
    # A number the owner asked about is exempt, as it was for the reply.
    asked = env.send(kind="summary", text="6 rows", turn_id="t1", question="any of the 6?")
    assert asked.marks == []


def test_summary_text_rules(env):
    for text, reason in ((None, "no_text"), ("  ", "no_text"),
                         ("x" * (MAX_SUMMARY_CHARS + 1), "too_long")):  # fmt: skip
        with pytest.raises(SendRefused) as refused:
            env.send(kind="summary", text=text)
        assert refused.value.reason == reason
    plain = env.send(kind="summary", text="x" * MAX_SUMMARY_CHARS)
    assert plain.marks is None  # a chat without files has no number check
    chart = env.output("chart", "C", data=SPEC)
    assert env.send(kind="chart", ref_id=chart, text="ignored").text is None


def test_inbox_is_per_device_and_dismiss_hides(env):
    first = env.send(kind="summary", text="first")
    second = env.send(kind="summary", text="second", devices=["cred-1"])
    third = env.send(kind="summary", text="third")
    assert [i.id for i in env.sent.inbox("cred-1")] == [third.id, second.id, first.id]
    assert [i.id for i in env.sent.inbox("cred-2")] == [third.id, first.id]
    assert env.sent.unread_count("cred-1") == 3
    assert env.sent.unread_count("cred-2") == 2

    assert env.sent.dismiss(third.id, "cred-1") is True
    assert env.sent.dismiss(third.id, "cred-1") is True  # again: still this device's
    inbox = env.sent.inbox("cred-1")
    assert [(i.id, i.dismissed) for i in inbox] == [
        (second.id, False), (first.id, False), (third.id, True),
    ]  # fmt: skip
    assert env.sent.unread_count("cred-1") == 2
    assert env.sent.unread_count("cred-2") == 2  # the other device still has it
    assert [i.id for i in env.sent.inbox("cred-1", limit=1)] == [second.id]


def test_other_device_cannot_fetch_item(env):
    item = env.send(kind="summary", text="for the laptop", devices=["cred-1"])
    assert env.sent.item_for(item.id, "cred-1").text == "for the laptop"
    assert env.sent.item_for(item.id, "cred-2") is None
    assert env.sent.dismiss(item.id, "cred-2") is False
    assert env.sent.item_for(item.id, "nobody") is None
    assert env.sent.item_for("bad id!", "cred-1") is None

    env.revoke("cred-1")  # a removed device sees nothing any more
    assert env.sent.item_for(item.id, "cred-1") is None
    assert env.sent.dismiss(item.id, "cred-1") is False
    assert env.sent.inbox("cred-1") == []
    assert env.sent.unread_count("cred-1") == 0


async def test_push_notifies_subscribed_targets_only_by_endpoint(env, caplog):
    env.device("cred-3", "Desk")
    env.subscriptions.set("cred-1", FCM_1)
    env.subscriptions.set("cred-3", FCM_3)  # subscribed, but not a target
    out_file = env.output("file", "secret-name.csv")
    item = env.send(kind="file", ref_id=out_file, devices=["cred-1", "cred-2"])
    with caplog.at_level(logging.DEBUG):
        await env.sent.notify(item)
    assert env.sender.calls == [FCM_1]  # cred-2 has no subscription; cred-3 isn't a target
    assert env.events("push.") == [("push.sent", {"status": "sent"})]
    assert "secret-name" not in caplog.text

    env.revoke("cred-1")  # a device removed after the send gets nothing
    await env.sent.notify(item)
    assert env.sender.calls == [FCM_1]


async def test_gone_subscription_removed(env):
    env.sender.outcomes = {FCM_1: "gone", FCM_2: "failed"}
    env.subscriptions.set("cred-1", FCM_1)
    env.subscriptions.set("cred-2", FCM_2)
    item = env.send(kind="summary", text="hello")
    await env.sent.notify(item)
    assert env.subscriptions.endpoint_for("cred-1") is None
    assert env.subscriptions.endpoint_for("cred-2") == FCM_2  # a failure keeps it
    assert env.events("push.") == [
        ("push.sent", {"status": "gone"}),
        ("push.sent", {"status": "failed"}),
    ]


async def test_push_off_sends_nothing(tmp_path):
    e = Env(tmp_path, None)
    e.device("cred-1")
    item = e.send(kind="summary", text="hello")
    await e.sent.notify(item)
    e.sent.notify_later(item)
    assert e.sent.pending == 0
    await e.sent.aclose()
    e.conn.close()


async def test_background_pushes_are_tracked_and_ended_at_shutdown(env):
    started = asyncio.Event()

    class Hanging(FakeSender):
        cancelled = False

        async def notify(self, endpoint):
            self.calls.append(endpoint)
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                Hanging.cancelled = True
                raise

    env.subscriptions.set("cred-1", FCM_1)
    item = env.send(kind="summary", text="hello", devices=["cred-1"])

    env.sent.notify_later(item)  # quick push: finishes within the grace time
    await env.sent.aclose(grace=1.0)
    assert env.sender.calls == [FCM_1] and env.sent.pending == 0

    hanging = Hanging()
    env.sent._push = hanging
    env.sent.notify_later(item)
    await asyncio.wait_for(started.wait(), 1.0)
    assert env.sent.pending == 1
    await asyncio.wait_for(env.sent.aclose(grace=0.05), 1.0)
    assert Hanging.cancelled is True
    assert env.sent.pending == 0
