from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pin_catalog.py"
spec = importlib.util.spec_from_file_location("pin_catalog", SCRIPT)
assert spec and spec.loader
pin_catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pin_catalog)

URL_A = "https://github.com/o/r/releases/download/v1/a.zip"
URL_B = "https://huggingface.co/o/r/resolve/" + "a" * 40 + "/dir/b.gguf"
SHA_A = "1" * 64
SHA_B = "2" * 64

CATALOG = f"""schema = 1

[[item]]
id = "one"
kind = "runtime"
name = "One"

[[item.file]]
url = "{URL_A}"
size = 10
sha256 = "{SHA_A}"

[[item]]
id = "two"
kind = "chat"
name = "Two"

[[item.file]]
url = "{URL_B}"
size = 20
sha256 = "{SHA_B}"
save_as = "model"
"""


def fake_api(answers):
    def api(url: str):
        return answers[url]

    return api


def test_check_reports_ok_and_differences(tmp_path, capsys):
    path = tmp_path / "catalog.toml"
    path.write_text(CATALOG, encoding="utf-8")
    api = fake_api({URL_A: (10, SHA_A), URL_B: (21, "3" * 64)})
    code = pin_catalog.main(["--check", "--catalog", str(path)], api=api)
    lines = capsys.readouterr().out.splitlines()
    assert code == 1
    assert lines[0].startswith("ok") and URL_A in lines[0]
    assert lines[1].startswith("differs: size 21 sha256 " + "3" * 64) and URL_B in lines[1]
    assert path.read_text(encoding="utf-8") == CATALOG


def test_check_all_ok_exits_zero(tmp_path, capsys):
    path = tmp_path / "catalog.toml"
    path.write_text(CATALOG, encoding="utf-8")
    api = fake_api({URL_A: (10, SHA_A), URL_B: (20, SHA_B)})
    assert pin_catalog.main(["--check", "--catalog", str(path)], api=api) == 0
    assert "differs" not in capsys.readouterr().out


def test_write_rewrites_size_and_sha_in_place(tmp_path):
    path = tmp_path / "catalog.toml"
    path.write_text(CATALOG, encoding="utf-8")
    api = fake_api({URL_A: (10, SHA_A), URL_B: (21, "3" * 64)})
    assert pin_catalog.main(["--write", "--catalog", str(path)], api=api) == 0
    text = path.read_text(encoding="utf-8")
    assert text == CATALOG.replace("size = 20", "size = 21").replace(SHA_B, "3" * 64)
