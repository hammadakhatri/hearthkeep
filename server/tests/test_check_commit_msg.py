import subprocess
import sys
from pathlib import Path

from check_commit_msg import find_violations

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_commit_msg.py"

# Built by concatenation so this file never contains the forbidden phrases verbatim.
TRAILER = "Co-Authored" + "-By: Someone <someone@example.com>"
BADGE = "Generated " + "with [Claude Code](https://example.invalid)"


def test_accepts_plain_conventional_message():
    assert find_violations("feat: add thing\n\nBody text.\n") == []


def test_rejects_coauthor_trailer_any_case():
    assert find_violations("fix: x\n\n" + TRAILER)
    assert find_violations("fix: x\n\n" + TRAILER.lower())
    assert find_violations("fix: x\n\n" + TRAILER.upper())


def test_rejects_generated_with_claude():
    assert find_violations("docs: y\n\n" + BADGE)
    assert find_violations("docs: y\n\ngenerated WITH claude")


def test_allows_generated_with_other_tools():
    assert find_violations("build: generated with protoc") == []


def test_cli_reads_file_and_stdin(tmp_path):
    msg = tmp_path / "MSG"
    msg.write_text("feat: ok\n", encoding="utf-8")
    assert subprocess.run([sys.executable, str(SCRIPT), str(msg)]).returncode == 0
    msg.write_text("feat: no\n\n" + TRAILER, encoding="utf-8")
    assert subprocess.run([sys.executable, str(SCRIPT), str(msg)]).returncode == 1
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "-"], input=TRAILER, capture_output=True, text=True
    )
    assert out.returncode == 1
