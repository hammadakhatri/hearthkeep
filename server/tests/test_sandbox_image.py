"""The sandbox image: its content-hash tag, its pins, the CLI, and the hk tests inside it."""

import re
import shutil
import subprocess

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
    expected = {
        "pandas": "2.3.1",
        "numpy": "2.3.1",
        "scipy": "1.16.0",
        "openpyxl": "3.1.5",
        "xlrd": "2.0.2",
        "pdfplumber": "0.11.7",
    }
    for name, version in expected.items():
        assert pins[name] == version
    assert pins["pytest"].startswith("8.4.")
    assert pins["pypdf"].startswith("5.0.")
    for name in ("pypdfium2", "pdfminer-six", "pillow"):
        assert name in pins


def test_cli_sandbox_status_without_docker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(image, "DOCKER", ("hearthkeep-test-no-such-docker",))
    assert cli.main(["sandbox", "status", "--data", str(tmp_path / "data")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Docker: not found"
    assert out[1] == f"Sandbox image: {image.image_tag()} (missing)"


def test_cli_sandbox_build_without_docker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(image, "DOCKER", ("hearthkeep-test-no-such-docker",))
    assert cli.main(["sandbox", "build", "--data", str(tmp_path / "data")]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith(
        "Building the Python sandbox image. The first build downloads Python and the "
        "analysis libraries (about 1 GB).\n"
    )
    assert "Docker" in captured.err


def test_cli_sandbox_build_runs_docker_build(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(image, "docker_ok", lambda docker=("docker",): True)

    def fake_build(tag, docker=("docker",)):
        calls.append(tag)
        return 0

    monkeypatch.setattr(image, "build_image", fake_build)
    assert cli.main(["sandbox", "build", "--data", str(tmp_path / "data")]) == 0
    assert calls == [image.image_tag()]
    assert f"Sandbox image ready: {image.image_tag()}" in capsys.readouterr().out


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
