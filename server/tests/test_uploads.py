"""POST /api/uploads and GET /api/uploads/{id}/file: the owner's own photos."""

import json
import struct
import zlib

import pytest
from fakes import FakeChatClient, FakeVerifier
from fastapi.testclient import TestClient
from test_api import CSRF, ORIGIN, enroll

from hearthkeep.api.app import DB_FILE, create_app
from hearthkeep.config import Settings
from hearthkeep.db import connect
from hearthkeep.images import image_info
from hearthkeep.tools.base import ToolRegistry

MB = 1024 * 1024


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00" * 16)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", pixels)
        + _chunk(b"IEND", b"")
    )  # fmt: skip


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + bytes(9)
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xda\x00\x02" + b"scan" + b"\xff\xd9"


def _riff(chunk: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_vp8x(width: int, height: int) -> bytes:
    data = bytes(4) + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return _riff(b"VP8X" + struct.pack("<I", len(data)) + data)


def webp_vp8l(width: int, height: int) -> bytes:
    data = b"\x2f" + struct.pack("<I", (width - 1) | ((height - 1) << 14)) + bytes(4)
    return _riff(b"VP8L" + struct.pack("<I", len(data)) + data)


def webp_vp8(width: int, height: int) -> bytes:
    data = b"\x10\x02\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + bytes(4)
    return _riff(b"VP8 " + struct.pack("<I", len(data)) + data)


GIF = b"GIF89a" + struct.pack("<HH", 10, 10) + bytes(20)


@pytest.fixture
def start(tmp_path):
    clients: list[TestClient] = []

    def _start(*, sign_in: bool = True) -> TestClient:
        app = create_app(
            Settings(data_dir=tmp_path, docker_available=False),
            verifier=FakeVerifier(),
            chat_client=FakeChatClient([]),
            registry=ToolRegistry(),
            health_check=_healthy,
            web_dist=None,
        )
        client = TestClient(app, base_url=ORIGIN)
        client.__enter__()
        clients.append(client)
        if sign_in:
            from hearthkeep.auth.enrollment import EnrollmentCodes

            conn = connect(tmp_path / DB_FILE)
            code = EnrollmentCodes(conn).issue("first_device", ttl_minutes=15)
            conn.close()
            enroll(client, code)
        return client

    yield _start
    for client in clients:
        client.__exit__(None, None, None)


async def _healthy():
    return True


def upload(client: TestClient, data: bytes, name: str = "holiday.png", mime: str = "image/png"):
    return client.post("/api/uploads", files={"file": (name, data, mime)}, headers=CSRF)


def rows(tmp_path, sql: str) -> list:
    conn = connect(tmp_path / DB_FILE)
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


# ---- header parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png(640, 480), ("image/png", "png", 640, 480)),
        (jpeg(800, 600), ("image/jpeg", "jpg", 800, 600)),
        (webp_vp8x(1200, 900), ("image/webp", "webp", 1200, 900)),
        (webp_vp8l(300, 200), ("image/webp", "webp", 300, 200)),
        (webp_vp8(321, 123), ("image/webp", "webp", 321, 123)),
    ],
)
def test_image_info_reads_dimensions_from_headers(data, expected):
    info = image_info(data)
    assert (info.mime, info.ext, info.width, info.height) == expected


@pytest.mark.parametrize(
    "data",
    [
        GIF,
        b"",
        b"\x89PNG\r\n\x1a\n",  # signature only
        b"\xff\xd8\xff\xe0\x00\x10JFIF",  # no frame header
        b"\xff\xd8" + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 0, 640, 3) + bytes(9),  # DNL
        b"RIFF\x00\x00\x00\x00WEBPVP8X",  # truncated
        png(0, 10),
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    ],
)
def test_image_info_rejects_what_it_cannot_parse(data):
    assert image_info(data) is None


# ---- POST /api/uploads ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "mime", "ext", "size"),
    [
        (png(640, 480), "image/png", "png", (640, 480)),
        (jpeg(800, 600), "image/jpeg", "jpg", (800, 600)),
        (webp_vp8x(4096, 4096), "image/webp", "webp", (4096, 4096)),
    ],
)
def test_upload_accepts_png_jpeg_webp(start, tmp_path, data, mime, ext, size):
    client = start()
    # The client's name and type claims are ignored: the type comes from the bytes.
    response = upload(client, data, name="My Family Photo.gif", mime="application/octet-stream")
    assert response.status_code == 201, response.text
    upload_id = response.json()["id"]
    assert response.json() == {"id": upload_id, "width": size[0], "height": size[1]}
    stored = tmp_path / "workspace" / "uploads" / f"{upload_id}.{ext}"
    assert stored.read_bytes() == data
    [row] = rows(tmp_path, "SELECT * FROM uploads")
    assert row["id"] == upload_id and row["mime"] == mime
    assert (row["width"], row["height"]) == size
    assert row["file_name"] == f"{upload_id}.{ext}"
    # The owner's file name is never kept: not in the database, not in the audit log.
    [event] = rows(tmp_path, "SELECT * FROM audit WHERE event = 'upload'")
    assert json.loads(event["detail"]) == {"id": upload_id, "type": mime, "bytes": len(data)}
    assert event["actor"].startswith("session:")
    everything = json.dumps(rows(tmp_path, "SELECT * FROM audit")) + json.dumps(row)
    assert "Family" not in everything

    served = client.get(f"/api/uploads/{upload_id}/file")
    assert served.status_code == 200 and served.content == data
    assert served.headers["content-type"] == mime
    assert served.headers["cache-control"] == "private, max-age=86400"
    assert served.headers["x-content-type-options"] == "nosniff"


def test_upload_ids_are_random(start):
    client = start()
    ids = {upload(client, png(10, 10)).json()["id"] for _ in range(3)}
    assert len(ids) == 3


def test_upload_rejects_other_types_by_content(start, tmp_path):
    client = start()
    for data in (GIF, b"just some text", b"\x89PNG\r\n\x1a\n" + b"broken"):
        response = upload(client, data, name="photo.png", mime="image/png")
        assert response.status_code == 415, data
        body = response.json()
        assert body["reason"] == "unsupported_type"
        assert body["message"] == (
            "That file isn't a photo Hearthkeep can use. Pick a PNG, JPEG or WebP image."
        )
    assert rows(tmp_path, "SELECT * FROM uploads") == []
    assert not any((tmp_path / "workspace" / "uploads").iterdir())


def test_upload_rejects_too_large(start, tmp_path):
    client = start()
    data = png(100, 100)
    data += b"\x00" * (15 * MB + 1 - len(data))
    response = upload(client, data)
    assert response.status_code == 413
    assert response.json() == {
        "reason": "too_large",
        "message": "That photo is too big. Photos can be up to 15 MB.",
    }
    exactly = png(100, 100)
    exactly += b"\x00" * (15 * MB - len(exactly))
    assert upload(client, exactly).status_code == 201
    assert len(rows(tmp_path, "SELECT * FROM uploads")) == 1


def test_upload_rejects_huge_declared_length_before_reading(start):
    client = start()
    response = client.post(
        "/api/uploads",
        content=b"x" * 10,
        headers={**CSRF, "Content-Type": "multipart/form-data; boundary=abc",
                 "Content-Length": str(40 * MB)},
    )  # fmt: skip
    assert response.status_code == 413 and response.json()["reason"] == "too_large"


@pytest.mark.parametrize(
    "data", [png(4097, 100), jpeg(5000, 3000), webp_vp8x(100, 4097), webp_vp8l(4100, 10)]
)
def test_upload_rejects_too_many_pixels(start, tmp_path, data):
    response = upload(start(), data)
    assert response.status_code == 422
    assert response.json() == {
        "reason": "too_many_pixels",
        "message": "That photo is too large. Photos can be up to 4096 pixels on each side.",
    }
    assert rows(tmp_path, "SELECT * FROM uploads") == []


def test_upload_needs_a_file_field(start):
    client = start()
    response = client.post(
        "/api/uploads", files={"other": ("a.png", png(10, 10), "image/png")}, headers=CSRF
    )
    assert response.status_code == 422 and response.json()["reason"] == "invalid_request"
    not_multipart = client.post("/api/uploads", json={"file": "x"}, headers=CSRF)
    assert not_multipart.status_code == 415


def test_upload_requires_session_and_csrf(start, tmp_path):
    signed_out = start(sign_in=False)
    response = upload(signed_out, png(10, 10))
    assert response.status_code == 401 and response.json() == {"reason": "unauthenticated"}

    client = start()
    for headers in ({}, {"Origin": ORIGIN}, {"X-Hearthkeep": "1"},
                    {"X-Hearthkeep": "1", "Origin": "https://evil.example"}):  # fmt: skip
        response = client.post(
            "/api/uploads", files={"file": ("a.png", png(10, 10), "image/png")}, headers=headers
        )
        assert response.status_code == 403 and response.json() == {"reason": "csrf"}
    assert rows(tmp_path, "SELECT * FROM uploads") == []


# ---- GET /api/uploads/{id}/file ---------------------------------------------------------------


def test_upload_file_rejects_bad_ids_and_requires_session(start, tmp_path):
    client = start()
    upload_id = upload(client, png(10, 10)).json()["id"]
    (tmp_path / "secret.png").write_bytes(png(10, 10))
    for path in (
        "/api/uploads/..%2F..%2Fsecret/file",
        "/api/uploads/..%2F..%2Fhearthkeep.db/file",
        "/api/uploads/nope/file",
        f"/api/uploads/{'a' * 33}/file",
        "/api/uploads/a.b/file",
        f"/api/uploads/{upload_id}.png/file",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"reason": "not_found"}, path

    # A row pointing outside the uploads folder is never followed.
    conn = connect(tmp_path / DB_FILE)
    conn.execute("UPDATE uploads SET file_name = ? WHERE id = ?", ("../../secret.png", upload_id))
    conn.close()
    assert client.get(f"/api/uploads/{upload_id}/file").status_code == 404

    signed_out = start(sign_in=False)
    response = signed_out.get(f"/api/uploads/{upload_id}/file")
    assert response.status_code == 401 and response.json() == {"reason": "unauthenticated"}


# ---- fix round 1 -------------------------------------------------------------------------------

STILL_ONLY = "Hearthkeep can only use still photos."


def apng(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    actl = struct.pack(">II", 2, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"acTL", actl)
        + _chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + _chunk(b"IEND", b"")
    )  # fmt: skip


def animated_webp(width: int, height: int) -> bytes:
    flags = bytes([0x02, 0, 0, 0])  # the animation bit
    data = flags + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return _riff(b"VP8X" + struct.pack("<I", len(data)) + data)


def test_png_with_actl_after_idat_is_still_a_still_photo(start):
    # Only an acTL before the first IDAT makes an animated PNG.
    ihdr = struct.pack(">IIBBBBB", 20, 20, 8, 2, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(b"\x00"))
        + _chunk(b"acTL", struct.pack(">II", 2, 0)) + _chunk(b"IEND", b"")
    )  # fmt: skip
    assert upload(start(), data).status_code == 201


@pytest.mark.parametrize("data", [apng(64, 64), animated_webp(64, 64)])
def test_upload_rejects_animated_images(start, tmp_path, data):
    response = upload(start(), data)
    assert response.status_code == 415
    assert response.json() == {"reason": "animated", "message": STILL_ONLY}
    assert rows(tmp_path, "SELECT * FROM uploads") == []


def _jpeg_tail(width=640, height=480) -> bytes:
    return b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + bytes(9) + b"\xff\xd9"


def test_jpeg_scan_is_bounded():
    # A frame header after a long run of fill bytes, or after very many segments, is not looked
    # for: the scan stops at 2 MiB or 512 segments.
    fill = b"\xff\xd8" + b"\xff" * (2 * MB + 10) + _jpeg_tail()
    assert image_info(fill) is None
    comments = b"\xff\xd8" + b"\xff\xfe\x00\x02" * 600 + _jpeg_tail()
    assert image_info(comments) is None
    few = b"\xff\xd8" + b"\xff\xfe\x00\x02" * 100 + b"\xff" * 1000 + _jpeg_tail()
    assert image_info(few).width == 640


def test_upload_rejects_jpeg_with_endless_fill_bytes(start):
    data = b"\xff\xd8" + b"\xff" * (3 * MB) + _jpeg_tail()
    response = upload(start(), data)
    assert response.status_code == 415 and response.json()["reason"] == "unsupported_type"


def test_upload_keeps_file_bytes_that_contain_the_boundary(start, tmp_path):
    client = start()
    boundary = b"hkBoundary42"
    # The boundary text inside the photo, without a CRLF in front, is not a delimiter.
    data = png(10, 10) + b"--" + boundary + b"--tail\r\nmore--" + boundary + b"x"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="a.png"\r\n'
        b"Content-Type: image/png\r\n\r\n" + data + b"\r\n--" + boundary + b"--\r\n"
    )
    response = client.post(
        "/api/uploads",
        content=body,
        headers={**CSRF, "Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["id"]
    assert (tmp_path / "workspace" / "uploads" / f"{upload_id}.png").read_bytes() == data


def test_upload_parses_and_writes_off_the_event_loop(start, monkeypatch):
    import hearthkeep.api.routes_uploads as routes

    offloaded: list[str] = []
    real = routes.run_in_threadpool

    async def recording(func, *args, **kwargs):
        offloaded.append(func.__name__)
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(routes, "run_in_threadpool", recording)
    assert upload(start(), png(10, 10)).status_code == 201
    assert offloaded == ["parse_upload", "write_upload_file"]
