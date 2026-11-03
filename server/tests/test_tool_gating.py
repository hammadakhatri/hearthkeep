"""Tools follow their add-on: offered only when it is ready, denied at call time when it is
not, and Tools & permissions lists only real tools."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import FakeChatClient, FakeSandbox
from test_addons import ModelSetup, probes, settings
from test_agent_loop import call, drain, user
from test_api import (  # noqa: F401  (harness is a fixture)
    CSRF,
    harness,
    levels,
    new_chat,
    put_level,
    read_events,
    registry_with,
    say,
    signed_in,
)

from hearthkeep import audit, cli, permissions
from hearthkeep.addons import ABOUT, Addons
from hearthkeep.agent.loop import NOT_READY, AgentLoop
from hearthkeep.approvals import ApprovalStore
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.permissions import ActionClass, ToolLevel, set_level
from hearthkeep.providers.chat import StreamDone, TextDelta, ToolCallRequest
from hearthkeep.tools.base import ToolRegistry, ToolResult


class StubTool:
    """A look-only tool with no `is_available` of its own: only the registry gate can deny it."""

    level_key = "github"
    description = "Read a repo."
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, name="repo_read"):
        self.name = name
        self.runs = 0

    def classify(self, args):
        return ActionClass.LOOK

    def summarize(self, args):
        return "Read a repo"

    async def run(self, args, ctx):
        self.runs += 1
        return ToolResult(ok=True, content="read")


class Gate:
    def __init__(self, ready=True):
        self.ready = ready

    def tool_ready(self, name):
        return self.ready


class Exploding:
    def tool_ready(self, name):
        raise RuntimeError("boom")


def make_loop(tmp_path, conn, client, registry):
    approvals = ApprovalStore(conn, ttl_minutes=10, clock=lambda: datetime.now(UTC))
    return AgentLoop(client, registry, approvals, conn, Settings(data_dir=tmp_path))


async def test_registry_gate_hides_tool_and_loop_denies(tmp_path):
    conn = connect(tmp_path / "hk.db")
    registry = ToolRegistry()
    tool = StubTool()
    registry.register(tool)
    assert registry.available(conn, True) == [tool]  # no gate: offered
    gate = Gate()
    registry.addons = gate
    assert registry.available(conn, True) == [tool]
    gate.ready = False
    assert registry.available(conn, True) == [] and not registry.is_available(tool)
    registry.addons = Exploding()  # a gate that raises counts as not available
    assert registry.available(conn, True) == [] and not registry.is_available(tool)

    registry.addons = gate  # not ready: a call that arrives anyway is denied, never run
    set_level(conn, "github", ToolLevel.ALLOWED)
    client = FakeChatClient(
        [
            [call("repo_read", {}), StreamDone("tool_calls")],
            [TextDelta("ok"), StreamDone("stop")],
        ]
    )
    events = await drain(make_loop(tmp_path, conn, client, registry), user("go"))
    assert tool.runs == 0 and client.calls[0]["tools"] == []
    assert [e for e in events if e["type"] == "step_finished"][0]["ok"] is False
    answer = [m for m in client.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert json.loads(answer) == {"error": NOT_READY}
    conn.close()


async def test_token_removed_mid_round_denies_github_call(tmp_path):
    conn = connect(tmp_path / "hk.db")
    token = {"saved": True}
    gate = Addons(
        settings(tmp_path),
        ModelSetup("external", "m"),
        probes(token_saved=lambda: token["saved"]),
    )
    await gate.refresh()
    registry = ToolRegistry()
    tool = StubTool()
    registry.register(tool)
    registry.addons = gate
    set_level(conn, "github", ToolLevel.ALLOWED)

    class TokenRemoved(FakeChatClient):
        async def stream(self, messages, tools, think):
            async for event in super().stream(messages, tools, think):
                token["saved"] = False  # removed in Settings while the model is answering
                yield event

    client = TokenRemoved(
        [
            [call("repo_read", {}), StreamDone("tool_calls")],
            [TextDelta("ok"), StreamDone("stop")],
        ]
    )
    events = await drain(make_loop(tmp_path, conn, client, registry), user("go"))
    assert [t["function"]["name"] for t in client.calls[0]["tools"]] == ["repo_read"]
    assert tool.runs == 0
    assert [e for e in events if e["type"] == "step_finished"][0]["ok"] is False
    answer = [m for m in client.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert json.loads(answer) == {"error": NOT_READY}
    denied = [e for e in audit.list_events(conn) if e["event"] == "tool.denied"]
    assert denied[0]["detail"]["tool"] == "repo_read"
    assert client.calls[1]["tools"] == []  # and it is not offered again
    conn.close()


def python_call():
    args = json.dumps({"script": "print(1)", "purpose": "Try"})
    return [ToolCallRequest("c1", "python", args), StreamDone("tool_calls")]


def offered_tool_names(chat, round):
    return [t["function"]["name"] for t in chat.calls[round]["tools"]]


def test_python_not_offered_after_docker_stops(harness, tmp_path):
    sandbox = FakeSandbox(tmp_path / "fake")  # Docker up
    client = signed_in(
        harness,
        [
            [TextDelta("hi"), StreamDone("stop")],
            python_call(),
            [TextDelta("done"), StreamDone("stop")],
        ],
        sandbox=sandbox,
    )
    chat_id = new_chat(client)
    turn = say(client, chat_id, "hello").json()["turn_id"]
    read_events(client, chat_id, turn)
    assert "python" in offered_tool_names(harness.chat, 0)

    sandbox.docker = False
    assert client.post("/api/addons/refresh", headers=CSRF).status_code == 200
    turn = say(client, chat_id, "run it").json()["turn_id"]
    read_events(client, chat_id, turn)
    assert "python" not in offered_tool_names(harness.chat, 1)
    assert sandbox.calls == []
    [(content,)] = harness.query("SELECT content FROM messages WHERE role = 'tool'")
    assert content == json.dumps({"error": NOT_READY})


@pytest.mark.real_levels
def test_stored_levels_for_removed_tools_are_harmless(harness):
    c = harness.conn()
    for key, level in (("wallet", "off"), ("news", "allowed"), ("demo", "ask")):
        set_level(c, key, ToolLevel(level))
    c.commit()
    c.close()
    client = signed_in(harness, [[TextDelta("hi"), StreamDone("stop")]], registry=registry_with())
    keys = [row["tool"] for row in client.get("/api/settings/tools").json()]
    assert not {"wallet", "news", "demo"} & set(keys) and "images" in keys
    assert put_level(client, "wallet", "allowed").status_code == 404
    rows = harness.query("SELECT COUNT(*) FROM tool_levels WHERE tool IN ('wallet','news','demo')")
    assert rows == [(3,)]
    chat_id = new_chat(client)
    turn = say(client, chat_id, "hi").json()["turn_id"]
    assert any(e["event"] == "turn_done" for e in read_events(client, chat_id, turn))


def test_tools_rows_carry_addon_state(harness):
    client = signed_in(harness)  # no sandbox: Docker is down
    python = levels(client)["python"]
    assert python["addon"]["id"] == "analysis" and python["addon"]["ready"] is False
    assert python["addon"]["short"] == "Needs Docker"
    assert set(python["addon"]) == {"id", "state", "ready", "short", "message"}
    assert levels(client)["send"]["addon"] is None


def test_images_row_without_comfyui_has_a_description(harness):
    client = signed_in(harness)
    images = levels(client)["images"]
    assert images["tools"] == [] and images["description"] == ABOUT["images"][1]
    assert images["addon"]["state"] == "not_set_up"


def test_build_registry_has_no_demo_tools():
    assert [t.name for t in cli.build_registry().all()] == []


@pytest.mark.real_levels
def test_default_levels_have_no_placeholders():
    assert not {"wallet", "news", "demo"} & set(permissions.DEFAULT_LEVELS)


def test_demo_tools_live_in_the_tests():
    assert not (Path(cli.__file__).parent / "tools" / "demo.py").exists()
