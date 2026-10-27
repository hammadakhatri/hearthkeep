"""Allowed folders on the owner's PC: adding one, and the path rules every search and read
follows (links, junctions, hidden and system entries, protected paths, Windows path forms)."""

import ctypes
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from hearthkeep import folders as folders_module
from hearthkeep.db import connect
from hearthkeep.folders import (
    FOLDER_MOVED,
    FolderError,
    FolderStore,
    protected_paths,
)

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows path rules")
REPO = Path(__file__).resolve().parents[2]
HIDDEN, SYSTEM = 0x2, 0x4


@pytest.fixture(autouse=True)
def no_system_paths(monkeypatch):
    """pytest's tmp folder lives under AppData on Windows, which is protected: the tests protect
    only the data folder and the repository unless a test says otherwise."""
    monkeypatch.setattr(folders_module, "system_paths", lambda: [])


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "hk.db")
    yield conn
    conn.close()


@pytest.fixture
def env(conn, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    return FolderStore(conn, data), docs, data


def refused(call, *args, message=None):
    with pytest.raises(FolderError) as caught:
        call(*args)
    if message is not None:
        assert caught.value.message == message, args
    return caught.value


def write(path: Path, data: bytes = b"a,b\n1,2\n", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def set_attributes(path: Path, flags: int) -> None:
    current = os.lstat(path).st_file_attributes
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), current | flags)


def junction(target: Path, link: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def try_symlink(target: Path, link: Path, directory: bool) -> bool:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        return False
    return True


def found_paths(store, **filters):
    args = {"query": None, "folder": None, "kinds": None, "modified_after": None,
            "modified_before": None, **filters}  # fmt: skip
    return [f"{f.folder}/{f.path}" for f in store.search(**args)]


def ts(day: str) -> float:
    return datetime.fromisoformat(day + "T12:00:00").timestamp()


# ---- adding a folder -----------------------------------------------------------------------------


def test_migration_6_adds_allowed_folders(tmp_path):
    conn = connect(tmp_path / "hk.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 6
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(allowed_folders)")}
    assert columns == {"id", "path", "label", "created_at"}
    conn.close()


def test_add_requires_existing_absolute_dir(env, tmp_path):
    store, docs, _ = env
    for bad in ("", "docs", "./docs", str(tmp_path / "missing"), str(write(docs / "a.csv"))):
        with pytest.raises(FolderError):
            store.add(bad)
    added = store.add(str(docs))
    assert added.label == "docs" and added.path == docs.resolve()
    assert [(f.id, f.label) for f in store.list()] == [(added.id, "docs")]
    with pytest.raises(FolderError):
        store.add(str(docs))  # the same folder twice
    if WINDOWS:
        with pytest.raises(FolderError):
            store.add(str(docs).upper())  # the same folder, compared case-insensitively

    other = tmp_path / "elsewhere" / "Docs"
    other.mkdir(parents=True)
    assert store.add(str(other)).label == "Docs 2"  # labels compare case-insensitively
    third = tmp_path / "third" / "docs"
    third.mkdir(parents=True)
    assert store.add(str(third)).label == "docs 3"
    assert store.remove(added.id) is True
    assert store.remove(added.id) is False
    assert [f.label for f in store.list()] == ["Docs 2", "docs 3"]


def test_add_refuses_root_data_folder_and_repo(env, tmp_path):
    store, docs, data = env
    inside_data = data / "workspace"
    inside_data.mkdir()
    refused(store.add, str(Path(tmp_path.anchor)), message=folders_module.IS_ROOT)
    for bad in (data, inside_data, REPO, REPO / "server" / "src"):
        refused(store.add, str(bad), message=folders_module.PROTECTED)
    assert store.list() == []
    assert data.resolve() in protected_paths(data)
    assert REPO in protected_paths(data)


@windows_only
def test_system_paths_are_protected_on_windows(monkeypatch, tmp_path):
    monkeypatch.undo()  # the real list
    paths = [os.path.normcase(p) for p in folders_module.system_paths()]
    for expected in (Path.home() / "AppData", Path(os.environ.get("SystemRoot", r"C:\Windows")),
                     Path(os.environ.get("ProgramFiles", r"C:\Program Files"))):  # fmt: skip
        assert os.path.normcase(expected.resolve()) in paths
    assert all(os.path.normcase(p) in [os.path.normcase(q) for q in protected_paths(tmp_path)]
               for p in folders_module.system_paths())  # fmt: skip
    conn = connect(tmp_path / "hk.db")
    store = FolderStore(conn, tmp_path / "data")
    for bad in (os.environ.get("SystemRoot", r"C:\Windows"),
                os.environ.get("ProgramFiles", r"C:\Program Files")):  # fmt: skip
        with pytest.raises(FolderError):
            store.add(bad)
    under_appdata = tmp_path.resolve().is_relative_to((Path.home() / "AppData").resolve())
    if under_appdata:
        with pytest.raises(FolderError):
            store.add(str(tmp_path))
    conn.close()


def test_folder_containing_data_folder_excludes_it(tmp_path):
    conn = connect(tmp_path / "hk.db")
    docs = tmp_path / "docs"
    data = docs / "hk-data"
    write(data / "workspace" / "secret.csv")
    write(data / "hearthkeep-notes.txt", b"private")
    write(docs / "budget.csv")
    store = FolderStore(conn, data)
    folder = store.add(str(docs))
    assert found_paths(store) == ["docs/budget.csv"]
    for rel in ("hk-data/workspace/secret.csv", "hk-data/hearthkeep-notes.txt"):
        with pytest.raises(FolderError):
            store.resolve_inside(folder, rel)
    assert store.resolve_inside(folder, "budget.csv") == (docs / "budget.csv").resolve()
    conn.close()


# ---- links and junctions -------------------------------------------------------------------------


@windows_only
def test_junction_at_every_depth_is_refused(env, tmp_path):
    store, docs, data = env
    outside = tmp_path / "outside"
    write(outside / "secret.csv")
    write(outside / "deeper" / "secret.csv")
    write(data / "data-secret.csv")
    write(docs / "ok.csv")
    folder = store.add(str(docs))
    links = [docs / "j1", docs / "a" / "j2", docs / "a" / "b" / "j3", docs / "a" / "b" / "c" / "j4"]
    for link in links:
        link.parent.mkdir(parents=True, exist_ok=True)
        junction(outside, link)
    junction(data, docs / "a" / "b" / "to-data")

    assert found_paths(store) == ["docs/ok.csv"]
    rel_links = ["j1", "a/j2", "a/b/j3", "a/b/c/j4"]
    for rel in rel_links:
        for name in ("secret.csv", "deeper/secret.csv"):
            with pytest.raises(FolderError):
                store.resolve_inside(folder, f"{rel}/{name}")
    with pytest.raises(FolderError):
        store.resolve_inside(folder, "a/b/to-data/data-secret.csv")
    with pytest.raises(FolderError):
        store.resolve_inside(folder, "j1")  # the junction itself


@windows_only
def test_added_folder_or_its_parent_may_not_be_a_junction(env, tmp_path):
    store, _, _ = env
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    junction(real, tmp_path / "link")
    # The folder itself, and a folder under a junction.
    refused(store.add, str(tmp_path / "link"), message=folders_module.NOT_REAL_PATH)
    refused(store.add, str(tmp_path / "link" / "inner"), message=folders_module.NOT_REAL_PATH)
    assert store.list() == []


def test_symlinks_refused_where_they_can_be_made(env, tmp_path):
    store, docs, data = env
    outside = tmp_path / "outside"
    write(outside / "secret.csv")
    write(data / "data-secret.csv")
    write(docs / "ok.csv")
    folder = store.add(str(docs))
    (docs / "sub").mkdir()
    made = [
        try_symlink(outside, docs / "dirlink", True),
        try_symlink(outside / "secret.csv", docs / "filelink.csv", False),
        try_symlink(data / "data-secret.csv", docs / "sub" / "deep.csv", False),
    ]
    if not any(made):
        pytest.skip("this account can't make symlinks")
    assert found_paths(store) == ["docs/ok.csv"]
    for rel in ("dirlink/secret.csv", "filelink.csv", "sub/deep.csv"):
        with pytest.raises(FolderError):
            store.resolve_inside(folder, rel)


def test_hard_link_to_a_file_elsewhere_is_refused(env, tmp_path):
    store, docs, data = env
    secret = write(data / "data-secret.csv")
    try:
        os.link(secret, docs / "linked.csv")
    except OSError:
        pytest.skip("hard links aren't available here")
    write(docs / "ok.csv")
    folder = store.add(str(docs))
    assert found_paths(store) == ["docs/ok.csv"]
    with pytest.raises(FolderError):
        store.resolve_inside(folder, "linked.csv")


# ---- hidden and system entries -------------------------------------------------------------------


@windows_only
def test_hidden_and_system_files_skipped(env):
    store, docs, _ = env
    write(docs / "ok.csv")
    hidden = write(docs / "hidden.csv")
    system = write(docs / "system.csv")
    hidden_dir = docs / "hidden-dir"
    write(hidden_dir / "inner.csv")
    write(docs / ".dotted.csv")
    write(docs / ".dot-dir" / "inner.csv")
    write(docs / "~$lock.csv")
    set_attributes(hidden, HIDDEN)
    set_attributes(system, SYSTEM)
    set_attributes(hidden_dir, HIDDEN)
    folder = store.add(str(docs))

    assert found_paths(store) == ["docs/ok.csv"]
    for rel in ("hidden.csv", "system.csv", "hidden-dir/inner.csv", ".dotted.csv",
                ".dot-dir/inner.csv", "~$lock.csv"):  # fmt: skip
        with pytest.raises(FolderError):
            store.resolve_inside(folder, rel)


@windows_only
def test_hidden_folder_cannot_be_added(env, tmp_path):
    store, _, _ = env
    hidden = tmp_path / "hidden-folder"
    hidden.mkdir()
    set_attributes(hidden, HIDDEN)
    with pytest.raises(FolderError):
        store.add(str(hidden))


# ---- Windows path forms --------------------------------------------------------------------------


@windows_only
def test_unc_device_and_stream_paths_refused(env):
    store, docs, _ = env
    plain = str(docs)
    for bad in (
        r"\\server\share",
        r"\\server\share\docs",
        "\\\\?\\" + plain,
        "\\\\.\\" + plain,
        "\\??\\" + plain,
        "//server/share",
        plain + ":stream",
        plain + "::$INDEX_ALLOCATION",
        plain[:2] + "docs",  # drive-relative
        plain[2:],  # rooted, no drive
        plain + "\x00",
    ):
        refused(store.add, bad, message=folders_module.BAD_PATH)
    assert store.list() == []


@windows_only
def test_reserved_device_names_refused(env):
    store, docs, _ = env
    for name in ("CON", "nul", "COM1", "LPT9", "AUX.txt", "PRN ", "COM¹"):
        refused(store.add, str(docs / name), message=folders_module.BAD_PATH)
    write(docs / "ok.csv")
    folder = store.add(str(docs))
    for rel in ("CON", "NUL.csv", "sub/COM1.txt", "lpt1", "CONIN$"):
        refused(store.resolve_inside, folder, rel, message=folders_module.NO_FILE)


@windows_only
def test_short_8_3_names_refused(env, tmp_path):
    store, _, _ = env
    long_dir = tmp_path / "a folder with a long name"
    write(long_dir / "a spreadsheet with a long name.csv")
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), buffer, 1024)
    if not buffer.value or Path(buffer.value).name == long_dir.name:
        pytest.skip("8.3 names are off on this volume")
    refused(store.add, buffer.value, message=folders_module.NOT_REAL_PATH)
    folder = store.add(str(long_dir))
    ctypes.windll.kernel32.GetShortPathNameW(
        str(long_dir / "a spreadsheet with a long name.csv"), buffer, 1024
    )
    short_file = Path(buffer.value).name
    assert short_file != "a spreadsheet with a long name.csv"
    with pytest.raises(FolderError):
        store.resolve_inside(folder, short_file)


@windows_only
def test_paths_compare_case_insensitively(env):
    store, docs, _ = env
    write(docs / "Sub" / "Budget.csv")
    folder = store.add(str(docs).upper())
    assert folder.path == docs.resolve()
    assert store.resolve_inside(folder, "sub/budget.CSV") == (docs / "Sub" / "Budget.csv").resolve()


def test_resolve_inside_rejects_escapes_and_non_files(env, tmp_path):
    store, docs, _ = env
    write(docs / "sub" / "ok.csv")
    write(tmp_path / "outside.csv")
    folder = store.add(str(docs))
    bad = ["", "../outside.csv", "sub/../../outside.csv", "sub/../sub/ok.csv", "./sub/ok.csv",
           "sub//ok.csv", str(tmp_path / "outside.csv"), "/outside.csv", "sub", "missing.csv",
           "sub/ok.csv\x00", "sub/ok.csv. ", "sub/ok.csv:secret"]  # fmt: skip
    if WINDOWS:
        bad += ["C:outside.csv", "C:/outside.csv", r"\\server\share\x.csv",
                r"sub\..\..\outside.csv"]
    for rel in bad:
        with pytest.raises(FolderError):
            store.resolve_inside(folder, rel)
    assert store.resolve_inside(folder, "sub/ok.csv") == (docs / "sub" / "ok.csv").resolve()
    if WINDOWS:
        assert store.resolve_inside(folder, "sub\\ok.csv") == (docs / "sub" / "ok.csv").resolve()


# ---- the folder changing after it was added ------------------------------------------------------


def test_moved_folder_is_refused(env, tmp_path):
    store, docs, _ = env
    write(docs / "ok.csv")
    folder = store.add(str(docs))
    docs.rename(tmp_path / "renamed")
    with pytest.raises(FolderError) as caught:
        store.resolve_inside(folder, "ok.csv")
    assert caught.value.message == FOLDER_MOVED
    with pytest.raises(FolderError) as caught:
        store.search(query=None, folder="docs", kinds=None, modified_after=None,
                     modified_before=None)  # fmt: skip
    assert caught.value.message == FOLDER_MOVED
    # A search of every folder leaves the moved one out.
    assert found_paths(store) == []
    outcome = store.search_in(store.list(), query=None, kinds=None, modified_after=None,
                              modified_before=None)  # fmt: skip
    assert outcome.moved == ["docs"]


@windows_only
def test_folder_replaced_by_a_junction_is_refused(env, tmp_path):
    store, docs, _ = env
    write(docs / "ok.csv")
    folder = store.add(str(docs))
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "ok.csv")
    docs.rename(tmp_path / "old-docs")
    junction(elsewhere, docs)
    with pytest.raises(FolderError) as caught:
        store.resolve_inside(folder, "ok.csv")
    assert caught.value.message == FOLDER_MOVED
    assert found_paths(store) == []


# ---- search --------------------------------------------------------------------------------------


def test_search_filters_and_limits(env):
    store, docs, _ = env
    write(docs / "2024" / "march.csv", mtime=ts("2024-03-05"))
    write(docs / "2024" / "april.xlsx", b"PK", mtime=ts("2024-04-05"))
    write(docs / "notes.txt", b"hi", mtime=ts("2024-05-05"))
    write(docs / "readme.md", b"# hi", mtime=ts("2024-05-06"))
    write(docs / "report.pdf", b"%PDF-", mtime=ts("2024-02-01"))
    write(docs / "photo.jpg", b"\xff\xd8", mtime=ts("2024-05-07"))
    store.add(str(docs))

    assert found_paths(store) == [
        "docs/readme.md", "docs/notes.txt", "docs/2024/april.xlsx", "docs/2024/march.csv",
        "docs/report.pdf",
    ]  # fmt: skip
    assert found_paths(store, kinds=["csv", "xlsx"]) == ["docs/2024/april.xlsx",
                                                         "docs/2024/march.csv"]  # fmt: skip
    assert found_paths(store, kinds=["txt"]) == ["docs/readme.md", "docs/notes.txt"]
    assert found_paths(store, query="MARCH") == ["docs/2024/march.csv"]
    assert found_paths(store, query="2024 apr") == ["docs/2024/april.xlsx"]
    assert found_paths(store, modified_after=date(2024, 4, 5),
                       modified_before=date(2024, 5, 5)) == ["docs/notes.txt",
                                                             "docs/2024/april.xlsx"]  # fmt: skip
    [one] = store.search(query="notes", folder="DOCS", kinds=None, modified_after=None,
                         modified_before=None)  # fmt: skip
    assert (one.folder, one.path, one.size, one.kind) == ("docs", "notes.txt", 2, "txt")
    assert one.modified.startswith("2024-05-05")
    with pytest.raises(FolderError):
        found_paths(store, folder="nope")

    for n in range(60):
        write(docs / "many" / f"f{n:02d}.csv", mtime=ts("2025-01-01") + n * 60)
    many = found_paths(store, query="many")
    assert len(many) == 50
    assert many[0] == "docs/many/f59.csv" and many[-1] == "docs/many/f10.csv"


def test_search_depth_is_limited(env):
    store, docs, _ = env
    deep = docs
    for n in range(10):
        deep = deep / f"d{n}"
        write(deep / f"at{n + 1}.csv")
    store.add(str(docs))
    found = found_paths(store)
    assert "docs/d0/d1/d2/d3/d4/d5/d6/d7/at8.csv" in found
    assert not any(p.endswith("at9.csv") or p.endswith("at10.csv") for p in found)


def test_search_stops_at_entry_and_time_budget(env, conn, monkeypatch):
    store, docs, data = env
    for n in range(30):
        write(docs / f"f{n:02d}.csv")
    store.add(str(docs))
    monkeypatch.setattr(folders_module, "MAX_ENTRIES", 10)
    outcome = store.search_in(store.list(), query=None, kinds=None, modified_after=None,
                              modified_before=None)  # fmt: skip
    assert len(outcome.found) == 10 and outcome.cut is True
    monkeypatch.setattr(folders_module, "MAX_ENTRIES", 20_000)

    ticks = iter(range(1000))
    timed = FolderStore(conn, data, clock=lambda: next(ticks))
    outcome = timed.search_in(timed.list(), query=None, kinds=None, modified_after=None,
                              modified_before=None)  # fmt: skip
    assert outcome.cut is True and len(outcome.found) < 30
    assert folders_module.MAX_ENTRIES == 20_000 and folders_module.SEARCH_SECONDS == 5
    assert folders_module.MAX_DEPTH == 8 and folders_module.MAX_MATCHES == 50


# ---- suggestions ---------------------------------------------------------------------------------


def test_suggestions_lists_existing_unadded_home_folders(env, tmp_path, monkeypatch):
    store, _, _ = env
    home = tmp_path / "home"
    for name in ("Documents", "Downloads", "OneDrive/Desktop"):
        (home / name).mkdir(parents=True)
    monkeypatch.setattr(folders_module, "_home", lambda: home)
    suggested = store.suggestions()
    assert suggested == [
        {"label": "Documents", "path": str((home / "Documents").resolve())},
        {"label": "Downloads", "path": str((home / "Downloads").resolve())},
        {"label": "OneDrive Desktop", "path": str((home / "OneDrive" / "Desktop").resolve())},
    ]
    store.add(str(home / "Downloads"))
    assert [s["label"] for s in store.suggestions()] == ["Documents", "OneDrive Desktop"]
