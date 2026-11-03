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
    fake.repos.append(REPO)
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


# -- final review: every result fits the model's context -----------------------------------

MODEL_CAP = 8000


async def test_read_stops_at_the_character_budget_on_a_whole_line(setup):
    store, repo, tool = setup
    body = "".join(f"{i:04d} " + "x" * 1990 + "\n" for i in range(1, 51))  # 50 lines, ~2 KB each
    commit_local(store, repo, "wide.txt", body, "wide")
    got = await read(tool, store, action="read", path="wide.txt")
    assert got.ok and len(got.content) <= MODEL_CAP
    rows = got.content.splitlines()
    numbered = [r for r in rows if r.split("\t")[0].isdigit()]
    last = int(numbered[-1].split("\t")[0])
    assert 1 < last < 50
    assert all(len(r.split("\t", 1)[1]) == 1995 for r in numbered)  # whole lines, never cut
    assert rows[-1].endswith(f"read on with start_line={last + 1}.")
    nxt = await read(tool, store, action="read", path="wide.txt", start_line=last + 1)
    assert nxt.content.startswith(f"{last + 1}\t{last + 1:04d} ")


async def test_every_action_is_capped_for_the_model(setup):
    store, repo, tool = setup
    await read(tool, store, action="fetch")
    # Many long files and many long matching lines: list, search and diff all run long.
    for i in range(120):
        commit_dir = store.dir(repo) / "many" / f"{'n' * 60}{i:03d}"
        commit_dir.mkdir(parents=True)
        (commit_dir / "f.txt").write_text(("needle " + "z" * 280 + "\n") * 2)
    plain_git(store.dir(repo), "add", "-A")
    plain_git(store.dir(repo), "commit", "-q", "-m", "many")
    for action, extra in (("list", {"path": "many"}), ("search", {"query": "needle"}),
                          ("diff", {})):  # fmt: skip
        got = await read(tool, store, action=action, **extra)
        assert got.ok, action
        assert len(got.content) <= MODEL_CAP + 200, (action, len(got.content))
        assert "characters cut" in got.content, action
        assert "narrow" in got.content, action


async def test_diff_can_be_scoped_to_a_path(setup):
    store, repo, tool = setup
    await read(tool, store, action="fetch")
    commit_local(store, repo, "src/one.py", "one = 1\n", "one")
    commit_local(store, repo, "two.py", "two = 2\n", "two")
    (store.dir(repo) / "src" / "loose.txt").write_text("x")
    (store.dir(repo) / "other.txt").write_text("y")
    got = await read(tool, store, action="diff", path="src")
    assert got.ok
    assert "+one = 1" in got.content and "two = 2" not in got.content
    assert "src/loose.txt" in got.content and "other.txt" not in got.content
    refused = await read(tool, store, action="diff", path=".git/config")
    assert not refused.ok
    assert refused.content == "That path is outside the repo or inside its .git folder."


async def test_repo_read_is_denied_once_the_token_is_gone(tmp_path):
    from datetime import UTC, datetime

    from fakes import FakeChatClient

    from hearthkeep.agent.loop import TURNED_OFF, AgentLoop
    from hearthkeep.approvals import ApprovalStore
    from hearthkeep.config import Settings
    from hearthkeep.permissions import ToolLevel, set_level
    from hearthkeep.providers.chat import StreamDone, TextDelta, ToolCallRequest
    from hearthkeep.tools.base import ToolRegistry

    data = tmp_path / "data"
    data.mkdir()
    store = make_store(data, None, FakeGitHub())
    tool = RepoReadTool(store)
    ran = []
    real_run = tool.run

    async def spy(args, ctx):
        ran.append(args)
        return await real_run(args, ctx)

    tool.run = spy
    registry = ToolRegistry()
    registry.register(tool)
    set_level(store.conn, "github", ToolLevel.ALLOWED)

    class TokenRemoved(FakeChatClient):
        async def stream(self, messages, tools, think):
            async for event in super().stream(messages, tools, think):
                store._secrets.delete("github_token")  # removed in Settings meanwhile
                yield event

    client = TokenRemoved(
        [
            [ToolCallRequest("c1", "repo_read", json.dumps({"repo": "acme/widgets",
                                                             "action": "status"})),
             StreamDone("tool_calls")],
            [TextDelta("ok"), StreamDone("stop")],
        ]
    )  # fmt: skip
    clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731
    approvals = ApprovalStore(store.conn, ttl_minutes=10, clock=clock, poll_interval=0.01)
    loop = AgentLoop(client, registry, approvals, store.conn,
                     Settings(data_dir=data, docker_available=False))  # fmt: skip
    events = [e async for e in loop.run_turn("c", "t", [{"role": "user", "content": "x"}], False)]
    assert [t["function"]["name"] for t in client.calls[0]["tools"]] == ["repo_read"]
    assert ran == []
    answer = [m for m in client.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert json.loads(answer) == {"error": TURNED_OFF}
    assert [e for e in events if e["type"] == "approval_required"] == []
    store.conn.close()
