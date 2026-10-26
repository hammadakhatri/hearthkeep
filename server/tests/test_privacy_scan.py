import subprocess
import sys
from pathlib import Path

from privacy_scan import scan_text

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "privacy_scan.py"

# Bad strings are built by concatenation so this file never contains a literal match
# and does not trip the scanner itself.
USERS = "C:" + "\\Users\\"
LINK = "https://claude" + ".ai/" + "artifact/AbC123"
CODE_LINK = "https://claude" + ".ai/" + "code/artifact/0000"


def run_cli(*args: str, stdin: str | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_flags_windows_user_path():
    found = scan_text("open " + USERS + "someone\\notes.txt", [])
    assert any(f.rule == "abs-path" for f in found)


def test_flags_windows_user_path_any_case_and_drive():
    assert scan_text("d:" + "\\users\\x", [])


def test_flags_unix_home_paths():
    assert any(f.rule == "abs-path" for f in scan_text("/home" + "/alice/.ssh", []))
    assert any(f.rule == "abs-path" for f in scan_text("/Users" + "/alice/Documents", []))


def test_flags_claude_artifact_link():
    assert any(f.rule == "private-link" for f in scan_text("see " + LINK, []))
    assert any(f.rule == "private-link" for f in scan_text("see " + CODE_LINK, []))


def test_flags_tokens():
    pem = "-----BEGIN " + "RSA PRIVATE KEY-----"
    for s in ["ghp_" + "a" * 36, "github_pat_" + "a" * 40, "sk-local-" + "b" * 40, pem]:
        assert any(f.rule == "token" for f in scan_text(f"token = '{s}'", [])), s


def test_flags_extra_marker_case_insensitive():
    found = scan_text("by Jane Example", ["jane example"])
    assert any(f.rule == "personal-marker" for f in found)


def test_blank_marker_never_matches():
    assert scan_text("anything at all", ["", "  "]) == []


def test_allows_placeholders():
    assert scan_text("data folder: <data>/workspace and C:\\Hearthkeep\\data", []) == []


def test_reports_line_numbers_and_excerpt():
    (f,) = scan_text("fine\nfine\nsee " + LINK + "\n", [])
    assert f.line == 3
    assert "artifact/AbC123" in f.excerpt


def test_cli_exits_1_on_findings_and_0_when_clean(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("see " + LINK, encoding="utf-8")
    good = tmp_path / "good.txt"
    good.write_text("nothing here", encoding="utf-8")
    assert run_cli(str(bad)).returncode == 1
    assert run_cli(str(good)).returncode == 0
    assert run_cli().returncode == 0


def test_cli_reads_stdin_when_path_is_dash():
    bad = run_cli("-", stdin="+ see " + LINK + "\n")
    assert bad.returncode == 1
    assert "private-link" in bad.stdout
    assert run_cli("-", stdin="clean text\n").returncode == 0


def test_cli_skips_missing_and_binary_files(tmp_path):
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01" + LINK.encode())
    assert run_cli(str(binary), str(tmp_path / "gone.txt")).returncode == 0
