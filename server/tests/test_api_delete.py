"""Deleting a chat or a file over HTTP: a passkey step-up bound to exactly that target."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import json
import time

from test_api import (  # noqa: F401  (harness is a fixture)
    CSRF,
    assertion,
    harness,
    new_chat,
    note_turn,
    read_events,
    say,
    signed_in,
    step_up,
    wait_for_approval,
)
from test_deletion import A, B, seed_chat


def target_token(client, target, purpose="delete"):
    payload = {"purpose": purpose, "target": target}
    options = client.post("/api/auth/stepup/options", json=payload, headers=CSRF)
    assert options.status_code == 200, options.text
    body = options.json()
    verified = client.post(
        "/api/auth/stepup/verify",
        json={**payload, "challenge_id": body["challenge_id"],
              "response": assertion("cred-1", body["options"])},
        headers=CSRF,
    )  # fmt: skip
    assert verified.status_code == 200, verified.text
    return verified.json()["step_up_token"]


def delete(client, url, token):
    return client.request("DELETE", url, json={"step_up_token": token}, headers=CSRF)


def seeded(harness):
    client = signed_in(harness)
    conn = harness.conn()
    try:
        a = seed_chat(conn, harness.data, A, "A")
        b = seed_chat(conn, harness.data, B, "B")
    finally:
        conn.close()
    return client, a, b


def chat_ids(client):
    return [c["id"] for c in client.get("/api/chats").json()]


def test_delete_requires_step_up_bound_to_target(harness):
    client, a, b = seeded(harness)

    assert delete(client, f"/api/chats/{A}", "nope").status_code == 403
    missing = client.request("DELETE", f"/api/chats/{A}", json={}, headers=CSRF)
    assert missing.status_code == 422
    for purpose in ("settings", "add_device", "add_folder"):
        refused = delete(client, f"/api/chats/{A}", step_up(client, purpose))
        assert refused.status_code == 403 and refused.json() == {"reason": "step_up_required"}
    # A token for chat A can't delete chat B, nor a file of A; using it there spends it.
    for_a = target_token(client, f"chat:{A}")
    assert delete(client, f"/api/chats/{B}", for_a).status_code == 403
    assert delete(client, f"/api/chats/{A}", for_a).status_code == 403
    assert delete(client, "/api/files/fileA", target_token(client, f"chat:{A}")).status_code == 403
    assert sorted(chat_ids(client)) == [A, B]

    done = delete(client, f"/api/chats/{A}", target_token(client, f"chat:{A}"))
    assert done.status_code == 200, done.text
    assert done.json()["deleted"]["chats"] == 1
    assert done.json()["deleted"]["chat_files"] == 2
    assert chat_ids(client) == [B]
    assert not a["chat_dir"].exists() and b["chat_dir"].is_dir()
    assert client.get(f"/api/chats/{A}/messages").status_code == 404
    gone = delete(client, f"/api/chats/{A}", target_token(client, f"chat:{A}"))
    assert gone.status_code == 404
    assert delete(client, "/api/chats/NOT-A-CHAT", "x").status_code == 404

    events = harness.events()
    deleted = [e["detail"] for e in events if e["event"] == "chat.deleted"]
    assert deleted == [{"chat_id": A, "counts": done.json()["deleted"]}]
    assert any(e["event"] == "chat.delete_denied" for e in events)
    everything = json.dumps(events)
    assert "bank statement" not in everything and "Chat A" not in everything


def test_a_delete_token_works_once(harness):
    client, a, _ = seeded(harness)
    token = target_token(client, "file:fileA")
    assert delete(client, "/api/files/fileA", token).status_code == 200
    conn = harness.conn()
    try:  # the same file again, as if it had never gone
        conn.execute("INSERT INTO chat_files (id, chat_id, name, kind, file_name) "
                     "VALUES ('fileA', ?, 'x.csv', 'csv', 'fileA.csv')", (A,))  # fmt: skip
    finally:
        conn.close()
    assert delete(client, "/api/files/fileA", token).status_code == 403


def test_delete_chat_while_turn_running_is_refused(harness):
    client = signed_in(harness, note_turn())
    chat_id = new_chat(client)
    turn_id = say(client, chat_id, "note hello").json()["turn_id"]
    approval = wait_for_approval(client)
    token = target_token(client, f"chat:{chat_id}")

    busy = delete(client, f"/api/chats/{chat_id}", token)
    assert busy.status_code == 409 and busy.json() == {"reason": "turn_in_progress"}
    conn = harness.conn()
    try:
        conn.execute(
            "INSERT INTO chat_files (id, chat_id, name, kind, file_name) "
            "VALUES ('busyfile', ?, 'x.csv', 'csv', 'busyfile.csv')",
            (chat_id,),
        )
    finally:
        conn.close()
    file_token = target_token(client, "file:busyfile")
    busy_file = delete(client, "/api/files/busyfile", file_token)
    assert busy_file.status_code == 409 and busy_file.json() == {"reason": "turn_in_progress"}
    assert chat_id in chat_ids(client)

    approved = client.post(f"/api/approvals/{approval['id']}", json={"approve": True},
                           headers=CSRF)  # fmt: skip
    assert approved.status_code == 200, approved.text
    read_events(client, chat_id, turn_id)
    deadline = time.monotonic() + 5
    while client.get("/api/chats").json()[0]["active_turn"] and time.monotonic() < deadline:
        time.sleep(0.02)
    # The refusal didn't spend the token.
    done = delete(client, f"/api/chats/{chat_id}", token)
    assert done.status_code == 200, done.text
    assert done.json()["deleted"]["messages"] >= 2
    assert chat_ids(client) == []


def test_delete_file_removes_file_and_its_sent_items_only(harness):
    client, a, _ = seeded(harness)
    assert delete(client, "/api/files/nope", "x").status_code == 404
    assert delete(client, "/api/files/fileA", "x").status_code == 403
    assert delete(client, "/api/files/fileA", target_token(client, "file:keepA")).status_code == 403

    done = delete(client, "/api/files/fileA", target_token(client, "file:fileA"))
    assert done.status_code == 200, done.text
    assert done.json() == {"deleted": {"id": "fileA", "chat_id": A}}
    assert not a["file"].exists() and a["file2"].exists() and a["output"].exists()
    assert client.get("/api/files/fileA").status_code == 404
    listed = client.get(f"/api/chats/{A}/files").json()["files"]
    assert [f["id"] for f in listed] == ["keepA"]
    assert harness.query("SELECT id FROM sent_items WHERE ref_id = 'fileA'") == []
    assert harness.query("SELECT count(*) FROM runs WHERE chat_id = ?", (A,)) == [(1,)]

    events = harness.events()
    assert [e["detail"] for e in events if e["event"] == "file.deleted"] == [{"id": "fileA"}]
    assert "bank statement" not in json.dumps(events)


def test_images_list_loses_deleted_chats_images(harness):
    client, a, b = seeded(harness)
    assert {i["id"] for i in client.get("/api/images").json()["images"]} == {"imgA", "imgB"}
    assert delete(client, f"/api/chats/{A}", target_token(client, f"chat:{A}")).status_code == 200
    assert [i["id"] for i in client.get("/api/images").json()["images"]] == ["imgB"]
    assert client.get("/api/images/imgA/file").status_code == 404
    assert client.get("/api/images/imgB/file").status_code == 200
    assert not a["image"].exists() and not a["upload"].exists()
    assert client.get("/api/uploads/upA/file").status_code == 404


def test_step_up_target_required_only_for_delete(harness):
    client = signed_in(harness)
    url = "/api/auth/stepup/options"

    def options(payload):
        return client.post(url, json=payload, headers=CSRF)

    need = options({"purpose": "delete"})
    assert need.status_code == 422 and need.json()["reason"] == "target_required"
    for purpose in ("settings", "add_device", "add_folder", "approval"):
        refused = options({"purpose": purpose, "target": f"chat:{A}"})
        assert refused.status_code == 422 and refused.json()["reason"] == "target_not_allowed"
    with_approval = options({"purpose": "delete", "target": f"chat:{A}", "approval_id": "x"})
    assert with_approval.status_code == 422
    for bad in ("folder:x", "chat:", "chat:../x", "file:" + "x" * 33, "chat:a b", "x" * 81):
        assert options({"purpose": "delete", "target": bad}).status_code == 422, bad

    # The target is part of the ceremony: a verify for another target finds no challenge.
    body = options({"purpose": "delete", "target": f"chat:{A}"}).json()
    verified = client.post(
        "/api/auth/stepup/verify",
        json={"purpose": "delete", "target": f"chat:{B}", "challenge_id": body["challenge_id"],
              "response": assertion("cred-1", body["options"])},
        headers=CSRF,
    )  # fmt: skip
    assert verified.status_code == 400 and verified.json() == {"reason": "challenge_invalid"}
    assert target_token(client, f"file:{'f' * 32}")
    events = [e for e in harness.events() if e["event"] == "step_up.verified"]
    assert events[-1]["detail"] == {"purpose": "delete", "target": f"file:{'f' * 32}"}


def test_delete_needs_session_and_csrf(harness):
    client = harness.start()
    assert client.request("DELETE", f"/api/chats/{A}", json={"step_up_token": "x"},
                          headers=CSRF).status_code == 401  # fmt: skip
    assert client.request("DELETE", "/api/files/x", json={"step_up_token": "x"},
                          headers=CSRF).status_code == 401  # fmt: skip
    assert client.request("DELETE", f"/api/chats/{A}",
                          json={"step_up_token": "x"}).status_code == 403  # fmt: skip


def test_startup_sweeps_folders_of_deleted_chats(harness):
    chats = harness.data / "workspace" / "chats"
    orphan = chats / ("c" * 32)
    (orphan / "files").mkdir(parents=True)
    (orphan / "files" / "left.csv").write_bytes(b"x")
    harness.start()
    assert not orphan.exists()
