"""Files attached to a chat: the schema, the store and the upload/list/download API."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import json

import pytest
from test_api import (  # noqa: F401  (harness is a fixture)
    CSRF,
    ORIGIN,
    harness,
    new_chat,
    read_events,
    say,
    signed_in,
)
from test_file_checks import make_cfb, make_xlsx
from test_uploads import png

from hearthkeep.db import connect
from hearthkeep.providers.chat import StreamDone, TextDelta

MB = 1024 * 1024
CSV = b"date,amount\n2024-03-01,12.50\n"
DONE = [TextDelta("Done."), StreamDone("stop")]


def attach(client, chat_id, data=CSV, name="sales.csv", mime="text/csv", headers=CSRF):
    return client.post(
        f"/api/chats/{chat_id}/files", files={"file": (name, data, mime)}, headers=headers
    )


def files_dir(h, chat_id):
    return h.data / "workspace" / "chats" / chat_id / "files"


# ---- schema ------------------------------------------------------------------------------------


def test_migration_3_adds_chat_files_and_upload_chats(tmp_path):
    conn = connect(tmp_path / "hk.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    files = {r["name"] for r in conn.execute("PRAGMA table_info(chat_files)")}
    assert files == {
        "id", "chat_id", "name", "kind", "mime", "size", "status", "summary", "source",
        "file_name", "created_at",
    }  # fmt: skip
    links = {r["name"] for r in conn.execute("PRAGMA table_info(upload_chats)")}
    assert links == {"upload_id", "chat_id"}
    conn.close()
    # Idempotent: opening again changes nothing.
    again = connect(tmp_path / "hk.db")
    assert again.execute("PRAGMA user_version").fetchone()[0] == 3
    again.close()


# ---- upload --------------------------------------------------------------------------------------


def test_upload_stores_under_chat_and_keeps_name(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    response = attach(client, chat_id, name="März Umsätze.csv", mime="application/octet-stream")
    assert response.status_code == 201, response.text
    body = response.json()
    file_id = body["id"]
    assert body == {
        "id": file_id,
        "chat_id": chat_id,
        "name": "März Umsätze.csv",
        "kind": "csv",
        "size": len(CSV),
        "status": "unchecked",
        "created_at": body["created_at"],
    }
    stored = files_dir(harness, chat_id) / f"{file_id}.csv"
    assert stored.read_bytes() == CSV
    assert [p.name for p in files_dir(harness, chat_id).iterdir()] == [stored.name]
    [row] = harness.query(
        "SELECT chat_id, name, kind, mime, size, status, source, file_name FROM chat_files"
    )
    assert row == (chat_id, "März Umsätze.csv", "csv", "text/csv", len(CSV), "unchecked",
                   "upload", f"{file_id}.csv")  # fmt: skip

    listed = client.get(f"/api/chats/{chat_id}/files")
    assert listed.status_code == 200 and listed.json() == {"files": [body]}
    assert client.get(f"/api/files/{file_id}").json() == body


def test_upload_refusals_keep_nothing(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    cases = [
        ("photo.csv", png(10, 10), 415, "unsupported"),
        ("budget.xlsx", CSV, 415, "mismatch"),
        ("budget.xlsm", make_xlsx(), 415, "macros"),
        ("locked.xlsx", make_cfb({"EncryptedPackage": b"x"}), 422, "password_protected"),
        ("data.json", b"{", 422, "invalid_json"),
    ]
    for name, data, status, reason in cases:
        response = attach(client, chat_id, data, name=name)
        assert response.status_code == status, name
        assert set(response.json()) == {"reason", "message"}
        assert response.json()["reason"] == reason
    assert harness.query("SELECT * FROM chat_files") == []
    folder = files_dir(harness, chat_id)
    assert not folder.exists() or not any(folder.iterdir())


def test_upload_needs_a_file_field(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    response = client.post(
        f"/api/chats/{chat_id}/files", files={"other": ("a.csv", CSV, "text/csv")}, headers=CSRF
    )
    assert response.status_code == 422 and response.json()["reason"] == "invalid_request"
    response = client.post(f"/api/chats/{chat_id}/files", json={"file": "x"}, headers=CSRF)
    assert response.status_code == 415


def test_upload_rejects_over_50mb_while_reading(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    too_big = {"reason": "too_large", "message": "That file is too big. Files can be up to 50 MB."}
    response = client.post(
        f"/api/chats/{chat_id}/files",
        content=b"x" * 10,
        headers={**CSRF, "Content-Type": "multipart/form-data; boundary=abc",
                 "Content-Length": str(60 * MB)},
    )  # fmt: skip
    assert response.status_code == 413 and response.json() == too_big
    # Over the cap by one byte inside the envelope: refused; exactly 50 MB: kept.
    over = b"a,b\n" + b"1" * (50 * MB + 1 - 4)
    response = attach(client, chat_id, over, name="big.csv")
    assert response.status_code == 413 and response.json() == too_big
    exactly = b"a,b\n" + b"1" * (50 * MB - 4)
    response = attach(client, chat_id, exactly, name="big.csv")
    assert response.status_code == 201, response.text
    assert response.json()["size"] == 50 * MB


def test_upload_unknown_chat_404(harness):
    client = signed_in(harness)
    for chat_id in ("0" * 32, "not-a-chat", "..%2F..%2Fsecrets"):
        response = attach(client, chat_id)
        assert response.status_code == 404, chat_id
        assert response.json() == {"reason": "not_found"}
    assert client.get(f"/api/chats/{'0' * 32}/files").status_code == 404
    assert not (harness.data / "workspace" / "chats" / ("0" * 32)).exists()


def test_upload_checks_off_the_event_loop(harness, monkeypatch):
    import hearthkeep.api.routes_files as routes

    offloaded: list[str] = []
    real = routes.run_in_threadpool

    async def recording(func, *args, **kwargs):
        offloaded.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(routes, "run_in_threadpool", recording)
    client = signed_in(harness)
    assert attach(client, new_chat(client)).status_code == 201
    assert offloaded == ["prepare_upload"]


# ---- download ------------------------------------------------------------------------------------


def test_download_sends_attachment_with_name_and_nosniff(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    file_id = attach(client, chat_id, name="märz.csv").json()["id"]
    response = client.get(f"/api/files/{file_id}/download")
    assert response.status_code == 200
    assert response.content == CSV
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"marz.csv\"; filename*=UTF-8''m%C3%A4rz.csv"
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"

    xlsx = attach(client, chat_id, make_xlsx(), name='q"1".xlsx').json()["id"]
    response = client.get(f"/api/files/{xlsx}/download")
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"q1.xlsx\"; filename*=UTF-8''q1.xlsx"
    )


def test_file_ids_cannot_escape(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    file_id = attach(client, chat_id).json()["id"]
    (harness.data / "config.toml").write_text("secret = 1")
    for path in (
        "/api/files/..%2Fconfig.toml/download",
        "/api/files/..%2F..%2Fhearthkeep.db/download",
        "/api/files/nope/download",
        f"/api/files/{'a' * 33}/download",
        f"/api/files/{file_id}.csv/download",
        "/api/files/..%2Fconfig.toml",
        "/api/files/nope",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"reason": "not_found"}, path

    # A row pointing outside its chat's files folder is never followed.
    conn = harness.conn()
    conn.execute("UPDATE chat_files SET file_name = '../../../../config.toml' WHERE id = ?",
                 (file_id,))  # fmt: skip
    conn.close()
    assert client.get(f"/api/files/{file_id}/download").status_code == 404

    # A file removed from disk is gone.
    other = attach(client, chat_id).json()["id"]
    (files_dir(harness, chat_id) / f"{other}.csv").unlink()
    assert client.get(f"/api/files/{other}/download").status_code == 404


def test_audit_records_id_type_size_not_name(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    file_id = attach(client, chat_id, name="Secret Salary Review.csv").json()["id"]
    [event] = [e for e in harness.events() if e["event"] == "file.attached"]
    assert event["detail"] == {"id": file_id, "type": "text/csv", "bytes": len(CSV)}
    assert event["actor"].startswith("session:")
    assert "Salary" not in json.dumps(harness.events())


# ---- messages ------------------------------------------------------------------------------------


def test_message_with_more_than_5_files_refused(harness):
    client = signed_in(harness, [DONE, DONE])
    chat_id = new_chat(client)
    ids = [attach(client, chat_id).json()["id"] for _ in range(6)]
    text = "Compare these\n" + "\n".join(f"Attached file: {i}" for i in ids)
    response = say(client, chat_id, text)
    assert response.status_code == 422
    assert response.json() == {
        "reason": "too_many_files",
        "message": "Attach up to 5 files at a time.",
    }
    assert harness.query("SELECT * FROM messages WHERE chat_id = ?", (chat_id,)) == []
    text = "Compare these\n" + "\n".join(f"Attached file: {i}" for i in ids[:5])
    response = say(client, chat_id, text)
    assert response.status_code == 202, response.text
    read_events(client, chat_id, response.json()["turn_id"])


def test_message_with_other_chats_file_refused(harness):
    client = signed_in(harness, [DONE])
    mine, theirs = new_chat(client), new_chat(client)
    other = attach(client, theirs).json()["id"]
    for line in (f"Attached file: {other}", "Attached file: unknown_id"):
        response = say(client, mine, f"Look\n{line}")
        assert response.status_code == 422, line
        assert response.json() == {
            "reason": "unknown_file",
            "message": "That file isn't part of this chat. Attach it again.",
        }
    assert harness.query("SELECT * FROM messages WHERE chat_id = ?", (mine,)) == []
    own = attach(client, mine).json()["id"]
    response = say(client, mine, f"Look\nAttached file: {own}")
    assert response.status_code == 202
    read_events(client, mine, response.json()["turn_id"])


def test_photo_line_links_upload_to_chat(harness):
    client = signed_in(harness, [DONE])
    chat_id = new_chat(client)
    photo = client.post(
        "/api/uploads", files={"file": ("a.png", png(10, 10), "image/png")}, headers=CSRF
    ).json()["id"]
    text = f"Paint this\nAttached photo: {photo}\nAttached photo: missing_id"
    response = say(client, chat_id, text)
    assert response.status_code == 202
    read_events(client, chat_id, response.json()["turn_id"])
    assert harness.query("SELECT upload_id, chat_id FROM upload_chats") == [(photo, chat_id)]


def test_file_only_message_is_titled_with_the_file_name(harness):
    client = signed_in(harness, [DONE, DONE])
    chat_id = new_chat(client)
    name = "Quarterly bank statement for the joint account, January to March 2024.pdf"
    from test_file_checks import make_pdf

    file_id = attach(client, chat_id, make_pdf(), name=name).json()["id"]
    response = say(client, chat_id, f"Attached file: {file_id}")
    assert response.status_code == 202
    read_events(client, chat_id, response.json()["turn_id"])
    worded = new_chat(client)
    own = attach(client, worded).json()["id"]
    response = say(client, worded, f"What changed?\nAttached file: {own}")
    read_events(client, worded, response.json()["turn_id"])
    titles = {c["id"]: c["title"] for c in client.get("/api/chats").json()}
    assert titles == {chat_id: name[:60], worded: "What changed?"}


# ---- access -------------------------------------------------------------------------------------


def test_files_require_session_and_csrf(harness):
    client = signed_in(harness)
    chat_id = new_chat(client)
    file_id = attach(client, chat_id).json()["id"]
    for headers in ({}, {"Origin": ORIGIN}, {"X-Hearthkeep": "1"},
                    {"X-Hearthkeep": "1", "Origin": "https://evil.example"}):  # fmt: skip
        response = attach(client, chat_id, headers=headers)
        assert response.status_code == 403 and response.json() == {"reason": "csrf"}

    signed_out = harness.start()
    for path in (
        f"/api/chats/{chat_id}/files",
        f"/api/files/{file_id}",
        f"/api/files/{file_id}/download",
    ):
        response = signed_out.get(path)
        assert response.status_code == 401 and response.json() == {"reason": "unauthenticated"}
    response = attach(signed_out, chat_id)
    assert response.status_code == 401
    assert len(harness.query("SELECT * FROM chat_files")) == 1


@pytest.mark.parametrize("status", ["ready", "unchecked"])
def test_store_set_status_and_summary(tmp_path, status):
    from hearthkeep.files.checks import FileCheck
    from hearthkeep.files.store import ChatFileStore

    conn = connect(tmp_path / "hk.db")
    store = ChatFileStore(conn, tmp_path)
    chat_id = "a" * 32
    staged = store.stage(chat_id, CSV)
    assert staged.tmp.is_file()
    saved = store.record(
        staged, name="a.csv", check=FileCheck("csv", "text/csv", "csv"), source="upload"
    )
    assert not staged.tmp.exists()
    store.set_status(saved.id, status, {"rows": 1})
    found = store.get(saved.id)
    assert (found.status, found.summary) == (status, {"rows": 1})
    assert store.path(saved.id).read_bytes() == CSV
    assert [f.id for f in store.list(chat_id)] == [saved.id]
    assert store.list("b" * 32) == []

    src = tmp_path / "src.csv"
    src.write_bytes(CSV * 10)
    copied = store.stage_copy(chat_id, src)
    assert copied.tmp.read_bytes() == CSV * 10
    store.discard(copied)
    assert not copied.tmp.exists()
    from hearthkeep.files.checks import FileRefused

    with pytest.raises(FileRefused) as caught:
        store.stage_copy(chat_id, src, max_bytes=len(CSV))
    assert caught.value.reason == "too_large"
    assert sorted(p.name for p in store.files_dir(chat_id).iterdir()) == [f"{saved.id}.csv"]
    with pytest.raises(ValueError):
        store.files_dir("../x")
    conn.close()


# ---- fix round 1 ---------------------------------------------------------------------------------


def test_chat_files_are_indexed_by_chat(tmp_path):
    conn = connect(tmp_path / "hk.db")
    index = conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = 'chat_files_chat'"
    ).fetchone()
    assert index is not None and index["tbl_name"] == "chat_files"
    conn.close()


def test_lines_ending_in_cr_are_counted_and_checked(harness):
    client = signed_in(harness, [DONE])
    mine, theirs = new_chat(client), new_chat(client)
    other = attach(client, theirs).json()["id"]
    response = say(client, mine, f"Look\r\nAttached file: {other}\r\n")
    assert response.status_code == 422 and response.json()["reason"] == "unknown_file"
    response = say(client, mine, f"Look\rAttached file: {other}")
    assert response.status_code == 422 and response.json()["reason"] == "unknown_file"
    ids = [attach(client, mine).json()["id"] for _ in range(6)]
    text = "Compare\r\n" + "\r\n".join(f"Attached file: {i}" for i in ids)
    response = say(client, mine, text)
    assert response.status_code == 422 and response.json()["reason"] == "too_many_files"
    # A file-only message with CRLF line ends is still titled with the file name.
    response = say(client, mine, f"Attached file: {ids[0]}\r\n")
    assert response.status_code == 202
    read_events(client, mine, response.json()["turn_id"])
    assert {c["id"]: c["title"] for c in client.get("/api/chats").json()}[mine] == "sales.csv"


def test_without_line_helpers_handle_cr():
    from hearthkeep.api.routes_chats import without_file_lines
    from hearthkeep.images import without_photo_lines

    assert without_photo_lines("a\r\nAttached photo: abc\r\nb") == "a\nb"
    assert without_file_lines("a\rAttached file: abc\r\nb") == "a\nb"


def test_stored_name_takes_the_detected_extension(harness):
    from test_file_checks import make_pdf

    client = signed_in(harness)
    chat_id = new_chat(client)
    renamed = attach(client, chat_id, name="run.hta").json()
    assert (renamed["name"], renamed["kind"]) == ("run.csv", "csv")
    statement = attach(client, chat_id, make_pdf(), name="statement").json()
    assert (statement["name"], statement["kind"]) == ("statement.pdf", "pdf")
    response = client.get(f"/api/files/{statement['id']}/download")
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"statement.pdf\"; filename*=UTF-8''statement.pdf"
    )


def test_startup_removes_leftover_temporary_files(harness):
    chat_id = "c" * 32
    folder = files_dir(harness, chat_id)
    folder.mkdir(parents=True)
    (folder / "abc.tmp").write_bytes(b"half")
    (folder / "keep.csv").write_bytes(CSV)
    (harness.data / "workspace" / "chats" / "other.tmp").write_bytes(b"not in a files folder")
    harness.start()
    assert sorted(p.name for p in folder.iterdir()) == ["keep.csv"]
    assert (harness.data / "workspace" / "chats" / "other.tmp").exists()


def test_inside_is_public_and_guards_both_stores(tmp_path):
    from hearthkeep.images import inside

    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "a.csv").write_bytes(CSV)
    (tmp_path / "secret.txt").write_text("x")
    assert inside(folder, "a.csv") == (folder / "a.csv").resolve()
    assert inside(folder, "../secret.txt") is None
    assert inside(folder, "missing.csv") is None
    assert inside(folder, "") is None


def test_parse_file_part_finds_the_file_field_by_offset():
    from hearthkeep.api.multipart import parse_file_part

    body = bytearray(
        b"preamble\r\n--XyZ\r\n"
        b'Content-Disposition: form-data; name="note"\r\n\r\nhello\r\n--XyZ\r\n'
        b'Content-Disposition: form-data; name="file"; filename="a.csv"; '
        b"filename*=UTF-8''m%C3%A4rz.csv\r\n\r\n"
        b"a,b\r\n--XyZnot-a-delimiter\r\n--XyZ--\r\n"
    )
    part = parse_file_part(body, b"XyZ")
    assert part.data == b"a,b" and isinstance(part.data, bytes)
    assert part.filename == "märz.csv"
    assert parse_file_part(bytearray(b"--XyZ--\r\n"), b"XyZ") is None
    assert parse_file_part(b"no delimiter at all", b"XyZ") is None


# ---- fix round 2 ---------------------------------------------------------------------------------


def _link_folder(link, target):
    """A directory junction on Windows (no privilege needed), a symlink elsewhere."""
    import os

    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_startup_sweep_does_not_follow_links(harness, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "files").mkdir()
    (outside / "files" / "victim.tmp").write_bytes(b"not ours")
    (outside / "victim.tmp").write_bytes(b"not ours either")
    chats = harness.data / "workspace" / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    _link_folder(chats / ("d" * 32), outside)  # a linked chat folder
    real_chat = chats / ("e" * 32)
    real_chat.mkdir()
    _link_folder(real_chat / "files", outside)  # a linked files folder
    harness.start()
    assert (outside / "files" / "victim.tmp").exists()
    assert (outside / "victim.tmp").exists()
