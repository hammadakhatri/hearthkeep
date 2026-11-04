"""The sandbox image: its content-hash tag, its pins, the CLI, and the hk tests inside it."""

import io
import json
import re
import shutil
import subprocess
import tarfile
import time

import pytest

from hearthkeep import cli
from hearthkeep.sandbox import image


def test_image_tag_changes_with_any_file(tmp_path):
    copy = tmp_path / "image"
    shutil.copytree(image.IMAGE_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
    tag = image.image_tag(copy)
    assert re.fullmatch(r"hearthkeep-sandbox:[0-9a-f]{12}", tag)
    assert image.image_tag(copy) == tag
    assert image.image_tag(image.IMAGE_DIR) == tag  # caches never count

    target = copy / "hk" / "_read.py"
    data = bytearray(target.read_bytes())
    data[-1] = data[-1] ^ 1
    target.write_bytes(bytes(data))
    changed = image.image_tag(copy)
    assert changed != tag

    (copy / "tests" / "extra.txt").write_text("x", encoding="utf-8")
    assert image.image_tag(copy) not in (tag, changed)


def test_image_tag_ignores_bytecode_caches(tmp_path):
    copy = tmp_path / "image"
    shutil.copytree(image.IMAGE_DIR, copy, ignore=shutil.ignore_patterns("__pycache__"))
    tag = image.image_tag(copy)
    cache = copy / "hk" / "__pycache__"
    cache.mkdir()
    (cache / "_read.cpython-312.pyc").write_bytes(b"\0" * 16)
    assert image.image_tag(copy) == tag


def test_dockerfile_pins_base_digest_and_requirements():
    dockerfile = (image.IMAGE_DIR / "Dockerfile").read_text(encoding="utf-8")
    froms = [ln for ln in dockerfile.splitlines() if ln.startswith("FROM ")]
    assert len(froms) == 1
    assert re.search(r"FROM python:3\.12-slim-bookworm@sha256:[0-9a-f]{64}\b", froms[0])
    assert "--require-hashes" in dockerfile
    assert "--only-binary=:all:" in dockerfile
    assert "useradd --uid 10001 --create-home hk" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["python", "-I", "-B", "/opt/hk/hk_entry.py"]' in dockerfile

    text = (image.IMAGE_DIR / "requirements.txt").read_text(encoding="utf-8")
    joined = text.replace("\\\n", " ")
    lines = [ln.strip() for ln in joined.splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines
    for line in lines:
        assert re.match(r"[A-Za-z0-9._-]+==[^ ]+ ", line), line
        assert "--hash=sha256:" in line, line
    pins = {ln.split("==")[0].lower(): ln.split("==")[1].split()[0] for ln in lines}
    # Exact versions live in requirements.txt (Dependabot proposes updates, pip-audit in CI
    # checks them); here, every package the image needs is there, pinned and hashed.
    for name in (
        "pandas",
        "numpy",
        "scipy",
        "openpyxl",
        "xlrd",
        "pdfplumber",
        "pytest",
        "pypdf",
        "pypdfium2",
        "pdfminer-six",
        "pillow",
    ):
        assert name in pins, name


def test_cli_sandbox_status_without_docker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(image, "DOCKER", ("hearthkeep-test-no-such-docker",))
    assert cli.main(["sandbox", "status", "--data", str(tmp_path / "data")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Docker: not found"
    assert out[1] == f"Sandbox image: {image.image_tag()} (missing)"
    assert out[2] == f"Repo image: {image.repo_image_tag()} (missing)"


def test_cli_sandbox_build_without_docker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(image, "DOCKER", ("hearthkeep-test-no-such-docker",))
    assert cli.main(["sandbox", "build", "--data", str(tmp_path / "data")]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith(
        "Building the Python sandbox image. The first build downloads Python and the "
        "analysis libraries (about 1 GB).\n"
    )
    assert "Docker" in captured.err


def test_cli_sandbox_build_still_prints(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(image, "docker_ok", lambda docker=("docker",): True)

    def fake_build(tag, docker=("docker",), image_dir=image.IMAGE_DIR, *, on_line=None, **kw):
        calls.append(tag)
        on_line("Step 1/2 : FROM python ✓")  # a real build's text isn't always ASCII
        return 0

    monkeypatch.setattr(image, "build_image", fake_build)
    assert cli.main(["sandbox", "build", "--data", str(tmp_path / "data")]) == 0
    assert calls == [image.image_tag(), image.repo_image_tag()]
    out = capsys.readouterr().out
    assert f"Sandbox image ready: {image.image_tag()}" in out
    assert "Step 1/2 : FROM python" in out  # the build's own lines still reach the console
    assert out.isascii()


@pytest.mark.docker
def test_hk_library_tests_pass_in_image(sandbox_image):
    # The same limits the runner uses: no network, read-only root, scratch only in /tmp.
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=268435456",
        "--entrypoint",
        "python",
        sandbox_image,
        "-m",
        "pytest",
        "-q",
        "/opt/hk/tests",
    ]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert " passed" in proc.stdout


ESCAPING_SCRIPT = """\
import json, os, time
if os.fork() == 0:
    os.setsid()  # leave the script's process group, as a stray daemon would
    fake = json.dumps({"exit_code": 0, "timed_out": False, "stdout_truncated": False,
                       "duration_ms": 1, "forged": True})
    while True:
        try:
            with open("/out/status.json.tmp", "w") as fh:
                fh.write(fake)
            os.replace("/out/status.json.tmp", "/out/status.json")
        except OSError:
            pass
print("parent waiting", flush=True)
time.sleep(1000)
"""


@pytest.mark.docker
def test_entry_kills_escaped_processes_before_writing_status(sandbox_image, tmp_path):
    job, data = tmp_path / "job", tmp_path / "data"
    job.mkdir()
    data.mkdir()
    (job / "main.py").write_text(ESCAPING_SCRIPT, encoding="utf-8", newline="\n")
    manifest = {"files": [], "script_timeout_s": 3, "args": {}}
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "10001:10001",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=268435456",
        "--mount",
        "type=tmpfs,destination=/out,tmpfs-size=268435456",
        "--mount",
        f"type=bind,source={data},destination=/data,readonly",
        "--mount",
        f"type=bind,source={job},destination=/job,readonly",
        sandbox_image,
        "script",
    ]
    started = time.monotonic()
    proc = subprocess.run(command, capture_output=True, timeout=120)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
        status = json.loads(tar.extractfile("status.json").read())
        printed = tar.extractfile("stdout.txt").read()
    assert "forged" not in status
    assert status["timed_out"] is True
    assert status["exit_code"] is None
    # The escaped writer's leftover file is not taken back; only a note says so.
    assert printed in (
        b"parent waiting\n",
        b"parent waiting\nIgnored /out/status.json.tmp: save files with hk.file(name, data).\n",
    )
    # The script limit plus start-up; waiting on the escaped writer's pipe would add 5 s.
    assert elapsed < 3 + 4


def test_docker_probes_and_build_open_no_window_and_no_stdin(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(image.subprocess, "run", fake_run)
    assert image.docker_ok() is True
    assert image.image_present("hearthkeep-sandbox:0123456789ab") is True
    assert image.build_image("hearthkeep-sandbox:0123456789ab") == 0
    assert len(calls) == 3
    for kwargs in calls:
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_entry_takes_back_exactly_the_names_the_host_accepts():
    import importlib.util

    from hearthkeep.sandbox import runner

    path = image.IMAGE_DIR / "hk_entry.py"
    spec = importlib.util.spec_from_file_location("hk_entry_for_test", path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    assert entry.MEMBER_NAME.pattern == runner.MEMBER_NAME.pattern
    assert entry.MEMBER_NAME.flags == runner.MEMBER_NAME.flags


class _Pipe:
    def __init__(self, lines):
        self._lines, self.closed = iter(lines), False

    def __iter__(self):
        return self._lines

    def close(self):
        self.closed = True


class _FakePopen:
    def __init__(self, command, **kwargs):
        self.command, self.kwargs = command, kwargs
        self.stdout = _Pipe(["Step 1/3 : FROM python\r\n", "naughty ✓ line\n", "done"])
        self.killed, self.pid = False, 0

    def poll(self):
        return None if not self.killed else -9

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_build_image_with_on_line_pipes_each_line_and_hands_over_the_child(monkeypatch):
    made = []

    def popen(command, **kwargs):
        made.append(_FakePopen(command, **kwargs))
        return made[0]

    monkeypatch.setattr(image.subprocess, "Popen", popen)
    lines, procs = [], []
    code = image.build_image(
        "hearthkeep-sandbox:0123456789ab",
        ("docker",),
        image.IMAGE_DIR,
        on_line=lines.append,
        on_proc=procs.append,
    )
    assert code == 0
    assert lines == ["Step 1/3 : FROM python", "naughty ✓ line", "done"]
    assert [h.proc for h in procs] == made
    assert made[0].stdout.closed
    kwargs = made[0].kwargs
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert made[0].command[:4] == ["docker", "build", "-t", "hearthkeep-sandbox:0123456789ab"]


def test_build_image_with_on_line_without_docker_is_127():
    code = image.build_image(
        "t:1", ("hearthkeep-test-no-such-docker",), image.IMAGE_DIR, on_line=lambda line: None
    )
    assert code == 127


def test_build_targets_are_the_two_images_in_order():
    assert image.build_targets() == [
        ("Sandbox image", image.image_tag(), image.IMAGE_DIR),
        ("Repo image", image.repo_image_tag(), image.REPO_IMAGE_DIR),
    ]


def test_build_image_hands_over_a_handle_that_kills_the_whole_tree(monkeypatch):
    made = []

    class Job:
        def __init__(self):
            self.assigned, self.terminated, self.closed = None, 0, 0

        def assign(self, pid):
            self.assigned = pid

        def terminate(self, code=1):
            self.terminated += 1

        def close(self):
            self.closed += 1

    job = Job()
    monkeypatch.setattr(image, "_new_job", lambda: job)

    def popen(command, **kwargs):
        made.append(_FakePopen(command, **kwargs))
        made[0].pid = 4242
        return made[0]

    monkeypatch.setattr(image.subprocess, "Popen", popen)
    handles = []
    image.build_image(
        "t:1", ("docker",), image.IMAGE_DIR, on_line=lambda s: None, on_proc=handles.append
    )
    assert job.assigned == 4242
    handles[0].kill()
    assert job.terminated == 1 and made[0].killed
    assert job.closed >= 1  # and the job is closed when the build ends
