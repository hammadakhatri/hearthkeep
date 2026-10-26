import asyncio
import io
import json
import threading
import time

import pytest
from fakes import FakeChatClient, FakeVerifier
from fastapi.testclient import TestClient

from hearthkeep import audit, cli
from hearthkeep.api.app import DB_FILE, create_app
from hearthkeep.api.common import RateLimiter
from hearthkeep.api.routes_chats import sse_events
from hearthkeep.auth.credentials import CredentialStore, complete_enrollment
from hearthkeep.auth.enrollment import EnrollmentCodes
from hearthkeep.auth.sessions import public_id
from hearthkeep.auth.webauthn import NewCredential, b64url_encode
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.permissions import ActionClass
from hearthkeep.providers.chat import StreamDone, TextDelta, ToolCallRequest
from hearthkeep.tools.base import ToolRegistry, ToolResult
from hearthkeep.tools.demo import ClockTool, NoteTool

ORIGIN = "http://localhost:8765"
CSRF = {"X-Hearthkeep": "1", "Origin": ORIGIN}
PK = b64url_encode(b"public-key")


class SendTool:
    """An outward tool: always needs an approval plus a passkey step-up."""

    name = "send"
    level_key = "demo"
    description = "Send a message to someone."
    parameters = {
        "type": "object",
        "properties": {"to": {"type": "string"}},
        "required": ["to"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.runs: list[dict] = []

    def classify(self, args):
        return ActionClass.OUTWARD

    def summarize(self, args):
        return f"Send a message to {args.get('to')}"

    async def run(self, args, ctx):
        self.runs.append(args)
        return ToolResult(ok=True, content="sent")


def registry_with(*tools):
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


@pytest.fixture
def harness(tmp_path):
    clients: list[TestClient] = []

    class Harness:
        data = tmp_path
        chat: FakeChatClient
        send = SendTool()

        def start(self, scripts=(), *, health=None, web_dist=None, limiter=None):
            self.chat = FakeChatClient(list(scripts))

            async def healthy():
                return True

            app = create_app(
                Settings(data_dir=tmp_path, docker_available=False),
                verifier=FakeVerifier(),
                chat_client=self.chat,
                registry=registry_with(ClockTool(), NoteTool(), self.send),
                health_check=health or healthy,
                web_dist=web_dist,
                rate_limiter=limiter,
                approval_poll_interval=0.01,
            )
            client = TestClient(app, base_url=ORIGIN)
            client.__enter__()
            clients.append(client)
            return client

        def conn(self):
            return connect(tmp_path / DB_FILE)

        def code(self, purpose="first_device"):
            c = self.conn()
            try:
                return EnrollmentCodes(c).issue(purpose, ttl_minutes=15)
            finally:
                c.close()

        def events(self):
            c = self.conn()
            try:
                return audit.list_events(c, limit=1000)
            finally:
                c.close()

        def query(self, sql, params=()):
            c = self.conn()
            try:
                return [tuple(r) for r in c.execute(sql, params).fetchall()]
            finally:
                c.close()

    yield Harness()
    for client in clients:
        client.__exit__(None, None, None)


# ---- helpers --------------------------------------------------------------------------


def enroll_options(client, code):
    return client.post("/api/auth/enroll/options", json={"code": code}, headers=CSRF)


def enroll_verify(client, code, options, cred_id="cred-1", device="laptop"):
    body = options.json()
    return client.post(
        "/api/auth/enroll/verify",
        json={
            "code": code,
            "challenge_id": body["challenge_id"],
            "response": {
                "id": cred_id,
                "public_key": PK,
                "challenge": body["options"]["challenge"],
            },
            "device_name": device,
        },
        headers=CSRF,
    )


def enroll(client, code, cred_id="cred-1"):
    options = enroll_options(client, code)
    assert options.status_code == 200, options.text
    response = enroll_verify(client, code, options, cred_id)
    assert response.status_code == 201, response.text
    return response


def signed_in(h, scripts=()):
    client = h.start(scripts)
    enroll(client, h.code())
    return client


def assertion(cred_id, options, sign_count=0):
    return {"id": cred_id, "challenge": options["challenge"], "sign_count": sign_count}


def login(client, cred_id="cred-1", sign_count=0):
    options = client.post("/api/auth/login/options", headers=CSRF)
    assert options.status_code == 200, options.text
    body = options.json()
    return client.post(
        "/api/auth/login/verify",
        json={
            "challenge_id": body["challenge_id"],
            "response": assertion(cred_id, body["options"], sign_count),
        },
        headers=CSRF,
    )


def step_up(client, purpose, approval_id=None, cred_id="cred-1"):
    payload = {"purpose": purpose, "approval_id": approval_id}
    options = client.post("/api/auth/stepup/options", json=payload, headers=CSRF)
    assert options.status_code == 200, options.text
    body = options.json()
    verified = client.post(
        "/api/auth/stepup/verify",
        json={
            **payload,
            "challenge_id": body["challenge_id"],
            "response": assertion(cred_id, body["options"]),
        },
        headers=CSRF,
    )
    assert verified.status_code == 200, verified.text
    return verified.json()["step_up_token"]


def new_chat(client):
    response = client.post("/api/chats", json={"think": False}, headers=CSRF)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def say(client, chat_id, text):
    return client.post(f"/api/chats/{chat_id}/messages", json={"text": text}, headers=CSRF)


def parse_sse(text):
    frames = []
    for block in text.strip().split("\n\n"):
        fields = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            fields[key] = value
        if fields:
            frames.append(
                {
                    "id": int(fields["id"]),
                    "event": fields["event"],
                    "data": json.loads(fields["data"]),
                }
            )
    return frames


def read_events(client, chat_id, turn_id, after=None, headers=None):
    url = f"/api/chats/{chat_id}/turns/{turn_id}/events"
    if after is not None:
        url += f"?after={after}"
    response = client.get(url, headers=headers or {})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


def wait_for_approval(client, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = client.get("/api/approvals?status=pending").json()
        if pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError("no approval appeared")


def send_turn():
    return [
        [ToolCallRequest("c1", "send", '{"to": "sam"}'), StreamDone("tool_calls")],
        [TextDelta("Sent."), StreamDone("stop")],
    ]


def note_turn():
    return [
        [ToolCallRequest("c1", "note", '{"text": "hello"}'), StreamDone("tool_calls")],
        [TextDelta("Saved."), StreamDone("stop")],
    ]


def event_names(h):
    return [e["event"] for e in h.events()]


# ---- authentication ---------------------------------------------------------------------


def test_unauthenticated_requests_401(harness):
    client = harness.start()
    for path in (
        "/api/chats",
        "/api/approvals",
        "/api/settings/tools",
        "/api/audit",
        "/api/devices",
        "/api/auth/session",
        "/api/chats/x/messages",
        "/api/chats/x/turns/t/events",
    ):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json() == {"reason": "unauthenticated"}
    for method, path, body in (
        ("POST", "/api/chats", {"think": False}),
        ("POST", "/api/chats/x/messages", {"text": "hi"}),
        ("POST", "/api/approvals/x", {"approve": True}),
        ("PUT", "/api/settings/tools/demo", {"level": "off"}),
        ("POST", "/api/auth/logout", None),
        ("POST", "/api/auth/stepup/options", {"purpose": "settings"}),
        ("POST", "/api/devices/add-code", {"step_up_token": "x"}),
        ("DELETE", "/api/devices/cred-1", None),
    ):
        response = client.request(method, path, json=body, headers=CSRF)
        assert response.status_code == 401, path
    client.cookies.set("hk_session", "forged")
    assert client.get("/api/chats").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_enroll_first_device_then_code_reuse_rejected(harness):
    client = harness.start()
    code = harness.code()
    response = enroll(client, code)
    assert response.json()["device"]["id"] == "cred-1"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("hk_session=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert "secure" not in lowered  # localhost origin
    assert client.get("/api/chats").status_code == 200
    assert client.get("/api/devices").json()[0]["current"] is True

    reuse = enroll_options(client, code)
    assert reuse.status_code == 400 and reuse.json() == {"reason": "invalid_code"}
    assert harness.query("SELECT id FROM credentials") == [("cred-1",)]
    assert "enrollment.failed" in event_names(harness)
    assert code not in json.dumps(harness.events())


def test_first_device_enroll_blocked_after_enrolment(harness):
    client = harness.start()
    first, second = harness.code(), harness.code()
    early = enroll_options(client, second)  # options obtained before anyone enrolled
    assert early.status_code == 200
    enroll(client, first)

    assert enroll_options(client, second).json() == {"reason": "invalid_code"}
    late = enroll_verify(client, second, early, cred_id="cred-2")
    assert late.status_code == 400 and late.json() == {"reason": "invalid_code"}
    assert harness.query("SELECT id FROM credentials") == [("cred-1",)]


def test_concurrent_first_device_enrollments_only_one_credential(harness):
    harness.start()
    codes = [harness.code(), harness.code(), harness.code(), harness.code()]
    barrier = threading.Barrier(len(codes))
    results = []

    def attempt(index, code):
        conn = harness.conn()
        try:
            barrier.wait()
            cred = complete_enrollment(
                conn,
                EnrollmentCodes(conn),
                CredentialStore(conn),
                code,
                "first_device",
                NewCredential(id=f"cred-{index}", public_key=b"k", sign_count=0),
                "device",
            )
            results.append(cred)
        finally:
            conn.close()

    threads = [threading.Thread(target=attempt, args=(i, c)) for i, c in enumerate(codes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
    assert len(harness.query("SELECT id FROM credentials")) == 1
    assert len(harness.query("SELECT 1 FROM enrollment_codes WHERE used_at IS NOT NULL")) == 1


def test_enrollment_and_login_rate_limited(harness):
    client = harness.start()
    for _ in range(10):
        assert enroll_options(client, "WRONGCODE").status_code == 400
    limited = enroll_options(client, "WRONGCODE")
    assert limited.status_code == 429 and limited.json() == {"reason": "rate_limited"}
    assert event_names(harness).count("enrollment.failed") == 10
    for _ in range(10):
        assert client.post("/api/auth/login/options", headers=CSRF).status_code == 409
    assert client.post("/api/auth/login/options", headers=CSRF).json() == {"reason": "rate_limited"}


def test_rate_limiter_global_cap_across_clients():
    now = [0.0]
    limiter = RateLimiter(per_client=10, global_limit=3, clock=lambda: now[0])
    assert [limiter.hit("login", f"ip{i}") for i in range(4)] == [True, True, True, False]
    assert limiter.hit("enroll", "ip0") is True  # groups are independent
    now[0] = 61.0
    assert limiter.hit("login", "ip9") is True


def test_login_options_409_when_no_devices(harness):
    client = harness.start()
    response = client.post("/api/auth/login/options", headers=CSRF)
    assert response.status_code == 409
    assert response.json() == {"reason": "no_devices"}


def test_login_logout_and_sign_count_persisted(harness):
    client = signed_in(harness)
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200
    assert client.get("/api/chats").status_code == 401

    options = client.post("/api/auth/login/options", headers=CSRF).json()
    assert options["options"]["allowCredentials"] == ["cred-1"]
    assert login(client, sign_count=5).status_code == 200
    assert client.get("/api/chats").status_code == 200
    assert harness.query("SELECT sign_count FROM credentials") == [(5,)]

    replay = login(client, sign_count=5)  # a repeated non-zero count looks cloned
    assert replay.status_code == 400 and replay.json() == {"reason": "verification_failed"}
    assert "login.failed" in event_names(harness)


def test_login_challenge_single_use(harness):
    client = signed_in(harness)
    body = client.post("/api/auth/login/options", headers=CSRF).json()
    payload = {
        "challenge_id": body["challenge_id"],
        "response": assertion("cred-1", body["options"]),
    }
    assert client.post("/api/auth/login/verify", json=payload, headers=CSRF).status_code == 200
    again = client.post("/api/auth/login/verify", json=payload, headers=CSRF)
    assert again.status_code == 400 and again.json() == {"reason": "challenge_invalid"}


def test_add_device_requires_step_up_and_excludes_existing(harness):
    client = signed_in(harness)
    denied = client.post("/api/devices/add-code", json={"step_up_token": "nope"}, headers=CSRF)
    assert denied.status_code == 403 and denied.json() == {"reason": "step_up_required"}
    wrong_purpose = step_up(client, "settings")
    denied = client.post(
        "/api/devices/add-code", json={"step_up_token": wrong_purpose}, headers=CSRF
    )
    assert denied.status_code == 403

    token = step_up(client, "add_device")
    response = client.post("/api/devices/add-code", json={"step_up_token": token}, headers=CSRF)
    assert response.status_code == 201
    code = response.json()["code"]

    options = enroll_options(client, code)
    assert options.json()["options"]["excludeCredentials"] == ["cred-1"]
    enroll_verify(client, code, options, cred_id="cred-2", device="phone")
    devices = client.get("/api/devices").json()
    assert [(d["id"], d["current"]) for d in devices] == [("cred-1", False), ("cred-2", True)]


def test_revoked_device_not_offered_or_accepted(harness):
    client = signed_in(harness)
    token = step_up(client, "add_device")
    code = client.post("/api/devices/add-code", json={"step_up_token": token}, headers=CSRF)
    enroll(client, code.json()["code"], cred_id="cred-2")  # now signed in as cred-2

    assert client.delete("/api/devices/cred-1", headers=CSRF).status_code == 200
    assert client.delete("/api/devices/cred-1", headers=CSRF).status_code == 404
    options = client.post("/api/auth/login/options", headers=CSRF).json()
    assert options["options"]["allowCredentials"] == ["cred-2"]
    assert login(client, "cred-1").json() == {"reason": "verification_failed"}
    assert login(client, "cred-2").status_code == 200


def test_step_up_bound_to_session_credential_and_purpose(harness):
    client = signed_in(harness)
    payload = {"purpose": "settings", "approval_id": None}
    body = client.post("/api/auth/stepup/options", json=payload, headers=CSRF).json()
    assert body["options"]["allowCredentials"] == ["cred-1"]
    # the challenge was issued for "settings": it cannot be redeemed for another purpose
    swapped = client.post(
        "/api/auth/stepup/verify",
        json={
            "purpose": "add_device",
            "challenge_id": body["challenge_id"],
            "response": assertion("cred-1", body["options"]),
        },
        headers=CSRF,
    )
    assert swapped.status_code == 400 and swapped.json() == {"reason": "challenge_invalid"}
    bad = client.post("/api/auth/stepup/options", json={"purpose": "approval"}, headers=CSRF)
    assert bad.status_code == 422
    missing = client.post(
        "/api/auth/stepup/options", json={"purpose": "approval", "approval_id": "x"}, headers=CSRF
    )
    assert missing.status_code == 404


# ---- chats and SSE ----------------------------------------------------------------------


def test_post_message_streams_events_over_sse(harness):
    script = [[TextDelta("Hello "), TextDelta("there."), StreamDone("stop")]]
    client = signed_in(harness, script)
    chat_id = new_chat(client)
    response = say(client, chat_id, "hi")
    assert response.status_code == 202
    turn_id = response.json()["turn_id"]

    frames = read_events(client, chat_id, turn_id)
    assert [f["event"] for f in frames] == ["text_delta", "text_delta", "turn_done"]
    assert [f["id"] for f in frames] == [1, 2, 3]
    assert frames[0]["data"] == {"text": "Hello "}

    assert [f["id"] for f in read_events(client, chat_id, turn_id, after=1)] == [2, 3]
    resumed = read_events(client, chat_id, turn_id, after=0, headers={"Last-Event-ID": "2"})
    assert [f["id"] for f in resumed] == [3]

    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hi"),
        ("assistant", "Hello there."),
    ]
    chats = client.get("/api/chats").json()
    assert chats[0]["id"] == chat_id and chats[0]["title"] == "hi"
    assert client.get(f"/api/chats/{chat_id}/turns/unknown/events").status_code == 404
    assert client.get("/api/chats/nope/messages").status_code == 404


def test_post_message_while_turn_running_409(harness):
    client = signed_in(harness, note_turn())
    chat_id = new_chat(client)
    assert say(client, chat_id, "note hello").status_code == 202
    wait_for_approval(client)
    busy = say(client, chat_id, "again")
    assert busy.status_code == 409 and busy.json() == {"reason": "turn_in_progress"}
    assert say(client, chat_id, "   ").status_code == 422


async def test_sse_keepalive_comment_while_idle():
    release = asyncio.Event()

    class SlowRunner:
        async def subscribe(self, chat_id, turn_id, after_seq=0):
            await release.wait()
            yield {"type": "turn_done", "seq": after_seq + 1}

    frames = []

    async def consume():
        async for frame in sse_events(SlowRunner(), "c", "t", 4, keepalive=0.01):
            frames.append(frame)
            if len(frames) == 3:
                release.set()

    await asyncio.wait_for(consume(), timeout=5)
    assert frames[:3] == [": keep-alive\n\n"] * 3
    assert frames[-1] == "id: 5\nevent: turn_done\ndata: {}\n\n"


# ---- approvals --------------------------------------------------------------------------


def test_outward_approval_without_step_up_403(harness):
    client = signed_in(harness, send_turn())
    chat_id = new_chat(client)
    say(client, chat_id, "send it")
    approval = wait_for_approval(client)
    assert approval["requires_step_up"] is True and approval["tool"] == "send"

    response = client.post(f"/api/approvals/{approval['id']}", json={"approve": True}, headers=CSRF)
    assert response.status_code == 403 and response.json() == {"reason": "step_up_required"}
    wrong = step_up(client, "settings")  # a token for another purpose does not count
    response = client.post(
        f"/api/approvals/{approval['id']}",
        json={"approve": True, "step_up_token": wrong},
        headers=CSRF,
    )
    assert response.status_code == 403
    assert harness.send.runs == []
    assert client.get("/api/approvals").json()[0]["status"] == "pending"


def test_outward_approval_with_step_up_runs_tool(harness):
    client = signed_in(harness, send_turn())
    chat_id = new_chat(client)
    turn_id = say(client, chat_id, "send it").json()["turn_id"]
    approval = wait_for_approval(client)

    token = step_up(client, "approval", approval["id"])
    response = client.post(
        f"/api/approvals/{approval['id']}",
        json={"approve": True, "step_up_token": token},
        headers=CSRF,
    )
    assert response.status_code == 200 and response.json()["status"] == "approved"
    frames = read_events(client, chat_id, turn_id)
    assert frames[-1]["event"] == "turn_done"
    assert harness.send.runs == [{"to": "sam"}]
    assert client.get("/api/approvals").json() == []

    session_cookie = client.cookies.get("hk_session")
    stored = harness.query("SELECT resolved_by_session FROM approvals")
    assert stored == [(public_id(session_cookie),)]
    assert session_cookie not in json.dumps(harness.events())


def test_double_resolve_returns_409(harness):
    client = signed_in(harness, note_turn())
    chat_id = new_chat(client)
    say(client, chat_id, "note hello")
    approval = wait_for_approval(client)
    assert approval["requires_step_up"] is False
    url = f"/api/approvals/{approval['id']}"
    assert client.post(url, json={"approve": True}, headers=CSRF).status_code == 200
    again = client.post(url, json={"approve": False}, headers=CSRF)
    assert again.status_code == 409 and again.json() == {"reason": "already_resolved"}
    missing = client.post("/api/approvals/nope", json={"approve": True}, headers=CSRF)
    assert missing.status_code == 404 and missing.json() == {"reason": "not_found"}


# ---- settings ---------------------------------------------------------------------------


def levels(client):
    return {i["tool"]: i for i in client.get("/api/settings/tools").json()}


def put_level(client, tool, level, token=None):
    body = {"level": level}
    if token:
        body["step_up_token"] = token
    return client.put(f"/api/settings/tools/{tool}", json=body, headers=CSRF)


def test_narrow_tool_level_without_step_up_ok(harness):
    client = signed_in(harness)
    demo = levels(client)["demo"]
    assert demo["level"] == "allowed" and demo["tools"] == ["clock", "note", "send"]
    response = put_level(client, "demo", "ask")
    assert response.status_code == 200 and response.json()["level"] == "ask"
    assert put_level(client, "clock", "off").json()["level"] == "off"  # by tool name
    assert levels(client)["demo"]["level"] == "off"


def test_widen_tool_level_requires_step_up(harness):
    client = signed_in(harness)
    put_level(client, "demo", "off")
    denied = put_level(client, "demo", "allowed")
    assert denied.status_code == 403 and denied.json() == {"reason": "step_up_required"}
    wrong = step_up(client, "add_device")
    assert put_level(client, "demo", "allowed", wrong).status_code == 403
    assert levels(client)["demo"]["level"] == "off"

    token = step_up(client, "settings")
    assert put_level(client, "demo", "allowed", token).status_code == 200
    assert levels(client)["demo"]["level"] == "allowed"
    assert (
        put_level(client, "demo", "allowed", token).status_code == 200
    )  # same level: not widening


def test_invalid_level_and_unknown_tool(harness):
    client = signed_in(harness)
    invalid = put_level(client, "demo", "everything")
    assert invalid.status_code == 422 and invalid.json() == {"reason": "invalid_level"}
    assert put_level(client, "no-such-tool", "off").status_code == 404
    assert put_level(client, "github", "off").status_code == 200  # a default key, not registered


def test_effective_level_reflects_missing_sandbox(harness):
    client = signed_in(harness)
    python = levels(client)["python"]
    assert python["level"] == "allowed" and python["effective_level"] == "ask"


def test_settings_changes_audited(harness):
    client = signed_in(harness)
    put_level(client, "demo", "off")
    put_level(client, "demo", "allowed")  # denied: no step-up
    token = step_up(client, "settings")
    put_level(client, "demo", "ask", token)
    events = [e for e in harness.events() if e["event"].startswith("settings.")]
    assert [(e["event"], e["detail"]) for e in reversed(events)] == [
        (
            "settings.tool_level_changed",
            {"tool": "demo", "from": "allowed", "to": "off", "step_up": False},
        ),
        (
            "settings.tool_level_denied",
            {"tool": "demo", "from": "off", "to": "allowed", "reason": "step_up_required"},
        ),
        (
            "settings.tool_level_changed",
            {"tool": "demo", "from": "off", "to": "ask", "step_up": True},
        ),
    ]
    cookie = client.cookies.get("hk_session")
    assert all(e["actor"] == f"session:{public_id(cookie)}" for e in events)
    listed = client.get("/api/audit?limit=5&q=tool_level").json()
    assert {e["event"] for e in listed} == {
        "settings.tool_level_changed",
        "settings.tool_level_denied",
    }


# ---- CSRF, headers, health, static ------------------------------------------------------


def test_state_change_without_csrf_header_rejected(harness):
    client = signed_in(harness)
    for headers in ({}, {"Origin": ORIGIN}, {"X-Hearthkeep": "1"}):
        response = client.post("/api/chats", json={}, headers=headers)
        assert response.status_code == 403 and response.json() == {"reason": "csrf"}
    evil = client.post(
        "/api/chats", json={}, headers={"X-Hearthkeep": "1", "Origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    assert client.put("/api/settings/tools/demo", json={"level": "off"}).status_code == 403
    assert client.delete("/api/devices/cred-1").status_code == 403
    no_csrf_enroll = client.post("/api/auth/enroll/options", json={"code": "X"})
    assert no_csrf_enroll.status_code == 403
    assert client.get("/api/chats").json() == []


def test_security_headers_on_every_response(harness):
    client = harness.start()
    for response in (
        client.get("/api/health"),
        client.get("/api/chats"),
        client.get("/api/nope"),
        client.post("/api/chats", json={}),
    ):
        assert response.headers["content-security-policy"] == (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_health_reports_model_reachability(harness):
    async def down():
        return False

    client = harness.start(health=down)
    assert client.get("/api/health").json() == {
        "status": "ok",
        "chat_model": "gemma-4-12b",
        "chat_model_reachable": False,
    }

    async def broken():
        raise RuntimeError("boom")

    other = harness.start(health=broken)
    assert other.get("/api/health").json()["chat_model_reachable"] is False


def test_static_spa_fallback_keeps_api_json(harness, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("private", encoding="utf-8")
    client = harness.start(web_dist=dist)
    assert client.get("/").text == "<html>app</html>"
    assert client.get("/chats/123").text == "<html>app</html>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert "private" not in client.get("/..%2Fsecret.txt").text
    missing = client.get("/api/nope")
    assert missing.status_code == 404 and missing.json() == {"reason": "not_found"}


def test_no_static_routes_without_dist(harness):
    client = harness.start(web_dist=None)
    response = client.get("/")
    assert response.status_code == 404 and response.json() == {"reason": "not_found"}


# ---- CLI --------------------------------------------------------------------------------


def write_config(data, **values):
    data.mkdir(parents=True, exist_ok=True)
    lines = [
        f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {str(v).lower()}"
        for k, v in values.items()
    ]
    (data / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_insecure_dev_auth_refused_for_non_localhost_origin(tmp_path, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cli, "run_server", lambda app, host, port: started.append((host, port)))
    remote = tmp_path / "remote"
    write_config(
        remote,
        public_origin="https://hk.example.ts.net",
        rp_id="hk.example.ts.net",
        docker_available=False,
    )
    assert cli.main(["serve", "--data", str(remote), "--insecure-dev-auth"]) == 2
    assert started == []
    assert "only allowed" in capsys.readouterr().err

    local = tmp_path / "local"
    write_config(local, docker_available=False)
    assert cli.main(["serve", "--data", str(local), "--insecure-dev-auth"]) == 0
    assert started == [("127.0.0.1", 8765)]
    captured = capsys.readouterr()
    assert "INSECURE DEV AUTH" in captured.err
    assert "First-device enrollment code:" in captured.out


def test_enroll_code_cli_only_before_first_device(tmp_path, capsys):
    data = tmp_path / "data"
    assert cli.main(["enroll-code", "--data", str(data)]) == 0
    assert "First-device enrollment code:" in capsys.readouterr().out
    conn = connect(data / DB_FILE)
    conn.execute("INSERT INTO credentials (id, public_key, sign_count) VALUES ('a', x'00', 0)")
    conn.close()
    assert cli.main(["enroll-code", "--data", str(data)]) == 1
    assert "already enrolled" in capsys.readouterr().err


def test_cli_secrets_set_from_stdin_and_list_names_only(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-local-value\n"))
    assert cli.main(["secrets", "set", "chat_api_key", "--data", str(data)]) == 0
    assert cli.main(["secrets", "list", "--data", str(data)]) == 0
    out = capsys.readouterr().out
    assert "chat_api_key" in out and "sk-local-value" not in out
    from hearthkeep.secrets import SecretStore

    assert SecretStore(data).get("chat_api_key") == "sk-local-value"
