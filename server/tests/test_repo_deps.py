"""Repo dependencies: detected uv → pip → npm, installed once per lock file into a volume by a
locked-down container that has the network but only the dependency files, and asked about
again (Outward) whenever the lock file changes."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from secrets import token_hex

import pytest
from fakes import FakeGitHub
from test_git import REPO, make_origin, make_store

from hearthkeep.db import connect
from hearthkeep.github.deps import (
    INSTALL_TIMEOUT_S,
    OUTPUT_CUT,
    DepsCache,
    cap_output,
)
from hearthkeep.github.repos import Repo
from hearthkeep.permissions import ActionClass
from hearthkeep.sandbox import image as sandbox_image
from hearthkeep.sandbox.runner import NO_DOCKER_MESSAGE, NO_IMAGE_MESSAGE
from hearthkeep.tools.base import ToolContext
from hearthkeep.tools.repo import UNKNOWN_REPO
from hearthkeep.tools.repo_deps import (
    ALREADY_INSTALLED,
    CHANGED_AFTER_APPROVAL,
    NETWORK_NOTE,
    NO_LONGER_INSTALLED,
    RepoInstallTool,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_docker.py"
TAG = "hearthkeep-repo:0123456789ab"
R = "acme/widgets"
LOCK = '{"name": "w", "lockfileVersion": 3, "packages": {}}'
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


class FakeDocker:
    def __init__(self, state: Path) -> None:
        self.state = state
        self.argv = (sys.executable, str(FAKE))

    def calls(self) -> list[list[str]]:
        log = self.state / "log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def volumes(self) -> list[str]:
        path = self.state / "volumes.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    state = tmp_path / "fake-docker"
    state.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(state / "log.jsonl"))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_MODE", "print")
    monkeypatch.setenv("FAKE_DOCKER_OUTPUT", "added 0 packages\n")
    return FakeDocker(state)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "hk.db")
    yield c
    c.close()


@pytest.fixture
def cache(conn, fake_docker):
    return DepsCache(conn, fake_docker.argv, TAG)


def a_repo(repo_id="0123456789abcdef") -> Repo:
    return Repo(repo_id, R, "main", True, "2026-11-03T00:00:00Z")


def npm_files(root: Path, lock: str = LOCK) -> None:
    (root / "package.json").write_text('{"name": "w", "version": "1.0.0"}')
    (root / "package-lock.json").write_text(lock)


def test_plan_prefers_uv_then_pip_then_npm(tmp_path, conn):
    cache = DepsCache(conn, ("docker",), TAG)
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    assert cache.plan(repo, root) is None
    (root / "uv.lock").write_text("version = 1\n")  # no pyproject.toml: not a uv project
    assert cache.plan(repo, root) is None

    npm_files(root)
    plan = cache.plan(repo, root)
    assert plan.ecosystem == "npm" and plan.command == "npm ci"
    assert plan.files == ["package.json", "package-lock.json"]

    (root / "requirements.txt").write_text("requests==2.32.3\n")
    plan = cache.plan(repo, root)
    assert plan.ecosystem == "pip" and plan.files == ["requirements.txt"]
    assert plan.command == (
        "python -m venv /deps/venv && /deps/venv/bin/pip install -r requirements.txt"
    )
    (root / "requirements-dev.txt").write_text("pytest==8.3.3\n")
    plan = cache.plan(repo, root)
    assert plan.files == ["requirements.txt", "requirements-dev.txt"]
    assert plan.command.endswith("pip install -r requirements.txt -r requirements-dev.txt")

    (root / "pyproject.toml").write_text('[project]\nname = "w"\nversion = "1"\n')
    plan = cache.plan(repo, root)
    assert plan.ecosystem == "uv" and plan.files == ["pyproject.toml", "uv.lock"]
    assert plan.command == "uv sync --frozen --no-install-project"
    assert plan.volume == f"hk-deps-{repo.id}-{plan.lock_hash[:12]}"
    assert len(plan.lock_hash) == 64


def test_lock_hash_changes_with_file_bytes_and_image(tmp_path, conn):
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    cache = DepsCache(conn, ("docker",), TAG)
    first = cache.plan(repo, root)
    assert cache.plan(repo, root) == first  # stable
    npm_files(root, lock=LOCK + "\n")
    changed = cache.plan(repo, root)
    assert changed.lock_hash != first.lock_hash and changed.volume != first.volume
    npm_files(root)
    assert cache.plan(repo, root) == first
    other_image = DepsCache(conn, ("docker",), "hearthkeep-repo:ba9876543210").plan(repo, root)
    assert other_image.lock_hash != first.lock_hash
    other = a_repo("fedcba9876543210")
    assert cache.plan(other, root).lock_hash == first.lock_hash  # repo id: only in the volume
    assert cache.plan(other, root).volume != first.volume


def test_install_argv_has_network_but_no_repo_mount(cache, tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = cache.plan(a_repo(), root)
    argv = cache.install_argv(plan, tmp_path)
    assert "--network" not in argv or argv[argv.index("--network") + 1] != "none"
    assert not any("target=/repo" in a and "node_modules" not in a for a in argv)
    assert "--cap-drop" in argv and "--read-only" in argv
    joined = " ".join(argv)
    for flag in (
        "run --rm --pull never --log-driver none --read-only --user 10001:10001 --cap-drop ALL "
        "--security-opt no-new-privileges --memory 4g --memory-swap 4g --cpus 2 "
        "--pids-limit 1024 --tmpfs /tmp:rw,nosuid,nodev,size=1073741824",
        "--env HOME=/tmp",
        "--env CI=1",
        "--workdir /src",
    ):
        assert flag in joined
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    src = str(tmp_path.resolve())
    assert mounts == [
        f"type=bind,source={Path(src, 'package.json')},target=/src/package.json,readonly",
        f"type=bind,source={Path(src, 'package-lock.json')},target=/src/package-lock.json,readonly",
        f"type=volume,source={plan.volume},target=/src/node_modules",
    ]
    assert argv[-4:] == [TAG, "sh", "-c", "npm ci"]
    assert not any("TOKEN" in a.upper() or "GITHUB" in a.upper() for a in argv)


def test_install_argv_for_python_mounts_deps_and_sets_uv_env(cache, tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "uv.lock").write_text("version = 1\n")
    plan = cache.plan(a_repo(), root)
    argv = cache.install_argv(plan, tmp_path)
    assert f"type=volume,source={plan.volume},target=/deps" in argv
    assert "UV_PROJECT_ENVIRONMENT=/deps/venv" in argv
    assert argv[-1] == "uv sync --frozen --no-install-project"


def test_output_keeps_head_and_tail():
    small = b"x" * 100
    assert cap_output(small) == "x" * 100
    big = b"h" * 64 * 1024 + b"m" * 500_000 + b"t" * 192 * 1024
    out = cap_output(big)
    assert out.startswith("h" * 64 * 1024 + "\n" + OUTPUT_CUT + "\n")
    assert out.endswith("t" * 192 * 1024) and "m" not in out


def test_installed_needs_row_and_volume_and_caches_the_check(cache, conn, fake_docker, tmp_path):
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = cache.plan(repo, root)
    assert not cache.installed(repo, plan)
    assert fake_docker.calls() == []  # no row: docker is never asked
    conn.execute(
        "INSERT INTO repo_deps VALUES (?, ?, 'npm', ?, 'x')", (repo.id, plan.lock_hash, plan.volume)
    )
    assert not cache.installed(repo, plan)  # the volume is gone
    (fake_docker.state / "volumes.json").write_text(json.dumps([plan.volume]))
    assert not cache.installed(repo, plan)  # cached for 30 s
    cache._volumes.clear()
    assert cache.installed(repo, plan)
    inspects = [c for c in fake_docker.calls() if c[:2] == ["volume", "inspect"]]
    assert len(inspects) == 2


async def test_install_records_row_and_removes_older_volumes(cache, conn, fake_docker, tmp_path):
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    old = cache.plan(repo, root)
    job = tmp_path / "job1"
    cache.stage(old, root, job)
    assert await cache.install(repo, old, job) == (True, "added 0 packages\n")
    assert cache.installed(repo, old)
    npm_files(root, lock=LOCK + " ")
    new = cache.plan(repo, root)
    job = tmp_path / "job2"
    cache.stage(new, root, job)
    ok, _ = await cache.install(repo, new, job)
    assert ok
    assert fake_docker.volumes() == [new.volume]
    rows = conn.execute("SELECT lock_hash, volume, ecosystem FROM repo_deps").fetchall()
    assert [tuple(r) for r in rows] == [(new.lock_hash, new.volume, "npm")]
    assert cache.state(repo, root) == "ready"


async def test_failed_install_keeps_no_row_or_volume(
    cache, conn, fake_docker, tmp_path, monkeypatch
):
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = cache.plan(repo, root)
    cache.stage(plan, root, tmp_path / "job")
    monkeypatch.setenv("FAKE_DOCKER_EXIT", "1")
    monkeypatch.setenv("FAKE_DOCKER_OUTPUT", "npm ERR! nope\n")
    ok, out = await cache.install(repo, plan, tmp_path / "job")
    assert not ok and "npm ERR! nope" in out
    assert conn.execute("SELECT * FROM repo_deps").fetchall() == []
    assert fake_docker.volumes() == []
    assert cache.state(repo, root) == "needed"


async def test_install_output_is_capped(cache, fake_docker, tmp_path, monkeypatch):
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = cache.plan(repo, root)
    cache.stage(plan, root, tmp_path / "job")
    monkeypatch.setenv("FAKE_DOCKER_OUTPUT_BYTES", str(1024 * 1024))
    ok, out = await cache.install(repo, plan, tmp_path / "job")
    assert ok and OUTPUT_CUT in out
    assert len(out) == 64 * 1024 + 192 * 1024 + len(OUTPUT_CUT) + 2


async def test_hanging_install_is_killed_and_leaves_nothing(
    cache, conn, fake_docker, tmp_path, monkeypatch
):
    from hearthkeep.github import deps

    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = cache.plan(repo, root)
    cache.stage(plan, root, tmp_path / "job")
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    monkeypatch.setattr(deps, "INSTALL_TIMEOUT_S", 1)
    ok, out = await cache.install(repo, plan, tmp_path / "job")
    assert not ok and out.startswith(deps.TIMED_OUT)
    kills = [c for c in fake_docker.calls() if c[0] == "kill"]
    assert len(kills) == 1 and kills[0][1].startswith("hk-install-")
    assert conn.execute("SELECT * FROM repo_deps").fetchall() == []
    assert fake_docker.volumes() == []


def test_stage_refuses_files_changed_since_the_plan(cache, tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = cache.plan(a_repo(), root)
    npm_files(root, lock="{}")
    with pytest.raises(ValueError):
        cache.stage(plan, root, tmp_path / "job")


def test_state_none_needed_ready_outdated(cache, conn, fake_docker, tmp_path):
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    assert cache.state(repo, root) == "none"
    npm_files(root)
    assert cache.state(repo, root) == "needed"
    plan = cache.plan(repo, root)
    conn.execute(
        "INSERT INTO repo_deps VALUES (?, ?, 'npm', ?, 'x')", (repo.id, plan.lock_hash, plan.volume)
    )
    (fake_docker.state / "volumes.json").write_text(json.dumps([plan.volume]))
    assert cache.state(repo, root) == "ready"
    npm_files(root, lock="{}")
    assert cache.state(repo, root) == "outdated"


def test_install_timeout_is_900_s():
    assert INSTALL_TIMEOUT_S == 900


# -- the tool, on a real clone ------------------------------------------------------------


@pytest.fixture
async def repo_fixture(tmp_path, fake_docker):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    data = tmp_path / "data"
    data.mkdir()
    fake = FakeGitHub()
    fake.routes[("GET", "/repos/acme/widgets")] = (200, REPO)
    store = make_store(data, make_origin(tmp_path), fake)
    repo = await store.add(R)
    deps = DepsCache(store.conn, fake_docker.argv, TAG)
    store.on_remove.append(deps.remove_for)
    tool = RepoInstallTool(store, deps, data)
    yield store, repo, deps, tool
    store.conn.close()


def ctx(store, detail=None, step_up=False):
    return ToolContext("c", "t", None, store.conn, approved_detail=detail, approved_step_up=step_up)


async def approve_and_run(tool, store, args):
    detail = await tool.approval_detail(args, ctx(store))
    return detail, await tool.run(args, ctx(store, {**detail, "arguments": args}, step_up=True))


async def test_lock_change_needs_new_approval(repo_fixture, fake_docker):
    store, repo, cache, tool = repo_fixture
    root = store.dir(repo)
    npm_files(root)
    args = {"repo": R}
    assert tool.classify(args) == ActionClass.OUTWARD
    unapproved = await tool.run(args, ctx(store))
    assert not unapproved.ok and unapproved.content == NO_LONGER_INSTALLED
    assert conn_rows(store) == []

    detail, got = await approve_and_run(tool, store, args)
    assert got.ok, got.content
    plan = cache.plan(repo, root)
    assert detail == {
        "repo": R,
        "command": "npm ci",
        "files": ["package.json", "package-lock.json"],
        "lock_hash": plan.lock_hash,
        "network": NETWORK_NOTE,
    }
    assert NETWORK_NOTE == (
        "This install has internet access. Only the dependency files are given to it."
    )
    assert tool.classify(args) == ActionClass.LOOK  # after a recorded install
    again = await tool.run(args, ctx(store))
    assert again.ok and again.content == ALREADY_INSTALLED
    assert ALREADY_INSTALLED == "Dependencies are already installed for this lock file."

    (root / "package-lock.json").write_text('{"changed": true}')
    assert tool.classify(args) == ActionClass.OUTWARD
    stale = await tool.run(args, ctx(store, {**detail, "arguments": args}, step_up=True))
    assert not stale.ok and stale.content == CHANGED_AFTER_APPROVAL
    runs = [c for c in fake_docker.calls() if c[0] == "run"]
    assert len(runs) == 1  # only the approved install ran a container


def conn_rows(store):
    return store.conn.execute("SELECT * FROM repo_deps").fetchall()


async def test_remove_repo_removes_its_volumes(repo_fixture, fake_docker):
    store, repo, cache, tool = repo_fixture
    npm_files(store.dir(repo))
    _, got = await approve_and_run(tool, store, {"repo": R})
    assert got.ok
    fake_docker.state.joinpath("volumes.json").write_text(
        json.dumps([*fake_docker.volumes(), "hk-deps-someoneelse-0123456789ab"])
    )
    assert any(v.startswith(f"hk-deps-{repo.id}-") for v in fake_docker.volumes())
    assert await store.remove(repo.id)
    assert fake_docker.volumes() == ["hk-deps-someoneelse-0123456789ab"]
    assert conn_rows(store) == []


async def test_remove_repo_works_without_docker(repo_fixture, monkeypatch):
    store, repo, cache, tool = repo_fixture
    cache.docker = ("hearthkeep-test-no-such-docker",)
    assert await store.remove(repo.id)


async def test_without_docker_or_image_nothing_runs_on_the_host(
    repo_fixture, fake_docker, monkeypatch
):
    store, repo, cache, tool = repo_fixture
    npm_files(store.dir(repo))
    args = {"repo": R}
    monkeypatch.setenv("FAKE_DOCKER_NO_IMAGE", "1")
    _, got = await approve_and_run(tool, store, args)
    assert not got.ok and got.content == NO_IMAGE_MESSAGE
    assert "hearthkeep sandbox build" in got.content
    monkeypatch.setenv("FAKE_DOCKER_NO_DAEMON", "1")
    _, got = await approve_and_run(tool, store, args)
    assert not got.ok and got.content == NO_DOCKER_MESSAGE
    assert not [c for c in fake_docker.calls() if c[0] == "run"]
    assert not (store.dir(repo) / "node_modules").exists()


async def test_unknown_repo_and_nothing_to_install(repo_fixture):
    store, repo, cache, tool = repo_fixture
    got = await tool.run({"repo": "acme/other"}, ctx(store))
    assert not got.ok and got.content == UNKNOWN_REPO
    assert tool.classify({"repo": "acme/other"}) == ActionClass.LOOK
    assert tool.classify({"repo": R}) == ActionClass.LOOK  # no dependency files
    got = await tool.run({"repo": R}, ctx(store))
    assert got.ok and "nothing to install" in got.content


def test_tool_shape():
    assert RepoInstallTool.name == "repo_install"
    assert RepoInstallTool.level_key == "github"
    assert RepoInstallTool.parameters["required"] == ["repo"]


# -- the real image ------------------------------------------------------------------------


@pytest.fixture
def repo_image():
    if not sandbox_image.docker_ok():
        pytest.skip("Docker is not running")
    tag = sandbox_image.image_tag(sandbox_image.REPO_IMAGE_DIR, sandbox_image.REPO_IMAGE_NAME)
    if not sandbox_image.image_present(tag):
        pytest.skip(f"repo image {tag} is not built (hearthkeep sandbox build --data <data>)")
    return tag


@pytest.mark.docker
@pytest.mark.parametrize("ecosystem", ["uv", "pip", "npm"])
async def test_real_install_in_the_repo_image(repo_image, conn, tmp_path, ecosystem):
    root = tmp_path / "clone"
    root.mkdir()
    if ecosystem == "uv":
        (root / "pyproject.toml").write_text(
            '[project]\nname = "w"\nversion = "1.0"\nrequires-python = ">=3.12"\n'
            "dependencies = []\n"
        )
        uv = shutil.which("uv")
        if uv is None or subprocess.run([uv, "lock", "--offline"], cwd=root).returncode != 0:
            pytest.skip("needs uv on the host to write a uv.lock")
    elif ecosystem == "pip":
        (root / "requirements.txt").write_text("# nothing\n")
    else:
        npm_files(root)
    cache = DepsCache(conn, ("docker",), repo_image)
    repo = a_repo(token_hex(8))
    plan = cache.plan(repo, root)
    assert plan.ecosystem == ecosystem
    cache.stage(plan, root, tmp_path / "job")
    try:
        ok, out = await cache.install(repo, plan, tmp_path / "job")
        assert ok, out
        assert cache.installed(repo, plan)
        npm = ecosystem == "npm"
        check = "ls -a /src/node_modules" if npm else "/deps/venv/bin/python -V"
        target = "/src/node_modules" if npm else "/deps"
        proc = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only", "--user",
             "10001:10001", "--mount", f"type=volume,source={plan.volume},target={target}",
             repo_image, "sh", "-c", check],
            capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        assert proc.returncode == 0, proc.stderr
    finally:
        await cache.remove_for(repo)


async def test_look_approval_never_installs_when_the_volume_went(repo_fixture, fake_docker):
    store, repo, cache, tool = repo_fixture
    npm_files(store.dir(repo))
    args = {"repo": R}
    _, got = await approve_and_run(tool, store, args)
    assert got.ok
    assert tool.classify(args) == ActionClass.LOOK  # so the card would be a plain one
    detail = await tool.approval_detail(args, ctx(store))
    fake_docker.state.joinpath("volumes.json").write_text("[]")  # docker volume prune
    cache._volumes.clear()  # (or the 30 s cache ran out while the card waited)
    runs_before = len([c for c in fake_docker.calls() if c[0] == "run"])
    got = await tool.run(args, ctx(store, {**detail, "arguments": args}, step_up=False))
    assert not got.ok and got.content == NO_LONGER_INSTALLED
    assert NO_LONGER_INSTALLED == (
        "The dependencies are no longer installed. "
        "Call repo_install again so the owner can approve the install."
    )
    assert len([c for c in fake_docker.calls() if c[0] == "run"]) == runs_before
    assert fake_docker.volumes() == []
    assert tool.classify(args) == ActionClass.OUTWARD  # the next call asks with a step-up


async def test_concurrent_installs_of_one_plan_start_one_container(cache, fake_docker, tmp_path):
    repo = a_repo()
    root = tmp_path / "clone"
    root.mkdir()
    npm_files(root)
    plan = cache.plan(repo, root)
    cache.stage(plan, root, tmp_path / "job1")
    cache.stage(plan, root, tmp_path / "job2")
    first, second = await asyncio.gather(
        cache.install(repo, plan, tmp_path / "job1"),
        cache.install(repo, plan, tmp_path / "job2"),
    )
    assert first[0] and second[0]
    assert ALREADY_INSTALLED in (first[1], second[1])  # the second waited, then found it
    assert len([c for c in fake_docker.calls() if c[0] == "run"]) == 1
