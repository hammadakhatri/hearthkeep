"""Reading an upload: a size-capped request body and the multipart field named `file`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import Request

from hearthkeep.api.common import ApiError

BOUNDARY = re.compile(r'boundary="?([^";]{1,70})"?', re.IGNORECASE)
_FIELD_NAME = re.compile(rb'(?:^|;)\s*name="([^"]*)"', re.IGNORECASE)
_FILENAME = re.compile(rb'(?:^|;)\s*filename="([^"]*)"', re.IGNORECASE)
_FILENAME_STAR = re.compile(rb"(?:^|;)\s*filename\*=UTF-8''([^;\s]*)", re.IGNORECASE)
_FORM_ESCAPES = ((b"%22", b'"'), (b"%0D", bytes([13])), (b"%0A", bytes([10])))


@dataclass
class FilePart:
    data: bytes
    filename: str | None


def multipart_boundary(content_type: str) -> bytes | None:
    """The boundary of a `multipart/form-data` content type, or None for anything else."""
    match = BOUNDARY.search(content_type)
    if not content_type.lower().startswith("multipart/form-data") or match is None:
        return None
    return match.group(1).encode("latin-1")


async def read_limited_body(request: Request, max_bytes: int, too_big: ApiError) -> bytes:
    """The request body, read in chunks and abandoned (raising `too_big`) as soon as it is over
    the limit; a declared length over the limit is refused before anything is read."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise too_big
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise too_big
    return bytes(body)


def _filename(disposition: bytes) -> str | None:
    star = _FILENAME_STAR.search(disposition)
    if star:
        return unquote(star.group(1).decode("latin-1"), encoding="utf-8", errors="replace")
    plain = _FILENAME.search(disposition)
    if plain:
        # Browsers send `"`, CR and LF in a name as %22, %0D and %0A (WHATWG form encoding);
        # nothing else is percent-decoded.
        raw = plain.group(1)
        for escaped, char in _FORM_ESCAPES:
            raw = raw.replace(escaped, char)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
    return None


def parse_file_part(body: bytes, boundary: bytes) -> FilePart | None:
    """The multipart field named `file` and its file name, or None (RFC 7578 / RFC 2046).

    The first delimiter is `--boundary` at the very start (or after a preamble); every later
    one is CRLF + `--boundary`, so boundary text inside the file without a CRLF in front is
    content. `filename*=UTF-8''…` is preferred over `filename="…"`."""
    dash = b"--" + boundary
    if body.startswith(dash):
        rest = body[len(dash) :]
    else:
        start = body.find(b"\r\n" + dash)
        if start < 0:
            return None
        rest = body[start + 2 + len(dash) :]
    for part in rest.split(b"\r\n" + dash):
        if part.startswith(b"--"):  # the closing delimiter
            break
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-disposition":
                match = _FIELD_NAME.search(value)
                if match and match.group(1) == b"file":
                    return FilePart(content, _filename(value))
    return None
