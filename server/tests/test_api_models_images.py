"""/api/models and /api/images, and the model stack wired into the app."""

import sqlite3

import pytest
from fakes import FakeBackend, FakeChatClient, FakeVerifier, card
from fastapi.testclient import TestClient
from test_api import ORIGIN, enroll

from hearthkeep.api.app import DB_FILE, create_app
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.images import ImageStore
from hearthkeep.models.cards import CardError
from hearthkeep.providers.scheduled_chat import ScheduledChatClient
from hearthkeep.runtime.stack import ModelStack
from hearthkeep.tools.base import ToolRegistry
from hearthkeep.tools.demo import ClockTool

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-data"
CHAT = card("chat-model", 11.0)
ZIMG = card("zimg", 10.0, kind="image")
FLUX = card("flux", 9.0, kind="image")


class FakeProcess:
    def __init__(self, name: str, stopped: list) -> None:
        self.name = name
        self._stopped = stopped

    async def stop(self, timeout: float = 10.0) -> None:
        self._stopped.append((self.name, timeout))


@pytest.fixture
def start(tmp_path):
    clients: list[TestClient] = []

    def _start(stack: ModelStack | None = None, *, sign_in: bool = True):
        app = create_app(
            Settings(data_dir=tmp_path, docker_available=False),
            verifier=FakeVerifier(),
            chat_client=FakeChatClient([]),
            registry=_registry(),
            health_check=_healthy,
            web_dist=None,
            models=stack,
        )
        client = TestClient(app, base_url=ORIGIN)
        client.__enter__()
        clients.append(client)
        if sign_in:
            conn = connect(tmp_path / DB_FILE)
            from hearthkeep.auth.enrollment import EnrollmentCodes

            code = EnrollmentCodes(conn).issue("first_device", ttl_minutes=15)
            conn.close()
            enroll(client, code)
        return client

    yield _start
    for client in clients:
        client.__exit__(None, None, None)


def _registry():
    registry = ToolRegistry()
    registry.register(ClockTool())
    return registry


async def _healthy():
    return True


def _stack(**kw) -> ModelStack:
    backend = FakeBackend()
    values = dict(
        cards={c.id: c for c in (CHAT, ZIMG, FLUX)},
        card_errors=[CardError("broken.toml", "missing required field kind")],
        managed=frozenset({"chat-model", "zimg", "flux"}),
        backends={"llama_server": backend, "comfyui": backend},
        chat_model_id="chat-model",
        comfy=object(),
        default_image_model="zimg",
        gpu_vram_gb=16.0,
    )
    values.update(kw)
    return ModelStack(**values)


def _save_images(data_dir, n: int) -> list:
    conn = connect(data_dir / DB_FILE)
    store = ImageStore(conn, data_dir)
    records = []
    for i in range(n):
        rec = store.save(PNG, chat_id=None, turn_id=None, model_id="zimg", prompt=f"p{i}",
                         width=1024, height=1024, seed=i)  # fmt: skip
        stamp = f"2026-10-2{i}T10:00:00.000000+00:00"
        conn.execute("UPDATE images SET created_at = ? WHERE id = ?", (stamp, rec.id))
        records.append((rec.id, stamp))
    conn.close()
    return records


# ---- /api/models ----------------------------------------------------------------------------


def test_models_requires_session(start):
    client = start(_stack(), sign_in=False)
    response = client.get("/api/models")
    assert response.status_code == 401 and response.json() == {"reason": "unauthenticated"}


def test_models_endpoint_lists_card_errors(start):
    body = start(_stack()).get("/api/models").json()
    assert body["errors"] == [
        {"id_or_file": "broken.toml", "message": "missing required field kind"}
    ]
    assert [m["id"] for m in body["models"]] == ["chat-model", "flux", "zimg"]
    flux = next(m for m in body["models"] if m["id"] == "flux")
    assert flux == {
        "id": "flux", "name": "Flux", "kind": "image", "capabilities": [], "vram_gb": 9.0,
        "source": "shipped", "resident": False, "managed": True,
    }  # fmt: skip


def test_models_shows_resident_and_gpu_status(start):
    client = start(_stack())
    gpu = client.app.state.hk.gpu

    async def use_chat():
        async with gpu.lease("chat-model"):
            pass

    client.portal.call(use_chat)
    body = client.get("/api/models").json()
    resident = {m["id"]: m["resident"] for m in body["models"]}
    assert resident == {"chat-model": True, "flux": False, "zimg": False}
    assert body["gpu"] == {"total_gb": 16.0, "used_gb": 11.0, "resident": ["chat-model"],
                           "waiting": 0}  # fmt: skip


def test_models_external_chat_card_is_not_managed(start):
    stack = _stack(managed=frozenset({"zimg", "flux"}), chat_model_id=None)
    body = start(stack).get("/api/models").json()
    managed = {m["id"]: m["managed"] for m in body["models"]}
    assert managed == {"chat-model": False, "flux": True, "zimg": True}


def test_default_stack_manages_nothing(start):
    client = start()
    body = client.get("/api/models").json()
    assert body["models"] == [] and body["errors"] == []
    assert body["gpu"]["resident"] == []
    hk = client.app.state.hk
    assert hk.registry.get("generate_image") is None


def test_stack_wires_image_tool_and_scheduled_chat(start):
    client = start(_stack())
    hk = client.app.state.hk
    assert hk.registry.get("generate_image") is not None
    assert isinstance(hk.runner._loop._client, ScheduledChatClient)


def test_app_shutdown_stops_processes(tmp_path):
    stopped: list = []
    stack = _stack(processes=[FakeProcess("llama-server", stopped),
                              FakeProcess("ComfyUI", stopped)])  # fmt: skip
    app = create_app(
        Settings(data_dir=tmp_path, docker_available=False),
        verifier=FakeVerifier(),
        chat_client=FakeChatClient([]),
        registry=_registry(),
        health_check=_healthy,
        web_dist=None,
        models=stack,
    )
    with TestClient(app, base_url=ORIGIN):
        assert stopped == []
    assert stopped == [("llama-server", 10.0), ("ComfyUI", 10.0)]


# ---- /api/images ----------------------------------------------------------------------------


def test_images_list_newest_first(start, tmp_path):
    client = start()
    saved = _save_images(tmp_path, 3)
    body = client.get("/api/images").json()
    assert [i["id"] for i in body["images"]] == [saved[2][0], saved[1][0], saved[0][0]]
    assert set(body["images"][0]) == {"id", "model_id", "prompt", "width", "height",
                                      "created_at"}  # fmt: skip
    page = client.get("/api/images", params={"before": saved[2][1], "limit": 1}).json()
    assert [i["id"] for i in page["images"]] == [saved[1][0]]


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"before": "yesterday"}, {"before": "2026-13-01"}]
)
def test_images_list_rejects_bad_params(start, params):
    response = start().get("/api/images", params=params)
    assert response.status_code == 422 and response.json()["reason"] == "invalid_request"


def test_images_list_requires_session(start):
    assert start(sign_in=False).get("/api/images").status_code == 401


def test_image_file_served_with_png_type(start, tmp_path):
    client = start()
    image_id = _save_images(tmp_path, 1)[0][0]
    response = client.get(f"/api/images/{image_id}/file")
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_image_file_rejects_unknown_and_traversal(start, tmp_path):
    client = start()
    (tmp_path / "secret.png").write_bytes(PNG)
    deleted = _save_images(tmp_path, 1)[0][0]
    (tmp_path / "workspace" / "output" / "images" / f"{deleted}.png").unlink()
    for path in (
        "/api/images/..%2F..%2Fconfig.toml/file",
        "/api/images/nope/file",
        "/api/images/..%2F..%2Fsecret/file",
        f"/api/images/{'a' * 33}/file",
        "/api/images/a.b/file",
        f"/api/images/{deleted}/file",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"reason": "not_found"}, path


def test_image_file_requires_session(start, tmp_path):
    client = start(sign_in=False)
    image_id = _save_images(tmp_path, 1)[0][0]
    response = client.get(f"/api/images/{image_id}/file")
    assert response.status_code == 401 and response.json() == {"reason": "unauthenticated"}


def test_image_file_vanished_after_lookup_is_404(start, tmp_path, monkeypatch):
    client = start()
    image_id = _save_images(tmp_path, 1)[0][0]
    store = client.app.state.hk.images
    monkeypatch.setattr(store, "path", lambda _id: tmp_path / "gone.png")
    response = client.get(f"/api/images/{image_id}/file")
    assert response.status_code == 404 and response.json() == {"reason": "not_found"}


def test_health_external_and_owned_states(start):
    external = start(_stack(chat_model_id=None), sign_in=False).get("/api/health").json()
    assert external["chat_model_reachable"] is True and external["chat_model_state"] == "ready"
    owned = start(_stack(), sign_in=False).get("/api/health").json()
    assert owned["chat_model_reachable"] is True
    assert owned["chat_model_state"] == "starts_on_first_use"


class _BrokenStack(ModelStack):
    def image_tool(self, gpu, store):
        raise RuntimeError("setup failed")


def test_lifespan_closes_connection_when_model_setup_fails(tmp_path, monkeypatch):
    import hearthkeep.api.app as app_module

    opened = []
    real_connect = app_module.db.connect

    def connect_and_keep(path):
        conn = real_connect(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(app_module.db, "connect", connect_and_keep)
    stopped: list = []
    stack = _BrokenStack(**{**_stack().__dict__, "processes": [FakeProcess("ComfyUI", stopped)]})
    app = create_app(
        Settings(data_dir=tmp_path, docker_available=False),
        verifier=FakeVerifier(),
        chat_client=FakeChatClient([]),
        registry=_registry(),
        health_check=_healthy,
        web_dist=None,
        models=stack,
    )
    with pytest.raises(RuntimeError, match="setup failed"), TestClient(app, base_url=ORIGIN):
        pass
    [conn] = opened
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert stopped == [("ComfyUI", 10.0)]


def test_models_unreadable_card_error_has_no_path(start, tmp_path):
    from hearthkeep.models.cards import load_cards

    (tmp_path / "models" / "cards" / "unreadable.toml").mkdir(parents=True)
    cards, errors = load_cards(tmp_path)
    response = start(ModelStack(cards=cards, card_errors=errors)).get("/api/models")
    [error] = response.json()["errors"]
    assert error["id_or_file"] == "unreadable.toml"
    assert error["message"].startswith("cannot read card: ")
    assert tmp_path.name not in response.text and "\\\\" not in response.text
