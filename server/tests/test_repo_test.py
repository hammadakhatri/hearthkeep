"""repo_test: a repo's tests run in the repo sandbox on a throwaway copy of its working tree
(never `.git`), with no network, the dependency volume read-only, a 600 s limit and the
output kept as its head and tail. Runs are saved and listed on the Repos screen."""

# ruff: noqa: F811  (fixtures are imported from other test modules)

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fakes import FakeGitHub
from test_api import CSRF, harness, signed_in  # noqa: F401  (harness is a fixture)
from test_api_repos import add
from test_git import REPO, TOKEN, make_origin, make_store
from test_repo_deps import TAG, a_repo, fake_docker, npm_files  # noqa: F401

from hearthkeep.db import utcnow
from hearthkeep.github import testrun
from hearthkeep.github.api import TOKEN_SECRET
from hearthkeep.github.deps import (
    MODEL_HEAD_CHARS,
    MODEL_OUTPUT_CHARS,
    MODEL_TAIL_CHARS,
    OUTPUT_CUT,
    DepsCache,
    for_model,
)
from hearthkeep.github.deps import REPO_NO_DOCKER as NO_DOCKER_MESSAGE
from hearthkeep.github.deps import REPO_NO_IMAGE as NO_IMAGE_MESSAGE
from hearthkeep.github.git import GitRunner
from hearthkeep.github.testrun import (
    DEPS_UNREADABLE,
    HEAD_BYTES,
    MAX_TREE_BYTES,
    MAX_TREE_FILES,
    RUN_BROKE,
    RUN_STOPPED,
    TAIL_BYTES,
    TEST_TIMEOUT_S,
    TREE_HAS_LINK,
    TREE_TOO_LARGE,
    RepoTestRunner,
    TreeRefused,
    copy_tree,
)
from hearthkeep.permissions import DEFAULT_LEVELS, ActionClass, ToolLevel
from hearthkeep.sandbox import image as sandbox_image
from hearthkeep.secrets import SecretStore
from hearthkeep.tools.base import ToolContext
from hearthkeep.tools.repo import UNKNOWN_REPO
from hearthkeep.tools.repo_deps import DEPS_MISSING, NO_COMMAND, RepoTestTool

R = "acme/widgets"
UV_PROJECT = '[project]\nname = "w"\nversion = "1.0"\nrequires-python = ">=3.12"\n'


def install(deps: DepsCache, repo, root: Path, fake) -> None:
    """Record the current lock file as installed (a row and its volume)."""
    plan = deps.plan(repo, root)
    deps.conn.execute(
        "INSERT INTO repo_deps (repo_id, lock_hash, ecosystem, volume, installed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (repo.id, plan.lock_hash, plan.ecosystem, plan.volume, utcnow()),
    )
    deps.conn.commit()
    if fake is not None:
        fake.state.joinpath("volumes.json").write_text(json.dumps([plan.volume]))


def link_or_skip(link: Path, target: Path) -> None:
    """A directory link: a symlink, or a junction on Windows when symlinks need rights."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        pass
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("can't make a link here")


# -- the container -------------------------------------------------------------------------


@pytest.fixture
def runner(tmp_path, fake_docker):
    from hearthkeep.db import connect

    conn = connect(tmp_path / "hk.db")
    deps = DepsCache(conn, fake_docker.argv, TAG)
    yield RepoTestRunner(conn, tmp_path / "data", fake_docker.argv, TAG, deps)
    conn.close()


def test_limits_are_the_constraints():
    assert TEST_TIMEOUT_S == 600
    assert HEAD_BYTES == 65_536 and TAIL_BYTES == 196_608
    assert MAX_TREE_BYTES == 500 * 1024**2 and MAX_TREE_FILES == 50_000


def test_argv_is_locked_down(runner, tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = runner.deps.plan(a_repo(), root)
    tree = tmp_path / "tree"
    tree.mkdir()
    argv = runner.argv("r1", tree, plan, "npm test")
    assert argv[argv.index("--network") + 1] == "none"
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--rm"):
        assert flag in argv
    assert not any("/.git" in a for a in argv)
    body = argv[len(runner.docker) :]
    assert body[: body.index("--mount")] == [
        "run", "--rm", "--pull", "never", "--log-driver", "none", "--network", "none",
        "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "4g", "--memory-swap", "4g",
        "--cpus", "2", "--pids-limit", "512",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=1073741824",
    ]  # fmt: skip
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert mounts == [
        f"type=bind,source={tree.resolve()},target=/src,readonly",  # the PC's copy: read-only
        "type=volume,target=/repo",  # the container's own copy, removed with it (--rm)
        f"type=volume,source={plan.volume},target=/repo/node_modules,readonly",
    ]
    assert argv[argv.index("--name") + 1] == "hk-repo-r1"
    assert argv[argv.index("--label") + 1] == "hearthkeep.repo=1"
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert envs == ["HOME=/tmp", "CI=1"]
    # the copy first, then the command: given as its own argument ($1), never in the script
    assert argv[-6:] == [TAG, "sh", "-c", testrun.COPY_THEN_RUN, "sh", "npm test"]
    assert testrun.COPY_THEN_RUN == (
        'cp -R --preserve=mode,timestamps /src/. /repo/ && cd /repo && exec sh -c "$1"'
    )
    assert "npm test" not in testrun.COPY_THEN_RUN
    assert argv[argv.index("--workdir") + 1] == "/repo"
    assert "--gpus" not in argv and not any("docker.sock" in a for a in argv)

    (root / "pyproject.toml").write_text(UV_PROJECT)
    (root / "uv.lock").write_text("version = 1\n")
    uv = runner.deps.plan(a_repo(), root)
    argv = runner.argv("r2", tree, uv, "uv run --no-sync pytest -q")
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert mounts[2] == f"type=volume,source={uv.volume},target=/deps,readonly"
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert envs == ["HOME=/tmp", "CI=1", "UV_PROJECT_ENVIRONMENT=/deps/venv"]

    argv = runner.argv("r3", tree, None, "make test")
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"] == [
        f"type=bind,source={tree.resolve()},target=/src,readonly",
        "type=volume,target=/repo",
    ]
    quoted = "echo \"a b\" 'c' $HOME"
    assert runner.argv("r4", tree, None, quoted)[-2:] == ["sh", quoted]
    with pytest.raises(ValueError):
        runner.argv("../x", tree, None, "make test")


def test_copy_tree_skips_git_and_refuses_links(tmp_path):
    src = tmp_path / "src"
    (src / ".git" / "hooks").mkdir(parents=True)
    (src / ".git" / "config").write_text("[remote]\n")
    (src / "pkg" / ".git").mkdir(parents=True)
    (src / "pkg" / ".git" / "x").write_text("x")
    (src / "node_modules" / "left").mkdir(parents=True)
    (src / "node_modules" / "left" / "index.js").write_text("x")
    (src / "pkg" / "mod.py").write_text("print(1)\n")
    (src / "README.md").write_bytes(b"one\r\n")
    dest = tmp_path / "dest"
    copy_tree(src, dest)
    found = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*"))
    assert found == ["README.md", "pkg", "pkg/mod.py"]
    assert (dest / "README.md").read_bytes() == b"one\r\n"

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("no")
    link_or_skip(src / "pkg" / "away", outside)
    with pytest.raises(TreeRefused) as refused:
        copy_tree(src, tmp_path / "dest2")
    assert str(refused.value) == TREE_HAS_LINK
    assert not (tmp_path / "dest2" / "pkg" / "away" / "secret.txt").exists()


def test_copy_tree_caps(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / ".git").mkdir(parents=True)
    for i in range(20):  # .git and node_modules don't count
        (src / ".git" / f"o{i}").write_text("x")
    for i in range(5):
        (src / f"f{i}.txt").write_text("x")
    # 50,001 real files take half a minute to make on Windows: the cap is lowered instead.
    monkeypatch.setattr(testrun, "MAX_TREE_FILES", 5)
    copy_tree(src, tmp_path / "ok")
    (src / "f5.txt").write_text("x")
    with pytest.raises(TreeRefused) as refused:
        copy_tree(src, tmp_path / "too-many")
    assert str(refused.value) == TREE_TOO_LARGE
    assert TREE_TOO_LARGE == (
        "The repo is too large to test: over 500 MB or 50,000 files "
        "(not counting .git and node_modules)."
    )
    monkeypatch.setattr(testrun, "MAX_TREE_FILES", 50_000)
    with open(src / "big.bin", "wb") as big:
        big.truncate(MAX_TREE_BYTES + 1 - 6)  # with the six 1-byte files: one byte over
    with pytest.raises(TreeRefused):
        copy_tree(src, tmp_path / "too-big")
    assert not (tmp_path / "too-big" / "big.bin").exists()  # refused before copying


# -- the tool, on a real clone -------------------------------------------------------------


class Fixture:
    def __init__(self, store, repo, deps, runner, tool, data):
        self.store, self.repo, self.deps, self.runner, self.tool, self.data = (
            store, repo, deps, runner, tool, data
        )  # fmt: skip

    @property
    def clone(self) -> Path:
        return self.store.dir(self.repo)


async def make_fixture(tmp_path, docker, tag) -> Fixture:
    data = tmp_path / "data"
    data.mkdir()
    fake = FakeGitHub()
    fake.repos.append(REPO)
    store = make_store(data, make_origin(tmp_path), fake)
    repo = await store.add(R)
    deps = DepsCache(store.conn, docker, tag)
    runner = RepoTestRunner(store.conn, data, docker, tag, deps)
    return Fixture(store, repo, deps, runner, RepoTestTool(store, deps, runner), data)


@pytest.fixture
async def repo_fixture(tmp_path, fake_docker):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    fixture = await make_fixture(tmp_path, fake_docker.argv, TAG)
    yield fixture
    fixture.store.conn.close()


@pytest.fixture
def docker_or_skip():
    """The built repo image's tag; skips when Docker or the image is missing."""
    if shutil.which("git") is None:
        pytest.skip("needs git")
    if not sandbox_image.docker_ok():
        pytest.skip("Docker is not running")
    tag = sandbox_image.repo_image_tag()
    if not sandbox_image.image_present(tag):
        pytest.skip(f"repo image {tag} is not built (hearthkeep sandbox build --data <data>)")
    return tag


def ctx(store, chat="c1"):
    return ToolContext(chat, "t", None, store.conn)


def runs_started(fake) -> list[list[str]]:
    return [c for c in fake.calls() if c[0] == "run"]


def test_tool_shape():
    assert RepoTestTool.name == "repo_test"
    assert RepoTestTool.level_key == "github_tests"
    assert RepoTestTool.parameters["required"] == ["repo"]
    assert RepoTestTool.parameters["properties"]["command"]["maxLength"] == 500
    assert DEFAULT_LEVELS["github_tests"] == ToolLevel.ASK


async def test_missing_deps_message(repo_fixture, fake_docker):
    f = repo_fixture
    npm_files(f.clone)
    assert f.tool.classify({"repo": R}) == ActionClass.LOOK
    for args in ({"repo": R}, {"repo": R, "command": "npm test"}):
        got = await f.tool.run(args, ctx(f.store))
        assert not got.ok and got.content == DEPS_MISSING
    assert DEPS_MISSING == (
        "Dependencies aren't installed for the current lock file. Call repo_install first."
    )
    install(f.deps, f.repo, f.clone, fake_docker)
    (f.clone / "package-lock.json").write_text('{"changed": true}')  # outdated now
    got = await f.tool.run({"repo": R}, ctx(f.store))
    assert not got.ok and got.content == DEPS_MISSING
    assert runs_started(fake_docker) == []
    assert f.runner.runs(f.repo.id) == []


async def test_default_commands_and_the_run_saved(repo_fixture, fake_docker, monkeypatch):
    f = repo_fixture
    got = await f.tool.run({"repo": R}, ctx(f.store))  # no dependency files: needs a command
    assert not got.ok and got.content == NO_COMMAND
    assert runs_started(fake_docker) == []

    monkeypatch.setenv("FAKE_DOCKER_OUTPUT", "3 passed\n")
    got = await f.tool.run({"repo": R, "command": "make test"}, ctx(f.store))
    assert got.ok and "passed" in got.content and "exit code 0" in got.content
    assert "3 passed" in got.content
    assert runs_started(fake_docker)[-1][-2:] == ["sh", "make test"]

    expected = {
        "npm": "npm test",
        "pip": "/deps/venv/bin/python -m pytest -q",
        "uv": "uv run --no-sync pytest -q",
    }
    npm_files(f.clone)
    install(f.deps, f.repo, f.clone, fake_docker)
    monkeypatch.setenv("FAKE_DOCKER_EXIT", "1")
    monkeypatch.setenv("FAKE_DOCKER_OUTPUT", "1 failed\n")
    got = await f.tool.run({"repo": R}, ctx(f.store, chat="c2"))
    assert got.ok and "failed" in got.content and "exit code 1" in got.content
    argv = runs_started(fake_docker)[-1]
    assert argv[-2:] == ["sh", expected["npm"]]
    assert not (f.clone / "node_modules").exists()
    (f.clone / "requirements.txt").write_text("pytest\n")
    install(f.deps, f.repo, f.clone, fake_docker)
    await f.tool.run({"repo": R}, ctx(f.store))
    assert runs_started(fake_docker)[-1][-2:] == ["sh", expected["pip"]]
    (f.clone / "pyproject.toml").write_text(UV_PROJECT)
    (f.clone / "uv.lock").write_text("version = 1\n")
    install(f.deps, f.repo, f.clone, fake_docker)
    await f.tool.run({"repo": R}, ctx(f.store))
    argv = runs_started(fake_docker)[-1]
    assert argv[-2:] == ["sh", expected["uv"]]
    assert "UV_PROJECT_ENVIRONMENT=/deps/venv" in argv

    runs = f.runner.runs(f.repo.id)
    assert [r.command for r in runs[:2]] == [expected["uv"], expected["pip"]]
    assert runs[-1].command == "make test" and runs[-1].status == "passed"
    assert runs[-1].exit_code == 0 and runs[-1].chat_id == "c1"
    failed = runs[-2]
    assert (failed.status, failed.exit_code, failed.chat_id) == ("failed", 1, "c2")
    assert all(r.output == "" for r in runs)  # the list leaves the output out
    one = f.runner.get(failed.id)
    assert one.output == "1 failed\n" and one.repo_id == f.repo.id
    assert f.runner.get("nope") is None
    assert list((f.data / "runtime" / "repo-jobs").glob("*")) == []  # job folders removed


async def test_a_run_never_mounts_the_clone_or_git(repo_fixture, fake_docker):
    f = repo_fixture
    await f.tool.run({"repo": R, "command": "true"}, ctx(f.store))
    argv = runs_started(fake_docker)[-1]
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert len(mounts) == 2 and mounts[0].endswith(",target=/src,readonly")
    assert mounts[1] == "type=volume,target=/repo"
    source = mounts[0].split("source=")[1].split(",target=")[0]
    assert Path(source).parent.parent == (f.data / "runtime" / "repo-jobs").resolve()
    assert str(f.clone.resolve()) not in " ".join(argv)
    assert TOKEN not in " ".join(argv)


async def test_unknown_repo_and_no_docker(repo_fixture, fake_docker, monkeypatch):
    f = repo_fixture
    got = await f.tool.run({"repo": "acme/other"}, ctx(f.store))
    assert not got.ok and got.content == UNKNOWN_REPO
    monkeypatch.setenv("FAKE_DOCKER_NO_IMAGE", "1")
    got = await f.tool.run({"repo": R, "command": "true"}, ctx(f.store))
    assert not got.ok and got.content == NO_IMAGE_MESSAGE
    monkeypatch.setenv("FAKE_DOCKER_NO_DAEMON", "1")
    got = await f.tool.run({"repo": R, "command": "true"}, ctx(f.store))
    assert not got.ok and got.content == NO_DOCKER_MESSAGE
    assert runs_started(fake_docker) == [] and f.runner.runs(f.repo.id) == []


async def test_a_refused_tree_is_an_error_run(repo_fixture, fake_docker, monkeypatch):
    f = repo_fixture
    monkeypatch.setattr(testrun, "MAX_TREE_FILES", 1)
    got = await f.tool.run({"repo": R, "command": "true"}, ctx(f.store))
    assert not got.ok and TREE_TOO_LARGE in got.content
    assert runs_started(fake_docker) == []
    (run,) = f.runner.runs(f.repo.id)
    assert run.status == "error" and run.exit_code is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_job_folder_is_private_and_the_tree_only_readable(runner, tmp_path):
    """The container only reads the PC's copy (at /src): folders and executables 0o755, files
    0o644, nothing group- or world-writable, and no node_modules folder in it (the npm volume
    is mounted at /repo/node_modules in the container's own copy). The job folder is 0o700."""
    jobs = runner.data_dir / "runtime" / "repo-jobs"
    jobs.mkdir(parents=True)
    jobs.chmod(0o755)  # left from an older version: tightened too
    job = jobs / "0123abcd"
    tree = job / "tree"
    src = tmp_path / "src"
    old_umask = os.umask(0o000)  # no umask: the clone's own files are world-writable too
    try:
        (src / "pkg").mkdir(parents=True)
        (src / "pkg" / "mod.py").write_text("print(1)\n")
        (src / "run.sh").write_text("#!/bin/sh\n")
        (src / "run.sh").chmod(0o700)  # executable: 0o755 in the copy
        (src / "open.txt").write_text("x")
        npm_files(src)
        assert stat.S_IMODE((src / "open.txt").stat().st_mode) & stat.S_IWOTH
        plan = runner.deps.plan(a_repo(), src)
        runner._prepare(src, job, tree, plan)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(jobs.stat().st_mode) == 0o700
    assert stat.S_IMODE(job.stat().st_mode) == 0o700
    assert not (tree / "node_modules").exists()
    modes = {
        p.relative_to(tree).as_posix(): stat.S_IMODE(p.stat().st_mode) for p in tree.rglob("*")
    }
    modes["."] = stat.S_IMODE(tree.stat().st_mode)
    assert modes == {
        ".": 0o755, "pkg": 0o755, "pkg/mod.py": 0o644, "run.sh": 0o755, "open.txt": 0o644,
        "package.json": 0o644, "package-lock.json": 0o644,
    }  # fmt: skip
    assert all(mode & (stat.S_IWGRP | stat.S_IWOTH) == 0 for mode in modes.values())


async def test_sweep_removes_left_jobs_and_containers(runner, fake_docker, monkeypatch):
    jobs = runner.data_dir / "runtime" / "repo-jobs"
    (jobs / "0123abcd" / "tree").mkdir(parents=True)
    (jobs / "0123abcd" / "tree" / "a.py").write_text("x")
    monkeypatch.setenv("FAKE_DOCKER_PS", "abc123 def456")
    runner.sweep_jobs()
    assert list(jobs.iterdir()) == []
    assert await runner.sweep_containers() == 2
    calls = fake_docker.calls()
    assert ["ps", "-aq", "--filter", "label=hearthkeep.repo=1"] in calls
    assert ["rm", "-f", "abc123", "def456"] in calls


# -- the real sandbox ----------------------------------------------------------------------


@pytest.mark.docker
async def test_test_run_cannot_reach_host_clone(tmp_path, docker_or_skip):
    f = await make_fixture(tmp_path, ("docker",), docker_or_skip)
    try:
        before = sorted(p.relative_to(f.clone).as_posix() for p in f.clone.rglob("*"))
        neighbours = sorted(p.name for p in f.clone.parent.iterdir())
        command = (
            "ls -a; mkdir -p .git/hooks && echo planted > .git/hooks/x && echo wrote-hook; "
            "echo planted > ../x && echo wrote-parent; echo planted > app.py; "
            "test -e .git/config && echo saw-config; exit 3"
        )
        run = await f.runner.run(f.repo, "c1", command)
        assert run.status == "failed" and run.exit_code == 3, run.output
        assert "app.py" in run.output and "README.md" in run.output
        assert "wrote-hook" in run.output  # only into the throwaway copy
        assert "wrote-parent" not in run.output and "saw-config" not in run.output
        after = sorted(p.relative_to(f.clone).as_posix() for p in f.clone.rglob("*"))
        assert after == before
        assert not (f.clone / ".git" / "hooks" / "x").exists()
        assert (f.clone / "app.py").read_text() == "print(1)\n"
        assert sorted(p.name for p in f.clone.parent.iterdir()) == neighbours
        assert list((f.data / "runtime" / "repo-jobs").glob("*")) == []
    finally:
        f.store.conn.close()


@pytest.mark.docker
async def test_hanging_tests_stop_and_keep_tail(tmp_path, docker_or_skip, monkeypatch):
    f = await make_fixture(tmp_path, ("docker",), docker_or_skip)
    # 15 s, not less: the limit includes `docker run` starting up, and the test needs well
    # over 256 KB printed before it, so the output is really cut.
    monkeypatch.setattr(testrun, "TEST_TIMEOUT_S", 15)
    try:
        run = await f.runner.run(
            f.repo, "c1", "python -c \"import time\nwhile 1: print('x'*1000)\""
        )
        assert run.status == "timed_out" and OUTPUT_CUT in run.output
        assert run.exit_code is None
        head, _, tail = run.output.partition(OUTPUT_CUT)
        assert len(head.encode()) <= HEAD_BYTES + 1 and len(tail.encode()) <= TAIL_BYTES + 1
        assert tail.rstrip().endswith("x")
        saved = f.runner.get(run.id)
        assert saved.status == "timed_out" and saved.output == run.output
        assert list((f.data / "runtime" / "repo-jobs").glob("*")) == []
        # `--rm` removes a killed container a moment after the kill: allow it up to 10 s.
        deadline = time.monotonic() + 10
        while True:
            left = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name=hk-repo-{run.id}"],
                capture_output=True, text=True, timeout=30,
            )  # fmt: skip
            if left.stdout.strip() == "" or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.5)
        assert left.stdout.strip() == ""
        # the chat carries on: the next run works
        again = await f.runner.run(f.repo, "c1", "echo ok")
        assert again.status == "passed" and "ok" in again.output
    finally:
        f.store.conn.close()


@pytest.mark.docker
async def test_a_run_writes_its_own_copy_and_never_the_pcs(tmp_path, docker_or_skip, monkeypatch):
    """The PC's copy is read-only at /src; the run works on the container's own copy at /repo
    (a volume Docker removes with the container), so it can write files there."""
    f = await make_fixture(tmp_path, ("docker",), docker_or_skip)
    seen: dict[str, dict[str, bytes]] = {}
    remove = testrun.remove_tree

    def snapshot_then_remove(job):
        tree = Path(job) / "tree"
        if tree.is_dir():
            seen["tree"] = {
                p.relative_to(tree).as_posix(): (p.read_bytes() if p.is_file() else b"/")
                for p in tree.rglob("*")
            }
        remove(job)

    monkeypatch.setattr(testrun, "remove_tree", snapshot_then_remove)
    try:
        expected = {
            p.relative_to(f.clone).as_posix(): (p.read_bytes() if p.is_file() else b"/")
            for p in f.clone.rglob("*")
            if ".git" not in p.relative_to(f.clone).parts
        }
        command = (
            "touch made && ls made && echo changed > app.py && cat app.py && pwd && "
            "{ touch /src/x 2>/dev/null || echo src-is-read-only; }"
        )
        run = await f.runner.run(f.repo, "c1", command)
        assert run.status == "passed", run.output
        assert "made" in run.output and "changed" in run.output and "/repo" in run.output
        assert "src-is-read-only" in run.output
        assert seen["tree"] == expected  # the PC's copy is as it was copied
        assert (f.clone / "app.py").read_text() == "print(1)\n"
        again = await f.runner.run(f.repo, "c1", "test -e made || echo fresh; cat app.py")
        assert again.status == "passed" and "fresh" in again.output, again.output
        assert "print(1)" in again.output  # each run starts from a new copy
        assert list((f.data / "runtime" / "repo-jobs").glob("*")) == []
    finally:
        f.store.conn.close()


@pytest.mark.docker
async def test_a_command_with_quotes_and_dollars_runs_as_written(tmp_path, docker_or_skip):
    f = await make_fixture(tmp_path, ("docker",), docker_or_skip)
    try:
        command = (
            "printf '%s|' \"a  b\" 'it'\"'\"'s' \"$HOME\" '$HOME' \"$1\"; echo; "
            "echo 'x\"; exit 9'"
        )
        run = await f.runner.run(f.repo, "c1", command)
        assert run.status == "passed", run.output
        lines = run.output.splitlines()
        assert lines[0] == "a  b|it's|/tmp|$HOME||"
        assert lines[1] == 'x"; exit 9'
    finally:
        f.store.conn.close()


@pytest.mark.docker
def test_a_failed_copy_fails_the_run_with_its_own_words(tmp_path, docker_or_skip):
    """No volume at /repo: the read-only root refuses the copy, and the command never runs."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("x")
    proc = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--read-only", "--user", "10001:10001",
         "--mount", f"type=bind,source={tree.resolve()},target=/src,readonly",
         docker_or_skip, "sh", "-c", testrun.COPY_THEN_RUN, "sh", "echo ran"],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert proc.returncode != 0
    assert "cp:" in proc.stderr and "ran" not in proc.stdout


@pytest.mark.docker
async def test_an_npm_run_sees_its_dependencies(tmp_path, docker_or_skip):
    f = await make_fixture(tmp_path, ("docker",), docker_or_skip)
    try:
        npm_files(f.clone)
        plan = f.deps.plan(f.repo, f.clone)
        job = tmp_path / "install-job"
        f.deps.stage(plan, f.clone, job)
        ok, out = await f.deps.install(f.repo, plan, job)
        assert ok, out
        try:
            run = await f.runner.run(
                f.repo, "c1",
                "ls -a node_modules && touch made && "
                "{ touch node_modules/x 2>/dev/null || echo deps-read-only; }",
            )  # fmt: skip
            assert run.status == "passed", run.output
            assert ".package-lock.json" in run.output and "deps-read-only" in run.output
        finally:
            await f.deps.remove_for(f.repo)
    finally:
        f.store.conn.close()


# -- over HTTP -----------------------------------------------------------------------------


@pytest.fixture
def api_client(harness, tmp_path_factory):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    fake = FakeGitHub()
    fake.repos.append(REPO)
    origin = make_origin(tmp_path_factory.mktemp("origin"))
    SecretStore(harness.data).put(TOKEN_SECRET, TOKEN)
    git = GitRunner(harness.data, protocol="file", remote_url=lambda name: origin.as_uri())
    return signed_in(harness, github_transport=fake.transport, git=git)


def test_runs_over_http(harness, api_client):
    repo_id = add(api_client).json()["id"]
    assert api_client.get("/api/repos").json()["repos"][0]["last_run"] is None
    assert api_client.get(f"/api/repos/{repo_id}/runs").json() == {"runs": []}
    c = harness.conn()
    rows = [
        ("run-old", "passed", 0, "2026-11-01T00:00:00.000000+00:00"),
        ("run-new", "failed", 1, "2026-11-02T00:00:00.000000+00:00"),
    ]
    for run_id, status, code, at in rows:
        c.execute(
            "INSERT INTO repo_runs (id, repo_id, chat_id, command, status, exit_code, "
            "duration_ms, output, created_at) VALUES (?, ?, 'c1', 'npm test', ?, ?, 1200, ?, ?)",
            (run_id, repo_id, status, code, f"output of {run_id}", at),
        )
    c.commit()
    c.close()
    last = api_client.get("/api/repos").json()["repos"][0]["last_run"]
    assert last == {
        "id": "run-new",
        "chat_id": "c1",
        "command": "npm test",
        "status": "failed",
        "exit_code": 1,
        "duration_ms": 1200,
        "created_at": "2026-11-02T00:00:00.000000+00:00",
    }
    listed = api_client.get(f"/api/repos/{repo_id}/runs").json()["runs"]
    assert [r["id"] for r in listed] == ["run-new", "run-old"]
    assert all("output" not in r for r in listed)
    one = api_client.get("/api/repo-runs/run-old").json()
    assert one["output"] == "output of run-old" and one["status"] == "passed"
    assert api_client.get("/api/repo-runs/nope").status_code == 404
    assert api_client.get("/api/repos/nope/runs").status_code == 404
    assert api_client.app.state.hk.registry.get("repo_test") is not None


def test_runs_need_a_session(harness):
    client = harness.start()
    assert client.get("/api/repos/r1/runs").status_code == 401
    assert client.get("/api/repo-runs/x").status_code == 401


def test_startup_sweeps_left_test_jobs(harness):
    left = harness.data / "runtime" / "repo-jobs" / "0123456789abcdef"
    (left / "tree").mkdir(parents=True)
    (left / "tree" / "a.py").write_text("x")
    harness.start()
    assert not left.exists()


# -- fix round 1: runs that stop, and what the model is shown ------------------------------


async def test_a_cancelled_run_is_saved_and_cleaned_up(repo_fixture, fake_docker, monkeypatch):
    import asyncio

    f = repo_fixture
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    task = asyncio.ensure_future(f.runner.run(f.repo, "c1", "pytest -q"))
    pid = fake_docker.state / "run.pid"
    for _ in range(500):  # the container is up
        if pid.exists():
            break
        await asyncio.sleep(0.02)
    assert pid.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (run,) = f.runner.runs(f.repo.id)
    assert (run.status, run.exit_code, run.command) == ("error", None, "pytest -q")
    assert f.runner.get(run.id).output == RUN_STOPPED
    kills = [c for c in fake_docker.calls() if c[0] == "kill"]
    assert kills == [["kill", f"hk-repo-{run.id}"]]
    assert list((f.data / "runtime" / "repo-jobs").glob("*")) == []


async def test_a_broken_plan_is_an_error_run(repo_fixture, fake_docker, monkeypatch):
    f = repo_fixture

    def broken(repo, root):
        raise OSError("disk gone")

    monkeypatch.setattr(f.deps, "plan", broken)
    run = await f.runner.run(f.repo, "c1", "true")
    assert run.status == "error" and run.output == DEPS_UNREADABLE
    got = await f.tool.run({"repo": R, "command": "true"}, ctx(f.store))
    assert not got.ok and got.content == DEPS_UNREADABLE

    async def boom(repo, plan):
        raise RuntimeError("bug")

    monkeypatch.undo()
    npm_files(f.clone)
    monkeypatch.setattr(f.deps, "is_installed", boom)
    with pytest.raises(RuntimeError):
        await f.runner.run(f.repo, "c1", "npm test")
    newest = f.runner.get(f.runner.runs(f.repo.id)[0].id)
    assert newest.status == "error" and newest.output == RUN_BROKE
    assert runs_started(fake_docker) == []


async def test_too_large_deps_say_so(repo_fixture, fake_docker, monkeypatch):
    from hearthkeep.github import deps as deps_module

    f = repo_fixture
    npm_files(f.clone)
    monkeypatch.setattr(deps_module, "MAX_DEP_FILE_BYTES", 10)
    got = await f.tool.run({"repo": R}, ctx(f.store))
    assert not got.ok and got.content.endswith("is over 50 MB.") and DEPS_MISSING not in got.content
    run = await f.runner.run(f.repo, "c1", "npm test")
    assert run.status == "error" and run.output.endswith("is over 50 MB.")
    assert runs_started(fake_docker) == []


async def test_no_orphan_row_when_the_repo_went_mid_run(repo_fixture, fake_docker):
    f = repo_fixture
    gone = a_repo("feedfacecafe0001")  # a clone folder but no row, as after a removal
    folder = f.data / "workspace" / "repos" / gone.id
    folder.mkdir(parents=True)
    (folder / "a.py").write_text("x")
    run = await f.runner.run(gone, "c1", "true")
    assert run.status == "passed"
    assert f.runner.get(run.id) is None and f.runner.runs(gone.id) == []


async def test_the_model_sees_at_most_8000_characters(repo_fixture, fake_docker, monkeypatch):
    f = repo_fixture
    monkeypatch.setenv("FAKE_DOCKER_OUTPUT_BYTES", str(100_000))
    printed = bytes(65 + i % 26 for i in range(100_000)).decode("ascii")
    got = await f.tool.run({"repo": R, "command": "make test"}, ctx(f.store))
    assert got.ok
    status, _, shown = got.content.partition("\n")
    assert status.startswith("The tests passed (exit code 0")
    head, note, tail = shown.split("\n")
    assert head == printed[:MODEL_HEAD_CHARS] and tail == printed[-MODEL_TAIL_CHARS:]
    assert len(head) + len(tail) == MODEL_OUTPUT_CHARS == 8000
    assert note == "… 92000 characters cut; the full output is in the Repos view …"
    (run,) = f.runner.runs(f.repo.id)
    assert f.runner.get(run.id).output == printed  # the row keeps all of it
    assert for_model("short") == "short"
