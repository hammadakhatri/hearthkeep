import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LINK = "https://claude" + ".ai/" + "artifact/AbC123"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def git(cwd: Path, *args: str):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def make_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "privacy_scan.py", tmp_path / "scripts")
    (tmp_path / ".githooks").mkdir()
    shutil.copy(REPO / ".githooks" / "pre-commit", tmp_path / ".githooks")
    return tmp_path


def run_hook(repo: Path):
    return subprocess.run(["sh", ".githooks/pre-commit"], cwd=repo, capture_output=True, text=True)


def test_hook_blocks_staged_content_even_if_disk_copy_was_cleaned(tmp_path):
    repo = make_repo(tmp_path)
    bad = repo / "notes.txt"
    bad.write_text("see " + LINK + "\n", encoding="utf-8")
    git(repo, "add", "notes.txt")
    bad.write_text("all clean now\n", encoding="utf-8")  # working tree differs from the index
    result = run_hook(repo)
    assert result.returncode == 1
    assert "notes.txt" in result.stdout and "private-link" in result.stdout


def test_hook_passes_clean_staged_content_even_if_disk_copy_is_dirty(tmp_path):
    repo = make_repo(tmp_path)
    f = repo / "notes.txt"
    f.write_text("clean\n", encoding="utf-8")
    git(repo, "add", "notes.txt")
    f.write_text("see " + LINK + "\n", encoding="utf-8")  # unstaged edit is not committed
    assert run_hook(repo).returncode == 0


def test_hook_passes_with_nothing_staged(tmp_path):
    assert run_hook(make_repo(tmp_path)).returncode == 0
