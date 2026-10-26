"""Fixtures for the in-image tests. Every file they make lives under pytest's tmp_path
(inside /tmp in the image); nothing personal is bundled."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest


class Job:
    """A job folder, an output folder and a data folder, wired up through HK_JOB/HK_OUT."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.job = root / "job"
        self.out = root / "out"
        self.data = root / "data"
        self.work = root / "work"
        for d in (self.job, self.out, self.data, self.work):
            d.mkdir()
        self.manifest: dict = {"files": [], "script_timeout_s": 110, "args": {}}
        self.save()

    def save(self) -> None:
        (self.job / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def add(self, name: str, kind: str, data: bytes) -> str:
        file_id = f"f{len(self.manifest['files']) + 1}"
        path = self.data / f"{file_id}.{kind}"
        path.write_bytes(data)
        self.manifest["files"].append(
            {"id": file_id, "name": name, "kind": kind, "path": str(path)}
        )
        self.save()
        return file_id

    def results(self) -> list[dict]:
        path = self.out / "results.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def job(tmp_path, monkeypatch) -> Job:
    j = Job(tmp_path)
    monkeypatch.setenv("HK_JOB", str(j.job))
    monkeypatch.setenv("HK_OUT", str(j.out))
    return j


# ------------------------------------------------------------------ PDFs made on the fly


def text_pdf(lines: list[str], *, width: int = 595, height: int = 842) -> bytes:
    """A one-page PDF with real text (Helvetica), written by hand."""
    ops = ["BT", "/F1 12 Tf", "14 TL", f"50 {height - 60} Td"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({safe}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode(),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def image_pdf() -> bytes:
    """A one-page PDF that is only a picture (like a scan): no extractable text."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(img).rectangle((40, 40, 360, 260), outline="black", width=4)
    buf = io.BytesIO()
    img.save(buf, format="PDF", resolution=72)
    return buf.getvalue()


def merge_pdfs(
    *docs: bytes, user_password: str | None = None, owner_password: str | None = None
) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for doc in docs:
        for page in PdfReader(io.BytesIO(doc)).pages:
            writer.add_page(page)
    if user_password is not None:
        writer.encrypt(
            user_password=user_password, owner_password=owner_password, algorithm="AES-128"
        )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
