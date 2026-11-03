"""The GitHub REST client: headers, no environment proxy, allowed paths, safe error text."""

import httpx
import pytest
from fakes import FakeGitHub

from hearthkeep.github.api import (
    GitHubClient,
    GitHubError,
    GitHubUser,
    RepoInfo,
    noreply_email,
)

TOKEN = "github_pat_" + "a" * 30
UNAUTHORIZED = "GitHub didn't accept the token. Check it hasn't expired, then save it again."


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def client(fake_github):
    return GitHubClient(lambda: TOKEN, transport=fake_github.transport)


async def test_client_sends_headers_and_no_env_proxy(fake_github, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    client = GitHubClient(lambda: TOKEN, transport=fake_github.transport)
    assert await client.user() == GitHubUser("octo", 42)
    sent = fake_github.requests[-1]
    assert str(sent.url) == "https://api.github.com/user"
    assert sent.headers["x-github-api-version"] == "2022-11-28"
    assert sent.headers["authorization"] == "Bearer " + TOKEN
    assert sent.headers["accept"] == "application/vnd.github+json"
    assert sent.headers["user-agent"] == "Hearthkeep"


async def test_http_client_ignores_env_proxies_and_redirects(fake_github, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    made: list[httpx.AsyncClient] = []
    real = httpx.AsyncClient

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", Spy)
    await GitHubClient(lambda: TOKEN, transport=fake_github.transport).user()
    assert len(made) == 1
    assert made[0].trust_env is False
    assert made[0].follow_redirects is False
    assert made[0].timeout == httpx.Timeout(20)
    assert made[0]._mounts == {}


async def test_explicit_token_checks_one_not_yet_saved(fake_github):
    client = GitHubClient(lambda: None, transport=fake_github.transport)
    other = "github_pat_" + "b" * 30
    assert (await client.user(other)).login == "octo"
    assert fake_github.requests[-1].headers["authorization"] == "Bearer " + other


async def test_no_token_is_unauthorized_without_a_request(fake_github):
    client = GitHubClient(lambda: None, transport=fake_github.transport)
    with pytest.raises(GitHubError) as e:
        await client.user()
    assert e.value.reason == "unauthorized"
    assert fake_github.requests == []


async def test_repo_info(client, fake_github):
    fake_github.routes[("GET", "/repos/acme/widgets")] = (
        200,
        {
            "full_name": "Acme/Widgets",
            "default_branch": "trunk",
            "private": True,
            "permissions": {"push": True},
        },
    )
    assert await client.repo("acme/widgets") == RepoInfo("Acme/Widgets", "trunk", True, True)


async def test_repo_refuses_a_bad_name(client, fake_github):
    for bad in ("acme", "acme/..", "-acme/widgets", "acme/wid gets", "acme/widgets/extra"):
        with pytest.raises(GitHubError) as e:
            await client.repo(bad)
        assert e.value.reason == "refused", bad
    assert fake_github.requests == []


async def test_request_refuses_paths_outside_added_repos(client, fake_github):
    for path in (
        "/orgs/acme/repos",
        "/user/repos",
        "/repos",
        "/repos/acme",
        "/repos/acme/widgets/../../orgs/x",
        "/repos/acme/widgets/issues?per_page=100",
        "/repos/-bad/widgets",
        "/repos/acme/..",
        "/repos/acme/widgets/%2e%2e/x",
        "https://evil.example/user",
    ):
        with pytest.raises(GitHubError) as e:
            await client.request("GET", path)
        assert e.value.reason == "refused", path
    assert fake_github.requests == []


async def test_request_allows_user_and_repo_paths(client, fake_github):
    fake_github.routes[("GET", "/repos/acme/widgets/pulls")] = (200, [{"number": 1}])
    assert await client.request("GET", "/user") == {"login": "octo", "id": 42}
    got = await client.request("GET", "/repos/acme/widgets/pulls", params={"state": "open"})
    assert got == [{"number": 1}]
    assert fake_github.requests[-1].url.params["state"] == "open"


async def test_error_message_never_contains_token(client, fake_github):
    fake_github.fail_next(401, body={"message": "Bad credentials " + TOKEN})
    with pytest.raises(GitHubError) as e:
        await client.user()
    assert "github_pat_" not in e.value.message
    assert "github_pat_" not in str(e.value)
    assert e.value.reason == "unauthorized"
    assert e.value.status == 401
    assert e.value.message == UNAUTHORIZED


@pytest.mark.parametrize(
    ("status", "headers", "reason"),
    [
        (401, {}, "unauthorized"),
        (404, {}, "not_found"),
        (403, {}, "forbidden"),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),
        (429, {}, "rate_limited"),
        (500, {}, "unavailable"),
        (302, {}, "unavailable"),
    ],
)
async def test_statuses_map_to_reasons(status, headers, reason):
    def handler(request):
        return httpx.Response(status, json={"message": "x"}, headers=headers)

    client = GitHubClient(lambda: TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError) as e:
        await client.user()
    assert (e.value.reason, e.value.status) == (reason, status)


async def test_network_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom " + TOKEN)

    client = GitHubClient(lambda: TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubError) as e:
        await client.user()
    assert e.value.reason == "unavailable"
    assert e.value.status is None
    assert e.value.message == "Can't reach GitHub right now."
    assert TOKEN not in str(e.value)
    assert TOKEN not in repr(e.value.__cause__)


def test_noreply_email():
    assert noreply_email(GitHubUser("octo", 42)) == "42+octo@users.noreply.github.com"
