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

With --images (the image model must be set up in <data>/models.toml; see docs/models.md):
  3. "Create one square image of a red apple on a white table" -> an `attachments` event
     arrives and GET /api/images/{id}/file returns a PNG            (prints `OK image`)
  4. a plain chat question -> a reply arrives, so the chat model came back after the GPU
     swapped to the image model and back                            (prints `OK chat after image`)

With --edit (implies --images):
  5. the generated PNG is uploaded to /api/uploads, then "Edit my photo {id}: make the apple
     green" with the line `Attached photo: {id}` -> a new PNG image attachment arrives
                                                                    (prints `OK edit`)

Each check prints OK; any failure prints FAIL and exits non-zero. It only talks HTTP.
Image steps get up to 300 s each, because the GPU has to swap models.
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
IMAGE_STEP_SECONDS = 300.0  # a whole image step, model swaps included
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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


def stream_turn(
    client: Client,
    chat_id: str,
    turn_id: str,
    stop_on: str | None = None,
    deadline: float | None = None,
):
    """Yield (event, data) pairs from the turn's SSE stream. With a `deadline` (seconds for
    the whole stream) a stream that goes on for longer fails."""
    url = f"/api/chats/{chat_id}/turns/{turn_id}/events"
    ends = None if deadline is None else time.monotonic() + deadline
    with client.http.stream("GET", url) as response:
        check(response.status_code == 200, f"events failed: HTTP {response.status_code}")
        event, data = None, None
        for line in response.iter_lines():
            # Checked on every line, keep-alive comments included, so a stalled turn that
            # keeps the connection alive still times out.
            check(ends is None or time.monotonic() < ends, f"no result after {deadline:.0f}s")
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


def run_image_turn(client: Client, text: str) -> tuple[list[dict], str]:
    """Send `text` in a new chat and return (image attachments, reply text) once it ends."""
    chat_id, turn_id = ask(client, text)
    images: list[dict] = []
    reply: list[str] = []
    for event, data in stream_turn(client, chat_id, turn_id, deadline=IMAGE_STEP_SECONDS):
        if event == "attachments":
            images += [i for i in data.get("items", []) if i.get("kind") == "image"]
        elif event == "text_delta":
            reply.append(data["text"])
        elif event == "turn_error":
            raise SmokeError(f"turn failed: {data.get('message')}")
    return images, "".join(reply)


def fetch_png(client: Client, image_id: str) -> bytes:
    response = client.get(f"/api/images/{image_id}/file")
    check(response.status_code == 200, f"image file failed: HTTP {response.status_code}")
    check(response.content.startswith(PNG_SIGNATURE), "the image file is not a PNG")
    return response.content


def check_image(client: Client, state: dict) -> None:
    images, _ = run_image_turn(client, "Create one square image of a red apple on a white table")
    check(bool(images), "no image attachment arrived")
    state["image_id"] = images[0]["id"]
    state["png"] = fetch_png(client, state["image_id"])


def check_chat_after_image(client: Client, state: dict) -> None:
    chat_id, turn_id = ask(client, "In one short sentence, what is a hearth?")
    reply, error = [], None
    for event, data in stream_turn(client, chat_id, turn_id, deadline=IMAGE_STEP_SECONDS):
        if event == "text_delta":
            reply.append(data["text"])
        elif event == "turn_error":
            error = data.get("message")
    check(error is None, f"turn failed: {error}")
    check(bool("".join(reply).strip()), "the chat model sent no reply after the image")


def check_edit(client: Client, state: dict) -> None:
    png = state.get("png")
    check(png is not None, "no generated image to edit (the image step did not pass)")
    uploaded = client.http.post(
        "/api/uploads", files={"file": ("apple.png", png, "image/png")}, headers=client.csrf
    )
    check(uploaded.status_code == 201, f"upload failed: HTTP {uploaded.status_code}")
    photo_id = uploaded.json()["id"]
    images, _ = run_image_turn(
        client, f"Edit my photo {photo_id}: make the apple green\nAttached photo: {photo_id}"
    )
    check(bool(images), "no edited image arrived")
    check(images[0]["id"] != state["image_id"], "the edit returned the original image")
    fetch_png(client, images[0]["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # 127.0.0.1 avoids slow IPv6-first "localhost" lookups; the server only listens on IPv4.
    parser.add_argument("--base", default="http://127.0.0.1:8765", help="server base URL")
    parser.add_argument(
        "--origin", default="http://localhost:8765", help="the server's public_origin"
    )
    parser.add_argument("--code", default=None, help="first-device enrollment code")
    parser.add_argument("--images", action="store_true", help="also make an image, then chat again")
    parser.add_argument(
        "--edit",
        action="store_true",
        help="also edit the image as an uploaded photo (implies --images)",
    )
    args = parser.parse_args(argv)
    args.images = args.images or args.edit

    client = Client(args.base, args.origin.rstrip("/"))
    try:
        health = client.get("/api/health").json()
        check(health.get("chat_model_reachable") is True, f"chat model not reachable: {health}")
        sign_in(client, args.code)
    except (SmokeError, httpx.HTTPError, ValueError, KeyError) as exc:
        print(f"FAIL setup: {exc if isinstance(exc, SmokeError) else type(exc).__name__}")
        return 1

    state: dict = {}
    # (name, run, the step it needs to have passed)
    steps = [
        ("clock tool", lambda: check_clock(client), None),
        ("note approval", lambda: check_note_approval(client), None),
    ]
    if args.images:
        steps += [
            ("image", lambda: check_image(client, state), None),
            ("chat after image", lambda: check_chat_after_image(client, state), "image"),
        ]
    if args.edit:
        steps.append(("edit", lambda: check_edit(client, state), "image"))
    failed_steps: set[str] = set()
    for name, run, needs in steps:
        if needs in failed_steps:
            failed_steps.add(name)
            print(f"SKIP {name} (the {needs} step failed)")
            continue
        started = time.monotonic()
        try:
            run()
        except httpx.TimeoutException:
            failed_steps.add(name)
            print(f"FAIL {name}: no result after {TIMEOUT.read:.0f}s")
        except (SmokeError, httpx.HTTPError, ValueError, KeyError) as exc:
            failed_steps.add(name)
            detail = (
                str(exc)
                if isinstance(exc, (SmokeError, httpx.HTTPError))
                else (f"unexpected response ({type(exc).__name__})")
            )
            print(f"FAIL {name}: {detail}".splitlines()[0])
        else:
            print(f"OK {name} ({time.monotonic() - started:.1f}s)")
    return 1 if failed_steps else 0


if __name__ == "__main__":
    sys.exit(main())
