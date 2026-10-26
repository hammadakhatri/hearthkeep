"""The container entry (`hk_entry.py script|inspect|pages`), run inside the sandbox image."""

from __future__ import annotations

import io
import json
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
