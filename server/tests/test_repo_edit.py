"""repo_edit: edits and commits only on hearthkeep/ branches, shows each diff before it
happens, commits as the checked account's noreply identity, and works without an approval."""

import shutil
import subprocess

import pytest
from fakes import FakeGitHub
from test_git import REPO, make_origin, make_store, plain_git

from hearthkeep.github.diff import DIFF_CAP, cap_diff, file_diff
from hearthkeep.tools.base import ToolContext
from hearthkeep.tools.repo import UNKNOWN_REPO, RepoEditTool

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

R = "acme/widgets"
NEED_BRANCH = 'Create a hearthkeep/ branch first with repo_edit action "branch".'
REFUSED = "That path is outside the repo or inside its .git folder."


@pytest.fixture
async def setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    fake = FakeGitHub()
    fake.routes[("GET", "/repos/acme/widgets")] = (200, REPO)
    store = make_store(data, make_origin(tmp_path), fake)
    repo = await store.add(R)
    store.conn.execute(
        "INSERT INTO github_account (id, login, user_id, checked_at) VALUES (1, 'octo', 42, 'x')"
    )
    store.conn.commit()
    yield store, repo, RepoEditTool(store)
    store.conn.close()


def ctx_for(store, detail=None):
    return ToolContext("c", "t", None, store.conn, approved_detail=detail)


async def edit(tool, store, **args):
    args.setdefault("repo", R)
    return await tool.run(args, ctx_for(store))


async def on_branch(tool, store, name="work"):
    got = await edit(tool, store, action="branch", name=name)
    assert got.ok, got.content


def git_out(store, repo, *args):
    return subprocess.run(
        ["git", *args], cwd=store.dir(repo), check=True, capture_output=True, text=True
    ).stdout


async def test_edit_refused_on_default_branch(setup):
    store, repo, tool = setup
    for args in (
        {"action": "write", "path": "a.txt", "content": "x"},
        {"action": "replace", "path": "app.py", "old": "1", "new": "2"},
        {"action": "delete", "path": "app.py"},
        {"action": "commit", "message": "m"},
    ):
        got = await edit(tool, store, **args)
        assert not got.ok and got.content == NEED_BRANCH
    assert not (store.dir(repo) / "a.txt").exists()


async def test_branch_starts_from_origin_default(setup):
    store, repo, tool = setup
    got = await edit(tool, store, action="branch", name="fix-1")
    assert got.ok and "hearthkeep/fix-1" in got.content
    assert git_out(store, repo, "branch", "--show-current").strip() == "hearthkeep/fix-1"
    await edit(tool, store, action="write", path="x.txt", content="x\n")
    again = await edit(tool, store, action="branch", name="fix-1")  # exists: switch to it
    assert again.ok and "existing" in again.content
    assert (store.dir(repo) / "x.txt").exists()
    for bad in ("Bad", "-x", "a/b", "", "x" * 41, "--detach"):
        got = await edit(tool, store, action="branch", name=bad)
        assert not got.ok


async def test_branch_without_origin_ref_says_fetch(setup):
    store, repo, tool = setup
    plain_git(store.dir(repo), "update-ref", "-d", "refs/remotes/origin/main")
    got = await edit(tool, store, action="branch", name="w")
    assert not got.ok and "fetch" in got.content


async def test_branch_then_write_shows_diff_in_approval(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    args = {"repo": R, "action": "write", "path": "a.txt", "content": "new\n"}
    detail = await tool.approval_detail(args, ctx_for(store))
    assert detail["diff"].startswith("--- /dev/null\n+++ b/a.txt")
    assert detail["repo"] == R and detail["branch"] == "hearthkeep/work"
    assert detail["path"] == "a.txt"
    got = await tool.run(args, ctx_for(store))  # approved_detail is None: still works
    assert got.ok
    assert (store.dir(repo) / "a.txt").read_text() == "new\n"
    args = {"repo": R, "action": "write", "path": "a.txt", "content": "newer\n"}
    detail = await tool.approval_detail(args, ctx_for(store))
    assert detail["diff"].startswith("--- a/a.txt\n+++ b/a.txt")
    assert "-new\n+newer\n" in detail["diff"]
    gone = await tool.approval_detail(
        {"repo": R, "action": "delete", "path": "a.txt"}, ctx_for(store)
    )
    assert gone["diff"].startswith("--- a/a.txt\n+++ /dev/null")
    branch_detail = await tool.approval_detail(
        {"repo": R, "action": "branch", "name": "other"}, ctx_for(store)
    )
    assert branch_detail == {"repo": R, "branch": "hearthkeep/other"}


async def test_write_creates_folders_and_delete_removes(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    assert (await edit(tool, store, action="write", path="d/e/f.txt", content="hi")).ok
    assert (store.dir(repo) / "d/e/f.txt").read_text() == "hi"
    assert (await edit(tool, store, action="delete", path="d/e/f.txt")).ok
    assert not (store.dir(repo) / "d/e/f.txt").exists()
    assert not (await edit(tool, store, action="delete", path="d/e/f.txt")).ok
    folder = await edit(tool, store, action="delete", path="d")
    assert not folder.ok
    over = await edit(tool, store, action="write", path="big.txt", content="a" * (512 * 1024 + 1))
    assert not over.ok


async def test_replace_must_match_once(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    await edit(tool, store, action="write", path="r.txt", content="a b a\nc\n")
    twice = await edit(tool, store, action="replace", path="r.txt", old="a", new="z")
    assert not twice.ok
    assert twice.content == "The text to replace was found 2 times; it must match exactly once."
    none = await edit(tool, store, action="replace", path="r.txt", old="q", new="z")
    assert none.content == "The text to replace was found 0 times; it must match exactly once."
    once = await edit(tool, store, action="replace", path="r.txt", old="b a", new="B")
    assert once.ok
    assert (store.dir(repo) / "r.txt").read_text() == "a B\nc\n"
    detail = await tool.approval_detail(
        {"repo": R, "action": "replace", "path": "r.txt", "old": "c", "new": "d"}, ctx_for(store)
    )
    assert "-c\n+d\n" in detail["diff"]


async def test_commit_uses_noreply_identity(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    await edit(tool, store, action="write", path="a.txt", content="new\n")
    detail = await tool.approval_detail(
        {"repo": R, "action": "commit", "message": "Add a"}, ctx_for(store)
    )
    assert "+++ b/a.txt" in detail["diff"] and detail["message"] == "Add a"
    got = await edit(tool, store, action="commit", message="Add a\n\nBecause.")
    assert got.ok, got.content
    author = git_out(store, repo, "log", "-1", "--format=%an <%ae>").strip()
    committer = git_out(store, repo, "log", "-1", "--format=%cn <%ce>").strip()
    assert author == "octo <42+octo@users.noreply.github.com>" == committer
    assert git_out(store, repo, "log", "-1", "--format=%B").startswith("Add a\n\nBecause.")
    assert "user" not in (store.dir(repo) / ".git" / "config").read_text()  # config untouched
    nothing = await edit(tool, store, action="commit", message="Again")
    assert not nothing.ok and "nothing to commit" in nothing.content.lower()


async def test_commit_refuses_without_account(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    await edit(tool, store, action="write", path="a.txt", content="new\n")
    store.conn.execute("DELETE FROM github_account")
    store.conn.commit()
    got = await edit(tool, store, action="commit", message="Add a")
    assert not got.ok and "token" in got.content
    assert git_out(store, repo, "log", "--format=%s", "-1").strip() == "second"


async def test_commit_message_rules(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    await edit(tool, store, action="write", path="a.txt", content="new\n")
    assert not (await edit(tool, store, action="commit", message="x" * 73)).ok
    assert not (await edit(tool, store, action="commit", message="ok\n" + "y" * 2000)).ok
    assert not (await edit(tool, store, action="commit", message="   ")).ok
    assert not (await edit(tool, store, action="commit")).ok
    assert (await edit(tool, store, action="commit", message="x" * 72)).ok
    await edit(tool, store, action="write", path="b.txt", content="b\n")
    dashed = await edit(tool, store, action="commit", message="--amend")
    assert dashed.ok and git_out(store, repo, "log", "-1", "--format=%s").strip() == "--amend"


async def test_crlf_file_keeps_crlf(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    (store.dir(repo) / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    assert (await edit(tool, store, action="write", path="w.txt", content="a\nb\n")).ok
    assert (store.dir(repo) / "w.txt").read_bytes() == b"a\r\nb\r\n"
    got = await edit(tool, store, action="replace", path="w.txt", old="a\nb", new="x\ny\nz")
    assert got.ok
    assert (store.dir(repo) / "w.txt").read_bytes() == b"x\r\ny\r\nz\r\n"
    assert (await edit(tool, store, action="write", path="lf.txt", content="a\nb\n")).ok
    assert (store.dir(repo) / "lf.txt").read_bytes() == b"a\nb\n"


async def test_write_into_git_dir_refused(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    for path in (
        ".GIT/config",
        ".git/hooks/post-checkout",
        "src/../.git/config",
        ".git./x",
        "../x",
    ):
        for action in ("write", "replace", "delete"):
            got = await edit(tool, store, action=action, path=path, content="x", old="a", new="b")
            assert not got.ok and got.content == REFUSED, (path, action)
    detail = await tool.approval_detail(
        {"repo": R, "action": "write", "path": ".GIT/config", "content": "x"}, ctx_for(store)
    )
    assert detail["diff"] == ""


async def test_binary_file_is_not_edited(setup):
    store, repo, tool = setup
    await on_branch(tool, store)
    (store.dir(repo) / "b.bin").write_bytes(b"ab\x00cd")
    got = await edit(tool, store, action="write", path="b.bin", content="x")
    assert not got.ok and "text" in got.content


async def test_unknown_repo(setup):
    store, repo, tool = setup
    got = await edit(tool, store, action="branch", name="x", repo="acme/other")
    assert not got.ok and got.content == UNKNOWN_REPO
    assert (
        await tool.approval_detail({"repo": "acme/other", "action": "commit"}, ctx_for(store))
        is None
    )


async def test_tool_class_and_level(setup):
    store, repo, tool = setup
    from hearthkeep.permissions import ActionClass

    assert tool.name == "repo_edit" and tool.level_key == "github"
    assert tool.classify({"action": "commit"}) == ActionClass.CHANGE_LOCAL
    assert tool.is_available()


def test_cap_diff_marks_cut():
    assert cap_diff("small\n") == "small\n"
    cut = cap_diff("x" * (DIFF_CAP + 10))
    assert cut.endswith("… diff cut at 200 KB") and len(cut.encode()) < DIFF_CAP + 40


def test_file_diff_shapes():
    assert file_diff("a", "x\n", "x\n") == ""
    assert file_diff("a", None, "x\n").startswith("--- /dev/null\n+++ b/a\n")
    assert file_diff("a", "x\n", None).startswith("--- a/a\n+++ /dev/null\n")
    assert "\\ No newline at end of file" in file_diff("a", "x", "y")
