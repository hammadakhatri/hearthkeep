"""The send_to_devices tool: Look, allowed by default, devices by name."""

import pytest
from test_sent import CHAT, FCM_1, SPEC, TOTAL_5, Env, FakeSender

from hearthkeep import audit
from hearthkeep.config import Settings
from hearthkeep.permissions import ActionClass, Decision, decide, get_level
from hearthkeep.tools.base import ToolContext
from hearthkeep.tools.send import SendToDevicesTool


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, FakeSender())
    e.device("cred-1", "Laptop")
    e.device("cred-2", "Phone")
    e.tool = SendToDevicesTool(e.sent, e.credentials)
    e.ctx = ToolContext(chat_id=CHAT, turn_id="t1", settings=Settings(data_dir=tmp_path),
                        conn=e.conn)  # fmt: skip
    yield e
    e.conn.close()


async def test_send_tool_is_look_and_runs_without_approval(env):
    tool = env.tool
    assert (tool.name, tool.level_key) == ("send_to_devices", "send")
    chart = env.output("chart", "By month", data=SPEC)
    args = {"kind": "chart", "id": chart}
    assert tool.classify(args) == ActionClass.LOOK
    assert decide(get_level(env.conn, "send"), tool.classify(args), False) == Decision.RUN
    assert tool.summarize(args) == "Send chart to all"
    assert tool.summarize({"kind": "table", "id": "x", "device": "Phone"}) == "Send table to Phone"

    env.subscriptions.set("cred-1", FCM_1)
    result = await tool.run(args, env.ctx)
    assert result.ok and result.content == "Sent to 2 device(s)."
    assert [i.title for i in env.sent.inbox("cred-2")] == ["By month"]
    assert env.sent.pending == 1  # the push runs in the background
    await env.sent.aclose()
    assert env.sender.calls == [FCM_1]
    created = [e for e in audit.list_events(env.conn) if e["event"] == "sent.created"]
    assert created[0]["actor"] == "agent"


def test_parameters_schema(env):
    props = env.tool.parameters["properties"]
    assert props["kind"]["enum"] == ["chart", "table", "file", "summary"]
    assert props["id"]["pattern"] == "^[A-Za-z0-9_-]{1,32}$"
    assert props["text"]["maxLength"] == 4000
    assert props["device"] == {"type": "string", "maxLength": 64, "default": "all",
                               "description": props["device"]["description"]}  # fmt: skip
    assert env.tool.parameters["required"] == ["kind"]


async def test_device_names_match_case_insensitively(env):
    result = await env.tool.run({"kind": "summary", "text": "Done.", "device": "  pHONE "},
                                env.ctx)  # fmt: skip
    assert result.ok and result.content == "Sent to 1 device(s)."
    assert env.sent.inbox("cred-1") == []
    assert [i.text for i in env.sent.inbox("cred-2")] == ["Done."]
    every = await env.tool.run({"kind": "summary", "text": "x", "device": "ALL"}, env.ctx)
    assert every.content == "Sent to 2 device(s)."


async def test_a_name_shared_by_several_devices_sends_to_each(env):
    env.device("cred-3", "phone")
    result = await env.tool.run({"kind": "summary", "text": "x", "device": "Phone"}, env.ctx)
    assert result.ok and result.content == 'Sent to 2 devices named "Phone".'
    assert env.sent.inbox("cred-1") == []
    assert len(env.sent.inbox("cred-2")) == len(env.sent.inbox("cred-3")) == 1


async def test_a_device_named_all(env):
    env.device("cred-3", "All")
    # Written exactly as the device's name: that device only.
    named = await env.tool.run({"kind": "summary", "text": "x", "device": "All"}, env.ctx)
    assert named.content == "Sent to 1 device(s)."
    assert [len(env.sent.inbox(c)) for c in ("cred-1", "cred-2", "cred-3")] == [0, 0, 1]
    # Any other spelling of all means every device.
    every = await env.tool.run({"kind": "summary", "text": "x", "device": "all"}, env.ctx)
    assert every.content == "Sent to 3 device(s)."
    env.device("cred-4", "all")  # two devices now answer to "All" in some case
    both = await env.tool.run({"kind": "summary", "text": "x", "device": "All"}, env.ctx)
    assert both.content == "Sent to 4 device(s)."  # ambiguous: every device
    assert "All" in env.tool.description and "every device" in env.tool.description


async def test_at_most_five_sends_per_turn(env):
    for _ in range(5):
        ok = await env.tool.run({"kind": "summary", "text": "x"}, env.ctx)
        assert ok.ok
    refused = await env.tool.run({"kind": "summary", "text": "x"}, env.ctx)
    assert (refused.ok, refused.content) == (False, "You've already sent 5 items this turn.")
    assert env.conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0] == 5
    # A refused send doesn't count; another turn has its own five.
    env.ctx.turn_id = "t2"
    assert (await env.tool.run({"kind": "summary"}, env.ctx)).ok is False
    for _ in range(5):
        assert (await env.tool.run({"kind": "summary", "text": "x"}, env.ctx)).ok


async def test_unknown_device_name_lists_names(env):
    env.device("cred-3", "Old tablet")
    env.revoke("cred-3")
    result = await env.tool.run({"kind": "summary", "text": "x", "device": "Tablet"}, env.ctx)
    assert not result.ok
    assert result.content == 'No device is called "Tablet". The devices are: Laptop, Phone.'
    assert env.conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0] == 0


async def test_summary_is_checked_with_this_turn(env):
    env.chat_file("sales.csv")
    env.run(turn_id="t1", values=[TOTAL_5])
    await env.tool.run({"kind": "summary", "text": "5 rows, 6 columns"}, env.ctx)
    [item] = env.sent.inbox("cred-1")
    assert [(m["text"], m["kind"]) for m in item.marks] == [("5", "computed"), ("6", "unverified")]


async def test_bad_requests_are_errors(env):
    other = env.output("chart", "Theirs", chat_id="b" * 32, data=SPEC)
    cases = [
        ({"kind": "chart", "id": other}, "There is no chart with that id in this chat."),
        ({"kind": "chart"}, "There is no chart with that id in this chat."),
        ({"kind": "summary"}, "A summary needs its text."),
        ({"kind": "summary", "text": "x" * 4001}, "A summary can be at most 4000 characters."),
        ({"kind": "picture", "id": other}, "kind must be chart, table, file or summary."),
        ({"kind": "summary", "text": "x", "device": 5}, "device must be a device name or all."),
    ]
    for args, message in cases:
        result = await env.tool.run(args, env.ctx)
        assert (result.ok, result.content) == (False, message)
    assert env.conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0] == 0
