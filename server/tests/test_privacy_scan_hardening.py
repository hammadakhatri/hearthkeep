import subprocess
import sys
from pathlib import Path

from privacy_scan import decode_bytes, load_markers, main, scan_text

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "privacy_scan.py"

# Bad strings are built by concatenation so this file never contains a literal match.
LINK = "https://claude" + ".ai/" + "artifact/AbC123"
ESCAPED_USERS = "C:" + "\\\\" + "Users" + "\\\\name"  # escaped backslashes, as in source code
SLASH_USERS = "C:/" + "Users/name"  # no trailing slash


def run_cli(*args: str, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd
    )


def test_flags_escaped_backslash_user_path():
    assert any(f.rule == "abs-path" for f in scan_text('p = "' + ESCAPED_USERS + '"', []))


def test_flags_forward_slash_user_path_without_trailing_slash():
    assert any(f.rule == "abs-path" for f in scan_text("cd " + SLASH_USERS, []))


def test_sk_rule_ignores_kebab_case_prose_but_flags_real_keys():
    assert scan_text("see disk-encryption-and-backup-strategy for details", []) == []
    assert scan_text("task-management-and-scheduling-overview", []) == []
    assert any(f.rule == "token" for f in scan_text("key=sk-" + "a" * 30, []))
    assert any(f.rule == "token" for f in scan_text("(sk-" + "a" * 30 + ")", []))


def test_decodes_utf16_with_bom_and_without():
    text = "see " + LINK
    assert decode_bytes(text.encode("utf-16")) == text.replace("see ", "see ")
    assert LINK in decode_bytes(text.encode("utf-16-le"))
    assert LINK in decode_bytes(text.encode("utf-16-be"))
    assert LINK in decode_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))


def test_real_binary_is_not_decoded():
    assert decode_bytes(b"\x00\x01\x02\x89PNG\x00\x00\x00\x0d" * 50) is None


def test_cli_scans_utf16_file_and_reports_skipped_binary(tmp_path):
    ps = tmp_path / "out.txt"
    ps.write_bytes(("see " + LINK + "\r\n").encode("utf-16"))
    result = run_cli(str(ps))
    assert result.returncode == 1
    assert "private-link" in result.stdout

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02\x03" * 100)
    result = run_cli(str(binary))
    assert result.returncode == 0
    assert "skipped binary file" in result.stderr


def test_markers_file_reads_bom_and_warns_when_empty(tmp_path):
    (tmp_path / ".hearthkeep-personal-markers").write_bytes(b"\xef\xbb\xbfJane Example\n# note\n")
    assert load_markers(tmp_path) == ["Jane Example"]
    found = scan_text("by jane example", load_markers(tmp_path))
    assert any(f.rule == "personal-marker" for f in found)


def test_empty_markers_file_warns_on_stderr(tmp_path, capsys):
    (tmp_path / ".hearthkeep-personal-markers").write_text("# nothing yet\n", encoding="utf-8")
    assert main([], root=tmp_path) == 0
    assert "has no markers" in capsys.readouterr().err
