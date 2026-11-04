"""The setup routes for the PC check, the catalog, downloads and the chat model: every one sits
behind the one setup gate (open setup, a TCP peer on this PC, the rate limit, the secret in the
body), answers like the session route it mirrors, and never echoes, logs or audits the secret.
Downloads run on the fake installer; nothing touches the network or nvidia-smi."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fakes import FakeChatClient, FakeVerifier
from fastapi.testclient import TestClient
from test_api import CSRF, ORIGIN, enroll, registry_with
from test_api_model_install import (
    SECRET as CHAT_KEY,
)
from test_api_model_install import (
    FakeInstaller,
    PcReader,
    events,
    install_local_chat,
)
from test_installer import NO_GPU, NVIDIA

from hearthkeep.api import routes_setup
from hearthkeep.api.app import DB_FILE, create_app
from hearthkeep.api.common import RateLimiter
from hearthkeep.auth.enrollment import EnrollmentCodes
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.installer import NotEnoughSpace, NothingToDo
from hearthkeep.models.runtime import load_runtime
from hearthkeep.secrets import SecretStore
from hearthkeep.setupmode import SetupMode

SECRET = "setup-secret-marker-" + "q" * 24
LOOPBACK = ("127.0.0.1", 50000)

# Every setup route this task adds, with a body that is valid apart from the secret.
ROUTES = [
    ("/api/setup/pc", {}),
    ("/api/setup/catalog", {}),
    ("/api/setup/install", {"items": ["chat-qwen3.5-4b"]}),
    ("/api/setup/install/status", {}),
    ("/api/setup/install/cancel", {}),
    ("/api/setup/install/retry", {}),
    ("/api/setup/chat", {"source": "local", "card": "qwen3.5-4b"}),
]
# And the two 2a routes, now behind the same dependency.
ALL_ROUTES = [("/api/setup/status", {}), ("/api/setup/enrollment-code", {}), *ROUTES]


async def _healthy():
    return True


@pytest.fixture
def setup_app(tmp_path):
    clients: list[TestClient] = []

    def _start(installer=None, *, pc=NO_GPU, peer=LOOPBACK, limiter=None, control=None):
        fake = installer if installer is not None else FakeInstaller()
        mode = SetupMode(SECRET)
        app = create_app(
            Settings(data_dir=tmp_path, docker_available=False),
            verifier=FakeVerifier(),
            chat_client=FakeChatClient([]),
            registry=registry_with(),
            health_check=_healthy,
            web_dist=None,
            control=control,
            setup=mode,
            installer_factory=lambda data, ctl, **kw: fake,
            pc_reader=PcReader(pc),
            rate_limiter=limiter,
        )
        client = TestClient(app, base_url=ORIGIN, client=peer)
        client.__enter__()
        clients.append(client)
        client.fake = fake
        client.mode = mode
        return client

    yield _start
    for client in clients:
        client.__exit__(None, None, None)


def post(client, path, body=None, secret=SECRET, headers=CSRF):
    payload = dict(body or {})
    if secret is not None:
        payload["secret"] = secret
    return client.post(path, json=payload, headers=headers)


def add_passkey(data: Path) -> None:
    conn = connect(data / DB_FILE)
    try:
        conn.execute("INSERT INTO credentials (id, public_key, sign_count) VALUES ('k', x'00', 0)")
    finally:
        conn.close()


def sign_in(client, data: Path) -> None:
    conn = connect(data / DB_FILE)
    code = EnrollmentCodes(conn).issue("first_device", ttl_minutes=15)
    conn.close()
    enroll(client, code)


# ---- the gate -------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "body"), ALL_ROUTES)
def test_every_setup_route_uses_the_gate(setup_app, tmp_path, caplog, path, body):
    caplog.set_level(logging.DEBUG)
    install_local_chat(tmp_path)
    client = setup_app()
    seen = []

    missing = post(client, path, body, secret=None)
    empty = post(client, path, body, secret="")
    assert (missing.status_code, missing.json()["reason"]) == (422, "invalid_request")
    assert (empty.status_code, empty.json()["reason"]) == (422, "invalid_request")
    wrong = post(client, path, body, secret="not-the-secret")
    assert (wrong.status_code, wrong.json()) == (403, {"reason": "setup_secret"})
    no_csrf = post(client, path, body, headers={})
    assert (no_csrf.status_code, no_csrf.json()) == (403, {"reason": "csrf"})
    # The secret is read from the body only, and no setup route answers a GET.
    as_query = client.post(
        path, json={**body, "secret": "nope"}, params={"secret": SECRET}, headers=CSRF
    )
    assert as_query.status_code == 403
    assert client.get(path).status_code in (404, 405)
    seen += [missing, empty, wrong, no_csrf, as_query]
    assert client.fake.calls == []  # nothing ran without the secret

    denied = [e for e in events(tmp_path) if e["event"] == "setup.denied"]
    assert [e["detail"] for e in denied] == [{"reason": "bad_secret", "client": "127.0.0.1"}] * 2
    assert all(e["actor"] == "anonymous" for e in denied)

    remote = setup_app(peer=("192.168.1.20", 50000))
    far = post(remote, path, body)
    assert (far.status_code, far.json()) == (403, {"reason": "loopback_only"})
    seen.append(far)

    ok = post(client, path, body)
    assert ok.status_code in (200, 202, 404, 409), ok.text  # 404/409: no job to retry
    seen.append(ok)

    add_passkey(tmp_path)
    gone = post(client, path, body)
    assert (gone.status_code, gone.json()) == (404, {"reason": "not_found"})
    seen.append(gone)

    for response in seen:
        assert SECRET not in response.text
    for e in events(tmp_path):
        assert SECRET not in json.dumps(e)
    # Every server record (the test client's own httpx lines carry the ?secret= URL above).
    server_logs = [r.getMessage() for r in caplog.records if not r.name.startswith("httpx")]
    assert server_logs and not any(SECRET in line for line in server_logs)


@pytest.mark.parametrize(("path", "body"), ROUTES)
def test_setup_routes_are_404_without_setup_mode(tmp_path, path, body):
    app = create_app(
        Settings(data_dir=tmp_path, docker_available=False),
        verifier=FakeVerifier(),
        chat_client=FakeChatClient([]),
        registry=registry_with(),
        health_check=_healthy,
        web_dist=None,
    )
    with TestClient(app, base_url=ORIGIN, client=LOOPBACK) as client:
        r = post(client, path, body)
        assert (r.status_code, r.json()) == (404, {"reason": "not_found"})


# ---- the PC and the catalog -----------------------------------------------------------------


def test_setup_pc_shows_the_pc_and_the_data_folder(setup_app, tmp_path):
    client = setup_app(pc=NVIDIA)
    r = post(client, "/api/setup/pc")
    assert r.status_code == 200
    assert r.json() == {
        "pc": NVIDIA.to_json(),
        "data_dir": str(tmp_path.resolve()),
        "data_dir_default": False,
        "can_move_data": False,
    }


def test_setup_pc_says_when_the_folder_is_the_default(setup_app, tmp_path, monkeypatch):
    class Host:
        def default_data_dir(self):
            return tmp_path

    monkeypatch.setattr(routes_setup, "current_host", lambda: Host())
    client = setup_app()
    assert post(client, "/api/setup/pc").json()["data_dir_default"] is True


def test_setup_can_move_data_only_before_a_download_in_a_folder_it_made(setup_app):
    client = setup_app()
    assert client.mode.created_folder is False
    client.mode.created_folder = True
    assert post(client, "/api/setup/pc").json()["can_move_data"] is True
    r = post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]})
    assert r.status_code == 202
    assert post(client, "/api/setup/pc").json()["can_move_data"] is False


def test_setup_catalog_matches_the_session_catalog(setup_app, tmp_path):
    fake = FakeInstaller()
    fake.installed_ids.add("llama-cpu")
    client = setup_app(fake)
    from_setup = post(client, "/api/setup/catalog")
    assert from_setup.status_code == 200
    sign_in(client, tmp_path)  # the same app, now with a passkey and a session
    from_session = client.get("/api/models/catalog")
    assert from_session.status_code == 200
    assert from_setup.json() == from_session.json()
    assert post(client, "/api/setup/catalog").status_code == 404


# ---- downloads ------------------------------------------------------------------------------


def test_setup_install_starts_a_job_without_step_up(setup_app, tmp_path):
    client = setup_app()
    r = post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"], "make_default": False})
    assert r.status_code == 202, r.text
    job = r.json()["job"]
    assert job["state"] == "running" and [i["id"] for i in job["items"]] == ["chat-qwen3.5-4b"]
    assert client.fake.calls == [("start", ["chat-qwen3.5-4b"], False)]
    assert post(client, "/api/setup/install/status").json() == {"job": job}
    rows = [e for e in events(tmp_path) if e["event"] == "models.install_approved"]
    assert [(e["actor"], e["detail"]) for e in rows] == [("setup", {"items": ["chat-qwen3.5-4b"]})]


def test_setup_install_defaults_to_making_the_model_the_default(setup_app):
    client = setup_app()
    assert post(client, "/api/setup/install", {"items": ["llama-cpu"]}).status_code == 202
    assert client.fake.calls == [("start", ["llama-cpu"], True)]


def test_setup_install_body_limits(setup_app):
    client = setup_app()
    for items in ([], ["llama-cpu"] * 11, ["x" * 65]):
        assert post(client, "/api/setup/install", {"items": items}).status_code == 422, items


def test_setup_install_maps_the_same_errors(setup_app):
    client = setup_app(pc=NO_GPU)
    fake = client.fake
    unknown = post(client, "/api/setup/install", {"items": ["llama-cpu", "https://evil.example"]})
    assert (unknown.status_code, unknown.json()) == (400, {"reason": "unknown_item"})
    no_fit = post(client, "/api/setup/install", {"items": ["chat-gemma-4-12b"]})
    assert (no_fit.status_code, no_fit.json()) == (
        400,
        {"reason": "doesnt_fit", "item": "chat-gemma-4-12b"},
    )
    assert fake.calls == []

    fake.start_error = NotEnoughSpace(5_000, 1_000)
    space = post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]})
    assert (space.status_code, space.json()) == (
        409,
        {"reason": "no_space", "needed_bytes": 5_000, "free_bytes": 1_000},
    )
    fake.start_error = NothingToDo()
    nothing = post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]})
    assert (nothing.status_code, nothing.json()) == (409, {"reason": "nothing_to_do"})

    fake.start_error = None
    first = post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]})
    assert first.status_code == 202
    busy = post(client, "/api/setup/install", {"items": ["llama-cpu"]})
    assert busy.status_code == 409
    assert busy.json() == {"reason": "busy", "job": first.json()["job"]}


def test_setup_install_without_an_installer_is_unavailable(setup_app):
    client = setup_app()
    client.app.state.hk.installer = None
    for path, body in (
        ("/api/setup/catalog", {}),
        ("/api/setup/install", {"items": ["llama-cpu"]}),
        ("/api/setup/install/cancel", {}),
        ("/api/setup/install/retry", {}),
    ):
        r = post(client, path, body)
        assert (r.status_code, r.json()) == (503, {"reason": "unavailable"}), path
    assert post(client, "/api/setup/install/status").json() == {"job": None}


def test_setup_cancel_and_retry(setup_app, tmp_path):
    client = setup_app()
    no_job = post(client, "/api/setup/install/retry")
    assert (no_job.status_code, no_job.json()) == (404, {"reason": "no_job"})
    assert post(client, "/api/setup/install/cancel").json() == {"job": None}
    assert post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]}).status_code == 202
    busy = post(client, "/api/setup/install/retry")
    assert busy.status_code == 409 and busy.json()["reason"] == "busy"
    cancelled = post(client, "/api/setup/install/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["job"]["state"] == "cancelled"
    retried = post(client, "/api/setup/install/retry")
    assert retried.status_code == 202 and retried.json()["job"]["state"] == "running"
    rows = {e["event"]: e for e in events(tmp_path)}
    assert rows["models.install_cancelled"]["actor"] == "setup"
    assert rows["models.install_retried"]["actor"] == "setup"


@pytest.mark.parametrize(
    ("error", "body"),
    [
        (NotEnoughSpace(9, 1), {"reason": "no_space", "needed_bytes": 9, "free_bytes": 1}),
        (NothingToDo(), {"reason": "nothing_to_do"}),
    ],
)
def test_setup_retry_maps_errors_like_start(setup_app, error, body):
    client = setup_app()
    post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]})
    post(client, "/api/setup/install/cancel")
    client.fake.retry_error = error
    r = post(client, "/api/setup/install/retry")
    assert (r.status_code, r.json()) == (409, body)


# ---- the rate limit -------------------------------------------------------------------------


def test_setup_polling_fits_the_rate_limit(setup_app):
    client = setup_app(limiter=RateLimiter(clock=lambda: 0.0))  # all inside one minute
    for n in range(120):
        r = post(client, "/api/setup/install/status")
        assert r.status_code == 200, n
    last = post(client, "/api/setup/install/status")
    assert (last.status_code, last.json()) == (429, {"reason": "rate_limited"})
    # Polling has its own group: the wizard's other calls still answer.
    assert post(client, "/api/setup/catalog").status_code == 200


def test_setup_group_allows_60_a_minute():
    limiter = RateLimiter(clock=lambda: 0.0)
    assert all(limiter.hit("setup", "127.0.0.1") for _ in range(60))
    assert not limiter.hit("setup", "127.0.0.1")
    assert all(limiter.hit("setup_poll", "127.0.0.1") for _ in range(120))
    assert not limiter.hit("setup_poll", "127.0.0.1")


def test_setup_groups_share_a_global_cap_of_240():
    limiter = RateLimiter(clock=lambda: 0.0)
    for n in range(240):
        assert limiter.hit("setup_poll", f"10.0.0.{n % 3}")  # 80 each, under 120
    assert not limiter.hit("setup_poll", "10.0.0.9")


def test_other_groups_keep_their_limits():
    limiter = RateLimiter(clock=lambda: 0.0)
    assert all(limiter.hit("models", "127.0.0.1") for _ in range(10))
    assert not limiter.hit("models", "127.0.0.1")
    custom = RateLimiter(per_client=2, global_limit=3, clock=lambda: 0.0)
    assert custom.hit("auth", "a") and custom.hit("auth", "a") and not custom.hit("auth", "a")


# ---- the chat model -------------------------------------------------------------------------


def test_setup_chat_switches_to_an_installed_local_model(setup_app, tmp_path):
    install_local_chat(tmp_path)
    client = setup_app()
    r = post(client, "/api/setup/chat", {"source": "local", "card": "qwen3.5-4b"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "local" and r.json()["model"] == "qwen3.5-4b"
    assert load_runtime(tmp_path).default_chat_model == "qwen3.5-4b"
    rows = [e for e in events(tmp_path) if e["event"] == "models.chat_switched"]
    assert [(e["actor"], e["detail"]) for e in rows] == [("setup", {"card": "qwen3.5-4b"})]
    missing = post(client, "/api/setup/chat", {"source": "local", "card": "gemma-4-12b"})
    assert (missing.status_code, missing.json()) == (400, {"reason": "not_installed"})


def test_setup_chat_points_at_an_external_server_without_step_up(setup_app, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    (tmp_path / "config.toml").write_text("# my config\nport = 8765\n", encoding="utf-8")
    client = setup_app()
    body = {"source": "external", "base_url": "http://127.0.0.1:9000/v1", "api_key": CHAT_KEY}
    r = post(client, "/api/setup/chat", body)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "external" and r.json()["base_url"] == body["base_url"]
    assert SecretStore(tmp_path).get("chat_api_key") == CHAT_KEY
    rows = [e for e in events(tmp_path) if e["event"] == "models.chat_external_set"]
    assert [(e["actor"], e["detail"]) for e in rows] == [("setup", {"host": "127.0.0.1"})]
    bad = post(client, "/api/setup/chat", {**body, "base_url": "ftp://x"})
    assert (bad.status_code, bad.json()) == (400, {"reason": "bad_url"})
    for text in (r.text, bad.text, caplog.text, json.dumps(events(tmp_path))):
        assert CHAT_KEY not in text and SECRET not in text
