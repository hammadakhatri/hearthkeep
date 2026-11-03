"""The GitHub token over HTTP: passkey step-up to save, session only to remove."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import pytest
from fakes import FakeGitHub
from test_api import CSRF, harness, signed_in, step_up  # noqa: F401  (harness is a fixture)

from hearthkeep.github.git import GitRunner
from hearthkeep.secrets import SecretStore

TOKEN = "github_pat_" + "a" * 30


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
def api_client(harness, fake_github):
    return signed_in(harness, github_transport=fake_github.transport)


def put_token(client, token=TOKEN, step_up_token=None):
    if step_up_token is None:
        step_up_token = step_up(client, "github_token")
    return client.put(
        "/api/github/token",
        json={"token": token, "step_up_token": step_up_token},
        headers=CSRF,
    )


def stored(harness):
    return SecretStore(harness.data).get("github_token")


def test_github_needs_a_session(harness):
    client = harness.start()
    assert client.get("/api/github").status_code == 401
    assert put_token(client, step_up_token="x").status_code == 401
    assert client.delete("/api/github/token", headers=CSRF).status_code == 401


def test_get_reports_git_and_no_connection(api_client, monkeypatch):
    monkeypatch.setattr(GitRunner, "available", lambda self: True)
    assert api_client.get("/api/github").json() == {"git": True, "connected": False, "login": None}
    monkeypatch.setattr(GitRunner, "available", lambda self: False)
    assert api_client.get("/api/github").json()["git"] is False


def test_put_token_needs_step_up(harness, api_client, fake_github):
    response = api_client.put(
        "/api/github/token", json={"token": TOKEN, "step_up_token": "nope"}, headers=CSRF
    )
    assert response.status_code == 403
    assert response.json() == {"reason": "step_up_required"}
    assert stored(harness) is None
    assert fake_github.requests == []
    wrong_purpose = step_up(api_client, "add_folder")
    assert put_token(api_client, step_up_token=wrong_purpose).status_code == 403
    assert stored(harness) is None


def test_put_token_bad_format_is_422(harness, api_client, fake_github):
    for bad in ("ghp_" + "a" * 36, "github_pat_short", "github_pat_" + "a" * 30 + " "):
        response = put_token(api_client, bad)
        assert response.status_code == 422
        assert response.json()["reason"] == "token_format"
    assert stored(harness) is None
    assert fake_github.requests == []


def test_put_token_rejected_by_github_is_422(harness, api_client, fake_github):
    fake_github.fail_next(401, body={"message": "Bad credentials"})
    response = put_token(api_client)
    assert response.status_code == 422
    assert response.json()["reason"] == "token_rejected"
    assert "github_pat_" not in response.text
    assert stored(harness) is None
    assert harness.query("SELECT * FROM github_account") == []


def test_put_token_when_github_is_down_is_502(harness, api_client, fake_github):
    fake_github.fail_next(503)
    response = put_token(api_client)
    assert response.status_code == 502
    assert response.json()["reason"] == "github_unavailable"
    assert stored(harness) is None


def test_put_token_saves_and_get_shows_login(harness, api_client, fake_github):
    response = put_token(api_client)
    assert response.status_code == 200
    assert response.json() == {"connected": True, "login": "octo"}
    assert stored(harness) == TOKEN
    assert fake_github.requests[-1].headers["authorization"] == "Bearer " + TOKEN
    assert harness.query("SELECT id, login, user_id FROM github_account") == [(1, "octo", 42)]
    state = api_client.get("/api/github")
    assert state.json()["connected"] is True and state.json()["login"] == "octo"
    assert TOKEN not in state.text


def test_replacing_the_token_keeps_one_account_row(harness, api_client, fake_github):
    put_token(api_client)
    fake_github.routes[("GET", "/user")] = (200, {"login": "hubot", "id": 7})
    other = "github_pat_" + "b" * 30
    assert put_token(api_client, other).json()["login"] == "hubot"
    assert stored(harness) == other
    assert harness.query("SELECT id, login, user_id FROM github_account") == [(1, "hubot", 7)]


def test_step_up_token_is_single_use(api_client):
    token = step_up(api_client, "github_token")
    assert put_token(api_client, step_up_token=token).status_code == 200
    assert put_token(api_client, step_up_token=token).status_code == 403


def test_delete_token_is_session_only(harness, api_client):
    put_token(api_client)
    response = api_client.delete("/api/github/token", headers=CSRF)
    assert response.status_code == 200
    assert stored(harness) is None
    assert harness.query("SELECT * FROM github_account") == []
    assert api_client.get("/api/github").json()["connected"] is False
    assert api_client.delete("/api/github/token", headers=CSRF).status_code == 200  # idempotent
    assert api_client.delete("/api/github/token").status_code == 403  # CSRF still needed


def test_audit_never_holds_token(harness, api_client, fake_github):
    fake_github.fail_next(401, body={"message": "Bad credentials " + TOKEN})
    put_token(api_client)
    put_token(api_client)
    api_client.delete("/api/github/token", headers=CSRF)
    events = harness.events()
    names = [e["event"] for e in events]
    assert "github.token_saved" in names and "github.token_removed" in names
    assert "github_pat_" not in str(events)


def test_step_up_purposes_for_github(api_client):
    for purpose in ("github_token", "add_repo"):
        assert step_up(api_client, purpose)


def test_delete_step_up_for_repo_target_needs_an_existing_repo(harness, api_client):
    def options(target):
        return api_client.post(
            "/api/auth/stepup/options",
            json={"purpose": "delete", "target": target},
            headers=CSRF,
        )

    assert options("repo:r1").status_code == 404
    c = harness.conn()
    c.execute("INSERT INTO repos (id, full_name) VALUES ('r1', 'acme/widgets')")
    c.close()
    assert options("repo:r1").status_code == 200
    assert options("repo:bad id").status_code == 422
