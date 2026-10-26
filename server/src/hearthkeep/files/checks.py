"""What kind of file the owner attached, decided from its content (never from its name)."""

from __future__ import annotations

import codecs
import json
import re
import unicodedata
import zipfile
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
_ZIP_MAGIC = b"PK\x03\x04"
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_BINARY_EXTS = ("xlsx", "xls", "pdf")
_TEXT_EXTS = ("csv", "txt", "json")
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f-\x9f\\/:*?"<>|]')


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


def _claimed_ext(name: str) -> str:
    last = re.split(r"[\\/]", name)[-1]
    stem, dot, ext = last.rpartition(".")
    return ext.lower() if dot and stem else ""


def _mismatch(ext: str) -> FileRefused:
    return FileRefused("mismatch", MESSAGES["mismatch"].format(ext=f".{ext}" if ext else "nothing"))


def _binary(kind: str, claimed: str) -> FileCheck:
    if claimed != kind:
        raise _mismatch(claimed)
    return _found(kind)


def _check_pdf(path: Path, size: int) -> None:
    with open(path, "rb") as fh:
        fh.seek(max(0, size - _PDF_TAIL_BYTES))
        if b"%%EOF" not in fh.read(_PDF_TAIL_BYTES):
            raise FileRefused("damaged")


def _check_zip(path: Path) -> None:
    """Raise unless the zip is a macro-free Excel workbook within the unpacked limit."""
    try:
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()
            names = {e.filename for e in entries}
            if "xl/vbaProject.bin" in names:
                raise FileRefused("macros")
            if "[Content_Types].xml" in names:
                with zf.open("[Content_Types].xml") as types:
                    if b"macroEnabled" in types.read(_CONTENT_TYPES_READ):
                        raise FileRefused("macros")
            if "xl/workbook.xml" not in names:
                raise FileRefused("unsupported")
            if sum(e.file_size for e in entries) > MAX_XLSX_UNPACKED:
                raise FileRefused("too_large_to_open")
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError):
        raise FileRefused("damaged") from None
    except NotImplementedError:  # an unsupported compression method
        raise FileRefused("unsupported") from None


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


def _parses_as_json(path: Path, encoding: str) -> bool:
    try:
        json.loads(path.read_bytes().decode(encoding))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return True


def check_file(path: Path, claimed_name: str) -> FileCheck:
    """The file's kind, or FileRefused. Runs on a worker thread: it reads the file."""
    claimed = _claimed_ext(claimed_name)
    if claimed == "xlsm":
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
    if claimed in _BINARY_EXTS:
        raise _mismatch(claimed)
    if claimed == "json":
        if not _parses_as_json(path, encoding):
            raise FileRefused("invalid_json")
        return _found("json")
    if claimed == "txt":
        return _found("txt")
    if claimed not in _TEXT_EXTS and _parses_as_json(path, encoding):
        return _found("json")
    return _found("csv")


def clean_file_name(raw: str | None, ext: str) -> str:
    """A name safe to show and to send back: the last path component, without control or
    reserved characters, NFC, at most 120 characters with its extension kept; empty becomes
    `file.<ext>`."""
    last = re.split(r"[\\/]", raw or "")[-1]
    name = unicodedata.normalize("NFC", _UNSAFE_NAME_CHARS.sub("", last)).strip()
    if name.strip(". ") == "":
        return f"file.{ext}"
    if len(name) > MAX_NAME_CHARS:
        stem, dot, suffix = name.rpartition(".")
        if dot and stem and len(suffix) < 20:
            name = stem[: MAX_NAME_CHARS - len(suffix) - 1].rstrip() + "." + suffix
        else:
            name = name[:MAX_NAME_CHARS]
    return name
