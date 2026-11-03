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


def _listed(full_name: str, push: bool = False) -> dict:
    return {
        "full_name": full_name,
        "default_branch": "main",
        "private": False,
        "permissions": {"push": push},
    }


async def test_repo_info(client, fake_github):
    fake_github.repos = [
        _listed("acme/other"),
        {
            "full_name": "Acme/Widgets",
            "default_branch": "trunk",
            "private": True,
            "permissions": {"push": True},
        },
    ]
    assert await client.repo("acme/widgets") == RepoInfo("Acme/Widgets", "trunk", True, True)
    sent = fake_github.requests[-1]
    assert sent.method == "GET"
    assert sent.url.path == "/user/repos"
    assert dict(sent.url.params) == {"per_page": "100", "page": "1"}


async def test_repo_without_push_permission(client, fake_github):
    fake_github.repos = [{"full_name": "acme/widgets", "default_branch": "main", "private": False}]
    assert await client.repo("acme/widgets") == RepoInfo("acme/widgets", "main", False, False)


async def test_repo_lookup_ignores_case(client, fake_github):
    fake_github.repos = [_listed("Acme/Widgets", push=True)]
    for typed in ("acme/widgets", "ACME/WIDGETS", "aCmE/wIdGeTs"):
        info = await client.repo(typed)
        assert info.full_name == "Acme/Widgets", typed
        assert info.can_push is True


async def test_repo_missing_from_the_list_is_not_found(client, fake_github):
    fake_github.repos = [_listed("acme/widgets"), _listed("acme/widgets-two")]
    with pytest.raises(GitHubError) as e:
        await client.repo("acme/gadgets")
    assert e.value.reason == "not_found"
    # A short page is the last one: no further page is asked for.
    assert [r.url.params["page"] for r in fake_github.requests] == ["1"]


async def test_repo_lookup_pages_until_it_finds_the_repo(client, fake_github):
    fake_github.repos = [_listed(f"acme/r{i}") for i in range(100)] + [_listed("acme/widgets")]
    assert (await client.repo("acme/widgets")).full_name == "acme/widgets"
    assert [r.url.params["page"] for r in fake_github.requests] == ["1", "2"]
    assert all(r.url.params["per_page"] == "100" for r in fake_github.requests)


async def test_repo_lookup_stops_after_ten_pages(client, fake_github):
    fake_github.repos = [_listed(f"acme/r{i}") for i in range(1100)] + [_listed("acme/widgets")]
    with pytest.raises(GitHubError) as e:
        await client.repo("acme/widgets")
    assert e.value.reason == "not_found"
    assert [r.url.params["page"] for r in fake_github.requests] == [str(p) for p in range(1, 11)]


async def test_repo_lookup_never_puts_the_typed_name_in_the_request(client, fake_github):
    typed = "Typed-Owner/Typed.Name"
    fake_github.repos = [_listed(f"acme/r{i}") for i in range(100)] + [_listed(typed.lower())]
    await client.repo(typed)
    with pytest.raises(GitHubError):
        await client.repo("typed-owner/missing-name")
    assert len(fake_github.requests) == 4  # two pages for each lookup
    for sent in fake_github.requests:
        assert sent.url.path == "/user/repos"
        url = str(sent.url).lower()
        for part in ("typed-owner", "typed.name", "missing-name"):
            assert part not in url


async def test_repo_lookup_rejects_a_malformed_page(client, fake_github):
    fake_github.routes[("GET", "/user/repos")] = (200, {"message": "not a list"})
    with pytest.raises(GitHubError) as e:
        await client.repo("acme/widgets")
    assert e.value.reason == "unavailable"


async def test_repo_lookup_passes_github_errors_on(client, fake_github):
    fake_github.fail_next(401)
    with pytest.raises(GitHubError) as e:
        await client.repo("acme/widgets")
    assert e.value.reason == "unauthorized"


async def test_repo_refuses_a_bad_name(client, fake_github):
    for bad in ("acme", "acme/..", "-acme/widgets", "acme/wid gets", "acme/widgets/extra"):
        with pytest.raises(GitHubError) as e:
            await client.repo(bad)
        assert e.value.reason == "refused", bad
    assert fake_github.requests == []


async def test_request_refuses_paths_outside_added_repos(client, fake_github):
    for path in (
        "/orgs/acme/repos",
        "/user/repos/x",
        "/user/emails",
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
    fake_github.repos = [_listed("acme/widgets")]
    assert await client.request("GET", "/user/repos", params={"per_page": 100, "page": 1}) == [
        _listed("acme/widgets")
    ]


async def test_request_allows_only_get_on_user_repos(client, fake_github):
    # POST /user/repos would create a repo; nothing here does that.
    for method in ("POST", "PATCH", "PUT"):
        with pytest.raises(GitHubError) as e:
            await client.request(method, "/user/repos", json={"name": "x"})
        assert e.value.reason == "refused", method
    assert fake_github.requests == []


async def test_request_refuses_methods_other_than_get_post_patch_put(client, fake_github):
    for method in ("DELETE", "delete", "HEAD", "OPTIONS", "TRACE", "CONNECT", ""):
        with pytest.raises(GitHubError) as e:
            await client.request(method, "/repos/acme/widgets/pulls/1")
        assert e.value.reason == "refused", method
    assert fake_github.requests == []
    fake_github.routes[("PATCH", "/repos/acme/widgets/issues/1")] = (200, {"number": 1})
    assert await client.request("PATCH", "/repos/acme/widgets/issues/1", json={}) == {"number": 1}


async def test_request_can_ask_for_a_diff_as_text(client, fake_github):
    path = "/repos/acme/widgets/pulls/3"
    fake_github.routes[("GET", path)] = (200, {"number": 3})
    fake_github.routes[("GET", path, "application/vnd.github.diff")] = (200, "diff --git a b\n")
    got = await client.request("GET", path, accept="application/vnd.github.diff")
    assert got == "diff --git a b\n"
    assert fake_github.requests[-1].headers["accept"] == "application/vnd.github.diff"
    assert await client.request("GET", path) == {"number": 3}  # default header unchanged
    assert fake_github.requests[-1].headers["accept"] == "application/vnd.github+json"
    with pytest.raises(GitHubError) as e:
        await client.request("GET", path, accept="text/html")
    assert e.value.reason == "refused"


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
