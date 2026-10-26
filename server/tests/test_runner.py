import asyncio
import json
from datetime import UTC, datetime

import pytest
from fakes import FakeChatClient

from hearthkeep import audit
from hearthkeep.agent.loop import AgentLoop, _Turn, load_history
from hearthkeep.agent.runner import TurnInProgressError, TurnRunner
from hearthkeep.approvals import ApprovalStore
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.providers.chat import StreamDone, TextDelta, ToolCallRequest
from hearthkeep.tools.base import ToolRegistry
from hearthkeep.tools.demo import ClockTool, NoteTool

CHAT = "chat1"


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "hk.db")


@pytest.fixture
def approvals(conn):
    return ApprovalStore(conn, ttl_minutes=10, clock=lambda: datetime.now(UTC), poll_interval=0.01)


def make_runner(conn, approvals, tmp_path, scripts):
    registry = ToolRegistry()
    registry.register(ClockTool())
    registry.register(NoteTool())
    settings = Settings(data_dir=tmp_path, docker_available=False)
    client = FakeChatClient(scripts)
    loop = AgentLoop(client, registry, approvals, conn, settings)
    return TurnRunner(conn, loop), client


def tool_turn():
    return [
        [ToolCallRequest("c1", "clock", "{}"), StreamDone("tool_calls")],
        [TextDelta("It is "), TextDelta("noon."), StreamDone("stop")],
    ]


async def collect(runner, turn_id, after_seq=0):
    return [e async for e in runner.subscribe(CHAT, turn_id, after_seq=after_seq)]


def note_turn(*after):
    return [
        [ToolCallRequest("c1", "note", '{"text": "x"}'), StreamDone("tool_calls")],
        [TextDelta("Saved."), StreamDone("stop")],
        *after,
    ]


async def wait_for(predicate):
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


async def test_turn_survives_subscriber_disconnect(conn, approvals, tmp_path):
    runner, client = make_runner(conn, approvals, tmp_path, note_turn())
    turn_id = runner.start(CHAT, "note x", think=False)

    seen: list[dict] = []

    async def subscriber():
        async for event in runner.subscribe(CHAT, turn_id):
            seen.append(event)

    task = asyncio.create_task(subscriber())
    # the turn is now blocked waiting on the approval; disconnect mid-turn
    await wait_for(lambda: any(e["type"] == "approval_required" for e in seen))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.active_turn(CHAT) == turn_id

    approval_id = next(e for e in seen if e["type"] == "approval_required")["approval_id"]
    approvals.resolve(approval_id, "s", approve=True, step_up_verified=False)
    await runner.wait_idle()

    replay = await collect(runner, turn_id)
    assert [e["seq"] for e in replay] == list(range(1, len(replay) + 1))
    assert [e["type"] for e in replay] == [
        "step_started",
        "approval_required",
        "approval_resolved",
        "step_finished",
        "text_delta",
        "turn_done",
    ]
    assert replay[: len(seen)] == seen
    # the user message was persisted before the model saw it; the answer after
    assert client.calls[0]["messages"][-1] == {"role": "user", "content": "note x"}
    history = load_history(conn, CHAT)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[-1]["content"] == "Saved."


async def test_cancelled_turn_leaves_well_formed_history(conn, approvals, tmp_path):
    again = [TextDelta("Hello again."), StreamDone("stop")]
    runner, client = make_runner(conn, approvals, tmp_path, note_turn()[:1] + [again])
    turn_id = runner.start(CHAT, "note x", think=False)
    await wait_for(lambda: len(approvals.pending()) == 1)
    approval_id = approvals.pending()[0].id

    assert runner.cancel(turn_id) is True
    await runner.wait_idle()

    events = await collect(runner, turn_id)
    assert events[-1]["type"] == "turn_error"
    assert approvals.get(approval_id).status == "expired"  # M2: no orphan pending approval

    second = runner.start(CHAT, "hello?", think=False)
    assert [e["type"] for e in await collect(runner, second)] == ["text_delta", "turn_done"]
    sent = client.calls[-1]["messages"]
    asked = [c["id"] for m in sent if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in sent if m["role"] == "tool"]
    assert asked == answered == ["c1"]
    tool_msg = next(m for m in sent if m["role"] == "tool")
    assert json.loads(tool_msg["content"]) == {"error": "This step was interrupted"}


async def test_runner_records_crashed_turn(conn, approvals, tmp_path):
    runner, _ = make_runner(conn, approvals, tmp_path, [])

    async def crashing_run_turn(chat_id, turn_id, history, think):
        yield runner._loop._emit(_Turn(chat_id, turn_id), "text_delta", text="hi")
        raise RuntimeError("secret detail")

    runner._loop.run_turn = crashing_run_turn
    turn_id = runner.start(CHAT, "hi", think=False)
    await runner.wait_idle()

    events = await collect(runner, turn_id)
    assert [(e["type"], e["seq"]) for e in events] == [("text_delta", 1), ("turn_error", 2)]
    assert "secret" not in events[-1]["message"]
    failed = [e for e in audit.list_events(conn) if e["event"] == "turn.failed"]
    assert [(e["actor"], e["detail"]) for e in failed] == [
        ("agent", {"error_type": "RuntimeError"})
    ]


async def test_subscribe_after_seq_skips_seen_events(conn, approvals, tmp_path):
    runner, _ = make_runner(conn, approvals, tmp_path, tool_turn())
    turn_id = runner.start(CHAT, "time?", think=False)
    live = await collect(runner, turn_id)
    assert live[-1]["type"] == "turn_done"
    await runner.wait_idle()

    rest = await collect(runner, turn_id, after_seq=2)
    assert rest == live[2:]
    assert await collect(runner, turn_id, after_seq=live[-1]["seq"]) == []


async def test_one_turn_per_chat_at_a_time(conn, approvals, tmp_path):
    scripts = [
        [ToolCallRequest("c1", "note", '{"text": "x"}'), StreamDone("tool_calls")],
        [TextDelta("ok"), StreamDone("stop")],
        [TextDelta("second"), StreamDone("stop")],
    ]
    runner, _ = make_runner(conn, approvals, tmp_path, scripts)
    turn_id = runner.start(CHAT, "note x", think=False)
    events = runner.subscribe(CHAT, turn_id)
    async for event in events:
        if event["type"] == "approval_required":
            with pytest.raises(TurnInProgressError):
                runner.start(CHAT, "again", think=False)
            approvals.resolve(event["approval_id"], "s", approve=True, step_up_verified=False)
    await runner.wait_idle()

    second = runner.start(CHAT, "again", think=False)
    assert second != turn_id
    assert [e["type"] for e in await collect(runner, second)] == ["text_delta", "turn_done"]


async def test_unknown_turn_subscription_ends_immediately(conn, approvals, tmp_path):
    runner, _ = make_runner(conn, approvals, tmp_path, [])
    assert await collect(runner, "nope") == []


# ---- recovery after a restart ------------------------------------------------------------


def seed_interrupted(conn, chat_id=CHAT, turn_id="t-crashed"):
    """What a force-exit mid-approval leaves behind: no terminal event, a pending approval and
    an assistant tool call without its answer."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO chats (id, title, think, created_at) VALUES (?, '', 0, ?)", (chat_id, now)
    )
    calls = [{"id": "c1", "type": "function", "function": {"name": "note", "arguments": "{}"}}]
    for role, content, tool_calls in (
        ("user", "save a note", None),
        ("assistant", "", json.dumps(calls)),
    ):
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, tool_calls, tool_call_id, created_at)"
            " VALUES (?, ?, ?, ?, NULL, ?)",
            (chat_id, role, content, tool_calls, now),
        )
    for seq, type_ in enumerate(("step_started", "approval_required"), start=1):
        conn.execute(
            "INSERT INTO turn_events (chat_id, turn_id, seq, type, data, created_at)"
            " VALUES (?, ?, ?, ?, '{}', ?)",
            (chat_id, turn_id, seq, type_, now),
        )
    conn.execute(
        "INSERT INTO approvals (id, chat_id, turn_id, tool, action_class, summary, detail,"
        " requires_step_up, status, created_at, expires_at)"
        " VALUES ('a1', ?, ?, 'note', 'change_local', 's', '{}', 0, 'pending', ?, '9999-01-01')",
        (chat_id, turn_id, now),
    )


def events_of(conn, turn_id):
    return [
        (r["seq"], r["type"], json.loads(r["data"]))
        for r in conn.execute(
            "SELECT seq, type, data FROM turn_events WHERE turn_id = ? ORDER BY seq", (turn_id,)
        )
    ]


def test_recovery_closes_interrupted_turns_and_expires_pending_approvals(conn):
    from hearthkeep.agent.loop import INTERRUPTED
    from hearthkeep.agent.runner import RESTARTED, recover_after_restart

    seed_interrupted(conn)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO turn_events (chat_id, turn_id, seq, type, data, created_at)"
        " VALUES (?, 't-done', 1, 'turn_done', '{}', ?)",
        (CHAT, now),
    )

    recover_after_restart(conn)

    status = conn.execute("SELECT status, resolved_at FROM approvals WHERE id = 'a1'").fetchone()
    assert status["status"] == "expired" and status["resolved_at"]
    assert events_of(conn, "t-crashed")[-1] == (3, "turn_error", {"message": RESTARTED})
    assert RESTARTED == "This reply was interrupted when Hearthkeep restarted."
    assert events_of(conn, "t-done") == [(1, "turn_done", {})]  # finished turns untouched
    tool_rows = conn.execute(
        "SELECT tool_call_id, content FROM messages WHERE role = 'tool'"
    ).fetchall()
    assert [tuple(r) for r in tool_rows] == [("c1", INTERRUPTED)]
    assert [m["role"] for m in load_history(conn, CHAT)] == ["user", "assistant", "tool"]
    assert any(e["event"] == "turns.recovered" for e in audit.list_events(conn, limit=10))

    recover_after_restart(conn)  # idempotent: nothing left to close
    assert len(events_of(conn, "t-crashed")) == 3
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'tool'").fetchone()[0] == 1


async def test_recovered_turn_replays_to_its_end(conn, approvals, tmp_path):
    from hearthkeep.agent.runner import recover_after_restart

    seed_interrupted(conn)
    recover_after_restart(conn)
    runner, _ = make_runner(conn, approvals, tmp_path, [])
    events = await asyncio.wait_for(collect(runner, "t-crashed"), 2)
    assert events[-1]["type"] == "turn_error"
