import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from check_commit_msg import find_violations, main

REPO = Path(__file__).resolve().parents[2]

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


def test_rejects_generated_by_claude():
    assert find_violations("docs: y\n\nGenerated " + "by Claude")


def test_rejects_attribution_across_link_and_lines():
    assert find_violations("docs: y\n\nGenerated with [Claude Code](https://example.invalid)")
    assert find_violations("docs: y\n\nGenerated with\n[Claude\nCode](https://example.invalid)")


def test_rejects_vendor_noreply_address():
    assert find_violations("fix: x\n\nsigned noreply" + "@anthropic.com")


def test_rejects_coauthored_by_anywhere_in_message():
    assert find_violations("fix: x thanks to co-authored" + "-by mention in prose")
    assert find_violations("fix: x\n\nbody\n\n\n   Co-authored" + "-by: A <a@example.com>")


# ---- personal data in the message --------------------------------------------------------

USER_PATH = "C:" + "\\Users\\someone\\project"
HOME_PATH = "/home" + "/someone/project"


def test_rejects_absolute_user_paths():
    assert find_violations("fix: x\n\nfound in " + USER_PATH + "\n")
    assert find_violations("fix: x\n\nsee " + HOME_PATH)


def test_rejects_personal_markers_case_insensitive():
    assert find_violations("feat: thanks Jane Example", markers=["jane example"])
    assert find_violations("feat: plain message", markers=["jane example"]) == []


def test_ignores_git_comment_lines_and_verbose_diff():
    message = (
        "fix: x\n\n# Please enter the commit message\n"
        "# ------------------------ >8 ------------------------\n"
        "+ a diff line with " + HOME_PATH + "\n"
    )
    assert find_violations(message) == []


def make_hook_repo(tmp_path: Path, markers: bytes | None) -> Path:
    (tmp_path / "scripts").mkdir()
    for name in ("privacy_scan.py", "check_commit_msg.py"):
        shutil.copy(REPO / "scripts" / name, tmp_path / "scripts")
    (tmp_path / ".githooks").mkdir()
    shutil.copy(REPO / ".githooks" / "commit-msg", tmp_path / ".githooks")
    if markers is not None:
        (tmp_path / ".hearthkeep-personal-markers").write_bytes(markers)
    return tmp_path


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
def test_commit_msg_hook_uses_markers_file(tmp_path):
    repo = make_hook_repo(tmp_path, "Jane Example\n".encode("utf-16"))
    msg = repo / "MSG"

    def hook():
        return subprocess.run(
            ["sh", ".githooks/commit-msg", "MSG"], cwd=repo, capture_output=True, text=True
        )

    msg.write_text("feat: ok\n", encoding="utf-8")
    assert hook().returncode == 0
    msg.write_text("feat: thanks jane example\n", encoding="utf-8")
    result = hook()
    assert result.returncode == 1 and "personal" in result.stderr
    assert "jane" not in result.stderr.lower()  # never echo the marker itself
    msg.write_text("feat: ok\n\nsee " + HOME_PATH + "\n", encoding="utf-8")
    assert hook().returncode == 1


def test_broken_markers_file_rejects_commit(tmp_path, capsys):
    root = make_hook_repo(tmp_path, b"# nothing here\n")
    msg = root / "MSG"
    msg.write_text("feat: ok\n", encoding="utf-8")
    assert main([str(msg)], root=root) == 1
    assert "has no markers" in capsys.readouterr().err
