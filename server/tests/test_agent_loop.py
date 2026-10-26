import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeChatClient

from hearthkeep import audit
from hearthkeep.agent.loop import SYSTEM_PROMPT, AgentLoop, load_history
from hearthkeep.approvals import ApprovalStore
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.permissions import ActionClass, ToolLevel, set_level
from hearthkeep.providers.chat import (
    ProviderError,
    ReasoningDelta,
    StreamDone,
    TextDelta,
    ToolCallRequest,
)
from hearthkeep.secrets import SecretStore
from hearthkeep.tools.base import ToolRegistry, ToolResult
from hearthkeep.tools.demo import ClockTool, NoteTool

CHAT = "chat1"
SECRET_VALUE = "hk-very-secret-value-123"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


class OutwardTool:
    name = "send_postcard"
    level_key = "postcards"
    description = "Send a postcard."
    parameters = {
        "type": "object",
        "properties": {"to": {"type": "string"}},
        "required": ["to"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.ran = 0

    def classify(self, args):
        return ActionClass.OUTWARD

    def summarize(self, args):
        return f"Send a postcard to {args['to']}"

    async def run(self, args, ctx):
        self.ran += 1
        return ToolResult(ok=True, content="sent")


class SecretLeakTool:
    name = "leak"
    level_key = "demo"
    description = "Returns text."
    parameters = {"type": "object", "properties": {}}

    def classify(self, args):
        return ActionClass.LOOK

    def summarize(self, args):
        return "Read something"

    async def run(self, args, ctx):
        return ToolResult(ok=True, content=f"the value is {SECRET_VALUE} ok")


class BoomTool:
    name = "boom"
    level_key = "demo"
    description = "Fails."
    parameters = {"type": "object", "properties": {}}

    def classify(self, args):
        return ActionClass.LOOK

    def summarize(self, args):
        return "Explode"

    async def run(self, args, ctx):
        raise RuntimeError(f"secret detail {SECRET_VALUE}")


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "hk.db")


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, docker_available=False, max_agent_steps=8)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def approvals(conn, clock):
    return ApprovalStore(conn, ttl_minutes=10, clock=clock, poll_interval=0.01)


@pytest.fixture
def registry():
    reg = ToolRegistry()
    reg.register(ClockTool())
    reg.register(NoteTool())
    return reg


def make_loop(client, registry, approvals, conn, settings):
    return AgentLoop(client, registry, approvals, conn, settings)


def call(name, args, call_id="c1"):
    raw = args if isinstance(args, str) else json.dumps(args)
    return ToolCallRequest(call_id, name, raw)


def user(text):
    return [{"role": "user", "content": text}]


async def drain(loop, history, turn_id="t1", on_approval=None, think=False):
    events = []
    async for event in loop.run_turn(CHAT, turn_id, history, think):
        events.append(event)
        if event["type"] == "approval_required" and on_approval:
            on_approval(event)
    return events


def types(events):
    return [e["type"] for e in events]


def tool_messages(messages):
    return [m for m in messages if m["role"] == "tool"]


async def test_plain_reply_streams_text_then_done(conn, settings, approvals, registry):
    client = FakeChatClient(
        [[ReasoningDelta("hmm"), TextDelta("Hel"), TextDelta("lo"), StreamDone("stop")]]
    )
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("hi"), think=True)

    assert types(events) == ["reasoning_delta", "text_delta", "text_delta", "turn_done"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[1]["text"] == "Hel"
    sent = client.calls[0]
    assert sent["think"] is True
    assert sent["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert sent["messages"][1:] == [{"role": "user", "content": "hi"}]
    assert {t["function"]["name"] for t in sent["tools"]} == {"clock", "note"}
    # events persisted before being yielded, final text persisted as a message
    rows = conn.execute(
        "SELECT seq, type FROM turn_events WHERE chat_id = ? AND turn_id = 't1' ORDER BY seq",
        (CHAT,),
    ).fetchall()
    assert [(r["seq"], r["type"]) for r in rows] == [(e["seq"], e["type"]) for e in events]
    assert load_history(conn, CHAT) == [{"role": "assistant", "content": "Hello"}]


async def test_allowed_look_tool_runs_without_approval(conn, settings, approvals, registry):
    client = FakeChatClient(
        [
            [call("clock", {}), StreamDone("tool_calls")],
            [TextDelta("It is noon."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("time?"))

    assert types(events) == ["step_started", "step_finished", "text_delta", "turn_done"]
    assert events[0]["tool"] == "clock"
    assert events[1]["ok"] is True
    assert events[0]["step_id"] == events[1]["step_id"]
    assert approvals.pending() == []
    second = client.calls[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["tool_calls"][0]["function"]["name"] == "clock"
    tool_msg = second[-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    datetime.fromisoformat(tool_msg["content"])  # local time as ISO text
    runs = [e for e in audit.list_events(conn) if e["event"] == "tool.run"]
    assert runs[0]["actor"] == "agent"
    assert runs[0]["detail"] == {
        "tool": "clock",
        "level_key": "demo",
        "decision": "run",
        "ok": True,
    }
    # the whole exchange is persisted for the next turn
    history = load_history(conn, CHAT)
    assert [m["role"] for m in history] == ["assistant", "tool", "assistant"]
    assert history[1]["tool_call_id"] == "c1"


async def test_change_local_asks_once_then_granted_for_chat(
    conn, settings, approvals, registry, tmp_path
):
    client = FakeChatClient(
        [
            [call("note", {"text": "buy milk"}), StreamDone("tool_calls")],
            [TextDelta("Noted."), StreamDone("stop")],
            [call("note", {"text": "buy eggs"}, "c2"), StreamDone("tool_calls")],
            [TextDelta("Noted again."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)

    def approve(event):
        approvals.resolve(event["approval_id"], "sess1", approve=True, step_up_verified=False)

    first = await drain(loop, user("note milk"), on_approval=approve)
    assert types(first) == [
        "step_started",
        "approval_required",
        "approval_resolved",
        "step_finished",
        "text_delta",
        "turn_done",
    ]
    req = first[1]
    assert req["tool"] == "note" and req["requires_step_up"] is False
    assert "buy milk" in req["summary"]
    assert first[2] == {**first[2], "approval_id": req["approval_id"], "status": "approved"}
    grants = conn.execute("SELECT chat_id, tool FROM chat_grants").fetchall()
    assert [tuple(g) for g in grants] == [(CHAT, "demo")]

    second = await drain(loop, user("note eggs"), turn_id="t2")
    assert "approval_required" not in types(second)
    assert [e["seq"] for e in second][0] == 1
    notes = (tmp_path / "workspace" / "output" / "notes.txt").read_text(encoding="utf-8")
    assert notes.splitlines() == ["buy milk", "buy eggs"]


async def test_outward_requires_step_up_approval(conn, settings, approvals, registry):
    tool = OutwardTool()
    registry.register(tool)
    client = FakeChatClient(
        [
            [call("send_postcard", {"to": "Grandma"}), StreamDone("tool_calls")],
            [TextDelta("Sent."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)

    def approve(event):
        assert event["requires_step_up"] is True
        approvals.resolve(event["approval_id"], "sess1", approve=True, step_up_verified=True)

    events = await drain(loop, user("send it"), on_approval=approve)
    assert "approval_required" in types(events)
    assert approvals.get(events[1]["approval_id"]).requires_step_up is True
    assert tool.ran == 1
    # outward actions are never granted for the chat
    assert conn.execute("SELECT COUNT(*) FROM chat_grants").fetchone()[0] == 0


async def test_declined_approval_reported_to_model(conn, settings, approvals, registry, tmp_path):
    client = FakeChatClient(
        [
            [call("note", {"text": "x"}), StreamDone("tool_calls")],
            [TextDelta("Ok, I won't."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)

    def decline(event):
        approvals.resolve(event["approval_id"], "sess1", approve=False, step_up_verified=False)

    events = await drain(loop, user("note x"), on_approval=decline)
    resolved = next(e for e in events if e["type"] == "approval_resolved")
    assert resolved["status"] == "declined"
    finished = next(e for e in events if e["type"] == "step_finished")
    assert finished["ok"] is False
    tool_msg = tool_messages(client.calls[1]["messages"])[0]
    assert json.loads(tool_msg["content"]) == {"error": "The owner declined this action"}
    assert not (tmp_path / "workspace" / "output" / "notes.txt").exists()
    assert conn.execute("SELECT COUNT(*) FROM chat_grants").fetchone()[0] == 0


async def test_expired_approval_reported_to_model(conn, settings, approvals, registry, clock):
    client = FakeChatClient(
        [
            [call("note", {"text": "x"}), StreamDone("tool_calls")],
            [TextDelta("Too late."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)

    def let_expire(event):
        clock.now += timedelta(minutes=11)

    events = await drain(loop, user("note x"), on_approval=let_expire)
    assert next(e for e in events if e["type"] == "approval_resolved")["status"] == "expired"
    tool_msg = tool_messages(client.calls[1]["messages"])[0]
    assert json.loads(tool_msg["content"]) == {"error": "The request expired"}


async def test_off_tool_not_offered_and_call_denied(conn, settings, approvals, registry, tmp_path):
    set_level(conn, "demo", ToolLevel.OFF)
    client = FakeChatClient(
        [
            [call("note", {"text": "x"}), StreamDone("tool_calls")],
            [TextDelta("It is off."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("note x"))

    assert client.calls[0]["tools"] == []
    assert "approval_required" not in types(events)
    tool_msg = tool_messages(client.calls[1]["messages"])[0]
    assert json.loads(tool_msg["content"]) == {"error": "This tool is turned off"}
    assert not (tmp_path / "workspace" / "output" / "notes.txt").exists()
    denied = [e for e in audit.list_events(conn) if e["event"] == "tool.denied"]
    assert denied[0]["detail"] == {"tool": "note", "level_key": "demo", "decision": "deny"}


async def test_malformed_tool_call_returns_error_to_model(conn, settings, approvals, registry):
    client = FakeChatClient(
        [
            [call("note", "{not json", "c1"), StreamDone("tool_calls")],
            [call("no_such_tool", {}, "c2"), StreamDone("tool_calls")],
            [call("note", {}, "c3"), StreamDone("tool_calls")],
            [call("", {}, "c4"), StreamDone("tool_calls")],
            [TextDelta("Sorry."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, replace(settings, max_agent_steps=4))
    events = await drain(loop, user("go"))

    assert types(events)[-2:] == ["text_delta", "turn_done"]
    assert len(client.calls) == 5
    errors = []
    for i, call_id in enumerate(["c1", "c2", "c3", "c4"], start=1):
        msg = tool_messages(client.calls[i]["messages"])[-1]
        assert msg["tool_call_id"] == call_id
        errors.append(json.loads(msg["content"])["error"])
    assert "valid JSON" in errors[0]
    assert "no_such_tool" in errors[1]
    assert "text" in errors[2]  # missing required argument
    assert errors[3]
    finished = [e for e in events if e["type"] == "step_finished"]
    assert len(finished) == 4 and not any(e["ok"] for e in finished)
    # invalid JSON arguments are not replayed verbatim to the model
    assistant = [m for m in client.calls[1]["messages"] if m.get("tool_calls")][0]
    json.loads(assistant["tool_calls"][0]["function"]["arguments"])


async def test_step_limit_stops_loop(conn, settings, approvals, registry):
    scripts = [[call("clock", {}, f"c{i}"), StreamDone("tool_calls")] for i in range(5)]
    client = FakeChatClient(scripts)
    loop = make_loop(client, registry, approvals, conn, replace(settings, max_agent_steps=3))
    events = await drain(loop, user("loop forever"))

    assert len(client.calls) == 4  # three runs, then the fourth request is cut off
    runs = [e for e in events if e["type"] == "step_finished" and e["ok"]]
    assert len(runs) == 3
    assert events[-1]["type"] == "turn_done"
    final = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert "step limit" in final
    history = load_history(conn, CHAT)
    assert "step limit" in history[-1]["content"]
    # every persisted tool call has a matching tool message, so the history stays valid
    call_ids = [c["id"] for m in history if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in history if m["role"] == "tool"]
    assert call_ids == answered


async def test_provider_failure_ends_turn_with_error_event(conn, settings, approvals, registry):
    client = FakeChatClient(
        [
            [TextDelta("Partial "), TextDelta("answer"), ProviderError("Model went away.", "x")],
            [TextDelta("Back again."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("hi"))
    assert types(events) == ["text_delta", "text_delta", "turn_error"]
    assert events[-1]["message"] == "Model went away."
    assert load_history(conn, CHAT) == [{"role": "assistant", "content": "Partial answer"}]

    history = load_history(conn, CHAT) + user("again")
    second = await drain(loop, history, turn_id="t2")
    assert types(second) == ["text_delta", "turn_done"]
    assert second[0]["seq"] == 1


async def test_tool_exception_reported_without_message(conn, settings, approvals, registry):
    registry.register(BoomTool())
    client = FakeChatClient(
        [
            [call("boom", {}), StreamDone("tool_calls")],
            [TextDelta("It broke."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("boom"))
    assert events[-1]["type"] == "turn_done"
    tool_msg = tool_messages(client.calls[1]["messages"])[0]
    assert json.loads(tool_msg["content"]) == {"error": "The tool failed: RuntimeError"}
    failed = [e for e in audit.list_events(conn) if e["event"] == "tool.failed"]
    assert len(failed) == 1
    assert "secret detail" not in json.dumps(audit.list_events(conn))


async def test_secrets_not_in_model_messages(conn, settings, approvals, registry, tmp_path):
    SecretStore(tmp_path).put("github_token", SECRET_VALUE)
    SecretStore(tmp_path).put("pin", "abc")  # too short to redact
    registry.register(SecretLeakTool())
    client = FakeChatClient(
        [
            [call("leak", {}), StreamDone("tool_calls")],
            [TextDelta("Done abc."), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("leak it"))

    tool_msg = tool_messages(client.calls[1]["messages"])[0]
    assert tool_msg["content"] == "the value is [redacted] ok"
    assert SECRET_VALUE not in json.dumps(client.calls)
    assert SECRET_VALUE not in json.dumps(events)
    stored = conn.execute("SELECT data FROM turn_events").fetchall()
    stored += conn.execute("SELECT content FROM messages").fetchall()
    assert all(SECRET_VALUE not in (row[0] or "") for row in stored)
    assert "abc" in events[-2]["text"]


async def test_secrets_redacted_from_approval_request(conn, settings, approvals, registry):
    SecretStore(settings.data_dir).put("github_token", SECRET_VALUE)
    client = FakeChatClient(
        [
            [call("note", {"text": f"token {SECRET_VALUE}"}), StreamDone("tool_calls")],
            [TextDelta("ok"), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)

    def decline(event):
        approvals.resolve(event["approval_id"], "s", approve=False, step_up_verified=False)

    events = await drain(loop, user("x"), on_approval=decline)
    req = next(e for e in events if e["type"] == "approval_required")
    stored = approvals.get(req["approval_id"])
    assert SECRET_VALUE not in json.dumps([stored.summary, stored.detail, req])
    assert "[redacted]" in stored.summary


async def test_unexpected_client_failure_ends_turn_with_generic_error(
    conn, settings, approvals, registry
):
    client = FakeChatClient([[TextDelta("a"), RuntimeError(f"boom {SECRET_VALUE}")]])
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("hi"))
    assert types(events) == ["text_delta", "turn_error"]
    assert "boom" not in events[-1]["message"]
