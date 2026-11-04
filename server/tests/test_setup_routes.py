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

from hearthkeep import host as host_module
from hearthkeep.api import routes_setup
from hearthkeep.api.app import DB_FILE, create_app
from hearthkeep.api.common import RateLimiter
from hearthkeep.auth.enrollment import EnrollmentCodes
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.host import BAD_CHARACTERS, NOT_EMPTY, NOT_LOCAL, TOO_LONG, WindowsHost
from hearthkeep.installer import NotEnoughSpace, NothingToDo
from hearthkeep.lifecycle import EXIT_RELOCATE, ServerControl
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
    ("/api/setup/data-folder", {"path": "C:\\Hearthkeep\\elsewhere"}),
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


# ---- the data folder ------------------------------------------------------------------------

GB = 1024**3


class LocalHost(WindowsHost):
    """Windows with every drive a local one (never asks the real kernel32)."""

    def drive_type(self, root: str) -> int:
        return 3


@pytest.fixture
def folder_host(tmp_path, monkeypatch):
    """The Windows folder rules, on a host whose drives are all local. Folders beside this
    test's temp folder pass the path-text rules on any OS (they are real folders to look in);
    every other path gets the real Windows text rules. Free space is what `space` says."""
    real_text = host_module.check_data_dir_text
    base = str(tmp_path.parent)
    monkeypatch.setattr(
        host_module,
        "check_data_dir_text",
        lambda text: None if text.startswith(base) else real_text(text),
    )
    host = LocalHost()
    monkeypatch.setattr(routes_setup, "current_host", lambda: host)
    space = {"free": 100 * GB, "asked": []}

    def free_bytes(path):
        space["asked"].append(Path(path))
        return space["free"]

    monkeypatch.setattr(routes_setup, "free_bytes", free_bytes)
    return space


def moved(tmp_path: Path, name: str = "moved") -> Path:
    return tmp_path.parent / f"{tmp_path.name}-{name}"


def mover(setup_app):
    control = ServerControl(supervised=False, setup_mode=True)
    client = setup_app(control=control)
    client.mode.created_folder = True
    client.control = control
    return client


def test_move_records_the_new_folder_and_asks_setup_to_start_again(
    setup_app, tmp_path, folder_host
):
    client = mover(setup_app)
    target = moved(tmp_path)
    r = post(client, "/api/setup/data-folder", {"path": str(target)})
    assert (r.status_code, r.json()) == (202, {"ok": True})
    assert client.mode.next_data_dir == target.resolve()
    assert client.control.exit_code == EXIT_RELOCATE  # after the response, as the hand-over
    assert folder_host["asked"] == [target.resolve()]
    assert not target.exists()  # the route only checks: setup makes the folder


def test_move_refused_after_a_download_started(setup_app, tmp_path, folder_host):
    client = mover(setup_app)
    assert post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]}).status_code == 202
    r = post(client, "/api/setup/data-folder", {"path": str(moved(tmp_path))})
    assert (r.status_code, r.json()) == (409, {"reason": "too_late"})
    assert client.mode.next_data_dir is None and client.control.exit_code is None


def test_move_refused_for_a_folder_this_run_did_not_make(setup_app, tmp_path, folder_host):
    """The server checks it itself, whatever `can_move_data` told the page."""
    control = ServerControl(supervised=False, setup_mode=True)
    client = setup_app(control=control)
    assert client.mode.created_folder is False
    r = post(client, "/api/setup/data-folder", {"path": str(moved(tmp_path))})
    assert (r.status_code, r.json()) == (409, {"reason": "too_late"})
    assert client.mode.next_data_dir is None and control.exit_code is None


def _not_empty(tmp_path: Path) -> str:
    folder = moved(tmp_path, "full")
    folder.mkdir()
    (folder / "notes.txt").write_text("mine", encoding="utf-8")
    return str(folder)


@pytest.mark.parametrize(
    ("make_path", "sentence"),
    [
        (lambda tmp: "C:\\" + "a" * 120, TOO_LONG),
        (lambda tmp: "C:\\Hearth%keep\\data", BAD_CHARACTERS),
        (_not_empty, NOT_EMPTY),
        (lambda tmp: "\\\\homeserver\\share\\data", NOT_LOCAL),
    ],
    ids=["TOO_LONG", "BAD_CHARACTERS", "NOT_EMPTY", "NOT_LOCAL"],
)
def test_bad_folder_gets_the_2a_sentence(setup_app, tmp_path, folder_host, make_path, sentence):
    client = mover(setup_app)
    r = post(client, "/api/setup/data-folder", {"path": make_path(tmp_path)})
    assert (r.status_code, r.json()) == (400, {"reason": "bad_folder", "message": sentence})
    assert client.mode.next_data_dir is None and client.control.exit_code is None
    assert folder_host["asked"] == []  # free space is asked only of a usable folder


def test_move_refuses_another_hearthkeep_data_folder(setup_app, tmp_path, folder_host):
    """An existing data folder passes check_data_dir (it is kept as it is), but a move is to a
    new folder: another install's folder is never taken over."""
    client = mover(setup_app)
    other = moved(tmp_path, "other")
    other.mkdir()
    (other / "config.toml").write_text("port = 8765\n", encoding="utf-8")
    r = post(client, "/api/setup/data-folder", {"path": str(other)})
    assert (r.status_code, r.json()) == (400, {"reason": "bad_folder", "message": NOT_EMPTY})


@pytest.mark.parametrize("where", ["same", "inside"])
def test_move_refuses_the_current_folder_and_folders_inside_it(
    setup_app, tmp_path, folder_host, where
):
    client = mover(setup_app)
    target = tmp_path if where == "same" else tmp_path / "workspace" / "new"
    r = post(client, "/api/setup/data-folder", {"path": str(target)})
    assert r.status_code == 400
    assert r.json() == {"reason": "bad_folder", "message": routes_setup.INSIDE_CURRENT}
    assert client.mode.next_data_dir is None and client.control.exit_code is None


def test_move_needs_5_gb_free(setup_app, tmp_path, folder_host):
    client = mover(setup_app)
    target = moved(tmp_path)
    folder_host["free"] = int(4.94 * GB)
    r = post(client, "/api/setup/data-folder", {"path": str(target)})
    assert r.status_code == 400
    assert r.json() == {"reason": "not_enough_space", "free_gb": 4.9, "needed_gb": 5}
    assert client.mode.next_data_dir is None and client.control.exit_code is None
    folder_host["free"] = 5 * GB
    assert post(client, "/api/setup/data-folder", {"path": str(target)}).status_code == 202


def test_move_path_limits(setup_app, folder_host):
    client = mover(setup_app)
    for path in ("", "C:\\" + "a" * 260):
        r = post(client, "/api/setup/data-folder", {"path": path})
        assert r.status_code == 422, path
    assert client.mode.next_data_dir is None


def test_move_on_another_os_is_unsupported(setup_app, tmp_path, folder_host, monkeypatch):
    monkeypatch.setattr(routes_setup, "current_host", lambda: host_module.UnsupportedHost())
    client = mover(setup_app)
    r = post(client, "/api/setup/data-folder", {"path": str(moved(tmp_path))})
    assert (r.status_code, r.json()) == (400, {"reason": "unsupported"})
    assert client.control.exit_code is None


def test_move_copy_is_clean():
    from copy_rules import forbidden_in

    text = routes_setup.INSIDE_CURRENT
    assert text.isascii() and forbidden_in(text) == [] and "<" not in text


def test_a_download_that_starts_during_the_move_check_wins(
    setup_app, tmp_path, folder_host, monkeypatch
):
    client = mover(setup_app)
    real = routes_setup.folder_move_problem

    def racing(host, target, current):
        client.fake.start(["chat-qwen3.5-4b"], make_default=True)  # lands mid-check
        return real(host, target, current)

    monkeypatch.setattr(routes_setup, "folder_move_problem", racing)
    r = post(client, "/api/setup/data-folder", {"path": str(moved(tmp_path))})
    assert (r.status_code, r.json()) == (409, {"reason": "too_late"})
    assert client.mode.next_data_dir is None and client.control.exit_code is None


def test_downloads_wait_while_a_move_is_under_way(setup_app, tmp_path, folder_host):
    client = mover(setup_app)
    target = str(moved(tmp_path))
    assert post(client, "/api/setup/data-folder", {"path": target}).status_code == 202
    for path, body in (
        ("/api/setup/install", {"items": ["chat-qwen3.5-4b"]}),
        ("/api/setup/install/retry", {}),
    ):
        r = post(client, path, body)
        assert (r.status_code, r.json()) == (409, {"reason": "moving"}), path
    assert client.fake.calls == [] and client.fake.current() is None


def test_move_refuses_a_folder_inside_another_data_folder(setup_app, tmp_path, folder_host):
    client = mover(setup_app)
    other = moved(tmp_path, "other")
    other.mkdir()
    (other / "hearthkeep.db").write_bytes(b"")
    r = post(client, "/api/setup/data-folder", {"path": str(other / "inner" / "data")})
    assert (r.status_code, r.json()) == (400, {"reason": "bad_folder", "message": NOT_EMPTY})


def test_a_move_accepted_while_an_install_reads_the_pc_stops_the_install(setup_app, tmp_path):
    """The install checks "not moving" again right before the job starts, with no await in
    between: a move accepted while it read this PC wins."""
    client = setup_app()
    hk = client.app.state.hk
    real_get = hk.pc.get

    async def get_while_moving():
        client.mode.next_data_dir = moved(tmp_path)  # the move's 202, during this await
        return await real_get()

    hk.pc.get = get_while_moving
    r = post(client, "/api/setup/install", {"items": ["chat-qwen3.5-4b"]})
    assert (r.status_code, r.json()) == (409, {"reason": "moving"})
    assert client.fake.calls == [] and client.fake.current() is None
    assert not [e for e in events(tmp_path) if e["event"] == "models.install_approved"]


def test_move_refuses_a_folder_it_cant_look_into(setup_app, tmp_path, folder_host, monkeypatch):
    """A parent that can't be read (no access, a drive that went away) is refused, not a 500."""
    client = mover(setup_app)
    real_exists = Path.exists

    def exists(self, *a, **k):
        if self.name in host_module.DATA_MARKERS:
            raise PermissionError("access is denied")
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", exists)
    r = post(client, "/api/setup/data-folder", {"path": str(moved(tmp_path))})
    assert (r.status_code, r.json()) == (400, {"reason": "bad_folder", "message": NOT_EMPTY})
    assert client.mode.next_data_dir is None and client.control.exit_code is None
