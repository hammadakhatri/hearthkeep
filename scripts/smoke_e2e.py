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

With --analysis (Docker running and the sandbox image built: `hearthkeep sandbox build`):
  6. a 60-row CSV (date, region, amount) with planted problems -- 3 negative amounts, no North
     rows from 12 to 24 March, 2 duplicate rows, 4 dates in another format -- is uploaded to
     /api/chats/{id}/files, then "What's wrong with this file, and what happened to North in
     March?" is sent with its `Attached file:` line. An `attachments` event of kind `run`
     arrives and GET /api/runs/{id} reports status `ok` with at least one table or value
                                                                    (prints `OK analysis`)
  7. the answer is right, not just present: a `number_check` event arrives; at least one
     number is marked computed; the final reply's computed numbers include 12 and 24 (the
     North gap) or the turn's runs reported the dates 2025-03-12 and 2025-03-24; and the reply
     has no `hk.` text (hk.result belongs in the script, never in the reply)
                                      (prints `OK number check (computed N, unverified M)`)

With HK_SMOKE_GITHUB_REPO=<owner>/<repo> (a throwaway repo of yours with a README.md, ideally
with a small test suite and a uv.lock, requirements.txt or package-lock.json) and the token in the
environment variable HK_SMOKE_GITHUB_TOKEN (a fine-grained token for that one repo, with the
permissions from docs/install.md; it is read from the environment only, never from a file, and
never printed). Use a development data folder: the test saves the token in it and removes it, and
the repo it added, at the end. The GitHub tools, Docker and the repo image (`hearthkeep sandbox
build`) are needed. Passkey step-ups are done through the API the way --insecure-dev-auth allows:
  8. github setup: host Git is found, the token is saved and the repo is added (passkey step-ups)
  9. github edit: the assistant starts a hearthkeep/smoke-<hex> branch, adds a line to README.md
     and commits it; the approval cards show the line in their diff; the clone ends clean
 10. github tests: dependencies are installed (an install card needs a passkey) and the tests run
     in the sandbox; the run is recorded as passed or failed
 11. github push: the push card needs a passkey and names a head commit; the branch on GitHub is
     exactly that commit
 12. github open pr: the card needs a passkey; one pull request is open on GitHub for that commit
     and its diff holds the same changed lines as the diff on the approved card
 13. github close pr: the card needs a passkey; the pull request is closed, not merged
Without HK_SMOKE_GITHUB_REPO the GitHub steps are skipped (prints SKIP github). At the end it
closes anything left open, deletes the smoke branch on GitHub and removes the repo and the token.

Each check prints OK; any failure prints FAIL and exits non-zero. It only talks HTTP.
Image steps get up to 300 s each, because the GPU has to swap models. The analysis step gets
up to 600 s: the model reads the file, writes and runs Python, then explains.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import sys
import time

import httpx

TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b")
GAP_NUMBERS = ("12", "24")  # North has no rows from 12 to 24 March
GAP_DATES = ("2025-03-12", "2025-03-24")
TIMEOUT = httpx.Timeout(600.0, connect=5.0)
IMAGE_STEP_SECONDS = 300.0  # a whole image step, model swaps included
ANALYSIS_STEP_SECONDS = 600.0  # a whole analysis turn: reading, running Python, explaining
ANALYSIS_QUESTION = "What's wrong with this file, and what happened to North in March?"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GITHUB_STEP_SECONDS = 900.0  # a whole GitHub turn: an install can take 15 minutes
GITHUB_API = "https://api.github.com"
GITHUB_TOOLS = {"repo_read", "repo_edit", "repo_install", "repo_test", "github"}
REPO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
TOKEN_RE = re.compile(r"github_pat_[A-Za-z0-9_]{20,250}")


class SmokeError(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


class Client:
    def __init__(self, base: str, origin: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=TIMEOUT, trust_env=False)
        self.csrf = {"X-Hearthkeep": "1", "Origin": origin}
        self.cred_id: str | None = None  # the device this run signed in with

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
        client.cred_id = verified.json()["device"]["id"]
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
    client.cred_id = cred_id


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
            if ends is not None and time.monotonic() >= ends:
                raise SmokeError(f"no result after {deadline:.0f}s")
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


def make_analysis_csv() -> bytes:
    """A 60-row CSV (date, region, amount) with planted problems: 3 negative amounts, no North
    rows from 12 to 24 March, 2 duplicate rows and 4 dates in another format (DD/MM/YYYY).
    Built in memory and always the same."""
    rng = random.Random(7)

    def amount() -> str:
        return f"{rng.randint(50, 500)}.{rng.randint(0, 99):02d}"

    rows: list[list[str]] = []
    for day in range(1, 32):
        for region in ("North", "South"):
            if region == "North" and 12 <= day <= 24:
                continue  # the gap the question asks about
            rows.append([f"2025-03-{day:02d}", region, amount()])
    for day in range(1, 10):
        rows.append([f"2025-03-{day:02d}", "East", amount()])
    rows.sort(key=lambda r: r[0])  # 58 rows, in date order
    for index in (3, 17, 40):  # negative amounts
        rows[index][2] = "-" + rows[index][2]
    for index in (8, 22, 35, 50):  # dates written the other way round
        year, month, day = rows[index][0].split("-")
        rows[index][0] = f"{day}/{month}/{year}"
    for index in (5, 30):  # exact duplicates, right after the original
        rows.append(list(rows[index]))
    lines = ["date,region,amount"] + [",".join(row) for row in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def run_analysis_turn(client: Client, state: dict) -> None:
    chat = client.post("/api/chats", {"think": False})
    check(chat.status_code == 201, f"create chat failed: {chat.text}")
    chat_id = chat.json()["id"]
    uploaded = client.http.post(
        f"/api/chats/{chat_id}/files",
        files={"file": ("sales.csv", make_analysis_csv(), "text/csv")},
        headers=client.csrf,
    )
    check(uploaded.status_code == 201, f"file upload failed: HTTP {uploaded.status_code}")
    file_id = uploaded.json()["id"]
    posted = client.post(
        f"/api/chats/{chat_id}/messages",
        {"text": f"{ANALYSIS_QUESTION}\nAttached file: {file_id}"},
    )
    check(posted.status_code == 202, f"post message failed: {posted.text}")
    run_ids: list[str] = []
    checks: list[dict] = []
    for event, data in stream_turn(
        client, chat_id, posted.json()["turn_id"], deadline=ANALYSIS_STEP_SECONDS
    ):
        if event == "attachments":
            run_ids += [i["id"] for i in data.get("items", []) if i.get("kind") == "run"]
        elif event == "number_check":
            checks.append(data)
        elif event == "turn_error":
            raise SmokeError(f"turn failed: {data.get('message')}")
    state["run_ids"] = run_ids
    state["number_checks"] = checks
    state["run_values"] = []


def check_analysis(client: Client, state: dict) -> None:
    run_analysis_turn(client, state)
    run_ids = state["run_ids"]
    check(bool(run_ids), "no analysis run attachment arrived (is Docker running?)")
    problems = []
    found = False
    for run_id in run_ids:
        response = client.get(f"/api/runs/{run_id}")
        check(response.status_code == 200, f"run failed to load: HTTP {response.status_code}")
        run = response.json()
        state["run_values"] += [v for v in run.get("values") or [] if isinstance(v, dict)]
        if run.get("status") != "ok":
            problems.append(f"status {run.get('status')!r}")
        elif not (run.get("tables") or run.get("values")):
            problems.append("ok but no table or value")
        else:
            found = True
    if not found:
        raise SmokeError("no run finished ok with a result: " + ", ".join(problems))


def judge_analysis(checks: list[dict], run_values: list[dict]) -> str:
    """Whether the analysis answer is right, from the turn's number checks (the last is the
    final reply) and its runs' values. Raises SmokeError saying what is wrong."""
    check(bool(checks), "no number_check event arrived")
    marks = [m for c in checks for m in c.get("marks", [])]
    computed = sum(1 for m in marks if m.get("kind") == "computed")
    unverified = sum(1 for m in marks if m.get("kind") == "unverified")
    check(computed >= 1, f"no computed numbers (unverified {unverified})")
    final = checks[-1]
    reply = str(final.get("text") or "")
    check("hk." not in reply, "the reply quotes hk. code instead of the values")
    final_computed = [
        str(m.get("text", "")) for m in final.get("marks", []) if m.get("kind") == "computed"
    ]
    numbers_ok = all(
        any(re.search(rf"(?<!\d){n}(?!\d)", text) for text in final_computed)
        for n in GAP_NUMBERS
    )
    dates_ok = all(
        any(str(v.get("value", "")).startswith(day) for v in run_values) for day in GAP_DATES
    )
    check(
        numbers_ok or dates_ok,
        "the answer misses the North gap (12 to 24 March): neither computed in the reply nor "
        "reported by a run",
    )
    return f"(computed {computed}, unverified {unverified})"


def check_number_check(client: Client, state: dict) -> str:
    return judge_analysis(state.get("number_checks") or [], state.get("run_values") or [])


def api_error(response: httpx.Response) -> str:
    """HTTP status and the server's error code, never the body (it could echo a value)."""
    try:
        code = response.json().get("error")
    except ValueError:
        code = None
    return f"HTTP {response.status_code}" + (f" {code}" if isinstance(code, str) else "")


def step_up(
    client: Client, purpose: str, approval_id: str | None = None, target: str | None = None
) -> str:
    """A step-up token for one purpose. `--insecure-dev-auth` accepts the session's own
    credential id as the passkey response, the same way sign-in does."""
    check(client.cred_id is not None, "no credential to step up with")
    body: dict = {"purpose": purpose}
    if approval_id is not None:
        body["approval_id"] = approval_id
    if target is not None:
        body["target"] = target
    options = client.post("/api/auth/stepup/options", body)
    check(options.status_code == 200, f"step-up ({purpose}) options failed: {api_error(options)}")
    verified = client.post(
        "/api/auth/stepup/verify",
        {
            **body,
            "challenge_id": options.json()["challenge_id"],
            "response": {"id": client.cred_id},
        },
    )
    check(verified.status_code == 200, f"step-up ({purpose}) failed: {api_error(verified)}")
    return verified.json()["step_up_token"]


class GitHubSide:
    """The smoke test's own read of GitHub, with the token from the environment. Used to check
    what really reached GitHub, and to clean up the branch it made. The token is only ever sent
    as the Authorization header of a request to api.github.com."""

    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.http = httpx.Client(
            base_url=GITHUB_API,
            timeout=httpx.Timeout(30.0),
            trust_env=False,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Hearthkeep-smoke",
            },
        )

    def get(self, path: str, accept: str | None = None, **params) -> httpx.Response:
        headers = {"Accept": accept} if accept else None
        return self.http.get(f"/repos/{self.repo}{path}", params=params or None, headers=headers)

    def delete(self, path: str) -> httpx.Response:
        return self.http.delete(f"/repos/{self.repo}{path}")


def changed_lines(diff: str) -> list[str]:
    """The added and removed lines of a unified diff (file headers left out), sorted, so two
    diffs of the same change compare equal whatever their headers say."""
    lines = [
        line
        for line in diff.splitlines()
        if line[:1] in ("+", "-") and not line.startswith(("+++", "---"))
    ]
    return sorted(lines)


def github_turn(
    client: Client, chat_id: str, text: str, actions: set[str] | None = None
) -> tuple[list[str], list[dict]]:
    """Send `text` in the chat and follow the turn, approving each card the way the owner would
    through the API (with a step-up where the card needs one). Returns (tools used, approval
    events). A card for a tool or a `github` action outside what this step expects is declined
    and fails the step: the model is not trusted to stay on script."""
    posted = client.post(f"/api/chats/{chat_id}/messages", {"text": text})
    check(posted.status_code == 202, f"post message failed: {api_error(posted)}")
    tools: list[str] = []
    cards: list[dict] = []
    for event, data in stream_turn(
        client, chat_id, posted.json()["turn_id"], deadline=GITHUB_STEP_SECONDS
    ):
        if event == "step_started":
            tools.append(data["tool"])
        elif event == "approval_required":
            detail = data.get("detail") or {}
            action = (detail.get("arguments") or {}).get("action")
            expected = data["tool"] in GITHUB_TOOLS and (
                data["tool"] != "github" or action in (actions or set())
            )
            if not expected:
                client.post(f"/api/approvals/{data['approval_id']}", {"approve": False})
                raise SmokeError(f"an unexpected approval for {data['tool']} ({action})")
            body: dict = {"approve": True}
            if data.get("requires_step_up"):
                body["step_up_token"] = step_up(client, "approval", approval_id=data["approval_id"])
            resolved = client.post(f"/api/approvals/{data['approval_id']}", body)
            check(resolved.status_code == 200, f"approving failed: {api_error(resolved)}")
            cards.append(data)
        elif event == "turn_error":
            raise SmokeError(f"turn failed: {data.get('message')}")
    return tools, cards


def card_for(cards: list[dict], tool: str, action: str | None = None) -> dict:
    """The first approval card for `tool` (and `action`), or a SmokeError saying it never came."""
    for card in cards:
        arguments = (card.get("detail") or {}).get("arguments") or {}
        if card["tool"] == tool and (action is None or arguments.get("action") == action):
            return card
    raise SmokeError(f"the assistant never asked to use {tool}" + (f" {action}" if action else ""))


def check_github_setup_checked(client: Client, state: dict) -> None:
    """The setup step, after checking what it was given. Neither value is ever printed."""
    check(bool(state["gh_token"]), "HK_SMOKE_GITHUB_TOKEN is not set")
    check(bool(REPO_NAME_RE.fullmatch(state["gh_repo"])), "HK_SMOKE_GITHUB_REPO is not owner/name")
    check(
        bool(TOKEN_RE.fullmatch(state["gh_token"])),
        "HK_SMOKE_GITHUB_TOKEN is not a fine-grained token",
    )
    check_github_setup(client, state)


def check_github_setup(client: Client, state: dict) -> None:
    health = client.get("/api/github").json()
    check(health.get("git") is True, "host Git 2.31 or newer was not found")
    check(
        health.get("connected") is not True,
        "a GitHub token is already saved in this data folder: use a development data folder "
        "(the GitHub steps save and remove a token, and would replace yours)",
    )
    token = state["gh_token"]
    saved = client.http.put(
        "/api/github/token",
        json={"token": token, "step_up_token": step_up(client, "github_token")},
        headers=client.csrf,
    )
    check(saved.status_code == 200, f"saving the token failed: {api_error(saved)}")
    state["token_saved"] = True
    added = client.post(
        "/api/repos",
        {"full_name": state["gh_repo"], "step_up_token": step_up(client, "add_repo")},
    )
    if added.status_code == 201:
        state["repo_added"] = True
        repo = added.json()
    else:
        check(added.status_code == 409, f"adding the repo failed: {api_error(added)}")
        listed = client.get("/api/repos").json()["repos"]
        found = [r for r in listed if r["full_name"].lower() == state["gh_repo"].lower()]
        check(bool(found), "the repo is added already but not listed")
        repo = found[0]
    state["repo"] = repo
    chat = client.post("/api/chats", {"think": False})
    check(chat.status_code == 201, f"create chat failed: {chat.text}")
    state["gh_chat"] = chat.json()["id"]
    state["hex"] = secrets.token_hex(3)
    state["branch"] = f"hearthkeep/smoke-{state['hex']}"
    state["marker"] = f"Hearthkeep smoke test {state['hex']}"


def check_github_edit(client: Client, state: dict) -> None:
    repo, marker, hex_ = state["gh_repo"], state["marker"], state["hex"]
    tools, cards = github_turn(
        client,
        state["gh_chat"],
        f"In the GitHub repo {repo}, start a branch named smoke-{hex_}, add the line "
        f'"{marker}" at the end of README.md, and commit it with the message '
        f'"Smoke test {hex_}". Use your repo tools.',
    )
    card_for(cards, "repo_edit")
    shown = [str((c.get("detail") or {}).get("diff") or "") for c in cards]
    check(any(marker in diff for diff in shown), "no edit card showed the new line in its diff")
    listed = client.get("/api/repos").json()["repos"]
    repo_row = next((r for r in listed if r["id"] == state["repo"]["id"]), None)
    check(repo_row is not None, "the repo disappeared from /api/repos")
    check(repo_row["branch"] == state["branch"], f"the clone is on {repo_row['branch']!r}")
    check(repo_row["changed"] == [], "the edit was not committed")


def check_github_tests(client: Client, state: dict) -> str:
    tools, cards = github_turn(
        client,
        state["gh_chat"],
        f"In {state['gh_repo']}, install the dependencies if they need installing, then run "
        "the tests.",
    )
    check("repo_test" in tools, f"the tests did not run (tools: {tools})")
    for card in cards:
        if card["tool"] == "repo_install":
            check(card.get("requires_step_up") is True, "the install card did not need a passkey")
    runs = client.get(f"/api/repos/{state['repo']['id']}/runs").json()["runs"]
    check(bool(runs), "no test run was recorded")
    check(
        runs[0]["status"] in ("passed", "failed"),
        f"the test run ended {runs[0]['status']!r}, not passed or failed",
    )
    return f"({runs[0]['status']})"


def check_github_push(client: Client, state: dict) -> None:
    branch = state["branch"]
    tools, cards = github_turn(
        client,
        state["gh_chat"],
        f"Push the branch {branch} to GitHub.",
        actions={"push"},
    )
    card = card_for(cards, "github", "push")
    detail = card.get("detail") or {}
    check(card.get("requires_step_up") is True, "the push card did not need a passkey")
    head = str(detail.get("head_sha") or "")
    check(bool(re.fullmatch(r"[0-9a-f]{40,64}", head)), "the push card has no head commit")
    check(state["marker"] in str(detail.get("diff") or ""), "the push card's diff lacks the edit")
    side: GitHubSide = state["side"]
    remote = side.get(f"/branches/{branch}")
    check(remote.status_code == 200, f"the branch is not on GitHub (HTTP {remote.status_code})")
    sha = remote.json()["commit"]["sha"]
    check(sha == head, "the commit on GitHub is not the commit on the approved card")
    state["pushed"] = True
    state["head_sha"] = head


def check_github_pr(client: Client, state: dict) -> None:
    branch, hex_ = state["branch"], state["hex"]
    tools, cards = github_turn(
        client,
        state["gh_chat"],
        f'Open a pull request for {branch} titled "Smoke test {hex_}" with a one-line '
        "description.",
        actions={"open_pr"},
    )
    card = card_for(cards, "github", "open_pr")
    detail = card.get("detail") or {}
    check(card.get("requires_step_up") is True, "the pull request card did not need a passkey")
    approved = str(detail.get("diff") or "")
    check(state["marker"] in approved, "the pull request card's diff lacks the edit")
    side: GitHubSide = state["side"]
    owner = state["gh_repo"].split("/")[0]
    found = side.get("/pulls", head=f"{owner}:{branch}", state="open")
    check(found.status_code == 200, f"listing pull requests failed (HTTP {found.status_code})")
    prs = found.json()
    check(len(prs) == 1, f"expected one open pull request, found {len(prs)}")
    number = prs[0]["number"]
    state["pr"] = number
    check(prs[0]["head"]["sha"] == state["head_sha"], "the pull request is not the pushed commit")
    diff = side.get(f"/pulls/{number}", accept="application/vnd.github.diff")
    check(
        diff.status_code == 200, f"reading the pull request diff failed (HTTP {diff.status_code})"
    )
    check(
        changed_lines(diff.text) == changed_lines(approved),
        "the pull request's diff is not the diff on the approved card",
    )


def check_github_close(client: Client, state: dict) -> None:
    number = state["pr"]
    tools, cards = github_turn(
        client,
        state["gh_chat"],
        f"Close pull request number {number} without merging it.",
        actions={"close"},
    )
    card = card_for(cards, "github", "close")
    check(card.get("requires_step_up") is True, "the close card did not need a passkey")
    side: GitHubSide = state["side"]
    pr = side.get(f"/pulls/{number}")
    check(pr.status_code == 200, f"reading the pull request failed (HTTP {pr.status_code})")
    check(pr.json()["state"] == "closed", "the pull request is still open")
    check(pr.json()["merged"] is False, "the pull request was merged")
    state["pr"] = None


def github_cleanup(client: Client, state: dict) -> None:
    """Best effort, never a failure: close a pull request left open, delete the smoke branch on
    GitHub, remove the repo this run added and remove the token it saved."""
    side: GitHubSide | None = state.get("side")
    problems: list[str] = []
    try:
        if side is not None and state.get("pr"):
            side.http.patch(f"/repos/{side.repo}/pulls/{state['pr']}", json={"state": "closed"})
        if side is not None and state.get("pushed"):
            side.delete(f"/git/refs/heads/{state['branch']}")
        if state.get("repo_added"):
            repo_id = state["repo"]["id"]
            gone = client.http.request(
                "DELETE",
                f"/api/repos/{repo_id}",
                json={"step_up_token": step_up(client, "delete", target=f"repo:{repo_id}")},
                headers=client.csrf,
            )
            if gone.status_code != 200:
                problems.append(f"removing the repo: {api_error(gone)}")
        if state.get("token_saved"):
            removed = client.http.delete("/api/github/token", headers=client.csrf)
            if removed.status_code != 200:
                problems.append(f"removing the token: {api_error(removed)}")
    except (SmokeError, httpx.HTTPError) as exc:
        problems.append(exc.__class__.__name__ if not isinstance(exc, SmokeError) else str(exc))
    print("OK github cleanup" if not problems else "WARN github cleanup: " + "; ".join(problems))


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
    parser.add_argument(
        "--analysis",
        action="store_true",
        help="also analyse a CSV with planted problems, and check the number check",
    )
    args = parser.parse_args(argv)
    args.images = args.images or args.edit

    client = Client(args.base, args.origin.rstrip("/"))
    try:
        health = client.get("/api/health").json()
        check(health.get("chat_model_reachable") is True, f"chat model not reachable: {health}")
        sign_in(client, args.code)
    except (SmokeError, httpx.HTTPError, ValueError, KeyError) as exc:
        detail = exc if isinstance(exc, SmokeError) else type(exc).__name__
        print(f"FAIL setup: {detail}".splitlines()[0])
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
    if args.analysis:
        steps += [
            ("analysis", lambda: check_analysis(client, state), None),
            ("number check", lambda: check_number_check(client, state), "analysis"),
        ]
    github_repo = os.environ.get("HK_SMOKE_GITHUB_REPO", "").strip()
    github_token = os.environ.get("HK_SMOKE_GITHUB_TOKEN", "").strip()
    github_on = bool(github_repo)
    if github_on:
        state["gh_repo"], state["gh_token"] = github_repo, github_token
        if REPO_NAME_RE.fullmatch(github_repo) and TOKEN_RE.fullmatch(github_token):
            state["side"] = GitHubSide(github_repo, github_token)
        steps += [
            ("github setup", lambda: check_github_setup_checked(client, state), None),
            ("github edit", lambda: check_github_edit(client, state), "github setup"),
            ("github tests", lambda: check_github_tests(client, state), "github edit"),
            ("github push", lambda: check_github_push(client, state), "github edit"),
            ("github open pr", lambda: check_github_pr(client, state), "github push"),
            ("github close pr", lambda: check_github_close(client, state), "github open pr"),
        ]
    failed_steps: set[str] = set()

    def fail(name: str, detail: str) -> None:
        # Every FAIL line is recorded here, so any FAIL means a non-zero exit.
        failed_steps.add(name)
        print(f"FAIL {name}: {detail}".splitlines()[0])

    for name, run, needs in steps:
        if needs in failed_steps:
            failed_steps.add(name)
            print(f"SKIP {name} (the {needs} step failed)")
            continue
        started = time.monotonic()
        try:
            detail = run()
        except httpx.TimeoutException:
            fail(name, f"no result after {TIMEOUT.read:.0f}s")
        except (SmokeError, httpx.HTTPError) as exc:
            fail(name, str(exc))
        except (ValueError, KeyError) as exc:
            fail(name, f"unexpected response ({type(exc).__name__})")
        except Exception as exc:  # a bug in a step is a failure too, never a pass
            fail(name, f"the step crashed ({type(exc).__name__})")
        else:
            # A step may return a short result of its own to print in place of the time.
            took = f"({time.monotonic() - started:.1f}s)"
            print(f"OK {name} {detail or took}")
    if github_on and ("token_saved" in state or "repo_added" in state):
        github_cleanup(client, state)
    if not github_on:
        print("SKIP github (HK_SMOKE_GITHUB_REPO is not set)")
    return 1 if failed_steps else 0


if __name__ == "__main__":
    sys.exit(main())
