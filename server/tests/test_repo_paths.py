"""Paths the model gives inside a clone, and repo names: nothing reaches `.git` or leaves."""

import os
import sys

import pytest

from hearthkeep.github.paths import (
    BRANCH_PREFIX,
    BRANCH_SLUG,
    PathRefused,
    check_repo_name,
    safe_path,
)


def link_dir(link, target):
    """A directory link: a junction on Windows (no privilege needed), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.mark.parametrize("rel", [".git/config", ".GIT/hooks/x", ".git./x", ".git /x",
                                 "a/../.git/HEAD", "../outside", "/abs", "C:/x", "a:stream",
                                 "GIT~1/config", "", "a//b"])  # fmt: skip
def test_safe_path_refuses_git_dir_spellings(tmp_path, rel):
    with pytest.raises(PathRefused):
        safe_path(tmp_path, rel)


@pytest.mark.parametrize("rel", ["a\\..\\.git\\config", ".git\\config", "./a", "a/.", "...",
                                 "src/.Git../hooks", "git~2", "a/b\x00", "a/" + "b" * 400,
                                 ".git", "a/"])  # fmt: skip
def test_safe_path_refuses_other_tricks(tmp_path, rel):
    with pytest.raises(PathRefused) as error:
        safe_path(tmp_path, rel)
    assert error.value.message == "That path is outside the repo or inside its .git folder."


def test_safe_path_allows_ordinary_paths(tmp_path):
    (tmp_path / "src").mkdir()
    assert safe_path(tmp_path, "src/app.py") == tmp_path / "src" / "app.py"
    assert safe_path(tmp_path, "new/dir/file.txt") == tmp_path / "new" / "dir" / "file.txt"
    assert safe_path(tmp_path, ".github/workflows/ci.yml").name == "ci.yml"
    assert safe_path(tmp_path, ".gitignore").name == ".gitignore"


def test_safe_path_refuses_symlinked_folder(tmp_path):
    root = tmp_path / "clone"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    link_dir(root / "docs", outside)
    for rel in ("docs/secret.txt", "docs", "docs/new.txt"):
        with pytest.raises(PathRefused):
            safe_path(root, rel)


def test_safe_path_refuses_a_link_file(tmp_path):
    if sys.platform == "win32":
        pytest.skip("file symlinks need a privilege on Windows")
    (tmp_path / "target.txt").write_text("x")
    os.symlink(tmp_path / "target.txt", tmp_path / "link.txt")
    with pytest.raises(PathRefused):
        safe_path(tmp_path, "link.txt")


@pytest.mark.parametrize("name", ["octo/hello-world", "a/b", "Octo-Cat/x.y_z", "a1/..a"])
def test_check_repo_name_accepts(name):
    assert check_repo_name(name) == name


@pytest.mark.parametrize("name", ["octo", "octo/", "/x", "-octo/x", "octo/..", "octo/.",
                                  "octo/x/y", "o" * 40 + "/x", "octo/" + "x" * 101,
                                  "octo/x y", "octo/x\n", 7])  # fmt: skip
def test_check_repo_name_refuses(name):
    with pytest.raises(ValueError):
        check_repo_name(name)


def test_branch_rules():
    assert BRANCH_PREFIX == "hearthkeep/"
    assert BRANCH_SLUG.fullmatch("fix-login-2")
    for bad in ("-x", "Fix", "a" * 41, "a_b", ""):
        assert not BRANCH_SLUG.fullmatch(bad)
