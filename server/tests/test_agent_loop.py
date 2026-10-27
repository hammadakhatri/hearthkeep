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
    # Even with no number check, the done event names the reply (what "Send" refers to).
    saved = conn.execute(
        "SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant'", (CHAT,)
    ).fetchone()
    assert events[-1]["message_id"] == saved["id"]
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
    failed = [e for e in audit.list_events(conn) if e["event"] == "turn.failed"]
    assert [(e["actor"], e["detail"]) for e in failed] == [
        ("agent", {"error_type": "RuntimeError"})
    ]


async def test_grant_uses_level_read_after_the_wait(conn, settings, approvals, registry, tmp_path):
    client = FakeChatClient(
        [
            [call("note", {"text": "x"}), StreamDone("tool_calls")],
            [TextDelta("ok"), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)

    def narrow_then_approve(event):
        set_level(conn, "demo", ToolLevel.ASK)
        approvals.resolve(event["approval_id"], "s", approve=True, step_up_verified=False)

    await drain(loop, user("note x"), on_approval=narrow_then_approve)
    assert conn.execute("SELECT COUNT(*) FROM chat_grants").fetchone()[0] == 0
    # the owner approved this one call, so it still ran
    notes = (tmp_path / "workspace" / "output" / "notes.txt").read_text(encoding="utf-8")
    assert notes.splitlines() == ["x"]


def test_load_history_answers_orphan_tool_calls(conn):
    from hearthkeep.agent.loop import save_message

    calls = [
        {"id": i, "type": "function", "function": {"name": "clock", "arguments": "{}"}}
        for i in ("a", "b")
    ]
    save_message(conn, CHAT, {"role": "assistant", "content": "", "tool_calls": calls}, [])
    save_message(conn, CHAT, {"role": "tool", "tool_call_id": "a", "content": "done"}, [])
    save_message(conn, CHAT, {"role": "user", "content": "next"}, [])
    save_message(conn, CHAT, {"role": "assistant", "content": "", "tool_calls": calls[:1]}, [])

    history = load_history(conn, CHAT)
    stub = json.dumps({"error": "This step was interrupted"})
    assert [(m["role"], m.get("tool_call_id"), m["content"]) for m in history[1:]] == [
        ("tool", "a", "done"),
        ("tool", "b", stub),
        ("user", None, "next"),
        ("assistant", None, ""),
        ("tool", "a", stub),
    ]


# --- attachments ---------------------------------------------------------------------------


class ImageMakerTool:
    """Saves real image records for this chat and returns `extra` items next to them."""

    name = "make_images"
    level_key = "images"
    description = "Makes images."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, count=2, extra=(), ok=True):
        self.count, self.extra, self.ok = count, list(extra), ok
        self.ids: list[str] = []

    def classify(self, args):
        return ActionClass.LOOK

    def summarize(self, args):
        return "Make images"

    async def run(self, args, ctx):
        from hearthkeep.images import PNG_SIGNATURE, ImageStore

        store = ImageStore(ctx.conn, ctx.settings.data_dir)
        for _ in range(self.count):
            rec = store.save(
                PNG_SIGNATURE + b"x", chat_id=ctx.chat_id, turn_id=ctx.turn_id, model_id="m",
                prompt="p", width=512, height=512, seed=1,
            )  # fmt: skip
            self.ids.append(rec.id)
        items = [{"kind": "image", "id": i} for i in self.ids] + self.extra
        return ToolResult(ok=self.ok, content="Made them.", data={"attachments": items})


def image_turn(tool_name="make_images"):
    return FakeChatClient(
        [
            [call(tool_name, {}), StreamDone("tool_calls")],
            [TextDelta("Here they are."), StreamDone("stop")],
        ]
    )


async def test_attachments_event_emitted_and_persisted(conn, settings, approvals, registry):
    tool = ImageMakerTool(count=2)
    registry.register(tool)
    loop = make_loop(image_turn(), registry, approvals, conn, settings)
    events = await drain(loop, user("draw"))

    assert types(events) == [
        "step_started", "attachments", "step_finished", "text_delta", "turn_done",
    ]  # fmt: skip
    attached = events[1]
    assert attached["step_id"] == events[0]["step_id"]
    assert attached["items"] == [{"kind": "image", "id": i} for i in tool.ids]
    row = conn.execute(
        "SELECT seq, data FROM turn_events WHERE type = 'attachments' AND turn_id = 't1'"
    ).fetchone()
    assert row["seq"] == attached["seq"]
    assert json.loads(row["data"]) == {"step_id": attached["step_id"], "items": attached["items"]}


async def test_attachments_kept_when_the_tool_reports_failure(conn, settings, approvals, registry):
    tool = ImageMakerTool(count=1, ok=False)
    registry.register(tool)
    loop = make_loop(image_turn(), registry, approvals, conn, settings)
    events = await drain(loop, user("draw"))
    attached = next(e for e in events if e["type"] == "attachments")
    assert attached["items"] == [{"kind": "image", "id": tool.ids[0]}]
    assert next(e for e in events if e["type"] == "step_finished")["ok"] is False


async def test_invalid_attachment_items_dropped(conn, settings, approvals, registry):
    from hearthkeep.images import PNG_SIGNATURE, ImageStore

    other = ImageStore(conn, settings.data_dir).save(
        PNG_SIGNATURE + b"x", chat_id="another-chat", turn_id="t9", model_id="m", prompt="p",
        width=512, height=512, seed=1,
    )  # fmt: skip
    extra = [
        {"kind": "file", "id": "abc"},
        {"kind": "image", "id": "../etc/passwd"},
        {"kind": "image", "id": "x" * 33},
        {"kind": "image", "id": 12345},
        {"kind": "image"},
        "not-a-dict",
        {"kind": "image", "id": "doesNotExist"},
        {"kind": "image", "id": other.id},  # exists, but belongs to another chat
    ]
    tool = ImageMakerTool(count=1, extra=extra)
    registry.register(tool)
    loop = make_loop(image_turn(), registry, approvals, conn, settings)
    events = await drain(loop, user("draw"))
    attached = [e for e in events if e["type"] == "attachments"]
    assert len(attached) == 1
    assert attached[0]["items"] == [{"kind": "image", "id": tool.ids[0]}]


@pytest.mark.parametrize(
    "data",
    [None, {}, {"attachments": "nope"}, {"attachments": [{"kind": "image", "id": "missing"}]}],
)
async def test_no_attachments_event_without_valid_items(conn, settings, approvals, registry, data):
    class Plain:
        name, level_key, description = "plain", "demo", "Plain."
        parameters = {"type": "object", "properties": {}}

        def classify(self, args):
            return ActionClass.LOOK

        def summarize(self, args):
            return "Plain"

        async def run(self, args, ctx):
            return ToolResult(ok=True, content="done", data=data)

    registry.register(Plain())
    loop = make_loop(image_turn("plain"), registry, approvals, conn, settings)
    events = await drain(loop, user("go"))
    assert "attachments" not in types(events)


async def test_model_never_sees_attachment_ids(conn, settings, approvals, registry):
    tool = ImageMakerTool(count=3)
    registry.register(tool)
    client = image_turn()
    loop = make_loop(client, registry, approvals, conn, settings)
    await drain(loop, user("draw"))

    tool_msg = tool_messages(client.calls[1]["messages"])[0]
    assert tool_msg["content"] == "Made them."
    sent = json.dumps(client.calls)
    stored = json.dumps([tuple(r) for r in conn.execute("SELECT * FROM messages").fetchall()])
    for image_id in tool.ids:
        assert image_id not in sent
        assert image_id not in stored


# --- provider streams are closed deterministically ------------------------------------------


class ClosingStream:
    """An async iterator over scripted events that records `aclose()`."""

    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def aclose(self):
        self.closed = True


class ClosingClient:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.streams: list[ClosingStream] = []

    def stream(self, messages, tools, think):
        s = ClosingStream(self.scripts.pop(0))
        self.streams.append(s)
        return s


async def test_provider_stream_closed_when_turn_ends_early(conn, settings, approvals, registry):
    client = ClosingClient([[TextDelta("a"), TextDelta("b"), StreamDone("stop")]])
    loop = make_loop(client, registry, approvals, conn, settings)
    turn = loop.run_turn(CHAT, "t1", user("hi"), False)
    first = await anext(turn)
    assert first["type"] == "text_delta"
    await turn.aclose()  # the client went away mid-reply
    assert client.streams[0].closed is True


async def test_provider_stream_closed_after_each_round(conn, settings, approvals, registry):
    client = ClosingClient(
        [
            [call("clock", {}), StreamDone("tool_calls")],
            [TextDelta("noon"), StreamDone("stop")],
        ]
    )
    loop = make_loop(client, registry, approvals, conn, settings)
    events = await drain(loop, user("time?"))
    assert events[-1]["type"] == "turn_done"
    assert [s.closed for s in client.streams] == [True, True]


# --- runs and the per-chat context --------------------------------------------------------------


class RunReporterTool:
    """Returns run attachment items as given (the runs rows are made by the test)."""

    name = "python"
    level_key = "python"
    description = "Runs Python."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, items):
        self.items = items

    def classify(self, args):
        return ActionClass.LOOK

    def summarize(self, args):
        return "Run Python"

    async def run(self, args, ctx):
        return ToolResult(ok=True, content="Run ok in 1.0 s.", data={"attachments": self.items})


def _add_run(conn, run_id, chat_id):
    conn.execute(
        "INSERT INTO runs (id, chat_id, turn_id, purpose, status, created_at) "
        "VALUES (?, ?, 't0', 'analysis', 'ok', '2026-01-01')",
        (run_id, chat_id),
    )


async def test_run_attachment_event_for_this_chats_run_only(conn, approvals, tmp_path):
    settings = Settings(data_dir=tmp_path, docker_available=True, max_agent_steps=8)
    _add_run(conn, "mine", CHAT)
    _add_run(conn, "theirs", "other-chat")
    items = [
        {"kind": "run", "id": "mine"},
        {"kind": "run", "id": "mine"},  # once only
        {"kind": "run", "id": "theirs"},
        {"kind": "run", "id": "missing"},
        {"kind": "run", "id": "../x"},
        {"kind": "run"},
        {"kind": "image", "id": "mine"},  # not an image
    ]
    registry = ToolRegistry()
    registry.register(RunReporterTool(items))
    loop = make_loop(image_turn("python"), registry, approvals, conn, settings)
    events = await drain(loop, user("sum it"))
    attached = [e for e in events if e["type"] == "attachments"]
    assert [a["items"] for a in attached] == [[{"kind": "run", "id": "mine"}]]


async def test_system_prompt_lists_chat_files_only_when_present(
    conn, approvals, registry, settings
):
    contexts = {CHAT: ""}
    seen: list[str] = []

    def context(chat_id):
        seen.append(chat_id)
        return contexts[chat_id]

    client = FakeChatClient([[TextDelta("Hi."), StreamDone("stop")]])
    loop = AgentLoop(client, registry, approvals, conn, settings, context=context)
    await drain(loop, user("hi"))
    assert client.calls[0]["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}

    contexts[CHAT] = "This chat has files the owner attached:\n- f1: \"a.csv\""
    client = FakeChatClient(
        [[call("clock", {}), StreamDone("tool_calls")], [TextDelta("Done."), StreamDone("stop")]]
    )
    loop = AgentLoop(client, registry, approvals, conn, settings, context=context)
    await drain(loop, user("hi"), turn_id="t2")
    expected = {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + contexts[CHAT]}
    # On every model round of the turn, not just the first.
    assert [c["messages"][0] for c in client.calls] == [expected, expected]
    assert seen == [CHAT, CHAT, CHAT]


async def test_docker_flag_refreshes_every_round(conn, approvals, tmp_path):
    settings = Settings(data_dir=tmp_path, docker_available=False, max_agent_steps=8)
    states = [False, True]

    async def docker():
        return states.pop(0) if len(states) > 1 else states[0]

    registry = ToolRegistry()
    registry.register(RunReporterTool([]))
    client = FakeChatClient(
        [
            [call("python", {}, "c1"), StreamDone("tool_calls")],
            [call("python", {}, "c2"), StreamDone("tool_calls")],
            [TextDelta("Done."), StreamDone("stop")],
        ]
    )
    loop = AgentLoop(client, registry, approvals, conn, settings, docker=docker)

    def decline(event):
        approvals.resolve(event["approval_id"], "s1", False, False)

    events = await drain(loop, user("sum it"), on_approval=decline)
    # Docker was down for the first call (asked, declined) and up for the second (ran).
    assert types(events).count("approval_required") == 1
    finished = [e["ok"] for e in events if e["type"] == "step_finished"]
    assert finished == [False, True]


# --- the number check ---------------------------------------------------------------------------

FILE_CHAT = "e" * 32


def _number_checker(conn, tmp_path, *, with_file=True, total=5, turn_id="t1"):
    from hearthkeep.files.store import ChatFileStore
    from hearthkeep.numbercheck import NumberChecker
    from hearthkeep.sandbox.runs import RunStore

    if with_file:
        conn.execute(
            "INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source,"
            " file_name, created_at) VALUES ('f1', ?, 'sales.csv', 'csv', 'text/csv', 1,"
            " 'ready', 'upload', 'f1.csv', '2026-01-01')",
            (FILE_CHAT,),
        )
    results = {"values": [{"name": "total", "value": total, "unit": None, "note": None,
                           "is_date": False, "kind": "computed", "page": None}]}  # fmt: skip
    conn.execute(
        "INSERT INTO runs (id, chat_id, turn_id, purpose, status, results, created_at)"
        " VALUES ('r1', ?, ?, 'analysis', 'ok', ?, '2026-01-01')",
        (FILE_CHAT, turn_id, json.dumps(results)),
    )
    return NumberChecker(conn, RunStore(conn, tmp_path), ChatFileStore(conn, tmp_path))


async def drain_chat(loop, history, chat_id, turn_id="t1"):
    return [e async for e in loop.run_turn(chat_id, turn_id, history, False)]


def _assistant_ids(conn, chat_id):
    rows = conn.execute(
        "SELECT id FROM messages WHERE chat_id = ? AND role = 'assistant' ORDER BY id", (chat_id,)
    )
    return [r["id"] for r in rows]


async def test_number_check_event_before_turn_done_and_persisted(
    conn, settings, approvals, registry, tmp_path
):
    checker = _number_checker(conn, tmp_path)
    client = FakeChatClient(
        [
            [TextDelta("Let me count."), call("clock", {}), StreamDone("tool_calls")],
            [TextDelta("There are 5 rows and 9 columns."), StreamDone("stop")],
        ]
    )
    loop = AgentLoop(client, registry, approvals, conn, settings, reply_checker=checker)
    question = "Attached file: f1\nHow many rows in 2024?"
    events = await drain_chat(loop, user(question), FILE_CHAT)

    assert types(events)[-2:] == ["number_check", "turn_done"]
    check = events[-2]
    text = "Let me count.\n\nThere are 5 rows and 9 columns."
    assert check["text"] == text
    assert [(m["text"], m["kind"]) for m in check["marks"]] == [
        ("5", "computed"),
        ("9", "unverified"),
    ]
    for m in check["marks"]:
        assert text[m["start"] : m["end"]] == m["text"]
    stored = conn.execute("SELECT type, data FROM turn_events WHERE turn_id = 't1' ORDER BY seq")
    stored = stored.fetchall()
    assert stored[-2]["type"] == "number_check"
    last_assistant = _assistant_ids(conn, FILE_CHAT)[-1]
    assert json.loads(stored[-2]["data"]) == {
        "text": check["text"],
        "marks": check["marks"],
        "message_id": last_assistant,
    }
    assert check["message_id"] == last_assistant
    assert events[-1]["message_id"] == last_assistant
    row = conn.execute("SELECT * FROM number_checks").fetchone()
    assert row["message_id"] == last_assistant
    assert row["chat_id"] == FILE_CHAT and row["turn_id"] == "t1"
    assert row["text"] == text


async def test_number_check_covers_plain_followup_reply(
    conn, settings, approvals, registry, tmp_path
):
    checker = _number_checker(conn, tmp_path)
    client = FakeChatClient([[TextDelta("Still 5, and 2024."), StreamDone("stop")]])
    loop = AgentLoop(client, registry, approvals, conn, settings, reply_checker=checker)
    events = await drain_chat(loop, user("And in 2024?"), FILE_CHAT, turn_id="t2")
    assert types(events)[-2:] == ["number_check", "turn_done"]
    # The year from the question is exempt; the 5 was reported in an earlier turn (t1), so it
    # is not evidence for this one.
    assert [(m["text"], m["kind"]) for m in events[-2]["marks"]] == [("5", "unverified")]


async def test_number_check_after_step_limit_message(conn, settings, approvals, registry, tmp_path):
    checker = _number_checker(conn, tmp_path)
    scripts = [
        [TextDelta("Found 5 rows."), call("clock", {}, "c0"), StreamDone("tool_calls")],
        [call("clock", {}, "c1"), StreamDone("tool_calls")],
    ]
    loop = AgentLoop(
        FakeChatClient(scripts), registry, approvals, conn, replace(settings, max_agent_steps=1),
        reply_checker=checker,
    )  # fmt: skip
    events = await drain_chat(loop, user("go"), FILE_CHAT)
    assert types(events)[-2:] == ["number_check", "turn_done"]
    assert "step limit" in events[-2]["text"]
    # Hearthkeep's own step-limit message ("1 tool steps") is never marked.
    assert [(m["text"], m["kind"]) for m in events[-2]["marks"]] == [("5", "computed")]
    row = conn.execute("SELECT message_id FROM number_checks").fetchone()
    assert row["message_id"] == _assistant_ids(conn, FILE_CHAT)[-1]


async def test_no_number_check_without_files(conn, settings, approvals, registry, tmp_path):
    checker = _number_checker(conn, tmp_path, with_file=False)
    client = FakeChatClient([[TextDelta("5 rows."), StreamDone("stop")]])
    loop = AgentLoop(client, registry, approvals, conn, settings, reply_checker=checker)
    events = await drain_chat(loop, user("how many?"), FILE_CHAT)
    assert types(events) == ["text_delta", "turn_done"]


async def test_checker_failure_never_breaks_the_turn(conn, settings, approvals, registry, caplog):
    class Broken:
        async def check(self, *args, **kwargs):
            raise ValueError("private 1234")

    client = FakeChatClient([[TextDelta("5 rows."), StreamDone("stop")]])
    loop = AgentLoop(client, registry, approvals, conn, settings, reply_checker=Broken())
    events = await drain_chat(loop, user("how many?"), FILE_CHAT)
    assert types(events) == ["text_delta", "turn_done"]
    assert "ValueError" in caplog.text
    assert "private 1234" not in caplog.text
    assert load_history(conn, FILE_CHAT)[-1] == {"role": "assistant", "content": "5 rows."}


def test_save_message_returns_the_row_id(conn):
    from hearthkeep.agent.loop import save_message

    first = save_message(conn, CHAT, {"role": "user", "content": "a"}, [])
    second = save_message(conn, CHAT, {"role": "assistant", "content": "b"}, [])
    assert isinstance(first, int) and second == first + 1


async def test_malformed_checker_result_never_breaks_the_turn(conn, settings, approvals, registry):
    class Odd:
        async def check(self, *args, **kwargs):
            return {"marks": []}  # no text

    client = FakeChatClient([[TextDelta("5 rows."), StreamDone("stop")]])
    loop = AgentLoop(client, registry, approvals, conn, settings, reply_checker=Odd())
    events = await drain_chat(loop, user("how many?"), FILE_CHAT)
    assert types(events) == ["text_delta", "turn_done"]


class StrictTool:
    name = "strict"
    level_key = "demo"
    description = "Takes strict arguments."
    parameters = {
        "type": "object",
        "properties": {
            "word": {"type": "string", "minLength": 1, "maxLength": 5},
            "colour": {"type": "string", "enum": ["red", "green"]},
            "code": {"type": "string", "pattern": "^[a-z]+$"},
            "count": {"type": "integer"},
        },
        "additionalProperties": False,
    }

    def classify(self, args):
        return ActionClass.LOOK

    def summarize(self, args):
        return "Strict"

    async def run(self, args, ctx):
        return ToolResult(ok=True, content="fine")


async def test_invalid_arguments_name_the_field_and_rule_never_the_value(
    conn, settings, approvals, registry
):
    from hearthkeep.tools.send import SendToDevicesTool

    registry.register(SendToDevicesTool(None, None))
    registry.register(StrictTool())
    secret_ish = "VALUE-" + "x" * 4100
    bad_calls = [
        ("send_to_devices", {"kind": "summary", "text": secret_ish}),
        ("strict", {"word": ""}),
        ("strict", {"colour": "VALUE-blue"}),
        ("strict", {"code": "VALUE-9"}),
        ("strict", {"count": "VALUE-seven"}),
        ("strict", {"extra": 1}),
    ]
    script = [
        [call(name, a, f"c{i}"), StreamDone("tool_calls")] for i, (name, a) in enumerate(bad_calls)
    ]
    client = FakeChatClient([*script, [TextDelta("Sorry."), StreamDone("stop")]])
    loop = make_loop(client, registry, approvals, conn, replace(settings, max_agent_steps=10))
    await drain(loop, user("go"))
    errors = [
        json.loads(tool_messages(client.calls[i]["messages"])[-1]["content"])["error"]
        for i in range(1, len(bad_calls) + 1)
    ]
    assert errors == [
        "Invalid arguments for 'send_to_devices': 'text' is too long (at most 4000 characters).",
        "Invalid arguments for 'strict': 'word' is empty.",
        "Invalid arguments for 'strict': 'colour' must be one of: red, green.",
        "Invalid arguments for 'strict': 'code' is not in the right format.",
        "Invalid arguments for 'strict': 'count' must be of type integer.",
        "Invalid arguments for 'strict': Additional properties are not allowed "
        "('extra' was unexpected)",
    ]
    assert not any("VALUE" in e for e in errors)
