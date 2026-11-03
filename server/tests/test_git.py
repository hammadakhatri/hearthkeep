"""Host git, hardened: fixed -c flags, a minimal environment, the token only in the
environment and redacted from every result and error; the repo store built on it."""

import base64
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fakes import FakeGitHub

from hearthkeep.db import connect
from hearthkeep.github import repos as repos_module
from hearthkeep.github.api import TOKEN_SECRET, GitHubClient
from hearthkeep.github.git import MIN_GIT, GitError, GitRunner, github_url, redact
from hearthkeep.github.repos import RepoError, RepoStore, sweep_repo_folders
from hearthkeep.secrets import SecretStore

TOKEN = "github_pat_" + "b" * 30
REPO = {"full_name": "acme/widgets", "default_branch": "main", "private": True,
        "permissions": {"push": True}}  # fmt: skip

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def plain_git(cwd, *args):
    """The test's own git (not the hardened runner), to build fixtures."""
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True,
    )  # fmt: skip


def make_origin(base: Path) -> Path:
    """A bare repo with two commits on `main`."""
    work = base / "work"
    work.mkdir()
    plain_git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("one\n")
    plain_git(work, "add", "README.md")
    plain_git(work, "commit", "-q", "-m", "first")
    (work / "app.py").write_text("print(1)\n")
    plain_git(work, "add", "app.py")
    plain_git(work, "commit", "-q", "-m", "second")
    origin = base / "origin.git"
    plain_git(base, "clone", "-q", "--bare", str(work), str(origin))
    return origin


def make_store(data: Path, origin: Path | None, fake: FakeGitHub, token: str | None = TOKEN):
    conn = connect(data / "hk.db")
    secrets = SecretStore(data)
    if token is not None:
        secrets.put(TOKEN_SECRET, token)

    def saved() -> str | None:
        return secrets.get(TOKEN_SECRET)

    url = (lambda name: origin.as_uri()) if origin is not None else github_url
    git = GitRunner(data, protocol="file" if origin is not None else "https", remote_url=url)
    return RepoStore(conn, data, git, GitHubClient(saved, transport=fake.transport), secrets)


@pytest.fixture
def runner(tmp_path):
    return GitRunner(tmp_path / "data")


@pytest.fixture
def fake_github():
    fake = FakeGitHub()
    fake.routes[("GET", "/repos/acme/widgets")] = (200, REPO)
    return fake


@pytest.fixture
def local_origin(tmp_path):
    return make_origin(tmp_path)


@pytest.fixture
def store(tmp_path, local_origin, fake_github):
    data = tmp_path / "data"
    data.mkdir()
    s = make_store(data, local_origin, fake_github)
    yield s
    s.conn.close()


def test_github_url():
    assert github_url("acme/widgets") == "https://github.com/acme/widgets.git"


def test_git_argv_has_every_hardening_flag(runner, tmp_path):
    argv = runner.argv("status")
    for flag in (
        "core.hooksPath=",
        "core.fsmonitor=false",
        "protocol.allow=never",
        "submodule.recurse=false",
        "credential.helper=",
        "core.autocrlf=false",
    ):
        assert any(a.startswith(flag) for a in argv)
    for flag in ("core.symlinks=false", "core.longpaths=true", "protocol.https.allow=always",
                 "http.followRedirects=false", "credential.helper="):  # fmt: skip
        assert flag in argv
    hooks = (tmp_path / "data" / "runtime" / "git-home" / "no-hooks").as_posix()
    assert f"core.hooksPath={hooks}" in argv
    assert argv[-1] == "status"
    # every setting is a `-c` pair before the subcommand
    assert all(argv[i - 1] == "-c" for i, a in enumerate(argv[:-1]) if "=" in a)


def test_file_protocol_only_when_asked(tmp_path):
    assert "protocol.file.allow=always" not in GitRunner(tmp_path).argv("fetch")
    assert "protocol.file.allow=always" in GitRunner(tmp_path, protocol="file").argv("fetch")


def test_env_is_minimal(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy")
    env = runner.env(None)
    home = tmp_path / "data" / "runtime" / "git-home"
    assert env["HOME"] == str(home) and env["USERPROFILE"] == str(home)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == str(home / "empty.gitconfig")
    assert env["GIT_ASKPASS"] == ""
    allowed = {"PATH", "HOME", "USERPROFILE", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM",
               "GIT_CONFIG_GLOBAL", "GIT_ASKPASS", "SYSTEMROOT"}  # fmt: skip
    assert set(env) <= allowed
    assert ("SYSTEMROOT" in env) == (sys.platform == "win32")


def test_token_is_in_env_not_argv(runner):
    token = "github_pat_" + "b" * 30
    assert all(token not in a for a in runner.argv("fetch"))
    env = runner.env(token)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_COUNT"] == "1"
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {basic}"


def test_redact_covers_token_and_basic_form():
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    text = f"url {TOKEN} header AUTHORIZATION: basic {basic} other github_pat_{'c' * 25}"
    cleaned = redact(text, TOKEN)
    assert "github_pat_" not in cleaned and basic not in cleaned
    assert "eC1hY2Nlc3MtdG9rZW4" not in cleaned
    assert cleaned.count("[redacted]") == 3
    assert redact(f"Authorization: Basic {basic}", None) == "Authorization: Basic [redacted]"


async def test_git_errors_never_show_token(runner, tmp_path):
    # a fetch from a missing remote fails; stderr echoes config: both forms must be redacted
    with pytest.raises(GitError) as e:
        await runner.run(tmp_path, "fetch", "file:///nope", token="github_pat_" + "b" * 30)
    assert "github_pat_" not in e.value.message and "eC1hY2Nlc3MtdG9rZW4" not in e.value.message


async def test_results_and_errors_are_redacted_when_git_prints_the_token(tmp_path):
    """git itself won't echo the header; a `config` listing does. Both forms come out."""
    runner = GitRunner(tmp_path / "data")
    result = await runner.run(tmp_path, "config", "--list", "--show-scope", token=TOKEN)
    assert result.code == 0
    assert "extraheader" in result.out
    assert "github_pat_" not in result.out and "eC1hY2Nlc3MtdG9rZW4" not in result.out
    with pytest.raises(GitError) as e:
        await runner.run(tmp_path, "config", "--get", "http.https://github.com/.extraheader",
                         "nope", token=TOKEN)  # fmt: skip
    assert "github_pat_" not in e.value.message


async def test_check_false_returns_the_failure(runner, tmp_path):
    result = await runner.run(tmp_path, "rev-parse", "HEAD", check=False)
    assert result.code != 0 and result.err


def test_available_needs_a_recent_git(tmp_path):
    assert MIN_GIT == (2, 31)
    assert GitRunner(tmp_path).available() is True
    assert GitRunner(tmp_path, git="no-such-git-binary").available() is False


async def test_missing_git_is_a_git_error(tmp_path):
    with pytest.raises(GitError):
        await GitRunner(tmp_path, git="no-such-git-binary").run(tmp_path, "status")


class SleepyRunner(GitRunner):
    """Runs a Python that starts a grandchild holding the output pipe, then sleeps."""

    def argv(self, *args):
        code = (
            "import subprocess, sys, time;"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
            "time.sleep(30)"
        )
        return [sys.executable, "-c", code]


async def test_timeout_kills_the_process_tree(tmp_path):
    runner = SleepyRunner(tmp_path / "data")
    started = time.monotonic()
    with pytest.raises(GitError) as e:
        await runner.run(tmp_path, "status", timeout=1.0)
    assert time.monotonic() - started < 15
    assert "stopped" in e.value.message


# --- the repo store ---------------------------------------------------------------------


async def test_add_clones_and_remove_deletes(local_origin, store, tmp_path):
    repo = await store.add("acme/widgets")
    assert repo.full_name == "acme/widgets" and repo.default_branch == "main" and repo.private
    folder = store.dir(repo)
    assert folder == tmp_path / "data" / "workspace" / "repos" / repo.id
    assert (folder / "app.py").read_text() == "print(1)\n"
    assert store.list() == [repo]
    assert store.get(repo.id) == repo
    assert store.by_name("ACME/Widgets") == repo
    assert list((tmp_path / "data" / "runtime").glob("clone-*")) == []

    status = await store.status(repo)
    assert status.branch == "main" and status.changed == []
    assert (status.ahead, status.behind) == (0, 0)
    (folder / "app.py").write_text("print(2)\n")
    (folder / "new.txt").write_text("n\n")
    status = await store.status(repo)
    assert sorted(status.changed) == ["app.py", "new.txt"]

    store.conn.execute("INSERT INTO repo_runs (id, repo_id) VALUES ('run1', ?)", (repo.id,))
    store.conn.execute("INSERT INTO repo_deps (repo_id, lock_hash) VALUES (?, 'h')", (repo.id,))
    removed: list[str] = []

    async def hook(r):
        removed.append(r.id)

    store.on_remove.append(hook)
    assert await store.remove(repo.id) is True
    assert removed == [repo.id]
    assert not folder.exists()
    assert store.list() == [] and store.get(repo.id) is None
    for table in ("repo_runs", "repo_deps"):
        assert store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert await store.remove(repo.id) is False


async def test_add_twice_is_already_added(store):
    await store.add("acme/widgets")
    with pytest.raises(RepoError) as e:
        await store.add("ACME/widgets")
    assert e.value.reason == "already_added"


async def test_fetch_brings_new_commits(local_origin, store, tmp_path):
    repo = await store.add("acme/widgets")
    work = tmp_path / "work"
    (work / "more.txt").write_text("m\n")
    plain_git(work, "add", "more.txt")
    plain_git(work, "commit", "-q", "-m", "third")
    plain_git(work, "push", "-q", str(local_origin), "main")
    await store.fetch(repo)
    status = await store.status(repo)
    assert (status.ahead, status.behind) == (0, 1)


async def test_hooks_in_clone_never_run(local_origin, store):
    repo = await store.add("acme/widgets")
    folder = store.dir(repo)
    marker = folder.parent / "hook-ran"
    hook = folder / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n")
    hook.chmod(0o755)
    await store.git.run(folder, "checkout", "-q", "-b", "hearthkeep/try")
    await store.git.run(folder, "checkout", "-q", "main")
    assert not marker.exists()
    # the clone has no hooks of its own, not even git's samples
    assert sorted(p.name for p in hook.parent.iterdir()) == ["post-checkout"]


async def test_add_failure_leaves_no_row_or_folder(store, fake_github, tmp_path):
    data = tmp_path / "data"
    fake_github.routes[("GET", "/repos/acme/gone")] = (404, {"message": "Not Found"})
    with pytest.raises(RepoError) as e:
        await store.add("acme/gone")
    assert e.value.reason == "repo_not_found"

    # GitHub knows it, but the clone fails: no row, no folder, nothing left in runtime/
    fake_github.routes[("GET", "/repos/acme/broken")] = (200, {**REPO, "full_name": "acme/broken"})
    store.git._remote_url = lambda name: (tmp_path / "missing.git").as_uri()
    with pytest.raises(RepoError) as e:
        await store.add("acme/broken")
    assert e.value.reason == "clone_failed"
    assert "github_pat_" not in e.value.message
    assert store.list() == []
    assert list((data / "workspace" / "repos").iterdir()) == []
    assert [p for p in (data / "runtime").iterdir() if p.name != "git-home"] == []

    with pytest.raises(RepoError) as e:
        await store.add("not a name")
    assert e.value.reason == "name_format"


async def test_add_needs_a_token(tmp_path, local_origin, fake_github):
    data = tmp_path / "data"
    data.mkdir()
    s = make_store(data, local_origin, fake_github, token=None)
    try:
        assert s.token() is None
        with pytest.raises(RepoError) as e:
            await s.add("acme/widgets")
        assert e.value.reason == "no_token"
        assert fake_github.requests == []
    finally:
        s.conn.close()


async def test_clone_carries_no_submodules_and_no_token_in_config(store, tmp_path):
    repo = await store.add("acme/widgets")
    config = (store.dir(repo) / ".git" / "config").read_text()
    assert "github_pat_" not in config and "extraheader" not in config


def test_runtime_dirs_are_data_subdirs():
    from hearthkeep.config import DATA_SUBDIRS

    assert "runtime/git-home/no-hooks" in DATA_SUBDIRS


def plant(folder: Path) -> Path:
    """A folder like a left-behind clone, with a read-only file as git leaves its objects."""
    (folder / ".git" / "objects").mkdir(parents=True)
    locked = folder / ".git" / "objects" / "pack"
    locked.write_text("x")
    locked.chmod(0o444)
    (folder / "app.py").write_text("print(1)\n")
    return folder


def test_sweep_removes_orphan_clones_and_staging(tmp_path):
    data = tmp_path / "data"
    conn = connect(tmp_path / "hk.db")
    try:
        conn.execute("INSERT INTO repos (id, full_name) VALUES ('kept0000kept0000', 'a/b')")
        repos_dir = data / "workspace" / "repos"
        kept = plant(repos_dir / "kept0000kept0000")
        orphan = plant(repos_dir / "0123456789abcdef")
        staging = plant(data / "runtime" / "clone-fedcba9876543210")
        (data / "runtime" / "git-home" / "no-hooks").mkdir(parents=True)
        odd = repos_dir / "not a repo id"
        odd.mkdir()
        assert sweep_repo_folders(conn, data) == 2
        assert kept.is_dir() and odd.is_dir()
        assert not orphan.exists() and not staging.exists()
        assert (data / "runtime" / "git-home" / "no-hooks").is_dir()
        assert sweep_repo_folders(conn, data) == 0
        assert sweep_repo_folders(conn, tmp_path / "empty") == 0  # no folders yet: nothing to do
    finally:
        conn.close()


def test_sweep_never_follows_a_linked_repos_folder(tmp_path):
    from test_repo_paths import link_dir

    data = tmp_path / "data"
    outside = plant(tmp_path / "outside" / "0123456789abcdef")
    (data / "workspace").mkdir(parents=True)
    link_dir(data / "workspace" / "repos", outside.parent)
    conn = connect(tmp_path / "hk.db")
    try:
        assert sweep_repo_folders(conn, data) == 0
        assert (outside / "app.py").exists()
    finally:
        conn.close()


async def test_remove_retries_and_sweep_finishes_a_left_clone(store, monkeypatch, tmp_path):
    repo = await store.add("acme/widgets")
    folder = store.dir(repo)
    calls = []

    def stuck(path):  # a file held open by an indexer: nothing can be removed now
        calls.append(path)
        return False

    monkeypatch.setattr(repos_module, "_remove_tree", stuck)
    assert await store.remove(repo.id) is True
    assert calls == [folder] and folder.is_dir() and store.get(repo.id) is None
    monkeypatch.undo()
    assert sweep_repo_folders(store.conn, tmp_path / "data") == 1
    assert not folder.exists()
