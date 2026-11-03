"""repo_read: lists, reads, searches and inspects an added repo; nothing the model writes is
ever read as a git option, and only repos in the table are reachable."""

import json
import shutil

import pytest
from fakes import FakeGitHub
from test_git import REPO, make_origin, make_store, plain_git

from hearthkeep.github.diff import DIFF_CAP, cap_diff
from hearthkeep.tools.base import ToolContext
from hearthkeep.tools.repo import UNKNOWN_REPO, RepoReadTool, github_ready

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
async def setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    fake = FakeGitHub()
    fake.routes[("GET", "/repos/acme/widgets")] = (200, REPO)
    store = make_store(data, make_origin(tmp_path), fake)
    repo = await store.add("acme/widgets")
    yield store, repo, RepoReadTool(store)
    store.conn.close()


async def read(tool, store, **args):
    ctx = ToolContext("c", "t", None, store.conn)
    args.setdefault("repo", "acme/widgets")
    return await tool.run(args, ctx)


def commit_local(store, repo, name, text, message):
    clone = store.dir(repo)
    (clone / name).parent.mkdir(parents=True, exist_ok=True)
    (clone / name).write_text(text)
    plain_git(clone, "add", name)
    plain_git(clone, "commit", "-q", "-m", message)


async def test_repo_read_lists_reads_and_searches(setup):
    store, repo, tool = setup
    commit_local(store, repo, "src/pkg/mod.py", "needle = 1\nother = 2\n", "add mod")

    listing = await read(tool, store, action="list")
    assert listing.ok
    lines = listing.content.splitlines()
    assert "README.md" in lines and "app.py" in lines and "src/" in lines
    assert "src/pkg/" in lines and "src/pkg/mod.py" not in lines  # two levels only
    assert not any(".git" in line.split("/") for line in lines)

    sub = await read(tool, store, action="list", path="src")
    assert "src/pkg/" in sub.content.splitlines() and "src/pkg/mod.py" in sub.content

    got = await read(tool, store, action="read", path="src/pkg/mod.py")
    assert got.ok and "needle = 1" in got.content and "other = 2" in got.content
    later = await read(tool, store, action="read", path="src/pkg/mod.py", start_line=2)
    assert "other = 2" in later.content and "needle" not in later.content

    found = await read(tool, store, action="search", query="needle")
    assert found.ok and "src/pkg/mod.py:1:" in found.content
    nothing = await read(tool, store, action="search", query="zzz-absent")
    assert nothing.ok and "No matches" in nothing.content
    scoped = await read(tool, store, action="search", query="print", path="src")
    assert "No matches" in scoped.content  # app.py is outside src


async def test_read_is_capped_at_400_lines(setup):
    store, repo, tool = setup
    commit_local(store, repo, "long.txt", "".join(f"line {i}\n" for i in range(1, 1001)), "long")
    got = await read(tool, store, action="read", path="long.txt")
    assert "line 400" in got.content and "line 401" not in got.content
    assert "start_line=401" in got.content


async def test_repo_read_refuses_binary_and_large(setup):
    store, repo, tool = setup
    clone = store.dir(repo)
    (clone / "blob.bin").write_bytes(b"abc\x00def")
    (clone / "big.txt").write_bytes(b"a" * (1024 * 1024 + 1))
    binary = await read(tool, store, action="read", path="blob.bin")
    assert not binary.ok and "binary" in binary.content.lower()
    big = await read(tool, store, action="read", path="big.txt")
    assert not big.ok and "1 MB" in big.content
    missing = await read(tool, store, action="read", path="nope.txt")
    assert not missing.ok
    folder = await read(tool, store, action="read", path="src")
    assert not folder.ok


async def test_repo_read_refuses_git_dir_and_escapes(setup):
    store, repo, tool = setup
    for path in (".git/config", ".GIT/config", "../x", "a/../../x", ".git./config"):
        for action in ("read", "list", "search"):
            extra = {"query": "x"} if action == "search" else {}
            got = await read(tool, store, action=action, path=path, **extra)
            assert not got.ok
            assert got.content == "That path is outside the repo or inside its .git folder."


async def test_repo_read_unknown_repo_message(setup):
    store, repo, tool = setup
    got = await read(tool, store, action="status", repo="acme/other")
    assert not got.ok
    assert got.content == "That repo isn't added to Hearthkeep. Add it in Repos first."
    assert got.content == UNKNOWN_REPO
    assert (await read(tool, store, action="list", repo="ACME/Widgets")).ok  # any case


async def test_repo_read_search_query_is_not_an_option(setup):
    store, repo, tool = setup
    commit_local(store, repo, "opt.txt", "see --open-files-in-pager=x here\n", "opt")
    got = await read(tool, store, action="search", query="--open-files-in-pager=x")
    assert got.ok and "opt.txt:1:" in got.content
    pager = await read(tool, store, action="search", query="-e")
    assert pager.ok  # literal text, never a flag
    glob = await read(tool, store, action="search", query="x", path="*")
    assert not glob.ok or "No matches" in glob.content  # a path is literal, not a pathspec


async def test_search_caps_matches_and_line_length(setup):
    store, repo, tool = setup
    commit_local(store, repo, "many.txt", "hit\n" * 300 + "hit " + "y" * 1000 + "\n", "many")
    got = await read(tool, store, action="search", query="hit")
    rows = [line for line in got.content.splitlines() if line.startswith("many.txt:")]
    assert len(rows) == 200 and all(len(line) <= 300 for line in rows)
    assert "more" in got.content.splitlines()[-1]


async def test_status_log_and_fetch(setup):
    store, repo, tool = setup
    (store.dir(repo) / "scratch.txt").write_text("x")
    status = await read(tool, store, action="status")
    assert "main" in status.content and "scratch.txt" in status.content
    log = await read(tool, store, action="log")
    rows = log.content.splitlines()
    assert len(rows) == 2 and "second" in rows[0] and "first" in rows[1]
    commit_local(store, repo, "z.txt", "z", "third")
    older = await read(tool, store, action="log", ref="HEAD~2")
    assert older.content.splitlines()[0].endswith("first")
    fetched = await read(tool, store, action="fetch")
    assert fetched.ok


async def test_refs_are_checked_and_never_options(setup):
    store, repo, tool = setup
    for ref in ("--output=evil.txt", "-p", "--upload-pack=x"):
        for action in ("log", "diff"):
            got = await read(tool, store, action=action, ref=ref)
            assert not got.ok
    assert not (store.dir(repo) / "evil.txt").exists()
    nope = await read(tool, store, action="log", ref="no-such-ref")
    assert not nope.ok


async def test_diff_covers_commits_since_merge_base_and_working_tree(setup):
    store, repo, tool = setup
    await read(tool, store, action="fetch")
    clean = await read(tool, store, action="diff")
    assert clean.ok and "No changes" in clean.content
    commit_local(store, repo, "new.py", "committed = True\n", "add new")
    (store.dir(repo) / "app.py").write_text("print(2)\n")
    (store.dir(repo) / "loose.txt").write_text("untracked")
    got = await read(tool, store, action="diff")
    assert got.ok
    assert "+committed = True" in got.content and "+print(2)" in got.content
    assert "loose.txt" in got.content  # untracked files are named


async def test_diff_without_remote_branch_says_to_fetch(setup):
    store, repo, tool = setup
    plain_git(store.dir(repo), "update-ref", "-d", "refs/remotes/origin/main")
    got = await read(tool, store, action="diff")
    assert not got.ok and "fetch" in got.content


async def test_missing_arguments_are_explained(setup):
    store, repo, tool = setup
    assert "path" in (await read(tool, store, action="read")).content
    assert "query" in (await read(tool, store, action="search")).content


def test_cap_diff():
    short = "diff --git a b\n+x\n"
    assert cap_diff(short) == short
    big = "+" + "é" * DIFF_CAP + "\n"
    out = cap_diff(big)
    assert out.endswith("\n… diff cut at 200 KB")
    assert len(out.encode()) < DIFF_CAP + 100


def test_tool_shape_and_availability(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    store = make_store(data, None, FakeGitHub())
    tool = RepoReadTool(store)
    assert (tool.name, tool.level_key) == ("repo_read", "github")
    props = tool.parameters["properties"]
    assert props["action"]["enum"] == ["list", "read", "search", "status", "diff", "log", "fetch"]
    assert tool.parameters["additionalProperties"] is False
    assert props["query"]["maxLength"] == 200 and props["start_line"]["minimum"] == 1
    assert json.loads(json.dumps(tool.parameters))
    assert tool.is_available() is True and github_ready(store) is True
    store.git._available = False
    assert tool.is_available() is False  # no host git
    store.git._available = True
    store._secrets.delete("github_token")
    assert tool.is_available() is False  # no saved token
    store.conn.close()
