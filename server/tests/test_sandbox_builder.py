"""The sandbox build job: both images in order, Docker checked first, output kept in the log
file only, one build at a time, and killed when the server stops. Never a real `docker build`."""

import asyncio
import threading
from pathlib import Path

import pytest

from hearthkeep.sandbox.builder import BuildBusy, SandboxBuilder

TARGETS = [("Sandbox image", "sb:1", Path("a")), ("Repo image", "repo:2", Path("b"))]


class FakeProc:
    def __init__(self, released: threading.Event):
        self.released, self.killed = released, False

    def kill(self):
        self.killed = True
        self.released.set()


class Fake:
    """A `build_image` stand-in: records each build, prints some lines, answers a code."""

    def __init__(self, *, lines=("one", "two"), codes=(0, 0), block=False):
        self.lines, self.codes, self.block = list(lines), list(codes), block
        self.calls: list[str] = []
        self.steps: list[str | None] = []
        self.kwargs: list[dict] = []
        self.entered = threading.Event()
        self.released = threading.Event()
        self.proc = FakeProc(self.released)
        self.builder: SandboxBuilder | None = None

    def __call__(self, tag, docker, image_dir, *, on_line=None, on_proc=None):
        self.calls.append(tag)
        self.kwargs.append({"docker": docker, "dir": image_dir})
        self.steps.append(self.builder.state()["step"])
        on_proc(self.proc)
        for line in self.lines:
            on_line(line)
        self.entered.set()
        if self.block:
            self.released.wait(5)
            return 1
        return self.codes[len(self.calls) - 1]


def make(tmp_path, fake=None, *, docker_ok=True, installed=True, events=None, refreshed=None):
    fake = fake or Fake()

    async def refresh():
        if refreshed is not None:
            refreshed.append(builder.state()["state"])

    builder = SandboxBuilder(
        tmp_path,
        docker=("docker-fake",),
        run_build=fake,
        record=(lambda who, event, detail: events.append((who, event, detail)))
        if events is not None
        else None,
        refresh=refresh,
        docker_ok=lambda docker: docker_ok,
        docker_installed=lambda: installed,
        targets=lambda: TARGETS,
    )
    fake.builder = builder
    return builder, fake


def log_lines(tmp_path):
    return (tmp_path / "logs" / "sandbox-build.log").read_text(encoding="utf-8").splitlines()


async def test_build_runs_both_images_in_order(tmp_path):
    events, refreshed = [], []
    builder, fake = make(tmp_path, events=events, refreshed=refreshed)
    assert builder.state() == {"state": "idle", "step": None, "reason": None}
    started = await builder.start("setup")
    assert started["state"] == "building"
    await builder.wait()
    assert fake.calls == ["sb:1", "repo:2"]
    assert fake.steps == ["sandbox", "repo"]
    assert [k["dir"] for k in fake.kwargs] == [Path("a"), Path("b")]
    assert all(k["docker"] == ("docker-fake",) for k in fake.kwargs)
    assert builder.state() == {"state": "done", "step": None, "reason": None}
    # The add-on is looked at again before "done" is shown.
    assert refreshed == ["building"]
    assert events == [
        ("setup", "sandbox.build_started", {}),
        ("setup", "sandbox.build_finished", {"ok": True, "reason": None}),
    ]


@pytest.mark.parametrize(
    ("installed", "reason"), [(False, "docker_missing"), (True, "docker_stopped")]
)
async def test_docker_missing_and_stopped_are_reported_before_building(tmp_path, installed, reason):
    events = []
    builder, fake = make(tmp_path, docker_ok=False, installed=installed, events=events)
    state = await builder.start()
    assert state == {"state": "failed", "step": None, "reason": reason}
    assert builder.state() == state
    assert fake.calls == []
    assert events == []  # nothing started, so nothing to audit
    assert not (tmp_path / "logs").exists()
    # And it can be tried again once Docker is there.
    ok_builder, _ = make(tmp_path)
    assert (await ok_builder.start())["state"] == "building"
    await ok_builder.wait()


async def test_failed_build_keeps_40_lines_in_the_log_only(tmp_path):
    secret_looking = [f"line {n} D:/data/x" for n in range(100)]
    events = []
    builder, fake = make(tmp_path, Fake(lines=secret_looking, codes=(1, 0)), events=events)
    await builder.start()
    await builder.wait()
    state = builder.state()
    assert state == {"state": "failed", "step": None, "reason": "build_failed"}
    assert fake.calls == ["sb:1"]  # the second image isn't tried
    assert log_lines(tmp_path) == secret_looking[-40:]
    # Nowhere else: not in the state, not in the audit rows.
    assert "line" not in str(state)
    assert "line" not in str(events) and "data" not in str(events)
    assert events[-1] == (
        "system",
        "sandbox.build_finished",
        {"ok": False, "reason": "build_failed"},
    )


async def test_a_build_that_raises_is_a_failed_build(tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("D:/private/path")

    builder, _ = make(tmp_path, boom)
    await builder.start()
    await builder.wait()
    assert builder.state() == {"state": "failed", "step": None, "reason": "build_failed"}


async def test_second_start_is_busy(tmp_path):
    builder, fake = make(tmp_path, Fake(block=True))
    await builder.start()
    assert await asyncio.to_thread(fake.entered.wait, 5)
    with pytest.raises(BuildBusy):
        await builder.start()
    assert builder.state()["state"] == "building"
    await builder.shutdown()


async def test_shutdown_kills_the_build(tmp_path):
    events = []
    builder, fake = make(tmp_path, Fake(block=True), events=events)
    await builder.start()
    assert await asyncio.to_thread(fake.entered.wait, 5)
    await asyncio.wait_for(builder.shutdown(), 5)
    assert fake.proc.killed
    assert fake.calls == ["sb:1"]  # the repo image is not started
    assert builder.state()["state"] == "failed"
    assert events[-1][1] == "sandbox.build_finished"


async def test_shutdown_with_nothing_running_is_fine(tmp_path):
    builder, _ = make(tmp_path)
    await builder.shutdown()
    assert builder.state()["state"] == "idle"


async def test_a_finished_build_can_be_started_again(tmp_path):
    builder, fake = make(tmp_path, Fake(codes=(1, 0, 0, 0)))
    await builder.start()
    await builder.wait()
    assert builder.state()["state"] == "failed"
    await builder.start()
    await builder.wait()
    assert builder.state()["state"] == "done"
    assert fake.calls == ["sb:1", "sb:1", "repo:2"]


def factory(fake, *, docker_ok=True, installed=True):
    """A `sandbox_builder_factory` for create_app that builds on `fake`, never on Docker."""

    def make_builder(data, **kw):
        builder = SandboxBuilder(
            data,
            run_build=fake,
            docker_ok=lambda docker: docker_ok,
            docker_installed=lambda: installed,
            targets=lambda: TARGETS,
            **kw,
        )
        fake.builder = builder
        return builder

    return make_builder


# ---- fix round 1 ----------------------------------------------------------------------------


async def test_shutdown_gives_up_when_the_build_will_not_end(tmp_path, caplog):
    """A grandchild (buildx) can keep the pipe open after the kill: shutdown is bounded."""
    import time

    stuck = threading.Event()

    def never_ends(tag, docker, image_dir, *, on_line=None, on_proc=None):
        on_proc(FakeProc(threading.Event()))
        stuck.wait(5)  # ignores the kill
        return 1

    builder = SandboxBuilder(
        tmp_path,
        run_build=never_ends,
        docker_ok=lambda d: True,
        docker_installed=lambda: True,
        targets=lambda: TARGETS,
        stop_timeout=0.2,
    )
    await builder.start()
    await asyncio.sleep(0.05)
    began = time.monotonic()
    await builder.shutdown()
    assert time.monotonic() - began < 2
    assert any("TimeoutError" in r.getMessage() for r in caplog.records)
    stuck.set()
    await builder.wait()


async def test_a_docker_check_that_raises_is_docker_missing_not_stuck(tmp_path):
    builder, fake = make(tmp_path)

    def boom(docker):
        raise RuntimeError("D:/private")

    builder._docker_ok = boom
    state = await builder.start()
    assert state == {"state": "failed", "step": None, "reason": "docker_missing"}
    assert fake.calls == []


async def test_a_stop_during_the_docker_check_builds_nothing(tmp_path):
    builder, fake = make(tmp_path)
    real = builder._docker_ok

    def stopping(docker):
        builder._stopping = True  # the server is stopping while Docker is looked at
        return real(docker)

    builder._docker_ok = stopping
    state = await builder.start()
    assert state["state"] == "failed" and fake.calls == []


async def test_busy_is_true_only_while_images_build(tmp_path):
    seen = []
    builder, fake = make(tmp_path)
    original = builder._refresh

    async def watch():
        seen.append(builder.busy())  # the add-on check runs after the build, not during it
        await original()

    builder._refresh = watch
    assert builder.busy() is False
    await builder.start()
    assert builder.busy() is True
    await builder.wait()
    assert seen == [False] and builder.busy() is False
