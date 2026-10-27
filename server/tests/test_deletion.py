"""Deleting a chat (everything it owns, rows then files) or one of its files, and the startup
sweep of chat folders whose chat is gone."""

import os
from pathlib import Path

import pytest

from hearthkeep import audit, db
from hearthkeep.deletion import delete_chat, delete_chat_file, sweep_orphans
from hearthkeep.files.store import ChatFileStore
from hearthkeep.sandbox.manifest import manifest_entries
from hearthkeep.tools.python import analysis_context

A = "a" * 32
B = "b" * 32

# Every table a chat owns, directly or through another row.
CHAT_TABLES = (
    "messages", "turn_events", "approvals", "chat_grants", "chat_files", "runs", "outputs",
    "number_checks", "images", "uploads", "upload_chats", "sent_items", "sent_targets", "chats",
)  # fmt: skip


def _link_folder(link, target):
    """A directory junction on Windows (no privilege needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def conn(data):
    data.mkdir()
    c = db.connect(data / "hearthkeep.db")
    yield c
    c.close()


def seed_chat(conn, data, chat_id: str, tag: str) -> dict:
    """One row in every table for `chat_id`, plus its files on disk. `tag` keeps ids unique."""
    now = db.utcnow()
    images = data / "workspace" / "output" / "images"
    uploads = data / "workspace" / "uploads"
    chat_dir = data / "workspace" / "chats" / chat_id
    images.mkdir(parents=True, exist_ok=True)
    uploads.mkdir(parents=True, exist_ok=True)
    (chat_dir / "files").mkdir(parents=True)
    (chat_dir / "runs" / f"run{tag}" / "files").mkdir(parents=True)
    paths = {
        "file": chat_dir / "files" / f"file{tag}.csv",
        "file2": chat_dir / "files" / f"keep{tag}.csv",
        "output": chat_dir / "runs" / f"run{tag}" / "files" / f"out{tag}.csv",
        "image": images / f"img{tag}.png",
        "upload": uploads / f"up{tag}.png",
        "chat_dir": chat_dir,
    }
    for key, path in paths.items():
        if key != "chat_dir":
            path.write_bytes(b"x")
    rows = [
        ("INSERT INTO chats (id, title, think, created_at) VALUES (?,?,?,?)",
         (chat_id, f"Chat {tag}", 0, now)),
        ("INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
         (chat_id, "user", f"Attached file: file{tag}", now)),
        ("INSERT INTO turn_events (chat_id, turn_id, seq, type, data, created_at) "
         "VALUES (?,?,?,?,?,?)", (chat_id, f"turn{tag}", 1, "turn_done", "{}", now)),
        ("INSERT INTO approvals (id, chat_id, turn_id, tool, status, created_at) "
         "VALUES (?,?,?,?,?,?)", (f"appr{tag}", chat_id, f"turn{tag}", "note", "approved", now)),
        ("INSERT INTO chat_grants (chat_id, tool) VALUES (?,?)", (chat_id, "note")),
        ("INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source, file_name,"
         " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
         (f"file{tag}", chat_id, "bank statement.csv", "csv", "text/csv", 1, "ready", "upload",
          f"file{tag}.csv", now)),
        ("INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source, file_name,"
         " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
         (f"keep{tag}", chat_id, "budget.csv", "csv", "text/csv", 1, "ready", "upload",
          f"keep{tag}.csv", now)),
        ("INSERT INTO runs (id, chat_id, turn_id, purpose, status, created_at) "
         "VALUES (?,?,?,?,?,?)", (f"run{tag}", chat_id, f"turn{tag}", "analysis", "ok", now)),
        ("INSERT INTO outputs (id, run_id, chat_id, kind, name, file_name, created_at) "
         "VALUES (?,?,?,?,?,?,?)",
         (f"out{tag}", f"run{tag}", chat_id, "file", "totals.csv", f"out{tag}.csv", now)),
        ("INSERT INTO number_checks (message_id, chat_id, turn_id, text, marks, created_at) "
         "VALUES ((SELECT max(id) FROM messages),?,?,?,?,?)",
         (chat_id, f"turn{tag}", "12", "[]", now)),
        ("INSERT INTO images (id, chat_id, turn_id, model_id, prompt, file_name, created_at) "
         "VALUES (?,?,?,?,?,?,?)",
         (f"img{tag}", chat_id, f"turn{tag}", "m", "a cat", f"img{tag}.png", now)),
        ("INSERT INTO uploads (id, file_name, mime, width, height, created_at) "
         "VALUES (?,?,?,?,?,?)", (f"up{tag}", f"up{tag}.png", "image/png", 1, 1, now)),
        ("INSERT INTO upload_chats (upload_id, chat_id) VALUES (?,?)", (f"up{tag}", chat_id)),
        # Sent items: the file (two devices), an output, and a summary.
        ("INSERT INTO sent_items (id, chat_id, kind, ref_id, title, text, created_at) "
         "VALUES (?,?,?,?,?,?,?)",
         (f"sentfile{tag}", chat_id, "file", f"file{tag}", "bank statement.csv", None, now)),
        ("INSERT INTO sent_targets (item_id, credential_id) VALUES (?,?)",
         (f"sentfile{tag}", "cred-1")),
        ("INSERT INTO sent_targets (item_id, credential_id) VALUES (?,?)",
         (f"sentfile{tag}", "cred-2")),
        ("INSERT INTO sent_items (id, chat_id, kind, ref_id, title, text, created_at) "
         "VALUES (?,?,?,?,?,?,?)",
         (f"sentout{tag}", chat_id, "file", f"out{tag}", "totals.csv", None, now)),
        ("INSERT INTO sent_targets (item_id, credential_id) VALUES (?,?)",
         (f"sentout{tag}", "cred-1")),
        ("INSERT INTO sent_items (id, chat_id, kind, ref_id, title, text, created_at) "
         "VALUES (?,?,?,?,?,?,?)",
         (f"sentsum{tag}", chat_id, "summary", None, "Summary", "You spent 12", now)),
        ("INSERT INTO sent_targets (item_id, credential_id) VALUES (?,?)",
         (f"sentsum{tag}", "cred-1")),
    ]  # fmt: skip
    for sql, params in rows:
        conn.execute(sql, params)
    return paths


def seed_shared(conn, data):
    """Rows no chat owns: they must survive any deletion."""
    now = db.utcnow()
    conn.execute("INSERT INTO push_subscriptions VALUES (?,?,?)", ("cred-1", "https://x", now))
    conn.execute("INSERT INTO allowed_folders VALUES (?,?,?,?)", ("f", "C:/x", "X", now))
    conn.execute("INSERT INTO tool_levels VALUES (?,?)", ("note", "allowed"))
    audit.record(conn, "test", "seeded", {})
    # A photo not yet sent in any chat (a composer still holds it).
    (data / "workspace" / "uploads" / "loose.png").write_bytes(b"x")
    conn.execute(
        "INSERT INTO uploads (id, file_name, mime, width, height, created_at) VALUES (?,?,?,?,?,?)",
        ("loose", "loose.png", "image/png", 1, 1, now),
    )


def counts_for(conn, chat_id: str) -> dict[str, int]:
    sent = "SELECT id FROM sent_items WHERE chat_id = ?"
    uploads = "SELECT upload_id FROM upload_chats WHERE chat_id = ?"
    queries = {
        "chats": ("SELECT count(*) FROM chats WHERE id = ?", (chat_id,)),
        "uploads": (f"SELECT count(*) FROM uploads WHERE id IN ({uploads})", (chat_id,)),
        "sent_targets": (f"SELECT count(*) FROM sent_targets WHERE item_id IN ({sent})",
                         (chat_id,)),
    }  # fmt: skip
    out = {}
    for table in CHAT_TABLES:
        sql, params = queries.get(table, (f"SELECT count(*) FROM {table} WHERE chat_id = ?",
                                          (chat_id,)))  # fmt: skip
        out[table] = conn.execute(sql, params).fetchone()[0]
    return out


def table_dump(conn) -> dict[str, list[tuple]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}")) for t in tables}


def test_delete_chat_removes_everything_it_owns(conn, data, tmp_path):
    a = seed_chat(conn, data, A, "A")
    b = seed_chat(conn, data, B, "B")
    seed_shared(conn, data)
    # A link inside A's folder that leads outside the data folder: never followed.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_bytes(b"not Hearthkeep's")
    _link_folder(a["chat_dir"] / "runs" / "elsewhere", outside)
    before = table_dump(conn)
    audit_before = before["audit"]

    counts = delete_chat(conn, data, A)

    assert counts == {
        "messages": 1, "turn_events": 1, "approvals": 1, "chat_grants": 1, "chat_files": 2,
        "runs": 1, "outputs": 1, "number_checks": 1, "images": 1, "uploads": 1,
        "upload_chats": 1, "sent_items": 3, "sent_targets": 4, "chats": 1,
    }  # fmt: skip
    assert set(counts_for(conn, A).values()) == {0}
    assert counts_for(conn, B) == {**{t: 1 for t in CHAT_TABLES}, "chat_files": 2,
                                   "sent_items": 3, "sent_targets": 4}  # fmt: skip
    after = table_dump(conn)
    # Rows nobody's chat owns are untouched; the audit log is never edited (delete_chat adds
    # nothing itself: the route records the deletion).
    for table in ("push_subscriptions", "allowed_folders", "tool_levels", "credentials"):
        assert after[table] == before[table]
    assert after["audit"] == audit_before
    assert any(r[0] == "loose" for r in after["uploads"])
    # Files: all of A's gone, B's all there, the linked folder's contents untouched.
    for key in ("file", "file2", "output", "image", "upload"):
        assert not a[key].exists(), key
        assert b[key].exists(), key
    assert not a["chat_dir"].exists()
    assert b["chat_dir"].is_dir()
    assert (outside / "precious.txt").read_bytes() == b"not Hearthkeep's"
    assert (data / "workspace" / "uploads" / "loose.png").exists()


def test_delete_chat_leaves_a_linked_chat_folder_and_outside_files_alone(conn, data, tmp_path):
    seed_chat(conn, data, A, "A")
    outside = tmp_path / "outside"
    (outside / "files").mkdir(parents=True)
    (outside / "files" / "fileA.csv").write_bytes(b"theirs")
    chat_dir = data / "workspace" / "chats" / A
    for child in sorted(chat_dir.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    chat_dir.rmdir()
    _link_folder(chat_dir, outside)  # the chat's folder itself is a junction
    # An image whose stored name tries to climb out of the images folder.
    conn.execute("UPDATE images SET file_name = ? WHERE chat_id = ?", ("../../../x.png", A))
    (data / "x.png").write_bytes(b"x")

    delete_chat(conn, data, A)

    assert (outside / "files" / "fileA.csv").read_bytes() == b"theirs"
    assert (data / "x.png").exists()
    assert conn.execute("SELECT count(*) FROM chats").fetchone()[0] == 0


def test_delete_chat_rolls_back_on_failure(conn, data, monkeypatch):
    a = seed_chat(conn, data, A, "A")
    before = table_dump(conn)
    conn.execute("CREATE TRIGGER no_chat_delete BEFORE DELETE ON chats "
                 "BEGIN SELECT RAISE(ABORT, 'nope'); END")  # fmt: skip
    with pytest.raises(Exception, match="nope"):
        delete_chat(conn, data, A)
    after = table_dump(conn)
    assert {t: after[t] for t in before} == before
    assert a["file"].exists() and a["image"].exists() and a["upload"].exists()


def test_delete_file_removes_file_and_its_sent_items_only(conn, data):
    a = seed_chat(conn, data, A, "A")
    seed_chat(conn, data, B, "B")
    before = table_dump(conn)

    assert delete_chat_file(conn, data, "fileA") == A

    after = table_dump(conn)
    assert not a["file"].exists()
    assert a["file2"].exists() and a["output"].exists()
    assert [r[0] for r in after["chat_files"]] == ["fileB", "keepA", "keepB"]
    # Only the sent item that pointed at the file (and its targets) went.
    assert sorted(r[0] for r in after["sent_items"]) == sorted(
        r[0] for r in before["sent_items"] if r[0] != "sentfileA"
    )
    assert [r for r in after["sent_targets"] if r[0] == "sentfileA"] == []
    assert len(after["sent_targets"]) == len(before["sent_targets"]) - 2
    # Runs and their outputs keep their results; everything else is untouched.
    for table in before:
        if table not in ("chat_files", "sent_items", "sent_targets"):
            assert after[table] == before[table], table

    assert delete_chat_file(conn, data, "fileA") is None
    assert delete_chat_file(conn, data, "../etc") is None


def test_later_turns_skip_a_deleted_file(conn, data):
    seed_chat(conn, data, A, "A")
    files = ChatFileStore(conn, data)
    delete_chat_file(conn, data, "fileA")
    # The chat's old message still says "Attached file: fileA"; the model's list and the
    # sandbox manifest just leave it out.
    assert "fileA" not in analysis_context(files, A)
    assert "keepA" in analysis_context(files, A)
    assert [entry["id"] for entry, _ in manifest_entries(files, A)] == ["keepA"]
    assert files.get("fileA") is None and files.path("fileA") is None


def test_sweep_orphans_removes_folders_of_missing_chats(conn, data, tmp_path):
    seed_chat(conn, data, A, "A")
    chats = data / "workspace" / "chats"
    orphan = chats / ("c" * 32)
    (orphan / "files").mkdir(parents=True)
    (orphan / "files" / "old.csv").write_bytes(b"x")
    odd = chats / "not-a-chat"
    odd.mkdir()
    upper = chats / ("C" * 32)  # not lower-case hex: not ours to judge
    if not upper.exists():  # a case-insensitive disk already has it as `orphan`
        upper.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"x")
    linked = chats / ("d" * 32)
    _link_folder(linked, outside)
    stray_file = chats / ("e" * 32)
    stray_file.write_bytes(b"a file, not a folder")

    assert sweep_orphans(conn, data) == 1

    assert not orphan.exists()
    assert (chats / A).is_dir()
    assert odd.is_dir()
    assert (outside / "keep.txt").exists()
    assert stray_file.exists()
    assert sweep_orphans(conn, data) == 0


def test_sweep_orphans_without_a_chats_folder(conn, data):
    assert sweep_orphans(conn, data) == 0


# ---- fix round 1 ---------------------------------------------------------------------------


def _locked(names):
    """Make unlink of any path whose name is in `names` fail as a locked file would."""
    real = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name in names:
            raise PermissionError(13, "locked", str(self))
        return real(self, *args, **kwargs)

    return unlink


def test_a_failed_removal_is_logged_by_type_and_swept_at_next_startup(
    conn, data, monkeypatch, caplog
):
    a = seed_chat(conn, data, A, "A")
    monkeypatch.setattr(Path, "unlink", _locked({"imgA.png", "upA.png"}))
    with caplog.at_level("WARNING", logger="hearthkeep.deletion"):
        delete_chat(conn, data, A)
    messages = [r.getMessage() for r in caplog.records]
    assert any("PermissionError" in m and "image" in m for m in messages)
    assert any("PermissionError" in m and "photo" in m for m in messages)
    assert not any(str(data) in m or "imgA" in m or "upA" in m for m in messages)
    assert a["image"].exists() and a["upload"].exists()

    monkeypatch.undo()
    assert sweep_orphans(conn, data) == 2
    assert not a["image"].exists() and not a["upload"].exists()


def test_a_chat_file_left_behind_is_swept_at_next_startup(conn, data, monkeypatch, caplog):
    a = seed_chat(conn, data, A, "A")
    monkeypatch.setattr(Path, "unlink", _locked({"fileA.csv"}))
    with caplog.at_level("WARNING", logger="hearthkeep.deletion"):
        assert delete_chat_file(conn, data, "fileA") == A
    assert any("PermissionError" in r.getMessage() for r in caplog.records)
    assert a["file"].exists()
    monkeypatch.undo()
    files = a["chat_dir"] / "files"
    (files / "partial.csv.tmp").write_bytes(b"x")  # temporary files are sweep_temp's
    (files / "notes.txt").write_bytes(b"x")  # a stored-name file with no row
    (files / "odd name.csv").write_bytes(b"x")  # not a stored name: not ours to judge

    assert sweep_orphans(conn, data) == 2

    assert not a["file"].exists() and not (files / "notes.txt").exists()
    assert a["file2"].exists()  # it has its row
    assert (files / "partial.csv.tmp").exists() and (files / "odd name.csv").exists()


def test_sweep_leaves_images_and_photos_with_rows_and_temporary_files(conn, data):
    a = seed_chat(conn, data, A, "A")
    images = data / "workspace" / "output" / "images"
    uploads = data / "workspace" / "uploads"
    (images / "stray.png").write_bytes(b"x")
    (images / "half.png.tmp").write_bytes(b"x")
    (uploads / "stray.jpg").write_bytes(b"x")
    (uploads / "half.jpg.tmp").write_bytes(b"x")
    assert sweep_orphans(conn, data) == 2
    assert not (images / "stray.png").exists() and not (uploads / "stray.jpg").exists()
    assert (images / "half.png.tmp").exists() and (uploads / "half.jpg.tmp").exists()
    assert a["image"].exists() and a["upload"].exists()


def test_a_linked_images_folder_is_left_in_place_with_a_warning(conn, data, tmp_path, caplog):
    seed_chat(conn, data, A, "A")
    images = data / "workspace" / "output" / "images"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "imgA.png").write_bytes(b"theirs")
    (outside / "other.png").write_bytes(b"theirs")
    for child in images.iterdir():
        child.unlink()
    images.rmdir()
    _link_folder(images, outside)
    with caplog.at_level("WARNING", logger="hearthkeep.deletion"):
        delete_chat(conn, data, A)
        sweep_orphans(conn, data)
    assert (outside / "imgA.png").exists() and (outside / "other.png").exists()
    messages = [r.getMessage() for r in caplog.records]
    assert any("left in place" in m and "image" in m for m in messages)
    assert not any(str(tmp_path) in m for m in messages)


def test_remove_tree_rechecks_the_folder_is_not_a_link(data, tmp_path):
    from hearthkeep.deletion import _remove_tree

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"x")
    data.mkdir()
    link = data / "linked"
    _link_folder(link, outside)
    _remove_tree(link)
    assert (outside / "keep.txt").exists()


def test_a_photo_sent_again_in_another_chat_stays_with_that_chat(conn, data):
    a = seed_chat(conn, data, A, "A")
    seed_chat(conn, data, B, "B")
    conn.execute(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, 'user', ?, '')",
        (B, "Again\nAttached photo: upA"),
    )
    # A mention that isn't its own line, or names a longer id, doesn't count.
    conn.execute(
        "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, 'user', ?, '')",
        (B, "said Attached photo: upA inline\nAttached photo: upAB"),
    )
    counts = delete_chat(conn, data, A)
    assert counts["uploads"] == 0
    assert a["upload"].exists()
    assert (
        conn.execute("SELECT chat_id FROM upload_chats WHERE upload_id = 'upA'").fetchall()[0][0]
        == B
    )
    delete_chat(conn, data, B)
    assert not a["upload"].exists()
    assert conn.execute("SELECT count(*) FROM uploads WHERE id = 'upA'").fetchone()[0] == 0


def test_secure_delete_and_an_empty_log_after_a_chat_delete(conn, data):
    seed_chat(conn, data, A, "A")
    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    delete_chat(conn, data, A)
    wal = data / "hearthkeep.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_deleting_the_file_a_chat_is_named_after_retitles_it(conn, data):
    seed_chat(conn, data, A, "A")
    seed_chat(conn, data, B, "B")
    conn.execute("UPDATE chats SET title = 'bank statement.csv' WHERE id = ?", (A,))
    conn.execute("UPDATE chats SET title = 'My  budget' WHERE id = ?", (B,))
    delete_chat_file(conn, data, "fileA")
    delete_chat_file(conn, data, "keepB")  # "budget.csv": B's own title stays
    titles = dict(conn.execute("SELECT id, title FROM chats").fetchall())
    assert titles == {A: "Chat", B: "My  budget"}


def test_commit_then_remove_planned(conn, data):
    from hearthkeep.deletion import commit_chat_delete, remove_planned

    a = seed_chat(conn, data, A, "A")
    counts, plan = commit_chat_delete(conn, A)
    assert counts["chats"] == 1
    assert a["image"].exists() and a["chat_dir"].exists()  # nothing on disk yet
    remove_planned(data, plan)
    assert not a["image"].exists() and not a["chat_dir"].exists() and not a["upload"].exists()
