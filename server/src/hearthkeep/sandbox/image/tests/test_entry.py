"""The container entry (`hk_entry.py script|inspect|pages`), run inside the sandbox image."""

from __future__ import annotations

import io
import json
import os
import tarfile

import hk_entry
from conftest import merge_pdfs, text_pdf
from PIL import Image


def run(job, mode, **manifest):
    job.manifest.update(manifest)
    job.save()
    stream = io.BytesIO()
    assert hk_entry.run(mode, job=job.job, out=job.out, work=job.work, stdout=stream) == 0
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:") as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert all(m.isfile() for m in members.values())
        contents = {name: tar.extractfile(m).read() for name, m in members.items()}
    return json.loads(contents["status.json"]), contents


def test_entry_caps_stdout_and_times_out(job):
    (job.job / "main.py").write_text('while True:\n    print("x" * 1000)\n', encoding="utf-8")
    status, contents = run(job, "script", script_timeout_s=2)
    assert status["timed_out"] is True
    assert status["exit_code"] is None
    assert status["stdout_truncated"] is True
    assert 2000 <= status["duration_ms"] < 20_000
    assert len(contents["stdout.txt"]) == 262_144
    assert (job.out / "stdout.txt").stat().st_size <= 262_144


def test_entry_emits_tar_of_out(job):
    job.add("a.csv", "csv", b"Name,Price\nTea,1.5\nCake,3\n")
    (job.job / "main.py").write_text(
        "import os, sys\n"
        "import hk\n"
        "df = hk.read('a.csv')\n"
        "hk.result('total', df['Price'].sum(), unit='GBP')\n"
        "hk.file('prices.csv', df)\n"
        "print('hello')\n"
        "print('oops', file=sys.stderr)\n"
        "os.symlink('/etc/passwd', os.path.join(os.environ['HK_OUT'], 'link'))\n"
        "assert os.getcwd() == os.environ['HOME']\n",
        encoding="utf-8",
    )
    status, contents = run(job, "script")
    assert status["exit_code"] == 0, contents.get("stdout.txt")
    assert status["timed_out"] is False
    assert status["stdout_truncated"] is False
    assert set(contents) == {"status.json", "stdout.txt", "results.jsonl", "files/prices.csv"}
    assert contents["stdout.txt"] == b"hello\noops\n"
    value = json.loads(contents["results.jsonl"].splitlines()[0])
    assert value["name"] == "total" and value["value"] == 4.5


def test_entry_reports_script_errors(job):
    (job.job / "main.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    status, contents = run(job, "script")
    assert status == {**status, "exit_code": 3, "timed_out": False}


def test_entry_inspect_writes_inspect_json(job):
    fid = job.add("s.csv", "csv", b"a;b\n1,5;2\n")
    status, contents = run(job, "inspect", args={"file": fid})
    assert status["exit_code"] == 0
    report = json.loads(contents["inspect.json"])
    assert report["ok"] is True and report["delimiter"] == ";"


def test_entry_pages_renders_png_within_2000px(job):
    pdf = merge_pdfs(text_pdf(["Wide page"], width=2000, height=1000), text_pdf(["Two"]))
    fid = job.add("wide.pdf", "pdf", pdf)
    status, contents = run(job, "pages", args={"file": fid, "pages": [1, 2, 9], "dpi": 150})
    assert status["exit_code"] == 0
    assert sorted(n for n in contents if n.startswith("pages/")) == ["pages/1.png", "pages/2.png"]
    wide = Image.open(io.BytesIO(contents["pages/1.png"]))
    assert max(wide.size) <= 2000 and wide.size[0] > wide.size[1]
    a4 = Image.open(io.BytesIO(contents["pages/2.png"]))
    width, height = a4.size  # 595 x 842 pt at 150 dpi
    assert abs(width - 1240) <= 1 and abs(height - 1754) <= 1


def test_entry_unknown_mode_still_emits_status(job):
    status, contents = run(job, "nonsense")
    assert status["exit_code"] == 1
    assert b"unknown mode" in contents["stdout.txt"]


# ------------------------------------------------------------------ hostile /out contents


def test_entry_tar_skips_unreadable_file_and_fifo(job):
    (job.job / "main.py").write_text(
        "import os\n"
        "out = os.environ['HK_OUT']\n"
        "open(os.path.join(out, 'secret.txt'), 'w').write('hidden')\n"
        "os.chmod(os.path.join(out, 'secret.txt'), 0)\n"
        "os.mkfifo(os.path.join(out, 'pipe'))\n"
        "open(os.path.join(out, 'fine.txt'), 'w').write('ok')\n",
        encoding="utf-8",
    )
    status, contents = run(job, "script")
    assert status["exit_code"] == 0
    assert contents["fine.txt"] == b"ok"
    assert "secret.txt" not in contents
    assert "pipe" not in contents


def test_entry_replaces_locked_directory_at_status_json(job):
    (job.job / "main.py").write_text(
        "import os\n"
        "target = os.path.join(os.environ['HK_OUT'], 'status.json')\n"
        "os.makedirs(os.path.join(target, 'sub', 'deeper'))\n"
        "open(os.path.join(target, 'sub', 'f'), 'w').write('x')\n"
        "os.chmod(os.path.join(target, 'sub'), 0o555)\n"
        "os.chmod(target, 0o555)\n"
        "stdout = os.path.join(os.environ['HK_OUT'], 'stdout.txt')\n"
        "os.makedirs(os.path.join(stdout, 'sub'))\n"
        "os.chmod(stdout, 0o500)\n",
        encoding="utf-8",
    )
    status, contents = run(job, "script")
    assert status["exit_code"] == 0 and status["timed_out"] is False
    assert not any(name.startswith("status.json/") for name in contents)


def test_entry_tar_skips_sparse_files(job):
    (job.job / "main.py").write_text(
        "import os\n"
        "out = os.environ['HK_OUT']\n"
        "with open(os.path.join(out, 'sparse.bin'), 'wb') as fh:\n"
        "    fh.truncate(1 << 30)  # 1 GB that takes no space\n"
        "open(os.path.join(out, 'fine.txt'), 'w').write('ok')\n",
        encoding="utf-8",
    )
    status, contents = run(job, "script")
    assert status["exit_code"] == 0
    assert "sparse.bin" not in contents
    assert contents["fine.txt"] == b"ok"


def test_entry_tar_keeps_small_files_that_report_no_blocks(job, monkeypatch):
    # Some filesystems store tiny files inline and report st_blocks == 0; only large
    # members are judged sparse.
    (job.job / "main.py").write_text(
        "import os\nopen(os.path.join(os.environ['HK_OUT'], 'tiny.txt'), 'w').write('tiny')\n",
        encoding="utf-8",
    )
    real_fstat = hk_entry.os.fstat

    class NoBlocks:
        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            return 0 if name == "st_blocks" else getattr(self._info, name)

    monkeypatch.setattr(hk_entry.os, "fstat", lambda fd: NoBlocks(real_fstat(fd)))
    status, contents = run(job, "script")
    assert status["exit_code"] == 0
    assert contents["tiny.txt"] == b"tiny"


def test_entry_tar_survives_a_file_shrinking_during_copy(job, monkeypatch):
    (job.job / "main.py").write_text(
        "import os\nopen(os.path.join(os.environ['HK_OUT'], 'big.txt'), 'w').write('y' * 1000)\n",
        encoding="utf-8",
    )
    real_fstat = hk_entry.os.fstat
    target = job.out / "big.txt"

    def fstat_then_shrink(fd):
        info = real_fstat(fd)
        if target.exists() and info.st_ino == target.stat().st_ino:
            os.truncate(target, 10)  # the file shrinks after its size was taken
        return info

    monkeypatch.setattr(hk_entry.os, "fstat", fstat_then_shrink)
    status, contents = run(job, "script")
    assert status["exit_code"] == 0
    assert len(contents["big.txt"]) == 1000
    assert contents["big.txt"].startswith(b"y" * 10)
    assert contents["big.txt"][10:] == b"\0" * 990


def test_entry_pages_clamps_dpi_and_scales_huge_pages(job, monkeypatch):
    import pypdfium2

    scales = []
    real_render = pypdfium2.PdfPage.render

    def spy(self, *args, scale=1, **kwargs):
        scales.append((scale, self.get_size()))
        return real_render(self, *args, scale=scale, **kwargs)

    monkeypatch.setattr(pypdfium2.PdfPage, "render", spy)
    huge = text_pdf(["Huge"], width=14400, height=7200)
    small = text_pdf(["Small"], width=100, height=100)
    fid = job.add("mixed.pdf", "pdf", merge_pdfs(huge, small))
    status, contents = run(job, "pages", args={"file": fid, "pages": [1, 2], "dpi": 1000})
    assert status["exit_code"] == 0
    (huge_scale, huge_size), (small_scale, _) = scales
    assert huge_scale * max(huge_size) <= 2000  # scaled down before rendering
    assert small_scale == 300 / 72  # dpi clamped to 300
    assert max(Image.open(io.BytesIO(contents["pages/1.png"])).size) <= 2000
    status, contents = run(job, "pages", args={"file": fid, "pages": [2], "dpi": 10})
    assert scales[-1][0] == 50 / 72  # dpi clamped to 50


def test_entry_pages_render_error_skips_only_that_page(job, monkeypatch):
    import pypdfium2

    real_render = pypdfium2.PdfPage.render
    calls = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise pypdfium2.PdfiumError("cannot render")
        return real_render(self, *args, **kwargs)

    monkeypatch.setattr(pypdfium2.PdfPage, "render", flaky)
    pdf = merge_pdfs(text_pdf(["One"]), text_pdf(["Two"]), text_pdf(["Three"]))
    fid = job.add("three.pdf", "pdf", pdf)
    status, contents = run(job, "pages", args={"file": fid, "pages": [1, 2, 3], "dpi": 72})
    assert status["exit_code"] == 0
    assert sorted(n for n in contents if n.startswith("pages/")) == ["pages/1.png", "pages/3.png"]


def test_entry_tar_is_plain_ustar_and_skips_names_ustar_cannot_hold(job):
    # Plain ustar only (the host refuses PAX and GNU headers). A name ustar can't hold is
    # left out, and the files after it still come through.
    (job.job / "main.py").write_text(
        "import os\n"
        "import hk\n"
        "out = os.environ['HK_OUT']\n"
        "os.makedirs(os.path.join(out, 'files'), exist_ok=True)\n"
        "open(os.path.join(out, 'files', 'a' * 101), 'w').write('long')\n"
        "hk.file('b' * 100, 'max')\n"
        "hk.file('zz.txt', 'after')\n",
        encoding="utf-8",
    )
    job.save()
    stream = io.BytesIO()
    assert hk_entry.run("script", job=job.job, out=job.out, work=job.work, stdout=stream) == 0
    raw = stream.getvalue()
    names = []
    pos = 0
    while raw[pos : pos + 512].strip(b"\0"):
        header = raw[pos : pos + 512]
        assert header[257:265] == b"ustar\x0000"
        assert header[156:157] in (b"0", b"\0")
        size = int(header[124:136].rstrip(b"\0 "), 8)
        prefix, name = header[345:500].rstrip(b"\0"), header[0:100].rstrip(b"\0")
        names.append((prefix + b"/" + name if prefix else name).decode("ascii"))
        pos += 512 + (size + 511) // 512 * 512
    assert names[0] == "status.json"
    assert "files/" + "b" * 100 in names and "files/zz.txt" in names
    assert "files/" + "a" * 101 not in names
