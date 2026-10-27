"""Allowed folders over HTTP: listing, adding (with a passkey step-up) and removing."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import json

import pytest
from test_api import CSRF, harness, signed_in, step_up  # noqa: F401  (harness is a fixture)

from hearthkeep import folders as folders_module


@pytest.fixture
def pc(tmp_path_factory):
    """Somewhere on the PC outside the harness's data folder (which is its tmp_path)."""
    return tmp_path_factory.mktemp("pc")


@pytest.fixture(autouse=True)
def test_home(monkeypatch, pc):
    """pytest's tmp folder is under AppData on Windows (protected): the tests protect only the
    data folder and the repository. The home folder is a test one."""
    monkeypatch.setattr(folders_module, "system_paths", lambda: [])
    home = pc / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(folders_module, "_home", lambda: home)
    return home


def shared(pc, name="Shared"):
    folder = pc / name
    folder.mkdir(parents=True)
    return folder


def add(client, path, token):
    return client.post("/api/folders", json={"path": str(path), "step_up_token": token},
                       headers=CSRF)  # fmt: skip


def test_folders_require_session(harness):
    client = harness.start()
    assert client.get("/api/folders").status_code == 401
    response = client.post("/api/folders", json={"path": "x", "step_up_token": "t"}, headers=CSRF)
    assert response.status_code == 401
    assert client.delete("/api/folders/abc", headers=CSRF).status_code == 401


def test_add_folder_needs_step_up_and_is_audited_without_path(harness, pc, test_home):
    client = signed_in(harness)
    folder = shared(pc)

    denied = add(client, folder, "nope")
    assert denied.status_code == 403 and denied.json() == {"reason": "step_up_required"}
    wrong = add(client, folder, step_up(client, "settings"))
    assert wrong.status_code == 403
    assert client.get("/api/folders").json()["folders"] == []

    response = add(client, folder, step_up(client, "add_folder"))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {"id": body["id"], "label": "Shared", "path": str(folder.resolve())}
    listed = client.get("/api/folders").json()
    assert listed["folders"] == [body]
    assert listed["suggestions"] == [
        {"label": "Documents", "path": str((test_home / "Documents").resolve())}
    ]

    refused = add(client, harness.data, step_up(client, "add_folder"))  # the data folder
    assert refused.status_code == 422 and refused.json()["reason"] == "folder_refused"
    assert str(harness.data) not in refused.text
    again = add(client, folder, step_up(client, "add_folder"))
    assert again.status_code == 422
    too_long = client.post("/api/folders", json={"path": "x" * 1025, "step_up_token": "t"},
                           headers=CSRF)  # fmt: skip
    assert too_long.status_code == 422

    events = harness.events()
    added = [e for e in events if e["event"] == "folders.added"]
    assert [e["detail"] for e in added] == [{"id": body["id"], "label": "Shared"}]
    assert any(e["event"] == "folders.add_denied" for e in events)
    everything = json.dumps(events)
    assert str(folder) not in everything and json.dumps(str(folder))[1:-1] not in everything


def test_remove_folder_needs_only_session(harness, pc):
    client = signed_in(harness)
    folder = shared(pc)
    body = add(client, folder, step_up(client, "add_folder")).json()

    response = client.delete(f"/api/folders/{body['id']}", headers=CSRF)
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert client.get("/api/folders").json()["folders"] == []
    assert client.delete(f"/api/folders/{body['id']}", headers=CSRF).status_code == 404
    removed = [e for e in harness.events() if e["event"] == "folders.removed"]
    assert [e["detail"] for e in removed] == [{"id": body["id"]}]
    assert folder.is_dir()  # removing never touches the folder itself


def test_files_tool_is_registered(harness):
    client = signed_in(harness)
    hk = client.app.state.hk
    assert hk.registry.get("files") is not None and hk.folders is not None
    items = {t["tool"]: t for t in client.get("/api/settings/tools").json()}
    assert items["files"]["tools"] == ["files"] and items["files"]["level"] == "allowed"
