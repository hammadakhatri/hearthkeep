"""Edit the owner's `models.toml` / `config.toml` without losing their lines.

A line-based editor: it changes only top-level `key = value` lines and `[table]` entries, keeps
every other line, comment, line ending and the file's encoding, and re-parses the result with
`tomllib` before anything is written. Shapes it can't edit safely (dotted keys, inline tables,
arrays of tables, repeated keys) raise `TomlEditError` and leave the file as it was.
"""

from __future__ import annotations

import copy
import math
import re
import shutil
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from hearthkeep.runtime.supervisor import _write_atomic

_BOM = b"\xef\xbb\xbf"
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}

Scalar = str | int | float | bool


class TomlEditError(Exception):
    """The file can't be edited safely. The file is untouched."""


@dataclass
class _Header:
    path: tuple[str, ...]
    is_array: bool
    line: int


@dataclass
class _Entry:
    section: tuple[str, ...]
    key: tuple[str, ...]
    start: int  # first line
    end: int  # last line (a multi-line value spans several)
    vstart: int  # column in `start` where the value begins
    vend: int  # column in `end` just after the value (before any trailing comment)


# ---------------------------------------------------------------- rendering


def _basic(text: str) -> str:
    out = []
    for ch in text:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _string(text: str) -> str:
    """A literal string (no escapes to misread in a Windows path), or a basic one when the text
    holds a `'` or a control character."""
    if "'" in text or any((ord(c) < 0x20 and c != "\t") or ord(c) == 0x7F for c in text):
        return _basic(text)
    return f"'{text}'"


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    raise TomlEditError(f"unsupported value type: {type(value).__name__}")


def _key_text(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _basic(key)


# ---------------------------------------------------------------- scanning


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _quoted_end(s: str, i: int) -> int:
    """Index just past the single-line quoted string starting at `s[i]`."""
    quote = s[i]
    j = i + 1
    while j < len(s):
        if quote == '"' and s[j] == "\\":
            j += 2
            continue
        if s[j] == quote:
            return j + 1
        j += 1
    raise TomlEditError("unterminated string")


def _parse_key(s: str, i: int) -> tuple[tuple[str, ...], int]:
    parts: list[str] = []
    while True:
        i = _skip_ws(s, i)
        if i < len(s) and s[i] in "\"'":
            j = _quoted_end(s, i)
            raw = s[i:j]
        else:
            m = _BARE_KEY.match(s, i)
            if not m:
                raise TomlEditError("unreadable key")
            j = m.end()
            raw = m.group()
        try:
            parts.append(next(iter(tomllib.loads(f"{raw} = 0"))))
        except (tomllib.TOMLDecodeError, StopIteration) as exc:
            raise TomlEditError("unreadable key") from exc
        i = _skip_ws(s, j)
        if i < len(s) and s[i] == ".":
            i += 1
            continue
        return tuple(parts), i


def _scan_value(lines: list[str], ln: int, col: int) -> tuple[int, int]:
    """(line, column) just past the value that starts at `lines[ln][col]`, following strings and
    brackets across lines. A trailing comment is not part of the value."""
    depth = 0
    last = (ln, col)
    i = col
    while True:
        s = lines[ln]
        while i < len(s):
            c = s[i]
            if c in " \t":
                i += 1
                continue
            if c == "#":
                break
            if s.startswith('"""', i) or s.startswith("'''", i):
                delim = s[i : i + 3]
                j, k = ln, i + 3
                while True:
                    t = lines[j]
                    while k < len(t):
                        if delim == '"""' and t[k] == "\\":
                            k += 2
                            continue
                        if t.startswith(delim, k):
                            k += 3
                            while k < len(t) and t[k] == delim[0]:
                                k += 1  # up to two quotes that belong to the string
                            break
                        k += 1
                    else:
                        j += 1
                        k = 0
                        if j >= len(lines):
                            raise TomlEditError("unterminated multi-line string")
                        continue
                    break
                ln, i, s = j, k, lines[j]
                last = (ln, i)
                continue
            if c in "\"'":
                i = _quoted_end(s, i)
            else:
                if c in "[{":
                    depth += 1
                elif c in "]}":
                    depth -= 1
                i += 1
            last = (ln, i)
        if depth > 0:
            ln += 1
            i = 0
            if ln >= len(lines):
                raise TomlEditError("unterminated array")
            continue
        return last


def _scan(lines: list[str]) -> tuple[list[_Header], list[_Entry]]:
    headers: list[_Header] = []
    entries: list[_Entry] = []
    section: tuple[str, ...] = ()
    ln = 0
    while ln < len(lines):
        s = lines[ln]
        i = _skip_ws(s, 0)
        if i >= len(s) or s[i] == "#":
            ln += 1
            continue
        if s[i] == "[":
            is_array = s.startswith("[[", i)
            path, j = _parse_key(s, i + (2 if is_array else 1))
            closer = "]]" if is_array else "]"
            if not s.startswith(closer, j):
                raise TomlEditError("unreadable table header")
            rest = _skip_ws(s, j + len(closer))
            if rest < len(s) and s[rest] != "#":
                raise TomlEditError("unreadable table header")
            headers.append(_Header(path, is_array, ln))
            section = path
            ln += 1
            continue
        key, j = _parse_key(s, i)
        j = _skip_ws(s, j)
        if j >= len(s) or s[j] != "=":
            raise TomlEditError("unreadable line")
        vstart = _skip_ws(s, j + 1)
        end_ln, vend = _scan_value(lines, ln, vstart)
        entries.append(_Entry(section, key, ln, end_ln, vstart, vend))
        ln = end_ln + 1
    return headers, entries


def _locate(
    headers: list[_Header], entries: list[_Entry], section: tuple[str, ...], key: str
) -> _Entry | None:
    """The one plain `key = value` entry for `key` in `section`, None when absent. Raises when the
    key is present in a shape this editor won't touch."""
    depth = len(section)
    for h in headers:
        if h.path[: depth + 1] == (*section, key):
            raise TomlEditError(f"'{key}' is a table or an array of tables")
    found = [e for e in entries if e.section == section and e.key[:1] == (key,)]
    if any(len(e.key) > 1 for e in found):
        raise TomlEditError(f"'{key}' is written as a dotted key")
    if len(found) > 1:
        raise TomlEditError(f"'{key}' appears more than once")
    if not found:
        return None
    return found[0]


# ---------------------------------------------------------------- editing


class _Doc:
    """The file's lines (without their line endings) plus the ending to use for new ones."""

    def __init__(self, text: str) -> None:
        pieces = re.split(r"(?<=\n)", text)
        if pieces and pieces[-1] == "":
            pieces.pop()
        self.lines: list[str] = []
        self.eols: list[str] = []
        for piece in pieces:
            if piece.endswith("\r\n"):
                self.lines.append(piece[:-2])
                self.eols.append("\r\n")
            elif piece.endswith("\n"):
                self.lines.append(piece[:-1])
                self.eols.append("\n")
            else:
                self.lines.append(piece)
                self.eols.append("")
        crlf = text.count("\r\n")
        self.newline = "\r\n" if crlf and crlf * 2 > text.count("\n") else "\n"

    def text(self) -> str:
        return "".join(a + b for a, b in zip(self.lines, self.eols, strict=True))

    def scan(self) -> tuple[list[_Header], list[_Entry]]:
        return _scan(self.lines)

    def insert(self, at: int, new: list[str]) -> None:
        if at > 0 and not self.eols[at - 1]:
            self.eols[at - 1] = self.newline
        self.lines[at:at] = new
        self.eols[at:at] = [self.newline] * len(new)

    def replace_value(self, e: _Entry, rendered: str) -> None:
        head = self.lines[e.start][: e.vstart]
        tail = self.lines[e.end][e.vend :]
        self.lines[e.start : e.end + 1] = [head + rendered + tail]
        self.eols[e.start : e.end + 1] = [self.eols[e.end]]

    def delete(self, e: _Entry) -> None:
        del self.lines[e.start : e.end + 1]
        del self.eols[e.start : e.end + 1]

    def set_top(self, key: str, value: object) -> None:
        headers, entries = self.scan()
        existing = _locate(headers, entries, (), key)
        rendered = _value(value)
        if existing is not None:
            if self.lines[existing.start][existing.vstart : existing.vstart + 1] == "{":
                raise TomlEditError(f"'{key}' is an inline table")
            self.replace_value(existing, rendered)
            return
        line = f"{key} = {rendered}"
        top = [e for e in entries if e.section == ()]
        if top:
            self.insert(top[-1].end + 1, [line])
        elif headers:
            at = min(h.line for h in headers)
            while at > 0 and self.lines[at - 1].lstrip().startswith("#"):
                at -= 1  # keep a comment that belongs to the first table with that table
            self.insert(at, [line, ""])
        else:
            self.insert(len(self.lines), [line])

    def remove_top(self, key: str) -> None:
        headers, entries = self.scan()
        existing = _locate(headers, entries, (), key)
        if existing is not None:
            self.delete(existing)

    def set_table_entry(self, table: str, key: str, value: str) -> None:
        headers, entries = self.scan()
        for e in entries:
            if e.section == () and e.key[:1] == (table,):
                raise TomlEditError(f"'{table}' is not a plain table")
        if any(h.path == (table,) and h.is_array for h in headers):
            raise TomlEditError(f"'{table}' is an array of tables")
        header = next((h for h in headers if h.path == (table,)), None)
        rendered = _string(value)
        if header is None:
            if self.lines and self.lines[-1].strip():
                self.insert(len(self.lines), [""])
            self.insert(len(self.lines), [f"[{_key_text(table)}]", f"{_basic(key)} = {rendered}"])
            return
        existing = _locate(headers, entries, (table,), key)
        if existing is not None:
            if self.lines[existing.start][existing.vstart : existing.vstart + 1] == "{":
                raise TomlEditError(f"'{key}' is an inline table")
            self.replace_value(existing, rendered)
            return
        inside = [e for e in entries if e.section == (table,)]
        at = inside[-1].end + 1 if inside else header.line + 1
        self.insert(at, [f"{_basic(key)} = {rendered}"])


# ---------------------------------------------------------------- checking


def _same(a: object, b: object) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def edit_toml(
    path: Path,
    *,
    set_top: Mapping[str, Scalar] = {},
    remove_top: Iterable[str] = (),
    set_table: Mapping[str, Mapping[str, str]] = {},
    backup: Path | None = None,
) -> None:
    """Apply the changes to `path` in place; see the module docstring.

    `set_top` replaces (or appends before the first table) top-level keys, `remove_top` deletes
    them, `set_table` replaces or appends `"key" = 'value'` entries in named tables, creating a
    table that's missing at the end of the file. A missing file is created. The first time
    `backup` is given and doesn't exist yet, the original is copied there before it is replaced.
    """
    path = Path(path)
    remove = list(remove_top)
    for key in (*set_top, *remove, *set_table):
        if not _BARE_KEY.fullmatch(key):
            raise TomlEditError(f"unsupported key name: {key!r}")
    if set(set_top) & set(remove):
        raise TomlEditError("a key can't be both set and removed")

    bom = b""
    text = ""
    exists = path.exists()
    if exists:
        raw = path.read_bytes()
        if raw.startswith(_BOM):
            bom, raw = _BOM, raw[len(_BOM) :]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TomlEditError("the file isn't UTF-8 text") from exc
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"the file isn't valid TOML: {exc}") from exc

    for table in set_table:
        if table in old and not isinstance(old[table], dict):
            raise TomlEditError(f"'{table}' is not a table")

    doc = _Doc(text)
    for key, value in set_top.items():
        doc.set_top(key, value)
    for key in remove:
        doc.remove_top(key)
    for table, entries in set_table.items():
        for key, value in entries.items():
            if not isinstance(value, str):
                raise TomlEditError("table entries must be strings")
            doc.set_table_entry(table, key, value)
    new_text = doc.text()

    expected = copy.deepcopy(old)
    expected.update(set_top)
    for key in remove:
        expected.pop(key, None)
    for table, entries in set_table.items():
        expected.setdefault(table, {}).update(entries)
    try:
        result = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"the edit would not be valid TOML: {exc}") from exc
    if not _same(result, expected):
        raise TomlEditError("the edit would change something other than what was asked")

    if exists and new_text == text:
        return
    if backup is not None and exists and not Path(backup).exists():
        Path(backup).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, backup)
    _write_atomic(path, bom.decode("utf-8") + new_text if bom else new_text)
