"""Hearthkeep's local connections never go through a system or environment proxy."""

import httpx
import pytest

from hearthkeep.api.app import chat_model_health_check
from hearthkeep.config import Settings
from hearthkeep.providers.chat import OpenAIChatClient, ProviderError

PROXY = "http://127.0.0.1:9"  # nothing listens there


@pytest.fixture
def created(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, PROXY)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    clients: list[httpx.AsyncClient] = []
    real = httpx.AsyncClient

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", Spy)
    return clients


def assert_direct(client: httpx.AsyncClient) -> None:
    assert client.trust_env is False
    assert client._mounts == {}  # no proxy transport mounted for any URL pattern


def test_env_proxy_is_honoured_by_default_sanity(created):
    # Guards the test itself: with the environment trusted, httpx would mount the proxy.
    client = httpx.AsyncClient()
    assert client._mounts != {}


async def test_chat_client_ignores_env_proxies(created):
    chat = OpenAIChatClient("http://127.0.0.1:1/v1", "sk-test-secret-key", "m")
    with pytest.raises(ProviderError):
        async for _ in chat.stream([{"role": "user", "content": "hi"}], [], False):
            pass
    assert len(created) == 1
    assert_direct(created[0])


async def test_health_check_ignores_env_proxies(created, tmp_path):
    settings = Settings(data_dir=tmp_path, chat_base_url="http://127.0.0.1:1/v1")
    assert await chat_model_health_check(settings)() is False
    assert len(created) == 1
    assert_direct(created[0])
