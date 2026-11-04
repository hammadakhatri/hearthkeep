"""Editing the owner's models.toml / config.toml in place, never mangling it."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from hearthkeep.tomledit import TomlEditError, edit_toml

FIXTURE = Path(__file__).parent / "fixtures" / "upgrade" / "models.toml"
WIN_PATH = r"C:\Hearthkeep\data\models\chat\qwen3.5-4b"


def _owner_file(tmp_path, newline="\n", bom=False):
    text = FIXTURE.read_text(encoding="utf-8").replace("{root}", "D:/owner")
    text += "\n# my own note, keep me\n# llama_ctx = 8192\n"
    text = text.replace("\n", newline)
    path = tmp_path / "models.toml"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def test_edit_keeps_comments_and_other_keys(tmp_path):
    path = _owner_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    edit_toml(
        path,
        set_top={"llama_server_exe": "C:\\tools\\llama-server.exe", "default_chat_model": "qwen"},
        set_table={"model_dirs": {"qwen3.5-4b": WIN_PATH}},
    )
    after = path.read_text(encoding="utf-8")
    data = tomllib.loads(after)
    assert data["llama_server_exe"] == "C:\\tools\\llama-server.exe"
    assert data["default_chat_model"] == "qwen"
    assert data["model_dirs"]["qwen3.5-4b"] == WIN_PATH
    assert data["model_dirs"]["my-gemma"] == "D:/owner/models/my-gemma"
    assert data["llama_port"] == 8080
    for kept in (
        "# A models.toml written by hand before add-ons existed.",
        "llama_port = 8080\n",
        "# my own note, keep me\n# llama_ctx = 8192\n",
        "\"my-gemma\" = 'D:/owner/models/my-gemma'\n",
    ):
        assert kept in after
    # Only the two replaced lines differ among the lines that were there before.
    assert [x for x in before.splitlines() if x not in after.splitlines()] == [
        "llama_server_exe = 'D:/owner/llama/llama-server.exe'",
        'default_chat_model = "my-gemma"',
    ]


def test_trailing_comment_is_kept_on_replace(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('port = 8000  # the port\nname = "a"\n', encoding="utf-8")
    edit_toml(path, set_top={"port": 9000})
    assert path.read_text(encoding="utf-8") == 'port = 9000  # the port\nname = "a"\n'


def test_new_top_key_goes_before_first_table(tmp_path):
    path = tmp_path / "models.toml"
    path.write_text("a = 1\n\n[t]\nb = 2\n", encoding="utf-8")
    edit_toml(path, set_top={"chat_server": "external"})
    assert path.read_text(encoding="utf-8") == "a = 1\nchat_server = 'external'\n\n[t]\nb = 2\n"


def test_missing_file_is_created(tmp_path):
    path = tmp_path / "sub" / "models.toml"
    edit_toml(path, set_top={"llama_port": 8081}, set_table={"model_dirs": {"x": WIN_PATH}})
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {
        "llama_port": 8081,
        "model_dirs": {"x": WIN_PATH},
    }


def test_replace_existing_table_entry_and_append_into_table(tmp_path):
    path = _owner_file(tmp_path)
    edit_toml(path, set_table={"model_dirs": {"my-gemma": "E:/new", "other": "E:/o"}})
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["model_dirs"] == {"my-gemma": "E:/new", "other": "E:/o"}
    assert "# my own note, keep me" in path.read_text(encoding="utf-8")


def test_remove_top_key(tmp_path):
    path = _owner_file(tmp_path)
    edit_toml(path, remove_top=["llama_port", "not_there"])
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "llama_port" not in data and "llama_server_exe" in data
    assert "# my own note, keep me" in path.read_text(encoding="utf-8")


def test_backup_written_once(tmp_path):
    path = _owner_file(tmp_path)
    original = path.read_bytes()
    backup = tmp_path / "models.toml.before-hearthkeep"
    edit_toml(path, set_top={"llama_port": 9001}, backup=backup)
    assert backup.read_bytes() == original
    edit_toml(path, set_top={"llama_port": 9002}, backup=backup)
    assert backup.read_bytes() == original
    assert tomllib.loads(path.read_text(encoding="utf-8"))["llama_port"] == 9002


def test_windows_path_round_trips(tmp_path):
    path = tmp_path / "models.toml"
    quoted = r"D:\Owner's Models\Hearthkeep"
    edit_toml(path, set_table={"model_dirs": {"a": WIN_PATH, "b": quoted}})
    assert f"\"a\" = '{WIN_PATH}'" in path.read_text(encoding="utf-8")
    dirs = tomllib.loads(path.read_text(encoding="utf-8"))["model_dirs"]
    assert dirs == {"a": WIN_PATH, "b": quoted}
    edit_toml(path, set_top={"p": quoted, "q": 'say "hi"\\'})
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert (data["p"], data["q"]) == (quoted, 'say "hi"\\')


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", [False, True])
def test_line_endings_and_bom_are_kept(tmp_path, newline, bom):
    path = _owner_file(tmp_path, newline=newline, bom=bom)
    edit_toml(
        path,
        set_top={"default_chat_model": "qwen", "chat_server": "local"},
        set_table={"model_dirs": {"new": "E:/n"}},
    )
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") == bom
    body = raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    if newline == "\r\n":
        assert body.count("\n") == body.count("\r\n")
    else:
        assert "\r" not in body
    data = tomllib.loads(body)
    assert data["chat_server"] == "local" and data["model_dirs"]["new"] == "E:/n"


def test_missing_table_is_created_at_the_end(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"a = 1")  # no final newline
    edit_toml(path, set_table={"t": {"k": "v"}})
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {"a": 1, "t": {"k": "v"}}


def test_multiline_values_are_not_confused_for_keys(tmp_path):
    path = tmp_path / "models.toml"
    text = (
        'notes = """\nllama_port = 1\n[model_dirs]\n"""\nlist = [\n  1,\n  2,\n]\nllama_port = 5\n'
    )
    path.write_text(text, encoding="utf-8")
    edit_toml(path, set_top={"llama_port": 6, "list": 3})
    assert tomllib.loads(path.read_text(encoding="utf-8")) == {
        "notes": "llama_port = 1\n[model_dirs]\n",
        "list": 3,
        "llama_port": 6,
    }


@pytest.mark.parametrize(
    ("text", "kwargs"),
    [
        ("model_dirs.x = 'a'\n", {"set_table": {"model_dirs": {"y": "b"}}}),
        ("[model_dirs]\nx.y = 'a'\n", {"set_table": {"model_dirs": {"x": "b"}}}),
        ("model_dirs = { x = 'a' }\n", {"set_table": {"model_dirs": {"y": "b"}}}),
        ("[[model_dirs]]\nx = 'a'\n", {"set_table": {"model_dirs": {"y": "b"}}}),
        ("[model_dirs.x]\nz = 1\n", {"set_table": {"model_dirs": {"x": "b"}}}),
        ("a = 1\na = 2\n", {"set_top": {"a": 3}}),
        ("a = 1\na = 2\n", {"remove_top": ["a"]}),
        ("a.b = 1\n", {"set_top": {"a": 3}}),
        ("a = { b = 1 }\n", {"set_top": {"a": 3}}),
        ("[a]\nb = 1\n", {"set_top": {"a": 3}}),
        ("a = 1\n[t]\nk = 1\nk = 2\n", {"set_table": {"t": {"k": "v"}}}),
        ("a = 1\n", {"set_top": {"bad key": 3}}),
        ("a = 1\n", {"set_top": {"a": 1}, "remove_top": ["a"]}),
        ("not toml [\n", {"set_top": {"a": 3}}),
    ],
)
def test_unsupported_shape_is_refused_not_mangled(tmp_path, text, kwargs):
    path = tmp_path / "models.toml"
    path.write_bytes(text.encode("utf-8"))
    backup = tmp_path / "models.toml.before-hearthkeep"
    with pytest.raises(TomlEditError):
        edit_toml(path, backup=backup, **kwargs)
    assert path.read_bytes() == text.encode("utf-8")
    assert not backup.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["models.toml"]


def test_non_utf8_file_is_refused(tmp_path):
    path = tmp_path / "models.toml"
    path.write_bytes(b"a = '\xff'\n")
    with pytest.raises(TomlEditError):
        edit_toml(path, set_top={"a": "b"})
    assert path.read_bytes() == b"a = '\xff'\n"
