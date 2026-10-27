"""The number check: every number, date and count in an assistant reply of a chat with files is
compared with the values this chat's sandbox runs reported.

Each mention is marked `computed` (it matches a value a run computed), `document` (it matches
only a value read from a document, with that value's page) or `unverified` (it matches nothing).
Mentions that also appear in the owner's question of that turn or in a file name of the chat get
no mark. When in doubt a mention is `unverified`: flagging a right number is better than letting
a wrong one through.

Work is bounded: at most MAX_REPORTED reported values, MAX_REPLY_CHARS characters of a reply
(or of an exempt text), MAX_PRINTED_CHARS characters of printed output in all; matching is a
binary search per mention.
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime
import itertools
import json
import logging
import math
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

from hearthkeep import db
from hearthkeep.files.store import ATTACHED_FILE_LINE, ChatFileStore
from hearthkeep.images import ATTACHED_PHOTO_LINE, normalize_newlines
from hearthkeep.sandbox.runs import RunStore

UNVERIFIED_NOTE = "Not found in any result from this chat's analysis."
MAX_REPORTED = 50_000
MAX_REPLY_CHARS = 20_000
MAX_PRINTED_CHARS = 1_000_000  # printed output scanned for numbers, over all runs
MAX_RUNS = 200

_log = logging.getLogger(__name__)

Kind = Literal["computed", "document"]


@dataclass
class Mention:
    start: int
    end: int
    text: str
    value: float | None  # as written, signed, before the scale
    decimals: int
    scale: float
    percent: bool
    date: datetime.date | None
    date_end: datetime.date | None  # the end of a range ("12–24 March")
    month_day_only: bool  # no year written: the year is ignored
    year_only: bool  # a bare year; `value` holds it too
    # Additions: the other reading of a D/M/YYYY date, and "March 2024" (a month match).
    alt_date: datetime.date | None = None
    month_year: bool = False


@dataclass
class Reported:
    value: float | None
    date: datetime.date | None
    kind: Kind
    page: int | None


@dataclass
class Mark:
    start: int
    end: int
    text: str
    kind: Literal["computed", "document", "unverified"]
    page: int | None
    note: str | None


def reply_text(parts: list[str]) -> str:
    """The reply as the web app shows it: the turn's assistant texts, blank ones left out."""
    return "\n\n".join(p for p in parts if p.strip())


# ---- extraction ----------------------------------------------------------------------------------

_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}  # fmt: skip
_MONTH = (
    r"(?<![A-Za-z])(?P<mon>(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)|(?:Sept|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?)"
    r"(?![A-Za-z])"
)
_ORD = r"(?:st|nd|rd|th)?"
_END = r"(?![A-Za-z0-9_])(?![.,]\d)"  # a number ends here: no letter, digit or decimal part
_YEAR_OPT = rf"(?:,?\s+(?P<y>\d{{4}}){_END})?"
_DASH = r"\s*[-–—]\s*"
_SIGNS = "-−–"
_CURRENCY = "£$€¥₹"

_ISO = re.compile(
    r"(?<![\w.,-])(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    r"(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?![\w])"
)
_SLASH = re.compile(r"(?<![\w/.,])(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{4})(?![\w/])")
_DAY_RANGE = re.compile(
    rf"(?<![\w.,])(?P<d1>\d{{1,2}}){_ORD}{_DASH}(?P<d2>\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MONTH}"
    rf"{_YEAR_OPT}"
)
_MONTH_RANGE = re.compile(
    rf"{_MONTH}\s+(?P<d1>\d{{1,2}}){_ORD}{_DASH}(?P<d2>\d{{1,2}}){_ORD}{_END}{_YEAR_OPT}"
)
_DAY_MONTH = re.compile(rf"(?<![\w.,])(?P<d1>\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MONTH}{_YEAR_OPT}")
_MONTH_DAY = re.compile(rf"{_MONTH}\s+(?P<d1>\d{{1,2}}){_ORD}{_END}{_YEAR_OPT}")
_MONTH_YEAR = re.compile(rf"{_MONTH},?\s+(?P<y>\d{{4}}){_END}")
_UNIT = r"(?:ms|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s|days?|d)"
_PART = rf"\d+(?:\.\d+)?\s?{_UNIT}(?![A-Za-z])"
_DURATION = re.compile(rf"(?<![\w.,]){_PART}(?:\s*,?\s*{_PART})+")
_SHORT_DURATION = re.compile(r"(?<![\w.,])\d+(?:\.\d+)?(?:ms|hrs?|h|mins?|secs?|s|d)(?![\w])")
_TOKEN = re.compile(r"(?<![\w])\d[\d,.]*\d|(?<![\w])\d")
_NUMBER = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
_SUFFIX = re.compile(
    r"(?P<ord>st|nd|rd|th)(?![A-Za-z0-9_])|(?P<scale>bn|k|K|m|M|B)(?![A-Za-z0-9_])"
    r"|\s(?P<word>thousand|million|billion)(?![A-Za-z])"
)
_PERCENT = re.compile(r"\s?%|\s?percent(?![A-Za-z])")
_SCALES = {"k": 1e3, "K": 1e3, "thousand": 1e3, "m": 1e6, "M": 1e6, "million": 1e6,
           "bn": 1e9, "B": 1e9, "billion": 1e9}  # fmt: skip
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.M | re.S)
_INLINE_CODE = re.compile(r"(`+)[^\n]*?\1")
_LIST_MARKER = re.compile(r"^[ \t]*(\d+[.)])(?=\s)", re.M)
_MASK = "\x00"


def _date(year: int, month: int, day: int) -> datetime.date | None:
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _month(name: str) -> int:
    return _MONTHS[name.rstrip(".").lower()]


def _date_mention(start: int, end: int, source: str, **fields) -> Mention:
    """A mention of source[start:end]; a date, or (without fields) a shape never matched."""
    values = dict(value=None, decimals=0, scale=1.0, percent=False, date=None, date_end=None,
                  month_day_only=False, year_only=False)  # fmt: skip
    values.update(fields)
    return Mention(start, end, source[start:end], **values)


def _iso(m: re.Match, text: str) -> Mention | None:
    when = _date(int(m["y"]), int(m["m"]), int(m["d"]))
    return _date_mention(m.start(), m.end(), text, date=when) if when else None


def _slash(m: re.Match, text: str) -> Mention | None:
    a, b, year = int(m["a"]), int(m["b"]), int(m["y"])
    readings = [d for d in (_date(year, b, a), _date(year, a, b)) if d is not None]
    if not readings:
        return None
    alt = readings[1] if len(readings) == 2 and readings[1] != readings[0] else None
    return _date_mention(m.start(), m.end(), text, date=readings[0], alt_date=alt)


def _day_month(m: re.Match, text: str, ranged: bool) -> Mention | None:
    month = _month(m["mon"])
    year = int(m["y"]) if m["y"] else 2000  # a leap year: 29 February stays valid
    first = _date(year, month, int(m["d1"]))
    last = _date(year, month, int(m["d2"])) if ranged else None
    if first is None or (ranged and (last is None or last < first)):
        return None
    return _date_mention(m.start(), m.end(), text, date=first, date_end=last,
                         month_day_only=not m["y"])  # fmt: skip


def _month_year(m: re.Match, text: str) -> Mention | None:
    when = _date(int(m["y"]), _month(m["mon"]), 1)
    return _date_mention(m.start(), m.end(), text, date=when, month_year=True) if when else None


def _duration(m: re.Match, text: str) -> Mention:
    return _date_mention(m.start(), m.end(), text)  # never matched: no value, no date


_DATE_PASSES = (
    (_ISO, _iso),
    (_SLASH, _slash),
    (_DAY_RANGE, lambda m, t: _day_month(m, t, True)),
    (_MONTH_RANGE, lambda m, t: _day_month(m, t, True)),
    (_DAY_MONTH, lambda m, t: _day_month(m, t, False)),
    (_MONTH_DAY, lambda m, t: _day_month(m, t, False)),
    (_MONTH_YEAR, _month_year),
    (_DURATION, _duration),
    (_SHORT_DURATION, _duration),
)


def _masked(text: str) -> str:
    """The text with code and list markers blanked out, offsets unchanged."""
    chars = list(text)

    def blank(start: int, end: int) -> None:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = _MASK

    for m in _FENCE.finditer(text):
        blank(m.start(), m.end())
    work = "".join(chars)
    for m in _INLINE_CODE.finditer(work):
        blank(m.start(), m.end())
    work = "".join(chars)
    for m in _LIST_MARKER.finditer(work):
        blank(m.start(1), m.end(1))
    return "".join(chars)


def _boundary(work: str, i: int) -> bool:
    """Nothing word-like right before position i."""
    return i == 0 or not (work[i - 1].isalnum() or work[i - 1] == "_")


def _number(m: re.Match, work: str, text: str) -> Mention | None:
    start, end = m.start(), m.end()
    token = m.group()
    if start > 0 and work[start - 1] in ".,":  # ".5": not a shape we read
        if not _boundary(work, start - 1):
            return None
        return _date_mention(start - 1, end, text)
    # Prefix: a sign directly before the digits (or before the currency sign), a currency sign.
    begin, negative = start, False
    if begin > 0 and work[begin - 1] in _SIGNS and _boundary(work, begin - 1):
        negative, begin = True, begin - 1
    if begin > 0 and work[begin - 1] in _CURRENCY:
        begin -= 1
        if not negative and begin > 0 and work[begin - 1] in _SIGNS and _boundary(work, begin - 1):
            negative, begin = True, begin - 1
    parsed = _NUMBER.fullmatch(token)
    scale, percent, ordinal = 1.0, False, False
    stop = end
    suffix = _SUFFIX.match(work, stop)
    if suffix:
        if suffix["ord"]:
            ordinal = True
        else:
            scale = _SCALES[suffix["scale"] or suffix["word"]]
        stop = suffix.end()
    if not ordinal:
        pct = _PERCENT.match(work, stop)
        if pct:
            percent, stop = True, pct.end()
    if stop < len(work) and (work[stop].isalnum() or work[stop] == "_"):
        return None  # digits touching letters: an identifier such as "5kg" or "2O"
    if parsed is None or (ordinal and parsed.group(2)):
        return _date_mention(begin, stop, text)  # "1.2.3", "1,23": unread shape
    decimals = len(parsed.group(2) or "")
    number = float(parsed.group(1).replace(",", "") + (f".{parsed.group(2)}" if decimals else ""))
    if negative:
        number = -number
    plain = begin == start and stop == end
    if plain and len(token) == 4 and 1900 <= number <= 2100:
        return _date_mention(start, end, text, value=number,
                             date=datetime.date(int(number), 1, 1), year_only=True)  # fmt: skip
    return Mention(begin, stop, text[begin:stop], number, decimals, scale, percent, None, None,
                   False, False)  # fmt: skip


def _scan(text: str) -> list[Mention]:
    work = _masked(text)
    found: list[Mention] = []
    for pattern, build in _DATE_PASSES:
        taken: list[tuple[int, int]] = []
        for m in pattern.finditer(work):
            mention = build(m, text)
            if mention is not None:
                found.append(mention)
                taken.append((m.start(), m.end()))
        if taken:
            chars = list(work)
            for start, end in taken:
                chars[start:end] = _MASK * (end - start)
            work = "".join(chars)
    for m in _TOKEN.finditer(work):
        mention = _number(m, work, text)
        if mention is not None:
            found.append(mention)
    found.sort(key=lambda x: x.start)
    return found


def extract_mentions(text: str) -> list[Mention]:
    """Numbers, dates and counts in the first MAX_REPLY_CHARS characters of `text`."""
    return _scan(text[:MAX_REPLY_CHARS])


# ---- reported values -----------------------------------------------------------------------------

_ISO_TEXT = re.compile(
    r"\s*(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\s*"
)
_NUMBER_TEXT = re.compile(
    r"\s*([-−–])?\s*[£$€¥₹]?\s*([-−–])?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*%?\s*"
)


def _number_or_date(raw: object, kind: Kind, page: int | None) -> Reported | None:
    """A number, or a whole-string ISO date or number; None for anything else."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int | float):
        number = float(raw)
        return Reported(number, None, kind, page) if math.isfinite(number) else None
    if not isinstance(raw, str):
        return None
    if m := _ISO_TEXT.fullmatch(raw):
        when = _date(int(m[1]), int(m[2]), int(m[3]))
        return Reported(None, when, kind, page) if when else None
    if m := _NUMBER_TEXT.fullmatch(raw):
        number = float(m[3].replace(",", "") + (m[4] or ""))
        return Reported(-number if (m[1] or m[2]) else number, None, kind, page)
    return None


def _from_mentions(mentions: Iterable[Mention], kind: Kind, page: int | None) -> Iterator[Reported]:
    for m in mentions:
        if m.value is not None:
            yield Reported(m.value * m.scale, None, kind, page)
        elif m.date is not None and not (m.month_day_only or m.month_year or m.alt_date):
            yield Reported(None, m.date, kind, page)
            if m.date_end is not None:
                yield Reported(None, m.date_end, kind, page)


def _all_reported(runs: RunStore, chat_id: str) -> Iterator[Reported]:
    printed_left = MAX_PRINTED_CHARS
    for run in runs.runs_for_chat(chat_id, MAX_RUNS):
        read = run.purpose == "vision"
        for v in run.values:
            kind: Kind = "document" if read or v.kind == "document" else "computed"
            found = _number_or_date(v.value, kind, v.page)
            if found is not None:
                yield found
            elif isinstance(v.value, str):
                yield from _from_mentions(_scan(v.value), kind, v.page)
        for output in runs.outputs(run.id):
            data = output.data
            if output.kind != "table" or not isinstance(data, dict):
                continue
            rows = data.get("rows")
            if not isinstance(rows, list):
                continue
            kind = "document" if read else "computed"
            for row in rows:
                for cell in row if isinstance(row, list) else ():
                    found = _number_or_date(cell, kind, None)
                    if found is not None:
                        yield found
        if printed_left > 0 and run.stdout:
            printed = run.stdout[:printed_left]
            printed_left -= len(printed)
            yield from _from_mentions(_scan(printed), "computed", None)


def reported_values(runs: RunStore, chat_id: str) -> list[Reported]:
    """The values this chat's runs reported, newest runs first, at most MAX_REPORTED."""
    return list(itertools.islice(_all_reported(runs, chat_id), MAX_REPORTED))


# ---- matching ------------------------------------------------------------------------------------


@dataclass
class _Side:
    """The values of one kind (computed or document), indexed for lookups."""

    plain: list[tuple[float, int]] = field(default_factory=list)  # (value, page or 0)
    hundred: list[tuple[float, int]] = field(default_factory=list)  # 100·v, for percents
    dates: dict[datetime.date, int | None] = field(default_factory=dict)
    month_days: dict[tuple[int, int], int | None] = field(default_factory=dict)
    months: dict[tuple[int, int], int | None] = field(default_factory=dict)
    years: dict[int, int | None] = field(default_factory=dict)

    def add(self, r: Reported) -> None:
        page = r.page or 0
        if r.value is not None:
            for v in {r.value, abs(r.value)}:
                self.plain.append((v, page))
                self.hundred.append((v * 100, page))
        if r.date is not None:
            d = r.date
            self.dates.setdefault(d, r.page)
            self.month_days.setdefault((d.month, d.day), r.page)
            self.months.setdefault((d.year, d.month), r.page)
            self.years.setdefault(d.year, r.page)

    def finish(self) -> None:
        self.plain.sort()
        self.hundred.sort()

    def number(self, m: Mention) -> tuple[bool, int | None]:
        target = m.value * m.scale  # type: ignore[operator]
        tolerance = 0.5 * 10.0 ** (-m.decimals) * m.scale + 1e-9 * max(1.0, abs(target))
        lists = (self.plain, self.hundred) if m.percent else (self.plain,)
        for values in lists:
            i = bisect.bisect_left(values, (target - tolerance, -1))
            if i < len(values) and values[i][0] <= target + tolerance:
                return True, values[i][1] or None
        return False, None

    def day(self, m: Mention, when: datetime.date) -> tuple[bool, int | None]:
        if m.month_day_only:
            key, table = (when.month, when.day), self.month_days
        elif m.month_year:
            key, table = (when.year, when.month), self.months
        elif m.year_only:
            key, table = when.year, self.years
        else:
            key, table = when, self.dates
        if key in table:
            return True, table[key]  # type: ignore[index]
        return False, None


_RANK = {"computed": 2, "document": 1}


def _best(sides: tuple[_Side, _Side], probe) -> tuple[str, int | None] | None:
    """("computed", None), ("document", page) or None, for one value or date."""
    found, _ = probe(sides[0])
    if found:
        return "computed", None
    found, page = probe(sides[1])
    return ("document", page) if found else None


def _resolve(m: Mention, sides: tuple[_Side, _Side]) -> tuple[str, int | None] | None:
    if m.date is None:
        if m.value is None:
            return None
        return _best(sides, lambda s: s.number(m))
    if m.year_only:
        by_year = _best(sides, lambda s: s.day(m, m.date))  # type: ignore[arg-type]
        by_value = _best(sides, lambda s: s.number(m))
        found = [r for r in (by_year, by_value) if r is not None]
        return max(found, key=lambda r: _RANK[r[0]]) if found else None
    ends = [m.date] if m.date_end is None else [m.date, m.date_end]
    readings = [ends] if m.alt_date is None else [ends, [m.alt_date]]
    results = []
    for reading in readings:
        per_end = [_best(sides, lambda s, d=d: s.day(m, d)) for d in reading]
        if all(per_end):
            if all(r[0] == "computed" for r in per_end):  # type: ignore[index]
                results.append(("computed", None))
            else:
                results.append(next(r for r in per_end if r[0] == "document"))  # type: ignore[index]
    return max(results, key=lambda r: _RANK[r[0]]) if results else None


def _value_key(m: Mention) -> tuple | None:
    return ("n", round(m.value * m.scale, 6)) if m.value is not None else None


def _exempt_keys(mentions: Iterable[Mention]) -> set[tuple]:
    keys: set[tuple] = set()
    for m in mentions:
        if (key := _value_key(m)) is not None:
            keys.add(key)
        if m.date is None:
            if m.value is None:
                keys.add(("t", m.text))
            continue
        for d in (m.date, m.date_end, m.alt_date):
            if d is None:
                continue
            if m.month_day_only:
                keys.add(("md", d.month, d.day))
                continue
            keys.add(("y", d.year))
            if m.year_only:
                continue
            keys.add(("ym", d.year, d.month))
            if m.month_year:
                continue
            keys.add(("md", d.month, d.day))
            keys.add(("d", d))
    return keys


def _is_exempt(m: Mention, keys: set[tuple]) -> bool:
    if not keys:
        return False
    if m.date is None:
        key = _value_key(m) or ("t", m.text)
        return key in keys
    if m.year_only:
        return ("y", m.date.year) in keys or _value_key(m) in keys

    def date_key(d: datetime.date) -> tuple:
        if m.month_day_only:
            return ("md", d.month, d.day)
        if m.month_year:
            return ("ym", d.year, d.month)
        return ("d", d)

    ends = [m.date] if m.date_end is None else [m.date, m.date_end]
    if all(date_key(d) in keys for d in ends):
        return True
    return m.alt_date is not None and date_key(m.alt_date) in keys


def check_reply(reply: str, reported: list[Reported], exempt: list[str]) -> list[Mark]:
    """A mark for every mention in the reply that is not also in an exempt text."""
    sides = (_Side(), _Side())
    for r in reported:
        sides[0 if r.kind == "computed" else 1].add(r)
    for side in sides:
        side.finish()
    keys = _exempt_keys(itertools.chain.from_iterable(extract_mentions(t) for t in exempt))
    marks: list[Mark] = []
    for m in extract_mentions(reply):
        if _is_exempt(m, keys):
            continue
        result = _resolve(m, sides)
        if result is None:
            marks.append(Mark(m.start, m.end, m.text, "unverified", None, UNVERIFIED_NOTE))
        else:
            kind, page = result
            marks.append(Mark(m.start, m.end, m.text, kind, page, None))  # type: ignore[arg-type]
    return marks


# ---- the checker -------------------------------------------------------------------------------


def without_attachment_lines(text: str) -> str:
    lines = normalize_newlines(text).split("\n")
    return "\n".join(
        line
        for line in lines
        if not (ATTACHED_FILE_LINE.fullmatch(line) or ATTACHED_PHOTO_LINE.fullmatch(line))
    )


class NumberChecker:
    def __init__(self, conn: sqlite3.Connection, runs: RunStore, files: ChatFileStore) -> None:
        self._conn = conn
        self._runs = runs
        self._files = files

    def marks_for(self, chat_id: str, text: str, question: str = "") -> list[Mark] | None:
        """The reply's marks, or None when the chat has no files (nothing to check against)."""
        chat_files = self._files.list(chat_id)
        if not chat_files:
            return None
        exempt = [without_attachment_lines(question), *(f.name for f in chat_files)]
        return check_reply(text, reported_values(self._runs, chat_id), exempt)

    def check(
        self, chat_id: str, turn_id: str, message_id: int, question: str, reply: str
    ) -> dict | None:
        """Check the reply, store the result for the message (once: a re-check replaces it) and
        return `{"text", "marks"}`. None when the chat has no files or anything goes wrong:
        the check never stops a reply."""
        try:
            marks = self.marks_for(chat_id, reply, question)
            if marks is None:
                return None
            result = {"text": reply, "marks": [dataclasses.asdict(m) for m in marks]}
            self._conn.execute(
                "INSERT OR REPLACE INTO number_checks"
                " (message_id, chat_id, turn_id, text, marks, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, chat_id, turn_id, reply, json.dumps(result["marks"]), db.utcnow()),
            )
            return result
        except Exception as exc:
            # Only the type: the message could carry the owner's numbers or file contents.
            _log.warning("number check failed (%s)", type(exc).__name__)
            return None
