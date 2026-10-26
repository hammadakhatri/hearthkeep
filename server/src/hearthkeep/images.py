"""Generated image records and the images folder."""

from __future__ import annotations

import contextlib
import os
import sqlite3
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


def validate_size(width: int, height: int) -> None:
    """Raise ValueError unless both sides are multiples of 16 within 512..2048."""
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        if value < MIN_SIDE or value > MAX_SIDE or value % STEP:
            raise ValueError(
                f"{name} {value} must be a multiple of {STEP} between {MIN_SIDE} and {MAX_SIDE}"
            )


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
        if not row:
            return None
        candidate = (self._dir / row["file_name"]).resolve()
        if candidate.parent != self._dir.resolve() or not candidate.is_file():
            return None
        return candidate
