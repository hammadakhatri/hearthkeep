"""/api/models/catalog, install jobs and chat switching: step-up, audit and what never leaks
(the data folder path, the external server's key). Job routes run on a fake installer; two
tests run the real one on a mock transport. Nothing touches the network or nvidia-smi."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

import pytest
from fakes import FakeChatClient, FakeVerifier
from fastapi.testclient import TestClient
from test_api import CSRF, ORIGIN, enroll, login, registry_with, step_up
from test_installer import CATALOG, NO_GPU, NVIDIA, QWEN4B, Server, Stall

from hearthkeep import audit
from hearthkeep.api.app import DB_FILE, create_app
from hearthkeep.api.common import RateLimiter
from hearthkeep.auth.enrollment import EnrollmentCodes
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.installer import (
    InstallBusy,
    Installer,
    ItemProgress,
    Job,
    NotEnoughSpace,
    NothingToDo,
)
from hearthkeep.lifecycle import ServerControl
from hearthkeep.models import service
from hearthkeep.models.runtime import load_runtime
from hearthkeep.secrets import SecretStore, SecretStoreError
from hearthkeep.tomledit import TomlEditError

SECRET = "external-chat-key-marker-7f3a"
CATALOG_KEYS = {
    "pc", "recommended", "items", "chat", "runtime_ready", "restart_needed", "supervised",
}  # fmt: skip
ITEM_KEYS = {
    "id", "kind", "name", "note", "licence", "licence_url", "size_bytes", "needs_nvidia",
    "variant", "card", "installed", "fits",
}  # fmt: skip


class FakeInstaller:
    """The installer's surface the routes use. `start_error`/`retry_error` are raised as is."""

    def __init__(self, catalog=CATALOG) -> None:
        self.catalog = catalog
        self.config_lock = asyncio.Lock()
        self.installed_ids: set[str] = set()
        self.job: Job | None = None
        self.running = False
        self.calls: list[tuple] = []
        self.start_error: Exception | None = None
        self.retry_error: Exception | None = None

    def installed(self, item) -> bool:
        return item.id in self.installed_ids

    def runtime_ready(self) -> bool:
        return any(self.installed(i) for i in self.catalog.values() if i.kind == "runtime")

    def current(self) -> Job | None:
        return self.job

    def start(self, ids, *, make_default):
        self.calls.append(("start", list(ids), make_default))
        if self.running and self.job is not None:
            raise InstallBusy(self.job)
        if self.start_error is not None:
            raise self.start_error
        items = [ItemProgress(i, self.catalog[i].name) for i in ids]
        self.job = Job(uuid.uuid4().hex, items, "running", make_default)
        self.running = True
        return self.job

    def cancel(self) -> None:
        self.calls.append(("cancel",))
        if self.running and self.job is not None:
            self.running = False
            self.job.state = "cancelled"
            for item in self.job.items:
                item.state = "cancelled"

    def retry(self):
        self.calls.append(("retry",))
        if self.running and self.job is not None:
            raise InstallBusy(self.job)
        if self.retry_error is not None:
            raise self.retry_error
        if self.job is None:
            raise NothingToDo()
        self.job.state = "running"
        self.running = True
        return self.job

    async def wait(self):
        return self.job

    async def shutdown(self) -> None:
        self.calls.append(("shutdown",))


class PcReader:
    def __init__(self, pc=NO_GPU) -> None:
        self.pc = pc
        self.calls = 0

    def __call__(self, data_dir: Path):
        self.calls += 1
        return self.pc


@pytest.fixture
def app_with(tmp_path):
    clients: list[TestClient] = []

    def _start(installer=None, *, pc=NO_GPU, control=None, sign_in=True, real=None, limiter=None):
        """`installer` is a FakeInstaller; `real` a Server, for the real installer on it."""
        reader = PcReader(pc)
        made: dict = {}

        def factory(data, ctl, **kw):
            if real is not None:
                made["installer"] = Installer(
                    data, ctl, catalog=CATALOG, client_factory=real.client, **kw
                )
            else:
                made["installer"] = installer
            return made["installer"]

        app = create_app(
            Settings(data_dir=tmp_path, docker_available=False),
            verifier=FakeVerifier(),
            chat_client=FakeChatClient([]),
            registry=registry_with(),
            health_check=_healthy,
            web_dist=None,
            control=control or ServerControl(supervised=True),
            installer_factory=factory,
            pc_reader=reader,
            rate_limiter=limiter or RateLimiter(per_client=1000, global_limit=1000),
        )
        client = TestClient(app, base_url=ORIGIN)
        client.__enter__()
        clients.append(client)
        client.pc_reader = reader
        if sign_in:
            conn = connect(tmp_path / DB_FILE)
            code = EnrollmentCodes(conn).issue("first_device", ttl_minutes=15)
            conn.close()
            enroll(client, code)
        return client

    yield _start
    for client in clients:
        client.__exit__(None, None, None)


async def _healthy():
    return True


def events(data: Path) -> list[dict]:
    conn = connect(data / DB_FILE)
    try:
        return audit.list_events(conn, limit=1000)
    finally:
        conn.close()


def other_session_token(client, purpose: str) -> str:
    first = next(c for c in client.cookies.jar if c.name == "hk_session")
    client.cookies.clear()
    assert login(client).status_code == 200
    token = step_up(client, purpose)
    client.cookies.clear()
    client.cookies.jar.set_cookie(first)
    return token


def install(client, items, token, **extra):
    body = {"items": items, "step_up_token": token, **extra}
    return client.post("/api/models/install", json=body, headers=CSRF)


def leaks_path(text: str, data: Path) -> bool:
    forms = {str(data), str(data).replace("\\", "\\\\"), data.as_posix(), data.name}
    return any(f in text for f in forms)


def install_local_chat(data: Path, card="qwen3.5-4b", file=QWEN4B, *, exe=True) -> None:
    folder = data / "models" / "chat" / card
    folder.mkdir(parents=True)
    (folder / file).write_bytes(b"gguf")
    server = data / "tools" / "llama-server.exe"
    server.parent.mkdir(parents=True, exist_ok=True)
    if exe:
        server.write_bytes(b"exe")
    (data / "models.toml").write_text(
        "# the owner's notes stay\n"
        f"llama_server_exe = '{server}'\n"
        "default_chat_model = 'gemma-4-12b'\n\n"
        "[model_dirs]\n"
        f"\"{card}\" = '{folder}'\n",
        encoding="utf-8",
    )


# ---- the catalog ----------------------------------------------------------------------------


def test_catalog_needs_a_session(app_with):
    client = app_with(FakeInstaller(), sign_in=False)
    assert client.get("/api/models/catalog").status_code == 401
    assert client.get("/api/models/install").status_code == 401
    assert install(client, ["llama-cpu"], "x").status_code == 401


def test_catalog_shape_and_no_paths(app_with, tmp_path):
    fake = FakeInstaller()
    fake.installed_ids.add("llama-cpu")
    client = app_with(fake, pc=NO_GPU)
    response = client.get("/api/models/catalog")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == CATALOG_KEYS
    assert body["pc"] == NO_GPU.to_json()
    assert body["recommended"] == {
        "chat": "chat-qwen3.5-4b", "runtime": "llama-cpu", "reason": "no_nvidia",
    }  # fmt: skip
    assert [i["id"] for i in body["items"]] == list(CATALOG)
    for item in body["items"]:
        assert set(item) == ITEM_KEYS
    by_id = {i["id"]: i for i in body["items"]}
    assert by_id["llama-cpu"]["installed"] is True and by_id["llama-cuda"]["installed"] is False
    assert by_id["chat-qwen3.5-4b"]["fits"] is True
    assert by_id["chat-gemma-4-12b"]["fits"] is False and by_id["comfyui"]["fits"] is False
    assert by_id["chat-qwen3.5-4b"]["card"] == "qwen3.5-4b"
    assert by_id["llama-cuda"]["variant"] == "cuda"
    assert body["chat"] == {"source": "none", "model": None, "base_url": None}
    assert body["restart_needed"] is False and body["supervised"] is True
    assert body["runtime_ready"] is True
    assert not leaks_path(response.text, tmp_path)


def test_catalog_reads_the_pc_once_a_minute(app_with):
    client = app_with(FakeInstaller())
    for _ in range(3):
        assert client.get("/api/models/catalog").status_code == 200
    assert client.pc_reader.calls == 1


def test_catalog_shows_a_local_chat_model(app_with, tmp_path):
    install_local_chat(tmp_path)
    (tmp_path / "models.toml").write_text(
        (tmp_path / "models.toml").read_text(encoding="utf-8").replace("gemma-4-12b", "qwen3.5-4b"),
        encoding="utf-8",
    )
    body = app_with(FakeInstaller()).get("/api/models/catalog").json()
    assert body["chat"] == {"source": "local", "model": "qwen3.5-4b", "base_url": None}


def test_catalog_runtime_ready_comes_from_the_installer(app_with):
    fake = FakeInstaller()
    client = app_with(fake)
    assert client.get("/api/models/catalog").json()["runtime_ready"] is False
    fake.installed_ids.add("llama-vulkan")
    assert client.get("/api/models/catalog").json()["runtime_ready"] is True


def test_catalog_says_whether_a_chat_engine_is_ready(app_with, tmp_path):
    """The installer's own check: a catalog engine, or one the owner set up by hand."""
    client = app_with(real=Server(), pc=NO_GPU)
    assert client.get("/api/models/catalog").json()["runtime_ready"] is False
    install_local_chat(tmp_path)  # llama_server_exe outside every catalog install folder
    body = client.get("/api/models/catalog").json()
    assert body["runtime_ready"] is True
    assert not any(i["installed"] for i in body["items"] if i["kind"] == "runtime")


# ---- starting a job -------------------------------------------------------------------------


def test_install_needs_download_step_up(app_with, tmp_path):
    fake = FakeInstaller()
    client = app_with(fake)
    server_token = step_up(client, "server")
    settings_token = step_up(client, "settings")
    other = other_session_token(client, "download")
    for token in (None, "", "nope", server_token, settings_token, other):
        r = install(client, ["chat-qwen3.5-4b"], token)
        assert r.status_code == 403 and r.json() == {"reason": "step_up_required"}, token
    no_field = client.post("/api/models/install", json={"items": ["llama-cpu"]}, headers=CSRF)
    assert no_field.status_code == 403
    assert fake.calls == [] and fake.job is None
    denied = [e for e in events(tmp_path) if e["event"] == "models.install_denied"]
    assert len(denied) == 7
    assert all(e["detail"] == {"reason": "step_up_required"} for e in denied)
    assert client.get("/api/models/install").json() == {"job": None}


def test_install_starts_a_job(app_with, tmp_path):
    fake = FakeInstaller()
    client = app_with(fake)
    r = install(client, ["chat-qwen3.5-4b"], step_up(client, "download"), make_default=False)
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["state"] == "running" and [i["id"] for i in job["items"]] == ["chat-qwen3.5-4b"]
    assert fake.calls == [("start", ["chat-qwen3.5-4b"], False)]
    assert client.get("/api/models/install").json() == {"job": job}
    rows = [e for e in events(tmp_path) if e["event"] == "models.install_approved"]
    assert rows and rows[0]["detail"] == {"items": ["chat-qwen3.5-4b"]}
    assert rows[0]["actor"].startswith("session:")


def test_install_body_limits(app_with):
    client = app_with(FakeInstaller())
    for items in ([], ["llama-cpu"] * 11, ["x" * 65]):
        r = install(client, items, step_up(client, "download"))
        assert r.status_code == 422, items


def test_second_install_while_running_is_busy(app_with):
    fake = FakeInstaller()
    client = app_with(fake)
    first = install(client, ["chat-qwen3.5-4b"], step_up(client, "download"))
    assert first.status_code == 202
    token = step_up(client, "download")
    second = install(client, ["llama-cpu"], token)
    assert second.status_code == 409
    assert second.json() == {"reason": "busy", "job": first.json()["job"]}
    reused = install(client, ["llama-cpu"], token)  # a refused request still spends its token
    assert reused.status_code == 403 and reused.json() == {"reason": "step_up_required"}
    assert fake.job.items[0].id == "chat-qwen3.5-4b"  # the running job is untouched


def test_install_refuses_unknown_and_unfitting_items(app_with):
    fake = FakeInstaller()
    client = app_with(fake, pc=NO_GPU)
    unknown = install(client, ["llama-cpu", "no-such-item"], step_up(client, "download"))
    assert unknown.status_code == 400 and unknown.json()["reason"] == "unknown_item"
    for item in ("chat-gemma-4-12b", "llama-cuda", "image-z-image-turbo", "comfyui"):
        r = install(client, [item], step_up(client, "download"))
        assert r.status_code == 400 and r.json() == {"reason": "doesnt_fit", "item": item}
    assert fake.calls == []


def test_install_fits_on_an_nvidia_pc(app_with):
    fake = FakeInstaller()
    client = app_with(fake, pc=NVIDIA)
    r = install(client, ["chat-gemma-4-12b"], step_up(client, "download"))
    assert r.status_code == 202


def test_install_no_space(app_with):
    fake = FakeInstaller()
    fake.start_error = NotEnoughSpace(5_000, 1_000)
    client = app_with(fake)
    r = install(client, ["chat-qwen3.5-4b"], step_up(client, "download"))
    assert r.status_code == 409
    assert r.json() == {"reason": "no_space", "needed_bytes": 5_000, "free_bytes": 1_000}


def test_install_nothing_to_do(app_with):
    fake = FakeInstaller()
    fake.start_error = NothingToDo()
    client = app_with(fake)
    r = install(client, ["chat-qwen3.5-4b"], step_up(client, "download"))
    assert r.status_code == 409 and r.json() == {"reason": "nothing_to_do"}


def test_urls_never_come_from_the_request(app_with, tmp_path):
    server = Server()
    client = app_with(real=server)
    for items in (["https://evil.example/x"], ["llama-cpu", "https://evil.example/x"]):
        r = install(client, items, step_up(client, "download"))
        assert r.status_code == 400 and r.json() == {"reason": "unknown_item"}
    assert server.requests == []
    assert client.get("/api/models/install").json() == {"job": None}
    assert not (tmp_path / "runtime" / "downloads").exists()


def test_real_install_end_to_end(app_with, tmp_path):
    server = Server()
    control = ServerControl(supervised=True)
    client = app_with(real=server, control=control, pc=NO_GPU)
    r = install(client, ["chat-qwen3.5-4b"], step_up(client, "download"))
    assert r.status_code == 202, r.text
    assert [i["id"] for i in r.json()["job"]["items"]] == ["llama-cpu", "chat-qwen3.5-4b"]
    client.portal.call(client.app.state.hk.installer.wait)
    job = client.get("/api/models/install").json()["job"]
    assert job["state"] == "done" and job["restart_needed"] is True
    assert all(h.url.scheme == "https" for h in server.requests)
    rt = load_runtime(tmp_path)
    assert rt.default_chat_model == "qwen3.5-4b" and rt.chat_server == "local"
    assert control.restart_needed == {"models"}
    catalog = client.get("/api/models/catalog").json()
    assert catalog["restart_needed"] is True
    assert catalog["chat"] == {"source": "local", "model": "qwen3.5-4b", "base_url": None}
    installed = {i["id"]: i["installed"] for i in catalog["items"]}
    assert installed["chat-qwen3.5-4b"] and installed["llama-cpu"]
    assert catalog["runtime_ready"] is True
    rows = {e["event"]: e for e in events(tmp_path)}
    started = rows["models.install_started"]
    assert started["actor"] == "system"
    assert started["detail"]["items"] == ["llama-cpu", "chat-qwen3.5-4b"]
    assert isinstance(started["detail"]["bytes"], int)
    assert rows["models.install_finished"]["detail"]["state"] == "done"
    assert not leaks_path(json.dumps([e["detail"] for e in events(tmp_path)]), tmp_path)


def test_download_keeps_an_external_chat_server_in_use(app_with, tmp_path):
    # The app gives the installer the same "chat is external" rule the panel shows.
    client = app_with(real=Server(), pc=NVIDIA)
    (tmp_path / "config.toml").write_text(
        "docker_available = false\nchat_base_url = 'http://127.0.0.1:11434/v1'\n",
        encoding="utf-8",
    )
    assert client.get("/api/models/catalog").json()["chat"]["source"] == "external"
    token = step_up(client, "download")
    r = install(client, ["chat-gemma-4-12b"], token, make_default=False)
    assert r.status_code == 202, r.text
    client.portal.call(client.app.state.hk.installer.wait)
    assert load_runtime(tmp_path).chat_server == "external"
    assert client.get("/api/models/catalog").json()["chat"]["source"] == "external"


# ---- cancel and retry -----------------------------------------------------------------------


def test_cancel_and_retry_need_only_the_session(app_with, tmp_path):
    fake = FakeInstaller()
    client = app_with(fake)
    assert client.post("/api/models/install/retry", headers=CSRF).json() == {"reason": "no_job"}
    assert install(client, ["chat-qwen3.5-4b"], step_up(client, "download")).status_code == 202
    busy = client.post("/api/models/install/retry", headers=CSRF)
    assert busy.status_code == 409 and busy.json()["reason"] == "busy"
    cancelled = client.post("/api/models/install/cancel", headers=CSRF)
    assert cancelled.status_code == 200 and cancelled.json()["job"]["state"] == "cancelled"
    retried = client.post("/api/models/install/retry", headers=CSRF)
    assert retried.status_code == 202 and retried.json()["job"]["state"] == "running"
    assert [c[0] for c in fake.calls] == ["start", "retry", "cancel", "retry"]
    names = [e["event"] for e in events(tmp_path)]
    assert "models.install_cancelled" in names and "models.install_retried" in names


def test_cancel_and_retry_need_a_session_and_csrf(app_with):
    client = app_with(FakeInstaller(), sign_in=False)
    for action in ("cancel", "retry"):
        assert client.post(f"/api/models/install/{action}", headers=CSRF).status_code == 401
        no_header = client.post(f"/api/models/install/{action}")
        assert no_header.status_code == 403 and no_header.json() == {"reason": "csrf"}


@pytest.mark.parametrize(
    ("error", "status", "body"),
    [
        (NotEnoughSpace(9, 1), 409, {"reason": "no_space", "needed_bytes": 9, "free_bytes": 1}),
        (NothingToDo(), 409, {"reason": "nothing_to_do"}),
    ],
)
def test_retry_maps_errors_like_start(app_with, error, status, body):
    fake = FakeInstaller()
    client = app_with(fake)
    install(client, ["chat-qwen3.5-4b"], step_up(client, "download"))
    client.post("/api/models/install/cancel", headers=CSRF)
    fake.retry_error = error
    r = client.post("/api/models/install/retry", headers=CSRF)
    assert r.status_code == status and r.json() == body


def test_real_cancel_then_retry(app_with, tmp_path):
    server = Server()
    stall = Stall()
    server.override[QWEN4B] = stall
    client = app_with(real=server, pc=NO_GPU)
    assert install(client, ["chat-qwen3.5-4b"], step_up(client, "download")).status_code == 202
    client.portal.call(stall.sent.wait)
    assert client.post("/api/models/install/cancel", headers=CSRF).status_code == 200
    job = client.portal.call(client.app.state.hk.installer.wait)
    assert job.state == "cancelled"
    del server.override[QWEN4B]
    assert client.post("/api/models/install/retry", headers=CSRF).status_code == 202
    job = client.portal.call(client.app.state.hk.installer.wait)
    assert job.state == "done"


def test_server_stop_stops_the_job(app_with):
    fake = FakeInstaller()
    client = app_with(fake)
    client.__exit__(None, None, None)
    assert ("shutdown",) in fake.calls


# ---- chat switching -------------------------------------------------------------------------


def test_switch_local_marks_restart(app_with, tmp_path):
    install_local_chat(tmp_path)
    control = ServerControl(supervised=True)
    client = app_with(FakeInstaller(), control=control)
    r = client.post(
        "/api/models/chat", json={"source": "local", "card": "qwen3.5-4b"}, headers=CSRF
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "source": "local", "model": "qwen3.5-4b", "base_url": None, "restart_needed": True,
    }  # fmt: skip
    rt = load_runtime(tmp_path)
    assert rt.default_chat_model == "qwen3.5-4b" and rt.chat_server == "local"
    text = (tmp_path / "models.toml").read_text(encoding="utf-8")
    assert "# the owner's notes stay" in text
    assert (tmp_path / "models.toml.before-hearthkeep").is_file()
    assert control.restart_needed == {"models"}
    rows = [e for e in events(tmp_path) if e["event"] == "models.chat_switched"]
    assert rows and rows[0]["detail"] == {"card": "qwen3.5-4b"}


def test_switch_local_refuses_missing_card(app_with, tmp_path):
    control = ServerControl(supervised=True)
    client = app_with(FakeInstaller(), control=control)

    def switch(card):
        return client.post("/api/models/chat", json={"source": "local", "card": card}, headers=CSRF)

    for card in ("qwen3.5-4b", "no-such-card", "z-image-turbo"):
        r = switch(card)
        assert r.status_code == 400 and r.json() == {"reason": "not_installed"}, card
    # Files there but no llama-server: still not installed.
    install_local_chat(tmp_path, exe=False)
    assert switch("qwen3.5-4b").json() == {"reason": "not_installed"}
    assert control.restart_needed == set()
    assert "chat_server" not in (tmp_path / "models.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize("error", [TomlEditError("odd shape"), PermissionError("locked")])
def test_switch_when_models_toml_cant_be_edited(app_with, tmp_path, monkeypatch, error):
    install_local_chat(tmp_path)
    control = ServerControl(supervised=True)
    client = app_with(FakeInstaller(), control=control)

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(service, "edit_toml", refuse)
    r = client.post(
        "/api/models/chat", json={"source": "local", "card": "qwen3.5-4b"}, headers=CSRF
    )
    assert r.status_code == 409 and r.json() == {"reason": "config_not_updated"}
    r = _external(client, "http://127.0.0.1:9000/v1", step_up(client, "settings"), None)
    assert r.status_code == 409 and r.json() == {"reason": "config_not_updated"}
    assert control.restart_needed == set()
    assert not [e for e in events(tmp_path) if e["event"].startswith("models.chat_")]


def test_model_actions_are_rate_limited(app_with):
    client = app_with(FakeInstaller(), limiter=RateLimiter(per_client=2, global_limit=30))
    codes = [client.post("/api/models/install/cancel", headers=CSRF).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def _external(client, url, token, api_key=SECRET):
    body = {"source": "external", "base_url": url, "api_key": api_key, "step_up_token": token}
    return client.post("/api/models/chat", json=body, headers=CSRF)


def test_external_needs_settings_step_up_and_loopback(app_with, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    (tmp_path / "config.toml").write_text("# my config\nport = 8765  # keep\n", encoding="utf-8")
    control = ServerControl(supervised=True)
    client = app_with(FakeInstaller(), control=control)
    secrets = SecretStore(tmp_path)
    responses = []
    good = "http://127.0.0.1:9000/v1"
    for token in (None, "nope", step_up(client, "download"), step_up(client, "server")):
        r = _external(client, good, token)
        responses.append(r.text)
        assert r.status_code == 403 and r.json() == {"reason": "step_up_required"}
    for url in (
        "https://api.example.com/v1",
        "http://10.0.0.5:8080/v1",
        "ftp://127.0.0.1/v1",
        "http://127.0.0.1@evil.example/v1",
        "http://[::1",
        "not a url",
    ):
        r = _external(client, url, step_up(client, "settings"))
        responses.append(r.text)
        assert r.status_code == 400 and r.json() == {"reason": "bad_url"}, url
    assert secrets.get("chat_api_key") is None and control.restart_needed == set()

    r = _external(client, good, step_up(client, "settings"))
    responses.append(r.text)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "source": "external", "model": "gemma-4-12b", "base_url": good, "restart_needed": True,
    }  # fmt: skip
    config = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "# my config" in config and "port = 8765  # keep" in config
    assert f"chat_base_url = '{good}'" in config
    assert load_runtime(tmp_path).chat_server == "external"
    assert secrets.get("chat_api_key") == SECRET
    assert control.restart_needed == {"models"}
    responses.append(client.get("/api/models/catalog").text)

    rows = events(tmp_path)
    assert [e["detail"] for e in rows if e["event"] == "models.chat_external_set"] == [
        {"host": "127.0.0.1"}
    ]
    denied = [e for e in rows if e["event"] == "models.chat_external_denied"]
    assert len(denied) == 4
    everything = [*responses, json.dumps([e["detail"] for e in rows]), config]
    everything.append((tmp_path / "models.toml").read_text(encoding="utf-8"))
    everything += [r.getMessage() for r in caplog.records]
    everything += [repr(r.args) for r in caplog.records]
    assert not any(SECRET in text for text in everything)


def test_external_key_null_keeps_and_empty_deletes(app_with, tmp_path):
    # Chat already uses this server: a null key keeps its stored key.
    good = "http://localhost:9000/v1"
    (tmp_path / "config.toml").write_text(f"chat_base_url = '{good}'\n", encoding="utf-8")
    client = app_with(FakeInstaller())
    secrets = SecretStore(tmp_path)
    secrets.put("chat_api_key", "old-key")
    assert _external(client, good, step_up(client, "settings"), None).status_code == 200
    assert secrets.get("chat_api_key") == "old-key"
    assert _external(client, good, step_up(client, "settings"), "").status_code == 200
    assert secrets.get("chat_api_key") is None


@pytest.mark.parametrize("before", ["local", "none"])
def test_switching_to_external_drops_a_key_from_before(app_with, tmp_path, before):
    # A key stored while chat wasn't external (an old generated one) is never sent to the
    # new server.
    install_local_chat(tmp_path)
    client = app_with(FakeInstaller())
    if before == "local":
        local = {"source": "local", "card": "qwen3.5-4b"}
        assert client.post("/api/models/chat", json=local, headers=CSRF).status_code == 200
    assert client.get("/api/models/catalog").json()["chat"]["source"] == before
    secrets = SecretStore(tmp_path)
    secrets.put("chat_api_key", "stale-key")
    r = _external(client, "http://127.0.0.1:9000/v1", step_up(client, "settings"), None)
    assert r.status_code == 200, r.text
    assert secrets.get("chat_api_key") is None


def test_dropping_a_stale_key_needs_a_readable_secret_store(app_with, tmp_path, monkeypatch):
    install_local_chat(tmp_path)
    client = app_with(FakeInstaller())
    store = client.app.state.hk.secrets

    def unreadable(*args):
        raise SecretStoreError("locked")

    monkeypatch.setattr(store, "names", unreadable)
    r = _external(client, "http://127.0.0.1:9000/v1", step_up(client, "settings"), None)
    assert r.status_code == 503 and r.json()["reason"] == "secrets_unreadable"
    assert not (tmp_path / "config.toml").exists()


def _external_model(client, url, token, model):
    body = {"source": "external", "base_url": url, "api_key": None, "step_up_token": token}
    return client.post("/api/models/chat", json={**body, "model": model}, headers=CSRF)


def test_external_model_name_is_saved_and_kept(app_with, tmp_path):
    (tmp_path / "config.toml").write_text("# mine\nport = 8765\n", encoding="utf-8")
    client = app_with(FakeInstaller())
    good = "http://127.0.0.1:11434/v1"
    r = _external_model(client, good, step_up(client, "settings"), "qwen2.5:7b-instruct")
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "qwen2.5:7b-instruct"
    config = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "chat_model = 'qwen2.5:7b-instruct'" in config and "# mine" in config
    # No name given: the saved one stays.
    r = _external_model(client, good, step_up(client, "settings"), None)
    assert r.status_code == 200 and r.json()["model"] == "qwen2.5:7b-instruct"
    r = _external(client, good, step_up(client, "settings"), None)
    assert r.status_code == 200 and r.json()["model"] == "qwen2.5:7b-instruct"


@pytest.mark.parametrize(
    "model",
    ["m" * 129, 'say "hi"', "it's", "two\nlines", "cr\rhere", "tab\there", "", "   ", "bell\x07"],
)
def test_external_model_name_must_be_plain(app_with, tmp_path, model):
    control = ServerControl(supervised=True)
    client = app_with(FakeInstaller(), control=control)
    r = _external_model(client, "http://127.0.0.1:9000/v1", step_up(client, "settings"), model)
    assert r.status_code == 400 and r.json() == {"reason": "bad_model"}
    assert not (tmp_path / "config.toml").exists() and not (tmp_path / "models.toml").exists()
    assert control.restart_needed == set()


def test_external_model_name_at_the_limit_is_fine(app_with, tmp_path):
    client = app_with(FakeInstaller())
    name = "org/" + "m" * 124
    r = _external_model(client, "http://127.0.0.1:9000/v1", step_up(client, "settings"), name)
    assert r.status_code == 200 and r.json()["model"] == name


def test_external_body_limits(app_with):
    client = app_with(FakeInstaller())
    long_url = "http://127.0.0.1/" + "a" * 200
    assert _external(client, long_url, step_up(client, "settings")).status_code == 422
    long_key = "k" * 513
    r = _external(client, "http://127.0.0.1/v1", step_up(client, "settings"), long_key)
    assert r.status_code == 422 and long_key not in r.text


# ---- the external switch is all or nothing (P14) -------------------------------------------


def _snapshot(data: Path, secrets: SecretStore) -> dict:
    files = {}
    for name in ("models.toml", "config.toml", "models.toml.before-hearthkeep"):
        path = data / name
        files[name] = path.read_bytes() if path.exists() else None
    return {"files": files, "secret": secrets.get("chat_api_key")}


@pytest.mark.parametrize("failing", ["models.toml", "config.toml", "secret"])
def test_external_switch_leaves_everything_as_it_was_when_a_step_fails(
    app_with, tmp_path, monkeypatch, failing
):
    install_local_chat(tmp_path)
    (tmp_path / "config.toml").write_text("# my config\nport = 8765\n", encoding="utf-8")
    control = ServerControl(supervised=True)
    client = app_with(FakeInstaller(), control=control)
    secrets = SecretStore(tmp_path)
    secrets.put("chat_api_key", "old-key")
    before = _snapshot(tmp_path, secrets)
    real_edit = service.edit_toml

    def edit(path, **kwargs):
        if Path(path).name == failing:
            raise TomlEditError("refused")
        real_edit(path, **kwargs)

    monkeypatch.setattr(service, "edit_toml", edit)
    if failing == "secret":
        store = client.app.state.hk.secrets

        def refuse(*args):
            raise SecretStoreError("locked")

        monkeypatch.setattr(store, "put", refuse)
    r = _external(client, "http://127.0.0.1:9000/v1", step_up(client, "settings"))
    assert r.status_code in (409, 503) and SECRET not in r.text
    expected = "secrets_unreadable" if failing == "secret" else "config_not_updated"
    assert r.json()["reason"] == expected
    assert _snapshot(tmp_path, secrets) == before
    assert control.restart_needed == set()
    assert not [e for e in events(tmp_path) if e["event"] == "models.chat_external_set"]


def test_external_switch_checks_the_secret_store_before_editing(app_with, tmp_path, monkeypatch):
    install_local_chat(tmp_path)
    client = app_with(FakeInstaller())
    secrets = SecretStore(tmp_path)
    before = _snapshot(tmp_path, secrets)
    store = client.app.state.hk.secrets

    def unreadable(*args):
        raise SecretStoreError("locked")

    monkeypatch.setattr(store, "names", unreadable)
    edits = []
    monkeypatch.setattr(service, "edit_toml", lambda path, **kw: edits.append(path))
    r = _external(client, "http://127.0.0.1:9000/v1", step_up(client, "settings"))
    assert r.status_code == 503 and r.json()["reason"] == "secrets_unreadable"
    assert edits == [] and _snapshot(tmp_path, secrets) == before


def test_external_url_with_userinfo_query_or_fragment_is_bad(app_with, tmp_path):
    client = app_with(FakeInstaller())
    for url in (
        "http://user:pw@127.0.0.1:9000/v1",
        "http://user@localhost/v1",
        "http://127.0.0.1:9000/v1?key=x",
        "http://127.0.0.1:9000/v1?",
        "http://127.0.0.1:9000/v1#frag",
    ):
        r = _external(client, url, step_up(client, "settings"))
        assert r.status_code == 400 and r.json() == {"reason": "bad_url"}, url
    assert not (tmp_path / "config.toml").exists()


# ---- one lock for every config edit ---------------------------------------------------------


def test_chat_switches_edit_under_the_installers_config_lock(app_with, tmp_path, monkeypatch):
    install_local_chat(tmp_path)
    fake = FakeInstaller()
    client = app_with(fake)
    held = []
    real_edit = service.edit_toml

    def edit(path, **kwargs):
        held.append((Path(path).name, fake.config_lock.locked()))
        real_edit(path, **kwargs)

    monkeypatch.setattr(service, "edit_toml", edit)
    local = {"source": "local", "card": "qwen3.5-4b"}
    assert client.post("/api/models/chat", json=local, headers=CSRF).status_code == 200
    assert (
        _external(client, "http://127.0.0.1:9000/v1", step_up(client, "settings")).status_code
        == 200
    )
    assert held == [("models.toml", True), ("models.toml", True), ("config.toml", True)]


def test_a_switch_waits_for_an_edit_in_progress(app_with, tmp_path):
    install_local_chat(tmp_path)
    fake = FakeInstaller()
    client = app_with(fake)
    lock = fake.config_lock
    local = {"source": "local", "card": "qwen3.5-4b"}

    async def held_switch():
        async with lock:  # another edit (the installer's) is in progress
            task = asyncio.ensure_future(
                asyncio.to_thread(client.post, "/api/models/chat", json=local, headers=CSRF)
            )
            await asyncio.sleep(0.3)
            assert not task.done()
            assert "chat_server" not in (tmp_path / "models.toml").read_text(encoding="utf-8")
        return await task

    response = client.portal.call(held_switch)
    assert response.status_code == 200
    assert load_runtime(tmp_path).chat_server == "local"
