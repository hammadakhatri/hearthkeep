"""What kind of file the owner attached, decided from its content (never from its name)."""

from __future__ import annotations

import codecs
import json
import lzma
import re
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from hearthkeep.files.cfb import MAGIC as CFB_MAGIC
from hearthkeep.files.cfb import cfb_stream_names

FILE_KINDS = ("csv", "xlsx", "xls", "pdf", "txt", "json")
MIME: dict[str, str] = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "json": "application/json",
}
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 5
MAX_XLSX_UNPACKED = 1024**3
MAX_NAME_CHARS = 120

TOO_BIG = "That file is too big. Files can be up to 50 MB."
TOO_MANY = "Attach up to 5 files at a time."
MESSAGES = {
    "unsupported": (
        "Hearthkeep can't read that file. Attach a CSV, Excel (.xlsx or .xls), PDF, text or "
        "JSON file."
    ),
    "mismatch": (
        "That file's name says {ext}, but its contents aren't. Check the file and attach it again."
    ),
    "macros": (
        "Excel files with macros can't be attached. Save it as .xlsx without macros and attach "
        "it again."
    ),
    "password_protected": (
        "This file is password-protected. Remove the password and attach it again."
    ),
    "too_large_to_open": "That spreadsheet is too large to open (over 1 GB once unpacked).",
    "invalid_json": "That JSON file can't be read: it isn't valid JSON.",
    "damaged": "Hearthkeep couldn't open that file. It may be damaged.",
    "too_large": TOO_BIG,
}

_HEAD_BYTES = 64 * 1024
_PDF_HEADER_WINDOW = 1024
_PDF_TAIL_BYTES = 64 * 1024
_CONTENT_TYPES_READ = 1024 * 1024
# Larger JSON files get a bounded check here; the sandbox's inspection reads them fully.
_JSON_PARSE_BYTES = 5 * 1024 * 1024
MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_DIRECTORY_BYTES = 16 * 1024 * 1024
# The end of central directory record (22 bytes plus a comment of up to 64 KB), and the ZIP64
# locator (20 bytes, just before it) and ZIP64 end record (56 bytes) it may point to.
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_BYTES = 22
_EOCD_SEARCH_BYTES = 64 * 1024 + _EOCD_BYTES
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_LOCATOR_BYTES = 20
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_EOCD_BYTES = 56
MAX_EXT_CHARS = 10
_ZIP_MAGIC = b"PK\x03\x04"
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_BINARY_EXTS = ("xlsx", "xls", "pdf")
_TEXT_EXTS = ("csv", "txt", "json")
# Control characters and the characters Windows forbids in names. Every format character
# (Unicode category Cf: bidi overrides and isolates, zero-width characters, soft hyphens, tags)
# is removed too, as are the line and paragraph separators (U+2028, U+2029): they can
# disguise a name (an override can make "xcs.pdf" read "fdp.scx").
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f\\/:*?"<>|]')
_HIDDEN_CATEGORIES = frozenset({"Cf", "Zl", "Zp"})
_NOT_EXT_CHARS = re.compile(r"[^A-Za-z0-9]")


class FileRefused(Exception):
    """A file Hearthkeep won't keep: `reason` for the API, `message` for the owner."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message if message is not None else MESSAGES[reason]


@dataclass(frozen=True)
class FileCheck:
    kind: str
    mime: str
    ext: str


def _found(kind: str) -> FileCheck:
    return FileCheck(kind=kind, mime=MIME[kind], ext=kind)


def _last_component(name: str) -> str:
    return re.split(r"[\\/]", name)[-1]


def _split_ext(name: str) -> tuple[str, str]:
    """(stem, extension) with the extension cleaned to at most 10 ASCII letters and digits;
    "" when there is none."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem.strip(". "):
        return name, ""
    return stem, _NOT_EXT_CHARS.sub("", ext)[:MAX_EXT_CHARS]


def _mismatch(ext: str) -> FileRefused:
    return FileRefused("mismatch", MESSAGES["mismatch"].format(ext=f".{ext}"))


def _binary(kind: str, claimed: str) -> FileCheck:
    """A binary kind: an extension, when there is one, must be the kind's own."""
    if claimed and claimed.lower() != kind:
        raise _mismatch(claimed)
    return _found(kind)


def _check_pdf(path: Path, size: int) -> None:
    with open(path, "rb") as fh:
        fh.seek(max(0, size - _PDF_TAIL_BYTES))
        if b"%%EOF" not in fh.read(_PDF_TAIL_BYTES):
            raise FileRefused("damaged")


def _check_zip_directory(path: Path) -> None:
    """Refuse (too_large_to_open) a zip whose end record declares more than 10,000 entries or a
    central directory over 16 MB, before zipfile reads that directory into memory. A ZIP64 end
    record, when a locator just before the classic one points to it, gives the real counts."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        fh.seek(max(0, size - _EOCD_SEARCH_BYTES))
        tail = fh.read(_EOCD_SEARCH_BYTES)
        at = tail.rfind(_EOCD_SIGNATURE)
        if at < 0 or len(tail) - at < _EOCD_BYTES:
            raise FileRefused("damaged")
        entries, directory_size = struct.unpack_from("<HI", tail, at + 10)
        locator_at = at - _ZIP64_LOCATOR_BYTES
        if locator_at >= 0 and tail[locator_at : locator_at + 4] == _ZIP64_LOCATOR_SIGNATURE:
            (zip64_at,) = struct.unpack_from("<Q", tail, locator_at + 8)
            if zip64_at >= size:
                raise FileRefused("damaged")
            fh.seek(zip64_at)
            record = fh.read(_ZIP64_EOCD_BYTES)
            if len(record) != _ZIP64_EOCD_BYTES or not record.startswith(_ZIP64_EOCD_SIGNATURE):
                raise FileRefused("damaged")
            entries, directory_size = struct.unpack_from("<QQ", record, 32)
    if entries > MAX_ZIP_ENTRIES or directory_size > MAX_ZIP_DIRECTORY_BYTES:
        raise FileRefused("too_large_to_open")


def _check_zip(path: Path) -> None:
    """Raise unless the zip is a macro-free Excel workbook within the entry and unpacked
    limits. Part names and content types are compared in lowercase."""
    _check_zip_directory(path)
    try:
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise FileRefused("too_large_to_open")
            if any(e.flag_bits & 0x1 for e in entries):
                raise FileRefused("password_protected")
            by_name = {e.filename.lower(): e for e in entries}
            if "xl/vbaproject.bin" in by_name:
                raise FileRefused("macros")
            types = by_name.get("[content_types].xml")
            if types is not None:
                with zf.open(types) as fh:
                    declared = fh.read(_CONTENT_TYPES_READ).lower()
                if b"macroenabled" in declared or b"vbaproject" in declared:
                    raise FileRefused("macros")
            if "xl/workbook.xml" not in by_name:
                raise FileRefused("unsupported")
            if sum(e.file_size for e in entries) > MAX_XLSX_UNPACKED:
                raise FileRefused("too_large_to_open")
    # NotImplementedError is a RuntimeError: it must be caught first.
    except NotImplementedError:  # an unsupported compression method (Deflate64, say)
        raise FileRefused("unsupported") from None
    except RuntimeError:  # an encrypted entry the flag check missed
        raise FileRefused("password_protected") from None
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        lzma.LZMAError,
        OSError,
        EOFError,
        ValueError,
    ):
        raise FileRefused("damaged") from None


def _check_cfb(path: Path) -> None:
    try:
        names = cfb_stream_names(path)
    except (ValueError, OSError):
        raise FileRefused("damaged") from None
    if "EncryptedPackage" in names:
        raise FileRefused("password_protected")
    if "_VBA_PROJECT_CUR" in names:
        raise FileRefused("macros")
    if not names & {"Workbook", "Book"}:
        raise FileRefused("unsupported")


def _text_encoding(head: bytes, at_end: bool) -> str | None:
    """The encoding the start of the file decodes in (UTF-8, UTF-8 with a BOM, UTF-16 with a
    BOM, else cp1252), or None when it isn't text."""
    if head.startswith(_UTF16_BOMS):
        candidates = ["utf-16"]
    elif b"\x00" in head:
        return None
    elif head.startswith(codecs.BOM_UTF8):
        candidates = ["utf-8-sig"]
    else:
        candidates = ["utf-8", "cp1252"]
    for encoding in candidates:
        decoder = codecs.getincrementaldecoder(encoding)()
        try:
            # Not final unless the whole file was read: the cut may split a character.
            decoder.decode(head, final=at_end)
        except UnicodeDecodeError:
            continue
        return encoding
    return None


def _looks_like_json(path: Path, size: int, head: bytes, encoding: str) -> bool:
    """Up to 5 MB: a full parse. Larger: text whose first non-blank character opens an object
    or an array (the sandbox's inspection reads it fully)."""
    if size > _JSON_PARSE_BYTES:
        start = codecs.getincrementaldecoder(encoding)().decode(head, final=False).lstrip()
        return start[:1] in ("{", "[")
    try:
        json.loads(path.read_bytes().decode(encoding))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return True


def check_file(path: Path, claimed_name: str) -> FileCheck:
    """The file's kind, or FileRefused. Runs on a worker thread: it reads the file. A name
    with no extension takes the kind its content shows."""
    _, claimed = _split_ext(_last_component(claimed_name))
    lowered = claimed.lower()
    if lowered == "xlsm":
        raise FileRefused("macros")
    size = path.stat().st_size
    with open(path, "rb") as fh:
        head = fh.read(_HEAD_BYTES)

    if b"%PDF-" in head[:_PDF_HEADER_WINDOW]:
        _check_pdf(path, size)
        return _binary("pdf", claimed)
    if head.startswith(_ZIP_MAGIC):
        _check_zip(path)
        return _binary("xlsx", claimed)
    if head.startswith(CFB_MAGIC):
        _check_cfb(path)
        return _binary("xls", claimed)

    encoding = _text_encoding(head, at_end=size <= len(head))
    if encoding is None:
        raise FileRefused("unsupported")
    if lowered in _BINARY_EXTS:
        raise _mismatch(claimed)
    if lowered == "json":
        if not _looks_like_json(path, size, head, encoding):
            raise FileRefused("invalid_json")
        return _found("json")
    if lowered == "txt":
        return _found("txt")
    if lowered not in _TEXT_EXTS and _looks_like_json(path, size, head, encoding):
        return _found("json")
    return _found("csv")


def clean_file_name(raw: str | None, ext: str) -> str:
    """A name safe to show and to send back: the last path component, without control,
    reserved, bidi or format characters, NFC, at most 120 characters, ending in the detected
    kind's extension `.<ext>` (replacing any other, or added when there is none; the owner's
    own spelling, such as `.TXT`, is kept when it is that extension). Empty becomes
    `file.<ext>`."""
    kept = _UNSAFE_NAME_CHARS.sub("", _last_component(raw or ""))
    kept = "".join(c for c in kept if unicodedata.category(c) not in _HIDDEN_CATEGORIES)
    name = unicodedata.normalize("NFC", kept)
    stem, dot, claimed = name.strip().rpartition(".")
    if not dot or not stem.strip(". "):
        stem, claimed = name, ""
    suffix = claimed if claimed.lower() == ext else ext
    stem = stem.strip().strip(".").strip() or "file"
    return f"{stem[: MAX_NAME_CHARS - len(suffix) - 1].rstrip()}.{suffix}"
