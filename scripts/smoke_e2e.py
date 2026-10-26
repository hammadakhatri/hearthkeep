"""End-to-end smoke test against a running Hearthkeep and the real local chat model.

Start the server in development mode first (localhost only):

    cd server
    uv run hearthkeep serve --data <data> --insecure-dev-auth

then, in another terminal (pass the enrollment code the server printed on a fresh data folder;
on later runs it signs in with the already-enrolled dev device instead):

    cd server
    uv run python ../scripts/smoke_e2e.py --code <CODE>

Checks:
  1. "What time is it?"      -> the `clock` tool ran and the reply contains a time.
  2. "Add a note saying hello" -> an approval for the `note` tool appears (it is then declined).

Each check prints OK; any failure prints FAIL and exits non-zero. It only talks HTTP.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time

import httpx

TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
TIMEOUT = httpx.Timeout(300.0, connect=5.0)


class SmokeError(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


class Client:
    def __init__(self, base: str, origin: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=TIMEOUT, trust_env=False)
        self.csrf = {"X-Hearthkeep": "1", "Origin": origin}

    def post(self, path: str, body: dict | None = None) -> httpx.Response:
        return self.http.post(path, json=body, headers=self.csrf)

    def get(self, path: str) -> httpx.Response:
        return self.http.get(path)


def sign_in(client: Client, code: str | None) -> None:
    if code:
        options = client.post("/api/auth/enroll/options", {"code": code})
        check(options.status_code == 200, f"enroll options failed: {options.text}")
        body = options.json()
        verified = client.post(
            "/api/auth/enroll/verify",
            {
                "code": code,
                "challenge_id": body["challenge_id"],
                "response": {"id": "smoke-" + secrets.token_hex(6)},
                "device_name": "smoke test",
            },
        )
        check(verified.status_code == 201, f"enroll verify failed: {verified.text}")
        return
    options = client.post("/api/auth/login/options")
    check(
        options.status_code == 200,
        f"login options failed ({options.text}); pass --code on a fresh data folder",
    )
    body = options.json()
    allowed = body["options"].get("allowCredentials") or []
    check(bool(allowed), "no enrolled device to sign in with")
    first = allowed[0]
    cred_id = first["id"] if isinstance(first, dict) else first
    verified = client.post(
        "/api/auth/login/verify",
        {"challenge_id": body["challenge_id"], "response": {"id": cred_id}},
    )
    check(verified.status_code == 200, f"login failed: {verified.text}")


def stream_turn(client: Client, chat_id: str, turn_id: str, stop_on: str | None = None):
    """Yield (event, data) pairs from the turn's SSE stream."""
    url = f"/api/chats/{chat_id}/turns/{turn_id}/events"
    with client.http.stream("GET", url) as response:
        check(response.status_code == 200, f"events failed: HTTP {response.status_code}")
        event, data = None, None
        for line in response.iter_lines():
            if line.startswith(":"):
                continue
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line == "" and event is not None:
                yield event, data
                if event in ("turn_done", "turn_error") or event == stop_on:
                    return
                event, data = None, None


def ask(client: Client, text: str) -> tuple[str, str]:
    chat = client.post("/api/chats", {"think": False})
    check(chat.status_code == 201, f"create chat failed: {chat.text}")
    chat_id = chat.json()["id"]
    posted = client.post(f"/api/chats/{chat_id}/messages", {"text": text})
    check(posted.status_code == 202, f"post message failed: {posted.text}")
    return chat_id, posted.json()["turn_id"]


def check_clock(client: Client) -> None:
    chat_id, turn_id = ask(client, "What time is it?")
    tools, reply, error = [], [], None
    for event, data in stream_turn(client, chat_id, turn_id):
        if event == "step_started":
            tools.append(data["tool"])
        elif event == "text_delta":
            reply.append(data["text"])
        elif event == "turn_error":
            error = data.get("message")
    text = "".join(reply)
    check(error is None, f"turn failed: {error}")
    check("clock" in tools, f"the clock tool did not run (tools: {tools})")
    check(bool(TIME_RE.search(text)), f"the reply has no time in it: {text!r}")


def check_note_approval(client: Client) -> None:
    chat_id, turn_id = ask(client, "Add a note saying hello")
    approval_id = None
    for event, data in stream_turn(client, chat_id, turn_id, stop_on="approval_required"):
        if event == "approval_required":
            check(data["tool"] == "note", f"approval is for {data['tool']}, not note")
            approval_id = data["approval_id"]
        elif event == "turn_error":
            raise SmokeError(f"turn failed: {data.get('message')}")
    check(approval_id is not None, "no approval appeared")
    pending = client.get("/api/approvals?status=pending").json()
    check(any(a["id"] == approval_id for a in pending), "approval missing from /api/approvals")
    declined = client.post(f"/api/approvals/{approval_id}", {"approve": False})
    check(declined.status_code == 200, f"declining failed: {declined.text}")
    for _ in stream_turn(client, chat_id, turn_id):
        pass  # let the turn finish cleanly


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # 127.0.0.1 avoids slow IPv6-first "localhost" lookups; the server only listens on IPv4.
    parser.add_argument("--base", default="http://127.0.0.1:8765", help="server base URL")
    parser.add_argument(
        "--origin", default="http://localhost:8765", help="the server's public_origin"
    )
    parser.add_argument("--code", default=None, help="first-device enrollment code")
    args = parser.parse_args(argv)

    client = Client(args.base, args.origin.rstrip("/"))
    try:
        health = client.get("/api/health").json()
        check(health.get("chat_model_reachable") is True, f"chat model not reachable: {health}")
        sign_in(client, args.code)
    except (SmokeError, httpx.HTTPError) as exc:
        print(f"FAIL setup: {exc}")
        return 1

    failed = False
    for name, run in (("clock tool", check_clock), ("note approval", check_note_approval)):
        started = time.monotonic()
        try:
            run(client)
        except (SmokeError, httpx.HTTPError) as exc:
            failed = True
            print(f"FAIL {name}: {exc}")
        else:
            print(f"OK   {name} ({time.monotonic() - started:.1f}s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
