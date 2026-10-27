import asyncio
import io
import json
import logging
import os
import re
import sys
import tarfile
import time
import unicodedata
from pathlib import Path

import pytest

from hearthkeep.sandbox import runner as runner_mod
from hearthkeep.sandbox.runner import (
    RawRun,
    SandboxLimits,
    SandboxRunner,
    SandboxUnavailable,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_docker.py"
SEP = os.sep
TAG = "hearthkeep-sandbox:0123456789ab"
CHAT = "a" * 32
OTHER_CHAT = "b" * 32


def files_dir(data: Path, chat: str = CHAT) -> Path:
    return data / "workspace" / "chats" / chat / "files"


def option_values(argv: list[str], option: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == option]


def make_tar(
    members: list[tuple], *, status: dict | None = None, fmt: int = tarfile.USTAR_FORMAT
) -> bytes:
    """members: (name, bytes) for files, or (TarInfo, bytes|None) for anything odd."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tar:
        if status is not False:
            body = json.dumps(
                status or {"exit_code": 0, "timed_out": False, "stdout_truncated": False}
            ).encode()
            info = tarfile.TarInfo("status.json")
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        for name, data in members:
            if isinstance(name, tarfile.TarInfo):
                tar.addfile(name, io.BytesIO(data) if data is not None else None)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    state = tmp_path / "fake-state"
    log = tmp_path / "fake-docker.log"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    for name in (
        "FAKE_DOCKER_MODE",
        "FAKE_DOCKER_TAR",
        "FAKE_DOCKER_EXIT",
        "FAKE_DOCKER_PS",
        "FAKE_DOCKER_NO_DAEMON",
        "FAKE_DOCKER_NO_IMAGE",
        "FAKE_DOCKER_KILL_IGNORED",
        "FAKE_DOCKER_STDERR",
    ):
        monkeypatch.delenv(name, raising=False)

    class Fake:
        data = tmp_path / "data"

        def runner(self, **limits) -> SandboxRunner:
            files_dir(self.data).mkdir(parents=True, exist_ok=True)
            return SandboxRunner(
                self.data,
                image_tag=TAG,
                docker=(sys.executable, str(FAKE)),
                limits=SandboxLimits(**limits),
            )

        def emit(self, raw: bytes) -> None:
            path = tmp_path / "emit.tar"
            path.write_bytes(raw)
            monkeypatch.setenv("FAKE_DOCKER_MODE", "tar")
            monkeypatch.setenv("FAKE_DOCKER_TAR", str(path))

        def calls(self) -> list[list[str]]:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

        def job_listing(self):
            return json.loads((state / "job.json").read_text(encoding="utf-8"))

        def jobs_root(self) -> Path:
            return self.data / "runtime" / "sandbox" / "jobs"

    return Fake()


async def run_script(runner: SandboxRunner, run_id: str = "run1", **kwargs) -> RawRun:
    return await runner.run(
        run_id,
        mode="script",
        files_dir=files_dir(runner.data_dir),
        manifest={"files": []},
        script=kwargs.pop("script", "print('hi')\n"),
        **kwargs,
    )


# The command line


def test_argv_has_every_isolation_flag(tmp_path):
    runner = SandboxRunner(tmp_path, image_tag=TAG)
    job = tmp_path / "runtime" / "sandbox" / "jobs" / "r1" / "in"
    argv = runner.argv("r1", job, files_dir(tmp_path), "script")
    assert argv[:3] == ["docker", "run", "--rm"]
    joined = " ".join(argv)
    pairs = {
        "--network": "none",
        "--user": "10001:10001",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
        "--memory": "4g",
        "--memory-swap": "4g",
        "--cpus": "2",
        "--pids-limit": "256",
        "--tmpfs": "/tmp:rw,nosuid,nodev,noexec,size=268435456",
        "--name": "hk-run-r1",
        "--label": "hearthkeep.run=1",
        "--env": "HOME=/tmp",
        "--pull": "never",
        "--log-driver": "none",
    }
    for option, value in pairs.items():
        assert option_values(argv, option) == [value], option
    assert "--read-only" in argv
    mounts = option_values(argv, "--mount")
    assert "type=tmpfs,destination=/out,tmpfs-size=268435456" in mounts
    binds = [m for m in mounts if m.startswith("type=bind,")]
    assert len(binds) == 2
    data_mount = next(m for m in binds if "target=/data" in m)
    job_mount = next(m for m in binds if "target=/job" in m)
    assert data_mount.split(",")[-1] == "readonly"
    assert job_mount.split(",")[-1] == "readonly"
    assert argv[-2:] == [TAG, "script"]
    assert "--gpus" not in joined
    assert "--init" not in argv and "--privileged" not in argv
    assert "docker.sock" not in joined
    assert "-v" not in argv and "--volume" not in argv and "-i" not in argv
    # Every option above, and nothing more: only the three mounts and the fixed flags.
    assert len(argv) == 3 + 1 + 2 * len(pairs) + 2 * len(mounts) + 2


def test_files_mount_is_only_that_chats_files_dir(tmp_path):
    runner = SandboxRunner(tmp_path, image_tag=TAG)
    job = tmp_path / "runtime" / "sandbox" / "jobs" / "r1" / "in"
    argv = runner.argv("r1", job, files_dir(tmp_path), "script")
    data_mount = next(m for m in option_values(argv, "--mount") if "target=/data" in m)
    assert f"source={files_dir(tmp_path).resolve()}," in data_mount
    job_mount = next(m for m in option_values(argv, "--mount") if "target=/job" in m)
    assert f"source={job.resolve()}," in job_mount
    # The runner only ever mounts a chat's own files folder: not the data folder, not a
    # chat folder, not anything outside the chats.
    for bad in (
        tmp_path,
        tmp_path / "workspace" / "chats" / CHAT,
        tmp_path / "workspace" / "chats" / "not-a-chat" / "files",
        tmp_path / "elsewhere" / CHAT / "files",
        files_dir(tmp_path) / ".." / ".." / OTHER_CHAT / "files" / ".." / ".." / "..",
    ):
        with pytest.raises(ValueError):
            runner._files_dir(bad)
    # A chat with no files folder yet is an error: the runner never creates it.
    with pytest.raises(FileNotFoundError):
        runner._files_dir(files_dir(tmp_path, OTHER_CHAT))
    assert not files_dir(tmp_path, OTHER_CHAT).exists()
    files_dir(tmp_path, OTHER_CHAT).mkdir(parents=True)
    assert (
        runner._files_dir(files_dir(tmp_path, OTHER_CHAT))
        == files_dir(tmp_path, OTHER_CHAT).resolve()
    )


def test_argv_refuses_bad_ids_modes_and_mount_paths(tmp_path):
    runner = SandboxRunner(tmp_path, image_tag=TAG)
    job = tmp_path / "job"
    with pytest.raises(ValueError):
        runner.argv("../x", job, files_dir(tmp_path), "script")
    with pytest.raises(ValueError):
        runner.argv("r1", job, files_dir(tmp_path), "shell")
    with pytest.raises(ValueError):
        runner.argv("r1", tmp_path / "a,b", files_dir(tmp_path), "script")
    with pytest.raises(ValueError):
        SandboxRunner(tmp_path, image_tag="python:3.12")


# Runs against the fake docker CLI


async def test_ok_run_returns_stdout_results_and_files(fake):
    results = b'{"type": "value", "name": "total", "value": 3, "kind": "computed"}\n'
    fake.emit(
        make_tar(
            [
                ("stdout.txt", b"hello\n"),
                ("results.jsonl", results),
                ("files/summary table.csv", b"a,b\n1,2\n"),
                ("pages/1.png", b"\x89PNG"),
            ]
        )
    )
    runner = fake.runner()
    raw = await run_script(runner, script="print('hello')\n")
    try:
        assert raw.status == "ok"
        assert raw.exit_code == 0
        assert raw.stdout == "hello\n"
        assert raw.stdout_truncated is False
        assert raw.error is None
        assert raw.duration_ms >= 0
        assert set(raw.members) == {
            "status.json",
            "stdout.txt",
            "results.jsonl",
            "files/summary table.csv",
            "pages/1.png",
        }
        assert raw.members["results.jsonl"].read_bytes() == results
        assert raw.members["files/summary table.csv"].read_bytes() == b"a,b\n1,2\n"
        job = fake.jobs_root() / "run1"
        for path in raw.members.values():
            assert path.parent == job / "out"
        # Only the member copies are left: the input folder and the tar are gone.
        assert sorted(p.name for p in job.iterdir()) == ["out"]
    finally:
        raw.cleanup()
    assert not (fake.jobs_root() / "run1").exists()
    assert raw.members == {}
    raw.cleanup()  # twice is fine


async def test_job_folder_holds_only_main_and_manifest(fake):
    fake.emit(make_tar([("stdout.txt", b"")]))
    runner = fake.runner(timeout_s=120.0, script_timeout_s=110.0)
    raw = await run_script(runner, timeout_s=20.0)
    raw.cleanup()
    assert fake.job_listing() == ["main.py", "manifest.json"]
    run = next(c for c in fake.calls() if c[0] == "run")
    job_mount = next(a for a in run if "target=/job" in a)
    assert "/in," in job_mount.replace("\\", "/")


async def test_manifest_and_script_are_written_for_the_container(fake, monkeypatch):
    seen = {}
    real_argv = SandboxRunner.argv

    def spy(self, run_id, job_dir, files, mode):
        seen["main"] = (job_dir / "main.py").read_text(encoding="utf-8")
        seen["manifest"] = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        return real_argv(self, run_id, job_dir, files, mode)

    monkeypatch.setattr(SandboxRunner, "argv", spy)
    fake.emit(make_tar([]))
    runner = fake.runner()
    raw = await runner.run(
        "run1",
        mode="script",
        files_dir=files_dir(fake.data),
        manifest={"files": [{"id": "f1", "name": "a.csv", "kind": "csv", "path": "/data/f1.csv"}]},
        script="x = 1\n",
    )
    raw.cleanup()
    assert seen["main"] == "x = 1\n"
    assert seen["manifest"]["files"][0]["path"] == "/data/f1.csv"
    assert seen["manifest"]["script_timeout_s"] == 110


async def test_container_output_over_cap_is_killed(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_MODE", "endless")
    runner = fake.runner(tar_bytes=1024 * 1024)
    started = time.monotonic()
    raw = await run_script(runner, timeout_s=30.0)
    assert raw.status == "too_much_output"
    assert raw.members == {}
    assert raw.error
    assert time.monotonic() - started < 20
    assert ["kill", "hk-run-run1"] in fake.calls()
    assert not (fake.jobs_root() / "run1").exists()


async def test_host_timeout_kills_container(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    runner = fake.runner()
    started = time.monotonic()
    raw = await run_script(runner, timeout_s=0.5)
    assert raw.status == "timeout"
    assert raw.exit_code is None
    assert time.monotonic() - started < 15
    assert ["kill", "hk-run-run1"] in fake.calls()
    assert not (fake.jobs_root() / "run1").exists()


async def test_client_that_outlives_docker_kill_is_killed(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    monkeypatch.setenv("FAKE_DOCKER_KILL_IGNORED", "1")
    monkeypatch.setattr(runner_mod, "KILL_GRACE_S", 0.5)
    runner = fake.runner()
    started = time.monotonic()
    raw = await run_script(runner, timeout_s=0.5)
    assert raw.status == "timeout"
    assert ["kill", "hk-run-run1"] in fake.calls()
    assert time.monotonic() - started < 15


async def test_host_timeout_overrides_the_containers_status(fake, monkeypatch):
    # Even a tar claiming success counts for nothing once the host's limit has passed.
    fake.emit(make_tar([("stdout.txt", b"ok")]))
    real = SandboxRunner._collect

    async def slow(self, proc, tar_path, timeout):
        await real(self, proc, tar_path, timeout)
        return "timeout"

    monkeypatch.setattr(SandboxRunner, "_collect", slow)
    raw = await run_script(fake.runner(), timeout_s=5.0)
    assert raw.status == "timeout"
    assert raw.members == {}


async def test_duration_is_the_hosts_own(fake):
    fake.emit(
        make_tar(
            [],
            status={
                "exit_code": 0,
                "timed_out": False,
                "stdout_truncated": False,
                "duration_ms": 999_999_999,
            },
        )
    )
    raw = await run_script(fake.runner())
    raw.cleanup()
    assert raw.status == "ok"
    assert raw.duration_ms < 60_000


async def test_entry_status_decides_failed_and_timeout(fake):
    fake.emit(
        make_tar(
            [("stdout.txt", b"Traceback\n")],
            status={"exit_code": 1, "timed_out": False, "stdout_truncated": False},
        )
    )
    raw = await run_script(fake.runner())
    raw.cleanup()
    assert (raw.status, raw.exit_code, raw.stdout) == ("failed", 1, "Traceback\n")

    fake.emit(
        make_tar(
            [("stdout.txt", b"x" * 10)],
            status={"exit_code": None, "timed_out": True, "stdout_truncated": True},
        )
    )
    raw = await run_script(fake.runner(), run_id="run2")
    raw.cleanup()
    assert (raw.status, raw.exit_code, raw.stdout_truncated) == ("timeout", None, True)


async def test_stdout_is_cut_to_its_limit(fake):
    fake.emit(make_tar([("stdout.txt", b"\xff" * 5000)]))
    raw = await run_script(fake.runner(stdout_bytes=1000))
    raw.cleanup()
    assert raw.stdout_truncated is True
    assert len(raw.stdout.encode("utf-8")) <= 1000


def _info(name: str, kind: bytes = tarfile.REGTYPE, size: int = 0, **extra) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size
    for key, value in extra.items():
        setattr(info, key, value)
    return info


BAD_TARS = {
    "dotdot": lambda: make_tar([("../evil", b"x")]),
    "dotdot_inside": lambda: make_tar([("files/a..csv", b"x")]),
    "absolute": lambda: make_tar([("/abs", b"x")]),
    "symlink": lambda: make_tar(
        [(_info("files/link", tarfile.SYMTYPE, linkname="/etc/passwd"), None)]
    ),
    "hardlink": lambda: make_tar(
        [(_info("files/hard", tarfile.LNKTYPE, linkname="status.json"), None)]
    ),
    "directory": lambda: make_tar([(_info("files", tarfile.DIRTYPE), None)]),
    "fifo": lambda: make_tar([(_info("files/pipe", tarfile.FIFOTYPE), None)]),
    "nested": lambda: make_tar([("files/a/b", b"x")]),
    "backslash": lambda: make_tar([("files\\evil", b"x")]),
    "control": lambda: make_tar([("files/a\x01b", b"x")]),
    "nul_in_pax_path": lambda: make_tar(
        [(_info("files/ab", size=1, pax_headers={"path": "files/a\x00b"}), b"x")],
        fmt=tarfile.PAX_FORMAT,
    ),
    "pax_extra_key": lambda: make_tar(
        [(_info("files/ab", size=1, pax_headers={"linkpath": "/etc"}), b"x")],
        fmt=tarfile.PAX_FORMAT,
    ),
    "pax_sparse": lambda: make_tar(
        [(_info("files/ab", size=1, pax_headers={"GNU.sparse.major": "1"}), b"x")],
        fmt=tarfile.PAX_FORMAT,
    ),
    "unknown_top_level": lambda: make_tar([("evil.txt", b"x")]),
    "unicode_digits": lambda: make_tar([("pages/\u0663.png", b"x")]),
    "long_name": lambda: make_tar([("files/" + "a" * 101, b"x")], fmt=tarfile.PAX_FORMAT),
    "leading_dot": lambda: make_tar([("files/.hidden", b"x")]),
    "trailing_newline": lambda: make_tar([("stdout.txt\n", b"x")]),
    "duplicate": lambda: make_tar([("stdout.txt", b"a"), ("stdout.txt", b"b")]),
    "too_many": lambda: make_tar([(f"files/f{i}.csv", b"x") for i in range(200)]),
    "status_not_first": lambda: make_tar(
        [("stdout.txt", b"x"), ("status.json", b"{}")], status=False
    ),
    "no_status": lambda: make_tar([("stdout.txt", b"x")], status=False),
    "bad_status": lambda: make_tar([], status={"exit_code": "0"}),
    "not_utf8_name": lambda: _binary_name_tar(),
    "truncated": lambda: make_tar([("stdout.txt", b"x" * 5000)])[:2000],
    "junk_after_end": lambda: make_tar([("stdout.txt", b"x")]) + b"junk" * 300,
    "empty": lambda: b"",
    "gzip": lambda: _gzip_tar(),
    # Header-level rules of Hearthkeep's own reader (each header built by hand).
    "gnu_magic": lambda: _ustar_tar(_ustar(b"files/a", 1, magic=b"ustar ", version=b" \0")),
    "bad_version": lambda: _ustar_tar(_ustar(b"files/a", 1, version=b"01")),
    "bad_checksum": lambda: _ustar_tar(_ustar(b"files/a", 1, checksum=1)),
    "checksum_not_octal": lambda: _ustar_tar(_ustar(b"files/a", 1, checksum_field=b"12345x\0 ")),
    "size_not_octal": lambda: _ustar_tar(_ustar(b"files/a", 1, size_field=b"0000000000a\0")),
    "size_digit_8": lambda: _ustar_tar(_ustar(b"files/a", 1, size_field=b"00000000008\0")),
    "size_base256": lambda: _ustar_tar(
        _ustar(b"files/a", 1, size_field=b"\x80" + b"\0" * 10 + b"\x01")
    ),
    "size_beyond_the_stream": lambda: _ustar_tar(_ustar(b"files/a", 1 << 30), data=b"x"),
    "name_junk_after_nul": lambda: _ustar_tar(_ustar(b"files/a\0junk", 1)),
    "non_ascii_name": lambda: _ustar_tar(_ustar(b"files/a\xffb", 1)),
    "prefix_escapes": lambda: _ustar_tar(_ustar(b"evil", 1, prefix=b"files/..")),
    "padding_not_zero": lambda: _ustar_tar(_ustar(b"files/a", 1), data=b"x" + b"p" * 511),
    "second_end_block_not_zero": lambda: _status_only() + bytes(512) + b"z" * 512,
    "one_end_block_only": lambda: _status_only() + bytes(512),
    "contiguous_type_7": lambda: _ustar_tar(_ustar(b"files/a", 1, kind=b"7")),
    **{
        f"type_{chr(kind[0]) if kind[0] > 32 else kind[0]}": (
            lambda kind=kind: _ustar_tar(_ustar(b"files/a", 0, kind=kind), data=b"")
        )
        for kind in (b"1", b"2", b"3", b"4", b"5", b"6", b"x", b"g", b"X", b"L", b"K", b"S")
    },
}


def _binary_name_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, encoding="utf-8", errors="surrogateescape"
    ) as tar:
        body = b'{"exit_code": 0, "timed_out": false, "stdout_truncated": false}'
        info = tarfile.TarInfo("status.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
        info = tarfile.TarInfo("files/a\udcff.csv")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    raw = buf.getvalue()
    assert b"hdrcharset=BINARY" in raw
    return raw


def _gzip_tar() -> bytes:
    import gzip

    return gzip.compress(make_tar([]))


@pytest.mark.parametrize("case", sorted(BAD_TARS))
async def test_tar_with_bad_members_rejected(fake, case):
    fake.emit(BAD_TARS[case]())
    raw = await run_script(fake.runner(), run_id=f"r-{case}")
    assert raw.status == "sandbox_error", case
    assert raw.members == {}
    assert raw.error == "The sandbox returned output Hearthkeep couldn't read."
    assert raw.diagnostics
    # Nothing was written anywhere: not outside the job folder, and the job folder is gone.
    assert not (fake.jobs_root() / f"r-{case}").exists()
    written = [p for p in fake.data.rglob("*") if p.is_file()]
    assert written == []
    assert not any(p.name in ("evil", "abs", "link", "hard") for p in fake.data.parent.rglob("*"))


async def test_two_hundred_members_are_accepted(fake):
    fake.emit(make_tar([(f"files/f{i}.csv", b"x") for i in range(199)]))
    raw = await run_script(fake.runner())
    count = len(raw.members)
    raw.cleanup()
    assert raw.status == "ok"
    assert count == 200


START_ERRORS = {
    125: "The sandbox couldn't start the container.",
    126: "The sandbox couldn't run its entry program.",
    127: "The sandbox couldn't run its entry program.",
    3: "The sandbox stopped unexpectedly.",
}


@pytest.mark.parametrize("code", sorted(START_ERRORS))
async def test_docker_start_failure_is_sandbox_error(fake, monkeypatch, caplog, code):
    fake.emit(b"")
    home = str(Path.home())
    data = str(fake.data.resolve())
    stderr = f"docker: Error response from daemon: mount {data}{SEP}x failed; see {home}{SEP}y"
    monkeypatch.setenv("FAKE_DOCKER_EXIT", str(code))
    monkeypatch.setenv("FAKE_DOCKER_STDERR", stderr + "z" * 5000)
    with caplog.at_level(logging.WARNING, logger="hearthkeep.sandbox.runner"):
        raw = await run_script(fake.runner())
    assert raw.status == "sandbox_error"
    # Docker's own words never reach the run's error (which the model sees) ...
    assert raw.error == START_ERRORS[code]
    # ... only the diagnostics, cut to 2 KB with the owner's folders replaced.
    assert "docker: Error response from daemon: mount <data>" in raw.diagnostics
    assert "<home>" in raw.diagnostics
    assert data not in raw.diagnostics and home not in raw.diagnostics
    assert len(raw.diagnostics.encode("utf-8")) <= 2048
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Error response from daemon" in m and data not in m for m in logged)
    assert not (fake.jobs_root() / "run1").exists()


async def test_out_of_memory_exit_is_failed(fake, monkeypatch):
    fake.emit(b"")
    monkeypatch.setenv("FAKE_DOCKER_EXIT", "137")
    raw = await run_script(fake.runner())
    assert raw.status == "failed"
    assert raw.error == "The script used more than 4 GB of memory and was stopped."
    assert not any(c[0] == "kill" for c in fake.calls())


async def test_exit_137_after_the_host_killed_it_is_sandbox_error(fake, monkeypatch):
    # The client closes its stdout but doesn't exit: the host kills the container, and the
    # 137 that follows is the host's own doing, not the script running out of memory.
    fake.emit(make_tar([]))
    monkeypatch.setenv("FAKE_DOCKER_MODE", "tar_then_hang")
    monkeypatch.setattr(runner_mod, "EXIT_GRACE_S", 0.5)
    raw = await run_script(fake.runner())
    assert ["kill", "hk-run-run1"] in fake.calls()
    assert raw.status == "sandbox_error"
    assert raw.error == "The sandbox stopped unexpectedly."
    assert raw.members == {}
    assert not (fake.jobs_root() / "run1").exists()


def _pax_global_tar() -> bytes:
    buf = io.BytesIO()
    big = {"comment": "c" * 70_000}
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, pax_headers=big) as tar:
        info = tarfile.TarInfo("status.json")
        tar.addfile(info, io.BytesIO(b""))
    return buf.getvalue()


HOSTILE_HEADERS = {
    "pax_x": lambda: make_tar(
        [(_info("files/ab", size=1, pax_headers={"comment": "c" * 70_000}), b"x")],
        fmt=tarfile.PAX_FORMAT,
    ),
    "pax_g": _pax_global_tar,
    "gnu_longname": lambda: make_tar([("files/" + "a" * 70_000, b"x")], fmt=tarfile.GNU_FORMAT),
    "gnu_longlink": lambda: make_tar(
        [(_info("files/l", tarfile.SYMTYPE, linkname="a" * 70_000), None)],
        fmt=tarfile.GNU_FORMAT,
    ),
}


def test_status_json_size_is_checked_before_reading(tmp_path, monkeypatch):
    big = tmp_path / "status.json"
    big.write_bytes(b" " * 70_000)

    def no_read(self, *args, **kwargs):
        raise AssertionError("read before its size was checked")

    monkeypatch.setattr(Path, "read_bytes", no_read)
    monkeypatch.setattr(Path, "open", no_read)
    with pytest.raises(runner_mod._BadOutput):
        runner_mod._read_status(big)


async def test_data_folder_with_a_comma_is_unavailable(tmp_path, fake):
    data = tmp_path / "my,data"
    files_dir(data).mkdir(parents=True)
    runner = SandboxRunner(data, image_tag=TAG, docker=(sys.executable, str(FAKE)))
    with pytest.raises(SandboxUnavailable) as caught:
        await run_script(runner)
    assert caught.value.user_message == (
        "Hearthkeep's data folder path contains a comma or quote, which Docker can't mount. "
        "Move the data folder to a simpler path."
    )
    assert not any(c[0] == "run" for c in fake.calls())


async def test_sweep_refuses_while_a_run_is_active(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    runner = fake.runner()
    task = asyncio.create_task(run_script(runner, timeout_s=60.0))
    pid = fake.data.parent / "fake-state" / "run.pid"
    for _ in range(500):
        if pid.exists():
            break
        await asyncio.sleep(0.02)
    with pytest.raises(RuntimeError):
        await runner.sweep()
    assert (fake.jobs_root() / "run1").exists()  # the live run's folder was left alone
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await runner.sweep() == 0  # and once nothing runs, it works again


async def test_docker_missing_raises_unavailable(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_NO_DAEMON", "1")
    runner = fake.runner()
    with pytest.raises(SandboxUnavailable) as caught:
        await run_script(runner)
    assert "Docker" in caught.value.user_message
    assert not any(c[0] == "run" for c in fake.calls())
    assert not (fake.jobs_root() / "run1").exists()

    missing = SandboxRunner(fake.data, image_tag=TAG, docker=("hearthkeep-test-no-such-docker",))
    with pytest.raises(SandboxUnavailable):
        await run_script(missing, run_id="run2")


async def test_status_reports_image_missing(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_NO_IMAGE", "1")
    runner = fake.runner()
    state = await runner.status()
    assert (state.docker, state.image, state.tag) == (True, False, TAG)
    assert ["image", "inspect", TAG] in fake.calls()
    with pytest.raises(SandboxUnavailable) as caught:
        await run_script(runner)
    assert "hearthkeep sandbox build" in caught.value.user_message


async def test_status_is_cached_for_thirty_seconds(fake, monkeypatch):
    runner = fake.runner()
    clock = [1000.0]
    monkeypatch.setattr(runner_mod.time, "monotonic", lambda: clock[0])
    assert (await runner.status()).image is True
    monkeypatch.setenv("FAKE_DOCKER_NO_IMAGE", "1")
    clock[0] += 29
    assert (await runner.status()).image is True
    clock[0] += 2
    assert (await runner.status()).image is False


async def test_sweep_removes_leftover_containers(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_PS", "abc123 def456")
    leftover = fake.jobs_root() / "old-run"
    (leftover / "in").mkdir(parents=True)
    (leftover / "in" / "main.py").write_text("x", encoding="utf-8")
    assert await fake.runner().sweep() == 2
    calls = fake.calls()
    assert ["ps", "-aq", "--filter", "label=hearthkeep.run=1"] in calls
    assert ["rm", "-f", "abc123", "def456"] in calls
    assert not leftover.exists()


async def test_sweep_with_nothing_left(fake):
    assert await fake.runner().sweep() == 0
    assert not any(c[0] == "rm" for c in fake.calls())


async def test_cancelled_run_kills_container_and_removes_job(fake, monkeypatch):
    monkeypatch.setenv("FAKE_DOCKER_MODE", "sleep")
    runner = fake.runner()
    task = asyncio.create_task(run_script(runner, timeout_s=60.0))
    state = Path(fake.data.parent / "fake-state" / "run.pid")
    for _ in range(500):
        if state.exists():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ["kill", "hk-run-run1"] in fake.calls()
    assert not (fake.jobs_root() / "run1").exists()


async def test_failure_before_start_removes_job(fake, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(SandboxRunner, "argv", boom)
    with pytest.raises(RuntimeError):
        await run_script(fake.runner())
    assert not (fake.jobs_root() / "run1").exists()


async def test_run_id_used_once(fake):
    fake.emit(make_tar([]))
    runner = fake.runner()
    raw = await run_script(runner)
    with pytest.raises(FileExistsError):
        await run_script(runner)
    raw.cleanup()


# Against the real image, with the runner's own command line


def real_runner(data: Path, tag: str, **limits) -> SandboxRunner:
    files_dir(data).mkdir(parents=True, exist_ok=True)
    return SandboxRunner(data, image_tag=tag, limits=SandboxLimits(**limits))


async def real_script(data: Path, tag: str, script: str, **limits) -> RawRun:
    runner = real_runner(data, tag, **limits)
    return await runner.run(
        "real1", mode="script", files_dir=files_dir(data), manifest={"files": []}, script=script
    )


@pytest.mark.docker
async def test_real_run_has_no_network(tmp_path, sandbox_image):
    script = (
        "import socket\n"
        "s = socket.socket()\n"
        "s.settimeout(3)\n"
        "try:\n"
        "    s.connect(('1.1.1.1', 53))\n"
        "    print('connected')\n"
        "except OSError as exc:\n"
        "    print('blocked', type(exc).__name__)\n"
    )
    raw = await real_script(tmp_path, sandbox_image, script)
    raw.cleanup()
    assert raw.status == "ok", raw
    assert raw.stdout.startswith("blocked")


@pytest.mark.docker
async def test_real_run_cannot_write_data_or_root(tmp_path, sandbox_image):
    files = files_dir(tmp_path)
    files.mkdir(parents=True)
    (files / "f1.csv").write_text("a\n1\n", encoding="utf-8")
    script = (
        "for path in ('/data/x', '/data/f1.csv', '/etc/x', '/opt/hk/x', '/job/x', '/x'):\n"
        "    try:\n"
        "        open(path, 'a').write('x')\n"
        "        print(path, 'written')\n"
        "    except OSError:\n"
        "        print(path, 'refused')\n"
    )
    raw = await real_script(tmp_path, sandbox_image, script)
    raw.cleanup()
    assert raw.status == "ok", raw
    lines = raw.stdout.splitlines()
    assert len(lines) == 6
    assert all(line.endswith(" refused") for line in lines), lines
    assert (files / "f1.csv").read_text(encoding="utf-8") == "a\n1\n"
    assert sorted(p.name for p in files.iterdir()) == ["f1.csv"]


@pytest.mark.docker
async def test_real_run_is_not_root(tmp_path, sandbox_image):
    raw = await real_script(tmp_path, sandbox_image, "import os\nprint(os.getuid(), os.getgid())\n")
    raw.cleanup()
    assert raw.status == "ok", raw
    assert raw.stdout.split() == ["10001", "10001"]


@pytest.mark.docker
async def test_real_job_folder_holds_only_main_and_manifest(tmp_path, sandbox_image):
    script = "import os, sys\nprint(sorted(os.listdir('/job')))\nprint(sys.path[0])\n"
    raw = await real_script(tmp_path, sandbox_image, script)
    raw.cleanup()
    assert raw.status == "ok", raw
    assert raw.stdout.splitlines() == ["['main.py', 'manifest.json']", "/job"]


@pytest.mark.docker
async def test_runaway_print_is_cut_and_run_ends(tmp_path, sandbox_image):
    started = time.monotonic()
    raw = await real_script(
        tmp_path,
        sandbox_image,
        'while True:\n    print("x" * 1000)\n',
        timeout_s=15,
        script_timeout_s=5,
    )
    raw.cleanup()
    assert raw.status == "timeout"
    assert raw.stdout_truncated is True
    assert len(raw.stdout.encode()) <= 262144
    assert raw.stdout.startswith("x" * 1000)
    assert time.monotonic() - started < 15


@pytest.mark.docker
async def test_real_escaped_process_is_killed_by_the_entry(tmp_path, sandbox_image):
    # A child that leaves the script's session, keeps writing into /out and holds stdout:
    # the entry (pid 1, so no --init) must kill it when the script's time is up, and the run
    # must end with the entry's own status well before the host's limit. The child notes how
    # long it has been alive: with --init it would live on while the tar is written.
    script = (
        "import os, sys, time\n"
        "os.makedirs('/out/files', exist_ok=True)\n"
        "start = time.monotonic()\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    i = 0\n"
        "    while True:\n"
        "        with open(f'/out/files/junk{i % 20}.txt', 'w') as f:\n"
        "            f.write('j' * 100000)\n"
        "        with open('/out/files/alive.tmp', 'w') as f:\n"
        "            f.write(f'{time.monotonic() - start:.2f}')\n"
        "        os.replace('/out/files/alive.tmp', '/out/files/alive.txt')\n"
        "        sys.stdout.write('y')\n"
        "        sys.stdout.flush()\n"
        "        i += 1\n"
        "time.sleep(1000)\n"
    )
    runner = real_runner(tmp_path, sandbox_image, timeout_s=40, script_timeout_s=3)
    argv = runner.argv("real1", tmp_path / "job", files_dir(tmp_path), "script")
    assert "--init" not in argv
    started = time.monotonic()
    raw = await runner.run(
        "real1",
        mode="script",
        files_dir=files_dir(tmp_path),
        manifest={"files": []},
        script=script,
    )
    try:
        assert time.monotonic() - started < 25
        assert raw.status == "timeout", raw
        status = json.loads(raw.members["status.json"].read_text(encoding="utf-8"))
        assert status["timed_out"] is True and status["exit_code"] is None
        alive = float(raw.members["files/alive.txt"].read_text(encoding="ascii"))
        assert alive < 6  # killed with the script, not after the tar was written
    finally:
        raw.cleanup()


@pytest.mark.docker
async def test_real_inspect_mode(tmp_path, sandbox_image):
    files = files_dir(tmp_path)
    files.mkdir(parents=True)
    (files / "f1.csv").write_text("name,amount\na,1\nb,2\n", encoding="utf-8")
    runner = real_runner(tmp_path, sandbox_image)
    raw = await runner.run(
        "real1",
        mode="inspect",
        files_dir=files,
        manifest={
            "files": [{"id": "f1", "name": "a.csv", "kind": "csv", "path": "/data/f1.csv"}],
            "args": {"file": "f1"},
        },
    )
    try:
        assert raw.status == "ok", raw
        report = json.loads(raw.members["inspect.json"].read_text(encoding="utf-8"))
        assert isinstance(report, dict)
    finally:
        raw.cleanup()


# The app


@pytest.mark.parametrize("docker", [True, False])
def test_lifespan_sweeps_once_when_docker_is_available(tmp_path, monkeypatch, docker):
    from fakes import FakeChatClient, FakeVerifier
    from fastapi.testclient import TestClient

    from hearthkeep.api.app import create_app
    from hearthkeep.config import Settings
    from hearthkeep.tools.base import ToolRegistry

    swept = []

    async def sweep(self):
        swept.append(self.data_dir)
        return 0

    monkeypatch.setattr(SandboxRunner, "sweep", sweep)
    app = create_app(
        Settings(data_dir=tmp_path, docker_available=docker),
        verifier=FakeVerifier(),
        chat_client=FakeChatClient([]),
        registry=ToolRegistry(),
        web_dist=None,
    )
    with TestClient(app, base_url="http://localhost:8765"):
        runner = app.state.hk.sandbox.runner  # configured off: never made, never checked
    assert swept == ([tmp_path] if docker else [])
    assert (runner is not None) is docker


async def test_docker_cli_gets_no_window_and_no_stdin(fake, monkeypatch):
    import subprocess

    seen = []
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        seen.append(kwargs)
        return await real(*argv, **kwargs)

    monkeypatch.setattr(runner_mod.asyncio, "create_subprocess_exec", spy)
    fake.emit(make_tar([]))
    raw = await run_script(fake.runner())
    raw.cleanup()
    assert seen
    for kwargs in seen:
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        if sys.platform == "win32":
            assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW


async def test_script_killed_by_memory_limit_is_failed(fake):
    # Inside the container the kernel kills just the script (the entry reports -9, Docker
    # exits 0): that is the memory limit too.
    fake.emit(make_tar([], status={"exit_code": -9, "timed_out": False, "stdout_truncated": False}))
    raw = await run_script(fake.runner())
    raw.cleanup()
    assert raw.status == "failed"
    assert raw.error == "The script used more than 4 GB of memory and was stopped."


@pytest.mark.docker
async def test_real_script_over_memory_limit_is_failed(tmp_path, sandbox_image):
    raw = await real_script(tmp_path, sandbox_image, "b = b'x' * (5 * 1024**3)\nprint(len(b))\n")
    raw.cleanup()
    assert raw.status == "failed", raw
    assert raw.error == "The script used more than 4 GB of memory and was stopped."


# Fix round 2


def _block(info: tarfile.TarInfo, fmt=tarfile.USTAR_FORMAT) -> bytes:
    return info.tobuf(format=fmt, encoding="utf-8", errors="strict")


def _padded(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 512)


def _pax_record(key: str, value: str) -> bytes:
    body = f" {key}={value}\n".encode()
    length = len(body) + 1
    while len(str(length).encode()) + len(body) != length:
        length += 1
    return str(length).encode() + body


def _size_override_probe() -> bytes:
    """The re-review's probe: a PAX `size=1024` before status.json, whose data starts with a
    zero block (so a scan by the ustar size thinks the archive ended), then a 70 KB `L`."""
    pax_body = _pax_record("size", "1024")
    pax = tarfile.TarInfo("././@PaxHeader")
    pax.type = tarfile.XHDTYPE
    pax.size = len(pax_body)
    status = tarfile.TarInfo("status.json")
    status.size = 0  # the ustar size; tarfile would use the PAX 1024
    longname = b"files/" + b"a" * 70_000 + b"\0"
    gnu = tarfile.TarInfo("././@LongLink")
    gnu.type = tarfile.GNUTYPE_LONGNAME
    gnu.size = len(longname)
    member = tarfile.TarInfo("files/x")
    return (
        _block(pax) + _padded(pax_body)
        + _block(status) + b"\0" * 1024
        + _block(gnu, tarfile.GNU_FORMAT) + _padded(longname)
        + _block(member) + b"\0" * 10240
    )  # fmt: skip


def _small_global_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": "hi"}
    ) as tar:
        tar.addfile(tarfile.TarInfo("status.json"), io.BytesIO(b""))
    return buf.getvalue()


def _pax_tar(*records: tuple[str, str]) -> bytes:
    body = b"".join(_pax_record(k, v) for k, v in records)
    pax = tarfile.TarInfo("././@PaxHeader")
    pax.type = tarfile.XHDTYPE
    pax.size = len(body)
    status = tarfile.TarInfo("status.json")
    return _block(pax) + _padded(body) + _block(status) + b"\0" * 10240


SCAN_REFUSED = {
    "size_override_probe": _size_override_probe,
    "small_global": _small_global_tar,
    "pax_size_record": lambda: _pax_tar(("size", "0")),
    "pax_unknown_key": lambda: _pax_tar(("comment", "hi")),
    "pax_hdrcharset": lambda: _pax_tar(("hdrcharset", "BINARY")),
    "pax_malformed": lambda: _pax_tar(("path", "status.json")).replace(b" path=", b"Xpath="),
}


@pytest.mark.parametrize(
    "spell",
    [
        lambda p: str(p).replace("\\", "\\\\"),  # doubled backslashes (JSON-escaped)
        lambda p: str(p).replace("\\", "/", 1),  # mixed separators
        lambda p: str(p).upper(),
        lambda p: p.as_posix(),
    ],
)
def test_redact_finds_odd_spellings_of_the_data_folder(tmp_path, spell):
    data = tmp_path / "Data Folder"
    data.mkdir()
    text = f"mount {spell(data)}{os.sep}files failed"
    redacted = runner_mod.redact(text, data)
    assert redacted.startswith("mount <data>"), redacted
    assert "Data Folder" not in redacted and "DATA FOLDER" not in redacted


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 names are a Windows thing")
def test_redact_finds_the_short_name_of_the_data_folder(tmp_path):
    import ctypes

    data = tmp_path / "a rather long data folder name"
    data.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(data), buf, 1024):
        pytest.skip("no short names on this volume")
    short = buf.value
    if short.lower() == str(data).lower():
        pytest.skip("8.3 names are off on this volume")
    redacted = runner_mod.redact(f"mount {short}\\files failed", data)
    assert redacted == "mount <data>\\files failed"


async def test_logged_diagnostics_have_no_line_breaks(fake, monkeypatch, caplog):
    fake.emit(b"")
    monkeypatch.setenv("FAKE_DOCKER_EXIT", "125")
    # (no NUL: an environment variable can't carry one)
    monkeypatch.setenv("FAKE_DOCKER_STDERR", "first\r\nFAKE forged log line\x1b[31m\x07end")
    with caplog.at_level(logging.WARNING, logger="hearthkeep.sandbox.runner"):
        await run_script(fake.runner())
    logged = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert logged
    for message in logged:
        assert not any(unicodedata.category(ch) == "Cc" for ch in message), repr(message)
    assert any(re.search(r"first +FAKE forged log line", m) for m in logged)


async def test_run_waits_for_a_sweep_in_progress(fake, monkeypatch):
    fake.emit(make_tar([]))
    runner = fake.runner()
    release = asyncio.Event()
    order = []
    real_docker = SandboxRunner._docker

    async def slow_ps(self, *args, timeout):
        if args[0] == "ps":
            order.append("sweep started")
            await release.wait()
            order.append("sweep done")
        return await real_docker(self, *args, timeout=timeout)

    monkeypatch.setattr(SandboxRunner, "_docker", slow_ps)
    await runner.status()  # cached, so the run goes straight to registering
    sweep = asyncio.create_task(runner.sweep())
    while not order:
        await asyncio.sleep(0.01)
    run = asyncio.create_task(run_script(runner))
    await asyncio.sleep(0.3)
    assert not run.done()
    assert not (fake.jobs_root() / "run1").exists()  # not registered, no folder yet
    release.set()
    await sweep
    raw = await run
    raw.cleanup()
    assert raw.status == "ok"
    assert order == ["sweep started", "sweep done"]


async def test_chats_folder_leading_to_a_comma_path_is_unavailable(tmp_path, fake):
    target = tmp_path / "odd,place" / "chats"
    (target / CHAT / "files").mkdir(parents=True)
    data = tmp_path / "data2"
    (data / "workspace").mkdir(parents=True)
    link = data / "workspace" / "chats"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link)
    runner = SandboxRunner(data, image_tag=TAG, docker=(sys.executable, str(FAKE)))
    with pytest.raises(SandboxUnavailable) as caught:
        await run_script(runner)
    assert caught.value.user_message == runner_mod.DATA_PATH_MESSAGE
    assert not any(c[0] == "run" for c in fake.calls())


# Fix round 3: Hearthkeep's own ustar reader


def _ustar(
    name: bytes,
    size: int,
    *,
    prefix: bytes = b"",
    kind: bytes = b"0",
    magic: bytes = b"ustar\0",
    version: bytes = b"00",
    size_field: bytes | None = None,
    checksum: int | None = None,
    checksum_field: bytes | None = None,
) -> bytes:
    """One 512-byte header, built by hand (so it can be wrong in exactly one way)."""
    block = bytearray(512)
    block[0 : len(name)] = name
    block[100:108] = b"0000644\0"
    block[108:116] = block[116:124] = b"0000000\0"
    block[124:136] = size_field if size_field is not None else b"%011o\0" % size
    block[136:148] = b"00000000000\0"
    block[156:157] = kind
    block[257:263] = magic
    block[263:265] = version
    block[345 : 345 + len(prefix)] = prefix
    block[148:156] = b" " * 8
    value = sum(block) if checksum is None else checksum
    block[148:156] = checksum_field if checksum_field is not None else b"%06o\0 " % value
    return bytes(block)


STATUS_BODY = b'{"exit_code": 0, "timed_out": false, "stdout_truncated": false}'


def _status_only() -> bytes:
    return _ustar(b"status.json", len(STATUS_BODY)) + STATUS_BODY + bytes(-len(STATUS_BODY) % 512)


def _ustar_tar(header: bytes, data: bytes = b"x" + bytes(511)) -> bytes:
    """status.json, then `header` with `data` (already padded), then a clean end."""
    return _status_only() + header + data + bytes(1024) + bytes(8192)


def _x_padding_records() -> bytes:
    # A tiny `x` whose padding holds GNU sparse 1.0 records: refused at the `x`, unread.
    body = _pax_record("path", "files/a")
    padding = _pax_record("GNU.sparse.major", "1") + _pax_record("GNU.sparse.minor", "0")
    return _ustar_tar(
        _ustar(b"././@PaxHeader", len(body), kind=b"x"),
        data=(body + padding).ljust(512, b"\0") + _ustar(b"files/a", 0),
    )


def _solaris_x() -> bytes:
    body = _pax_record("path", "files/a")
    return _ustar_tar(_ustar(b"././@PaxHeader", len(body), kind=b"X"), data=_padded(body))


def _gnu_sparse_extended() -> bytes:
    header = bytearray(_ustar(b"files/a", 512, kind=b"S"))
    header[482] = 1  # isextended: extension blocks follow
    header[148:156] = b" " * 8
    header[148:156] = b"%06o\0 " % sum(header)
    extension = bytearray(512)
    extension[504] = 1  # and another, and another...
    return _status_only() + bytes(header) + bytes(extension) * 50 + bytes(1024)


def _negative_size() -> bytes:
    return _ustar_tar(_ustar(b"files/a", 0, size_field=b"-0000000001\0"))


def _x_chain(count: int = 600) -> bytes:
    body = _pax_record("comment", "x")
    one = _ustar(b"././@PaxHeader", len(body), kind=b"x") + _padded(body)
    return _status_only() + one * count + _ustar(b"files/a", 0) + bytes(1024)


PROBES = {
    **HOSTILE_HEADERS,
    **SCAN_REFUSED,
    "x_padding_records": _x_padding_records,
    "solaris_X": _solaris_x,
    "gnu_sparse_extension_blocks": _gnu_sparse_extended,
    "negative_size": _negative_size,
    "x_chain_600": _x_chain,
}


@pytest.mark.parametrize("case", sorted(PROBES))
async def test_hostile_streams_are_refused(fake, case):
    fake.emit(PROBES[case]())
    raw = await run_script(fake.runner(), run_id=f"p-{case}"[:32])
    assert raw.status == "sandbox_error"
    assert raw.error == "The sandbox returned output Hearthkeep couldn't read."
    assert raw.members == {}
    assert not (fake.jobs_root() / f"p-{case}"[:32]).exists()


# (bad_status unpacks fine: status.json is checked after the reader)
@pytest.mark.parametrize("case", sorted(PROBES) + sorted(set(BAD_TARS) - {"bad_status"}))
def test_reader_refuses_promptly_with_bounded_memory(tmp_path, case):
    import threading
    import tracemalloc

    source = (PROBES | BAD_TARS)[case]
    tar = tmp_path / "out.tar"
    tar.write_bytes(source())
    tracemalloc.start()
    started = time.monotonic()
    try:
        with pytest.raises(runner_mod._BadOutput):
            runner_mod._unpack(tar, tmp_path / "out", 200, threading.Event())
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert time.monotonic() - started < 2
    assert peak < 512 * 1024  # a few blocks and one chunk, whatever the stream says


def test_x_chain_is_refused_at_the_first_x(tmp_path):
    import threading

    tar = tmp_path / "out.tar"
    tar.write_bytes(_x_chain())
    with pytest.raises(runner_mod._BadOutput, match="not a regular file"):
        runner_mod._unpack(tar, tmp_path / "out", 200, threading.Event())
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["m000"]  # status.json only


def test_the_runner_does_not_use_tarfile():
    source = Path(runner_mod.__file__).read_text(encoding="utf-8")
    assert "import tarfile" not in source and "tarfile." not in source


async def test_hundred_character_file_name_fits_ustar(fake):
    name = "files/" + "a" * 100  # ustar holds it as prefix "files" + a 100-byte name
    fake.emit(make_tar([(name, b"x")]))
    raw = await run_script(fake.runner())
    names = set(raw.members)
    raw.cleanup()
    assert raw.status == "ok"
    assert name in names


async def test_old_style_regular_type_is_accepted(fake):
    fake.emit(_status_only() + _ustar(b"stdout.txt", 2, kind=b"\0") + b"hi" + bytes(510)
              + bytes(1024))  # fmt: skip
    raw = await run_script(fake.runner())
    raw.cleanup()
    assert (raw.status, raw.stdout) == ("ok", "hi")


def test_file_names_are_at_most_a_hundred_characters():
    assert runner_mod.MEMBER_NAME.fullmatch("files/" + "a" * 100)
    assert not runner_mod.MEMBER_NAME.fullmatch("files/" + "a" * 101)
    from hearthkeep.sandbox import results

    assert results.MAX_FILE_NAME == 100


def test_page_images_up_to_page_2000_may_come_back():
    # read_pages asks for pages 1 to 2000.
    assert runner_mod.MEMBER_NAME.fullmatch("pages/1.png")
    assert runner_mod.MEMBER_NAME.fullmatch("pages/2000.png")
    assert not runner_mod.MEMBER_NAME.fullmatch("pages/20000.png")
    assert not runner_mod.MEMBER_NAME.fullmatch("pages/x.png")


def test_redact_finds_the_real_path_behind_a_junction(tmp_path):
    real = tmp_path / "real data place"
    real.mkdir()
    link = tmp_path / "linked"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(real), str(link))
    else:
        os.symlink(real, link)
    redacted = runner_mod.redact(f"mount {real}{os.sep}files failed", link)
    assert redacted == f"mount <data>{os.sep}files failed"


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 names are a Windows thing")
def test_redact_finds_the_long_name_given_the_short_one(tmp_path):
    import ctypes

    data = tmp_path / "another long data folder"
    data.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(data), buf, 1024):
        pytest.skip("no short names on this volume")
    short = buf.value
    if short.lower() == str(data).lower():
        pytest.skip("8.3 names are off on this volume")
    # Hearthkeep knows only the short spelling; Docker reports the long one.
    redacted = runner_mod.redact(f"mount {data}\\files failed", Path(short))
    assert redacted == "mount <data>\\files failed"


def test_one_line_replaces_unicode_line_separators():
    assert runner_mod._one_line("a\u2028b\u2029c\r\nd") == "a b c  d"


def test_files_dir_with_a_comma_is_unavailable(tmp_path):
    runner = SandboxRunner(tmp_path, image_tag=TAG)
    target = tmp_path / "odd,files"
    target.mkdir()
    chat = tmp_path / "workspace" / "chats" / CHAT
    chat.mkdir(parents=True)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(chat / "files"))
    else:
        os.symlink(target, chat / "files")
    with pytest.raises(SandboxUnavailable) as caught:
        runner._files_dir(chat / "files")
    assert caught.value.user_message == runner_mod.DATA_PATH_MESSAGE
