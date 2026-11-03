"""Repos over HTTP: adding needs an `add_repo` step-up, removing a `delete` step-up bound to
exactly that repo; the audit log carries repo ids, never names."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import shutil

import pytest
from fakes import FakeGitHub
from test_api import CSRF, harness, signed_in, step_up  # noqa: F401  (harness is a fixture)
from test_api_delete import target_token
from test_git import REPO, TOKEN, make_origin

from hearthkeep.github.api import TOKEN_SECRET
from hearthkeep.github.git import GitRunner
from hearthkeep.secrets import SecretStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def fake_github():
    fake = FakeGitHub()
    fake.routes[("GET", "/repos/acme/widgets")] = (200, REPO)
    return fake


@pytest.fixture
def origin(tmp_path_factory):
    return make_origin(tmp_path_factory.mktemp("origin"))


@pytest.fixture
def api_client(harness, fake_github, origin):
    SecretStore(harness.data).put(TOKEN_SECRET, TOKEN)
    git = GitRunner(harness.data, protocol="file", remote_url=lambda name: origin.as_uri())
    return signed_in(harness, github_transport=fake_github.transport, git=git)


def add(client, full_name="acme/widgets", token=None):
    if token is None:
        token = step_up(client, "add_repo")
    return client.post(
        "/api/repos", json={"full_name": full_name, "step_up_token": token}, headers=CSRF
    )


def remove(client, repo_id, token):
    return client.request(
        "DELETE", f"/api/repos/{repo_id}", json={"step_up_token": token}, headers=CSRF
    )


def test_repos_need_a_session(harness):
    client = harness.start()
    assert client.get("/api/repos").status_code == 401
    assert add(client, token="x").status_code == 401
    assert remove(client, "r1", "x").status_code == 401
    assert client.post("/api/repos/r1/fetch", headers=CSRF).status_code == 401


def test_add_repo_needs_step_up(harness, api_client, fake_github):
    response = add(api_client, token="nope")
    assert response.status_code == 403
    assert response.json() == {"reason": "step_up_required"}
    assert add(api_client, token=step_up(api_client, "add_folder")).status_code == 403
    assert harness.query("SELECT * FROM repos") == []
    assert not any(r.url.path.startswith("/repos/") for r in fake_github.requests)
    assert list((harness.data / "workspace" / "repos").glob("*")) == []


def test_add_list_and_errors(harness, api_client, fake_github):
    response = add(api_client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["full_name"] == "acme/widgets" and body["default_branch"] == "main"
    assert body["private"] is True
    listed = api_client.get("/api/repos").json()["repos"]
    assert listed == [{**body}]
    assert listed[0]["branch"] == "main" and listed[0]["changed"] == []
    assert listed[0]["ahead"] == 0 and listed[0]["behind"] == 0
    # The origin has no dependency files; with them it would be "needed" until installed.
    assert listed[0]["deps"] == "none" and listed[0]["last_run"] is None

    assert add(api_client).json()["reason"] == "already_added"
    assert add(api_client).status_code == 409
    bad = add(api_client, "not a name")
    assert (bad.status_code, bad.json()["reason"]) == (422, "name_format")
    missing = add(api_client, "acme/gone")
    assert (missing.status_code, missing.json()["reason"]) == (404, "repo_not_found")
    fake_github.routes[("GET", "/repos/acme/broken")] = (200, {**REPO, "full_name": "acme/broken"})
    # the clone can't reach this one's origin
    hk = api_client.app.state.hk
    hk.repos.git._remote_url = lambda name: (harness.data / "nowhere.git").as_uri()
    broken = add(api_client, "acme/broken")
    assert (broken.status_code, broken.json()["reason"]) == (502, "clone_failed")
    assert TOKEN not in broken.text
    assert len(harness.query("SELECT * FROM repos")) == 1


def test_add_without_a_token_is_409(harness, api_client):
    api_client.delete("/api/github/token", headers=CSRF)
    response = add(api_client)
    assert (response.status_code, response.json()["reason"]) == (409, "no_token")


def test_remove_repo_needs_delete_step_up_bound_to_repo(harness, api_client):
    repo_id = add(api_client).json()["id"]
    c = harness.conn()
    c.execute("INSERT INTO repos (id, full_name) VALUES ('other', 'acme/other')")
    c.close()
    folder = harness.data / "workspace" / "repos" / repo_id
    assert folder.is_dir()
    assert remove(api_client, repo_id, target_token(api_client, "repo:other")).status_code == 403
    assert remove(api_client, repo_id, step_up(api_client, "add_repo")).status_code == 403
    assert folder.is_dir()
    assert remove(api_client, "nope", "x").status_code == 404
    response = remove(api_client, repo_id, target_token(api_client, f"repo:{repo_id}"))
    assert response.status_code == 200
    assert not folder.exists()
    assert [r[0] for r in harness.query("SELECT id FROM repos")] == ["other"]


def test_fetch_route(harness, api_client):
    repo_id = add(api_client).json()["id"]
    response = api_client.post(f"/api/repos/{repo_id}/fetch", headers=CSRF)
    assert response.status_code == 200
    assert response.json()["id"] == repo_id
    assert api_client.post("/api/repos/nope/fetch", headers=CSRF).status_code == 404


def test_audit_has_repo_id_never_name(harness, api_client):
    add(api_client, token="nope")
    repo_id = add(api_client).json()["id"]
    api_client.post(f"/api/repos/{repo_id}/fetch", headers=CSRF)
    remove(api_client, repo_id, target_token(api_client, f"repo:{repo_id}"))
    events = harness.events()
    names = [e["event"] for e in events]
    for event in ("repos.added", "repos.fetched", "repos.removed"):
        assert event in names
    for e in events:
        if e["event"] in ("repos.added", "repos.fetched", "repos.removed"):
            assert e["detail"] == {"id": repo_id}
    text = str(events).lower()
    assert "widgets" not in text and "acme" not in text and "github_pat_" not in text


def test_github_state_reports_git_from_the_runner(api_client, monkeypatch):
    monkeypatch.setattr(GitRunner, "available", lambda self: False)
    assert api_client.get("/api/github").json()["git"] is False
    monkeypatch.setattr(GitRunner, "available", lambda self: True)
    assert api_client.get("/api/github").json()["git"] is True


def test_startup_sweeps_left_clones(harness):
    from test_git import plant

    orphan = plant(harness.data / "workspace" / "repos" / "0123456789abcdef")
    staging = plant(harness.data / "runtime" / "clone-0123456789abcdef")
    harness.start()
    assert not orphan.exists() and not staging.exists()


def test_repo_list_shows_dependency_state(harness, api_client):
    added = add(api_client)
    assert added.status_code == 201
    hk = api_client.app.state.hk
    clone = harness.data / "workspace" / "repos" / added.json()["id"]
    (clone / "requirements.txt").write_text("requests==2.32.3\n")
    listed = api_client.get("/api/repos").json()["repos"]
    assert listed[0]["deps"] == "needed"  # nothing installed yet; docker is never asked
    assert hk.registry.get("repo_install") is not None
