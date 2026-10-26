import contextlib
import sqlite3

import pytest

from hearthkeep import db
from hearthkeep.images import ASPECTS, ImageStore, validate_size

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-data"


@pytest.fixture
def store(tmp_path):
    conn = db.connect(tmp_path / "hk.db")
    yield ImageStore(conn, tmp_path)
    conn.close()


def _save(store, prompt="a lamp", **kw):
    args = dict(
        chat_id="c1", turn_id="t1", model_id="m", prompt=prompt, width=1024, height=1024, seed=7
    )
    args.update(kw)
    return store.save(PNG, **args)


def test_save_writes_file_and_row(store, tmp_path):
    rec = _save(store)
    f = tmp_path / "workspace" / "output" / "images" / f"{rec.id}.png"
    assert f.read_bytes() == PNG
    assert store.get(rec.id) == rec
    assert (rec.prompt, rec.width, rec.height, rec.seed) == ("a lamp", 1024, 1024, 7)
    assert store.path(rec.id) == f.resolve()
    assert list(f.parent.glob("*.tmp")) == []


def test_save_rejects_non_png(store):
    with pytest.raises(ValueError):
        store.save(b"nope", chat_id=None, turn_id=None, model_id="m", prompt="p",
                   width=1024, height=1024, seed=1)
    assert store.list() == []


def _ticking(monkeypatch):
    counter = iter(range(1, 1000))
    monkeypatch.setattr(db, "utcnow", lambda: f"2026-01-01T00:00:00.{next(counter):06d}+00:00")


def test_list_newest_first_and_paginates(store, monkeypatch):
    _ticking(monkeypatch)
    recs = [_save(store, prompt=f"p{i}") for i in range(5)]
    newest_first = list(reversed(recs))
    assert [r.id for r in store.list()] == [r.id for r in newest_first]
    page = store.list(limit=2)
    assert [r.id for r in page] == [r.id for r in newest_first[:2]]
    rest = store.list(limit=10, before=page[-1].created_at)
    assert [r.id for r in rest] == [r.id for r in newest_first[2:]]


def test_list_same_timestamp_orders_by_id_desc(store, monkeypatch):
    monkeypatch.setattr(db, "utcnow", lambda: "2026-01-01T00:00:00.000001+00:00")
    ids = iter(["aaa", "ccc", "bbb"])
    monkeypatch.setattr("hearthkeep.images.token_urlsafe", lambda n: next(ids))
    for _ in range(3):
        _save(store)
    assert [r.id for r in store.list()] == ["ccc", "bbb", "aaa"]


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_list_rejects_bad_limit(store, limit):
    with pytest.raises(ValueError):
        store.list(limit=limit)


def test_path_rejects_unknown_id_and_traversal(store, tmp_path):
    (tmp_path / "x.png").write_bytes(PNG)
    assert store.path("../x") is None
    assert store.path("nope") is None
    assert store.get("nope") is None


def test_path_guard_rejects_escaping_and_missing_files(store, tmp_path):
    (tmp_path / "x.png").write_bytes(PNG)
    for image_id, name in (("esc", "../x.png"), ("gone", "gone.png")):
        store._conn.execute(
            "INSERT INTO images (id, file_name, created_at) VALUES (?,?,?)",
            (image_id, name, db.utcnow()),
        )
    assert store.path("esc") is None
    assert store.path("gone") is None


def test_save_removes_file_when_commit_fails_after_placement(store, tmp_path, monkeypatch):
    orig = db.transaction

    @contextlib.contextmanager
    def failing(conn):
        with orig(conn) as c:
            yield c
        raise RuntimeError("commit failed")

    monkeypatch.setattr(db, "transaction", failing)
    with pytest.raises(RuntimeError):
        _save(store)
    assert list((tmp_path / "workspace" / "output" / "images").iterdir()) == []


def test_save_rolls_back_file_when_insert_fails(store, tmp_path, monkeypatch):
    monkeypatch.setattr("hearthkeep.images.token_urlsafe", lambda n: "dup")
    _save(store)
    with pytest.raises(sqlite3.IntegrityError):
        _save(store)
    folder = tmp_path / "workspace" / "output" / "images"
    assert [p.name for p in folder.iterdir()] == ["dup.png"]
    assert len(store.list()) == 1


def test_validate_size():
    validate_size(1024, 1024)
    validate_size(1040, 1024)
    for w, h in ((1000, 1024), (256, 256), (4096, 1024)):
        with pytest.raises(ValueError):
            validate_size(w, h)
    for w, h in ASPECTS.values():
        validate_size(w, h)
