"""No chat model set up: the server still starts and says so. Today's configs keep working."""

import logging
import shutil
from pathlib import Path

import pytest
from copy_rules import forbidden_in
from fakes import FakeVerifier
from test_api import CSRF, enroll, new_chat
from test_cli import Serve, write

from hearthkeep.api.app import DB_FILE
from hearthkeep.auth.enrollment import EnrollmentCodes
from hearthkeep.config import EXAMPLE_CONFIG, ConfigError, init_data_dir, load_settings
from hearthkeep.db import connect
from hearthkeep.providers.chat import (
    NO_CHAT_MODEL,
    NoChatModelClient,
    OpenAIChatClient,
    ProviderError,
)
from hearthkeep.providers.scheduled_chat import ScheduledChatClient

UPGRADE = Path(__file__).parent / "fixtures" / "upgrade"


def copy_todays_config(data: Path) -> None:
    """Today's example config as an install copied it (Docker pinned off so no test probes it)."""
    data.mkdir(parents=True, exist_ok=True)
    text = (UPGRADE / "config.toml").read_text(encoding="utf-8")
    (data / "config.toml").write_text(text + "docker_available = false\n", encoding="utf-8")


def copy_owned_models(data: Path, root: Path) -> None:
    """The hand-written models.toml and the owner's card, with `{root}` filled in."""
    text = (UPGRADE / "models.toml").read_text(encoding="utf-8")
    (data / "models.toml").write_text(text.replace("{root}", root.as_posix()), encoding="utf-8")
    cards = data / "models" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(UPGRADE / "my-gemma.toml", cards / "my-gemma.toml")


def issue_code(data: Path) -> str:
    conn = connect(data / DB_FILE)
    try:
        return EnrollmentCodes(conn).issue("first_device", ttl_minutes=15)
    finally:
        conn.close()


def count(data: Path, table: str) -> int:
    conn = connect(data / DB_FILE)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def serve(tmp_path, monkeypatch):
    data = tmp_path / "data"
    copy_todays_config(data)
    return Serve(data, monkeypatch)


@pytest.fixture
def serve_fresh(tmp_path, monkeypatch):
    """`serve` with a config.toml that has no chat_base_url and no models.toml. Passkeys are
    checked by the fake verifier, so `signed_in` can enrol from inside the running server."""
    from hearthkeep.auth import webauthn

    monkeypatch.setattr(webauthn, "PyWebAuthnVerifier", lambda settings: FakeVerifier())
    data = tmp_path / "data"
    write(data, "config.toml", "docker_available = false\n")
    return Serve(data, monkeypatch)


# ---- settings ---------------------------------------------------------------------------


def test_fresh_data_folder_has_no_chat_server(tmp_path):
    init_data_dir(tmp_path)
    assert load_settings(tmp_path).chat_base_url_set is False


def test_todays_config_keeps_its_chat_server(tmp_path):
    shutil.copyfile(UPGRADE / "config.toml", tmp_path / "config.toml")
    settings = load_settings(tmp_path)
    assert settings.chat_base_url_set is True
    assert settings.chat_base_url == "http://127.0.0.1:8080/v1"


def test_chat_base_url_set_is_not_a_config_key(tmp_path):
    (tmp_path / "config.toml").write_text("chat_base_url_set = true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config key: 'chat_base_url_set'"):
        load_settings(tmp_path)


def test_example_config_names_no_placeholder_or_command():
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert forbidden_in(text) == []
    assert '# chat_base_url = "http://127.0.0.1:8080/v1"' in text
    assert '\nchat_model = "gemma-4-12b"' in text


# ---- no chat model ----------------------------------------------------------------------


def test_no_chat_model_server_starts_and_refuses_messages(serve_fresh, capsys):
    seen = {}

    def during(client):
        hk = serve_fresh.hk
        seen["mode"] = hk.chat_mode
        seen["chat"] = hk.addons.get("chat")
        enroll(client, issue_code(serve_fresh.data))
        seen["health"] = client.get("/api/health").json()
        chat = new_chat(client)
        seen["post"] = client.post(f"/api/chats/{chat}/messages", json={"text": "hi"}, headers=CSRF)
        seen["messages"] = count(serve_fresh.data, "messages")

    serve_fresh.during = during
    assert serve_fresh() == 0
    assert seen["mode"] == "none" and seen["chat"].state == "not_set_up"
    assert seen["health"]["chat_model_state"] == "not_set_up"
    assert seen["health"]["chat_model_reachable"] is False
    r = seen["post"]
    assert r.status_code == 409 and r.json()["reason"] == "chat_not_set_up"
    assert r.json()["message"] == seen["chat"].message and seen["chat"].message
    assert seen["messages"] == 0
    out = capsys.readouterr().out
    assert "Chat: no chat model is set up yet. Open Hearthkeep and go to Settings › Add-ons." in out
    assert type(serve_fresh.hk.runner._loop._client) is NoChatModelClient


def test_no_chat_model_health_does_not_probe(serve_fresh):
    calls = []

    def during(client):
        hk = serve_fresh.hk

        async def probe():
            calls.append(1)
            return True

        hk.health_check = probe
        hk.health_cache = None
        calls.clear()
        assert client.get("/api/health").json()["chat_model_state"] == "not_set_up"

    serve_fresh.during = during
    assert serve_fresh() == 0
    assert calls == []


async def test_no_chat_model_client_raises_provider_error():
    with pytest.raises(ProviderError) as e:
        async for _ in NoChatModelClient().stream([], [], False):
            pass
    assert e.value.user_message == NO_CHAT_MODEL
    assert NO_CHAT_MODEL == "No chat model is set up yet. Open Settings › Add-ons to set one up."


def test_no_chat_model_warnings_do_not_name_chat_base_url(serve_fresh, tmp_path, capsys):
    write(serve_fresh.data, "models.toml", f"llama_server_exe = '{tmp_path / 'llama.exe'}'\n")
    assert serve_fresh() == 0
    err = capsys.readouterr().err
    assert "model_dirs" in err and "models.toml" in err
    assert "Hearthkeep won't run llama-server." in err
    assert "chat_base_url" not in err and "<data>" not in err
    assert serve_fresh.hk.chat_mode == "none"


def test_startup_survives_a_refresh_that_raises(serve_fresh, monkeypatch, caplog):
    from hearthkeep import addons

    async def broken(self, *ids):
        raise RuntimeError("private detail")

    monkeypatch.setattr(addons.Addons, "refresh", broken)
    seen = {}

    def during(client):
        seen["health"] = client.get("/api/health").status_code

    serve_fresh.during = during
    with caplog.at_level(logging.WARNING):
        assert serve_fresh() == 0
    assert seen["health"] == 200
    warned = [r.getMessage() for r in caplog.records if "RuntimeError" in r.getMessage()]
    assert warned and all("private detail" not in m for m in warned)


# ---- today's formats --------------------------------------------------------------------


def test_upgrade_external_config_unchanged(serve, capsys):
    assert serve() == 0
    client = serve.hk.runner._loop._client
    assert type(client) is OpenAIChatClient
    assert client._url == "http://127.0.0.1:8080/v1/chat/completions"
    assert serve.hk.chat_mode == "external"
    assert serve.llama_calls == []
    assert serve.hk.addons.get("chat").state in ("ready", "offline")
    out = capsys.readouterr().out
    assert (
        "Chat: using the chat server at chat_base_url (Hearthkeep doesn't start or schedule it)."
        in out
    )
    assert "gemma-4-12b (chat, external server)" in out


def test_upgrade_owned_models_toml_with_ngl_999(serve, tmp_path):
    root = tmp_path / "pc"
    copy_owned_models(serve.data, root)
    (root / "llama").mkdir(parents=True)
    (root / "llama" / "llama-server.exe").write_bytes(b"")
    (root / "models" / "my-gemma").mkdir(parents=True)
    (root / "models" / "my-gemma" / "my-gemma-Q4_K_M.gguf").write_bytes(b"")
    assert serve() == 0
    card, *_ = serve.llama_calls[0]
    assert card.id == "my-gemma"
    assert ("-ngl", "999") in zip(card.launch_args, card.launch_args[1:], strict=False)
    assert isinstance(serve.hk.runner._loop._client, ScheduledChatClient)
    assert serve.hk.chat_mode == "owned" and serve.hk.addons.get("chat").state == "ready"


def test_owned_config_with_missing_files_starts_with_chat_error(serve, tmp_path):
    copy_owned_models(serve.data, tmp_path / "pc")  # neither the exe nor the GGUF exists
    seen = {}

    def during(client):
        seen["health"] = client.get("/api/health").status_code

    serve.during = during
    assert serve() == 0
    assert seen["health"] == 200
    assert serve.hk.chat_mode == "owned"
    status = serve.hk.addons.get("chat")
    assert status.state == "error"
    assert str(tmp_path) not in (status.message or "")


@pytest.mark.parametrize("setup", ["external", "none", "owned_missing", "exe_only"])
def test_console_lines_name_no_placeholder(setup, tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    if setup == "none":
        write(data, "config.toml", "docker_available = false\n")
    else:
        copy_todays_config(data)
    if setup == "owned_missing":
        copy_owned_models(data, tmp_path / "pc")
    if setup == "exe_only":
        write(data, "models.toml", f"llama_server_exe = '{(tmp_path / 'x.exe').as_posix()}'\n")
    assert Serve(data, monkeypatch)() == 0
    captured = capsys.readouterr()
    assert forbidden_in(captured.out + captured.err) == []
    assert "Images: not set up. Open Settings › Add-ons to see what it needs." in captured.out
