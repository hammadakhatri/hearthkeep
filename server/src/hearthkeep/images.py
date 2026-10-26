"""Generated image records and the images folder, and the owner's uploaded photos."""

from __future__ import annotations

import contextlib
import math
import os
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe

from hearthkeep import db

ASPECTS: dict[str, tuple[int, int]] = {
    "square": (1024, 1024),
    "landscape": (1536, 864),
    "portrait": (864, 1536),
    "banner": (1536, 640),
}
MIN_SIDE, MAX_SIDE, STEP = 512, 2048, 16
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
VP8_START_CODE = b"\x9d\x01\x2a"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_SIDE = 4096
UPLOAD_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# An upload id: what the server hands out (token_urlsafe), and all a lookup will accept.
UPLOAD_ID_PATTERN = "^[A-Za-z0-9_-]{1,32}$"
UPLOAD_ID = re.compile(UPLOAD_ID_PATTERN.strip("^$"))
ATTACHED_PHOTO_LINE = re.compile(r"Attached photo: " + UPLOAD_ID.pattern)
# The JPEG header scan gives up after this much data or this many segments.
JPEG_SCAN_BYTES = 2 * 1024 * 1024
JPEG_SCAN_SEGMENTS = 512


def validate_size(width: int, height: int) -> None:
    """Raise ValueError unless both sides are multiples of 16 within 512..2048."""
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        if value < MIN_SIDE or value > MAX_SIDE or value % STEP:
            raise ValueError(
                f"{name} {value} must be a multiple of {STEP} between {MIN_SIDE} and {MAX_SIDE}"
            )


def nearest_aspect(width: int, height: int) -> str:
    """The ASPECTS key whose shape is closest to width x height (by log ratio)."""
    ratio = math.log(width / height)
    return min(ASPECTS, key=lambda k: abs(math.log(ASPECTS[k][0] / ASPECTS[k][1]) - ratio))


def without_photo_lines(text: str) -> str:
    """The text with its `Attached photo: <id>` lines left out (exact matches only, as the web
    app shows them)."""
    lines = text.split("\n")
    return "\n".join(line for line in lines if not ATTACHED_PHOTO_LINE.fullmatch(line))


@dataclass(frozen=True)
class ImageInfo:
    mime: str
    ext: str
    width: int
    height: int
    animated: bool = False


def image_type(data: bytes) -> str | None:
    """The type ("image/png", "image/jpeg" or "image/webp") from the first bytes, else None."""
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(JPEG_SIGNATURE):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _png_size(data: bytes) -> tuple[int, int, bool] | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    # An APNG has an acTL chunk before its first IDAT.
    animated, i = False, 8
    while i + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[i : i + 8])
        if kind == b"IDAT":
            break
        if kind == b"acTL":
            animated = True
            break
        i += 12 + length
    return width, height, animated


# Start-of-frame markers carry the size; C4 (huffman), C8 (reserved) and CC (arithmetic) don't.
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_JPEG_NO_LENGTH = frozenset({0x01, *range(0xD0, 0xD8)})


def _jpeg_size(data: bytes) -> tuple[int, int, bool] | None:
    i, segments = 2, 0
    end = min(len(data), JPEG_SCAN_BYTES)
    while i + 4 <= end:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        segments += 1
        if segments > JPEG_SCAN_SEGMENTS:
            return None
        if marker in _JPEG_NO_LENGTH:
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image or start of scan before any frame header
            return None
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if i + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height, False
        i += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int, bool] | None:
    kind, body = data[12:16], data[20:]
    if kind == b"VP8X" and len(body) >= 10:
        width = int.from_bytes(body[4:7], "little") + 1
        height = int.from_bytes(body[7:10], "little") + 1
        return width, height, bool(body[0] & 0x02)  # the animation flag
    if kind == b"VP8L" and len(body) >= 5 and body[0] == 0x2F:
        (bits,) = struct.unpack("<I", body[1:5])
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, False
    if kind == b"VP8 " and len(body) >= 10 and body[3:6] == VP8_START_CODE:
        width, height = struct.unpack("<HH", body[6:10])
        return width & 0x3FFF, height & 0x3FFF, False
    return None


def image_info(data: bytes) -> ImageInfo | None:
    """Type, size and whether it is animated, for a PNG, JPEG or WebP, read from its headers
    only (nothing is decoded). None for any other type, or a file whose headers can't be read."""
    mime = image_type(data)
    if mime is None:
        return None
    reader = {"image/png": _png_size, "image/jpeg": _jpeg_size, "image/webp": _webp_size}[mime]
    try:
        found = reader(data)
    except struct.error:
        return None
    if found is None or found[0] < 1 or found[1] < 1:
        return None
    width, height, animated = found
    return ImageInfo(mime, UPLOAD_EXTENSIONS[mime], width, height, animated)


@dataclass(frozen=True)
class StagedUpload:
    id: str
    ext: str
    tmp: Path


@dataclass
class UploadRecord:
    id: str
    mime: str
    width: int
    height: int
    created_at: str


@dataclass
class ImageRecord:
    id: str
    chat_id: str | None
    turn_id: str | None
    model_id: str
    prompt: str
    width: int
    height: int
    seed: int
    created_at: str


_COLUMNS = "id, chat_id, turn_id, model_id, prompt, width, height, seed, created_at"


def _record(row: sqlite3.Row) -> ImageRecord:
    return ImageRecord(**{k: row[k] for k in row.keys() if k != "file_name"})


class ImageStore:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path) -> None:
        self._conn = conn
        self._dir = Path(data_dir) / "workspace" / "output" / "images"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._uploads = Path(data_dir) / "workspace" / "uploads"
        self._uploads.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        png: bytes,
        *,
        chat_id: str | None,
        turn_id: str | None,
        model_id: str,
        prompt: str,
        width: int,
        height: int,
        seed: int,
    ) -> ImageRecord:
        if not png.startswith(PNG_SIGNATURE):
            raise ValueError("image data is not a PNG")
        image_id = token_urlsafe(12)
        file_name = f"{image_id}.png"
        final = self._dir / file_name
        tmp = self._dir / f"{file_name}.tmp"
        record = ImageRecord(
            image_id, chat_id, turn_id, model_id, prompt, width, height, seed, db.utcnow()
        )
        placed = False
        try:
            with db.transaction(self._conn):
                # Insert first so a failed insert never touches an existing file.
                self._conn.execute(
                    f"INSERT INTO images ({_COLUMNS}, file_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*(getattr(record, c.strip()) for c in _COLUMNS.split(",")), file_name),
                )
                tmp.write_bytes(png)
                os.replace(tmp, final)
                placed = True
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            if placed:
                with contextlib.suppress(OSError):
                    final.unlink(missing_ok=True)
            raise
        return record

    def list(self, limit: int = 60, before: str | None = None) -> list[ImageRecord]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        sql = f"SELECT {_COLUMNS} FROM images"
        params: list[object] = []
        if before is not None:
            sql += " WHERE created_at < ?"
            params.append(before)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [_record(r) for r in self._conn.execute(sql, params)]

    def get(self, image_id: str) -> ImageRecord | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        return _record(row) if row else None

    def path(self, image_id: str) -> Path | None:
        row = self._conn.execute(
            "SELECT file_name FROM images WHERE id = ?", (image_id,)
        ).fetchone()
        return _inside(self._dir, row["file_name"]) if row else None

    # ---- the owner's photos ----

    def save_upload(
        self, data: bytes, *, mime: str, ext: str, width: int, height: int
    ) -> UploadRecord:
        """Store an uploaded photo under a new random id. The caller has checked its type and
        size; the file name is built from the id and the detected type only."""
        staged = self.write_upload_file(data, ext)
        return self.record_upload(staged, mime=mime, width=width, height=height)

    def write_upload_file(self, data: bytes, ext: str) -> StagedUpload:
        """Step 1 of saving a photo: write it to a temporary file under a new random id. Touches
        no database, so it may run on a worker thread (the connection may not)."""
        if ext not in UPLOAD_EXTENSIONS.values():
            raise ValueError("unsupported upload type")
        upload_id = token_urlsafe(12)
        tmp = self._uploads / f"{upload_id}.{ext}.tmp"
        try:
            tmp.write_bytes(data)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        return StagedUpload(upload_id, ext, tmp)

    def record_upload(
        self, staged: StagedUpload, *, mime: str, width: int, height: int
    ) -> UploadRecord:
        """Step 2: the row, and the file moved into place, together; the temporary file is
        removed if either fails."""
        if UPLOAD_EXTENSIONS.get(mime) != staged.ext:
            with contextlib.suppress(OSError):
                staged.tmp.unlink(missing_ok=True)
            raise ValueError("unsupported upload type")
        file_name = f"{staged.id}.{staged.ext}"
        final = self._uploads / file_name
        record = UploadRecord(staged.id, mime, width, height, db.utcnow())
        placed = False
        try:
            with db.transaction(self._conn):
                self._conn.execute(
                    "INSERT INTO uploads (id, file_name, mime, width, height, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (staged.id, file_name, mime, width, height, record.created_at),
                )
                os.replace(staged.tmp, final)
                placed = True
        except BaseException:
            with contextlib.suppress(OSError):
                staged.tmp.unlink(missing_ok=True)
            if placed:
                with contextlib.suppress(OSError):
                    final.unlink(missing_ok=True)
            raise
        return record

    def get_upload(self, upload_id: str) -> UploadRecord | None:
        row = self._conn.execute(
            "SELECT id, mime, width, height, created_at FROM uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        return UploadRecord(**dict(row)) if row else None

    def upload_path(self, upload_id: str) -> Path | None:
        """The photo's file, only ever inside the uploads folder; None if unknown or gone."""
        row = self._conn.execute(
            "SELECT file_name FROM uploads WHERE id = ?", (upload_id,)
        ).fetchone()
        return _inside(self._uploads, row["file_name"]) if row else None


def _inside(folder: Path, file_name: str) -> Path | None:
    candidate = (folder / file_name).resolve()
    if candidate.parent != folder.resolve() or not candidate.is_file():
        return None
    return candidate
