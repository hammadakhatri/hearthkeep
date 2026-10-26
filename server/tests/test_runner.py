import asyncio
from datetime import UTC, datetime

import pytest
from fakes import FakeChatClient

from hearthkeep.agent.loop import AgentLoop, load_history
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


async def test_turn_survives_subscriber_disconnect(conn, approvals, tmp_path):
    runner, client = make_runner(conn, approvals, tmp_path, tool_turn())
    turn_id = runner.start(CHAT, "what time is it?", think=False)

    first: list[dict] = []

    async def subscriber():
        async for event in runner.subscribe(CHAT, turn_id):
            first.append(event)
            await asyncio.sleep(3600)  # stalls until cancelled

    task = asyncio.create_task(subscriber())
    while not first:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await runner.wait_idle()
    replay = await collect(runner, turn_id)
    assert [e["seq"] for e in replay] == list(range(1, len(replay) + 1))
    assert [e["type"] for e in replay] == [
        "step_started",
        "step_finished",
        "text_delta",
        "text_delta",
        "turn_done",
    ]
    assert replay[0] == first[0]
    # the user message was persisted before the model saw it; the answer after
    assert client.calls[0]["messages"][-1] == {"role": "user", "content": "what time is it?"}
    history = load_history(conn, CHAT)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[-1]["content"] == "It is noon."


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
