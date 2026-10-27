"""The number check: every number, date and count in an assistant reply of a chat with files is
compared with the values this chat's sandbox runs reported.

Each mention is marked `computed` (it matches a value a run computed), `document` (it matches
only a value read from a document, with that value's page) or `unverified` (it matches nothing).
Mentions that also appear in the owner's question of that turn or in a file name of the chat get
no mark. When in doubt a mention is `unverified`: flagging a right number is better than letting
a wrong one through. So:

- Only named results of this turn confirm a number: the `hk.result` values of the turn's
  analysis runs give `computed`. The turn's `hk.document` values, and what any vision run of
  the chat read (values and table cells), give `document`. Table cells and printed output of
  analysis runs, and earlier turns' named results, are never evidence: with many unnamed or
  unrelated numbers, almost any number finds a match by chance.
- Named values match within the precision written ("3" covers [2.5, 3.5)). When a side has
  more than BIG_POOL named values, integers with at most 2 significant digits and every
  percent match only exactly. Vision table cells match only exactly, or within the precision
  written when 3 or more significant digits are written, and never as 100·v.
- A written sign (`+`, `−`) must match the value's sign; an unsigned number may state a size.
- A percent matches `100·v` only for ratio-like values (|v| ≤ 10), and `v` itself only when the
  value's unit is a percent.
- Durations, times, fractions, short-year dates, phone numbers, URLs and code are never matched.

Work is bounded: at most MAX_REPORTED reported values and MAX_REPLY_CHARS characters of a
reply (or of an exempt text); matching is a binary search per mention. Offsets are code points
inside; the web gets UTF-16 offsets.
"""

from __future__ import annotations

import asyncio
import bisect
import dataclasses
import datetime
import itertools
import json
import logging
import math
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

from hearthkeep import db
from hearthkeep.files.store import ATTACHED_FILE_LINE, ChatFileStore
from hearthkeep.images import ATTACHED_PHOTO_LINE, normalize_newlines
from hearthkeep.sandbox.runs import RunRecord, RunStore

UNVERIFIED_NOTE = "Not found in any result from this chat's analysis."
MAX_REPORTED = 50_000
MAX_REPLY_CHARS = 20_000
MAX_RUNS = 200
BIG_POOL = 100  # more named values than this: small numbers and percents match exactly only
MAX_ABS = 2.0**63  # larger run values are dropped (results.py stores them as null too)

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
    year_only: bool  # a bare year after "in", "since", ...; `value` holds it too
    # Additions to the brief's fields:
    month_year: bool = False  # "March 2024": matches any day of that month
    signed: bool = False  # a "+" or "−" was written
    ordinal: bool = False  # "12th"
    digits: int = 0  # significant digits written


@dataclass
class Reported:
    value: float | None
    date: datetime.date | None
    kind: Kind
    page: int | None
    percent: bool = False  # the value's unit is a percent
    cell: bool = False  # a vision run's table cell (unnamed: matched more strictly)


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


def reply_spans(parts: list[str], unchecked: set[int]) -> list[tuple[int, int]]:
    """Where the parts with these indexes sit in `reply_text(parts)` (code points)."""
    spans: list[tuple[int, int]] = []
    position = 0
    for index, part in enumerate(parts):
        if not part.strip():
            continue
        if index in unchecked:
            spans.append((position, position + len(part)))
        position += len(part) + 2
    return spans


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
_MONTH_NAMES = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"|(?:Sept|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?"
)
_MONTH = rf"(?<![A-Za-z])(?P<mon>{_MONTH_NAMES})(?![A-Za-z])"
_ORD = r"(?:st|nd|rd|th)?"
_END = r"(?![A-Za-z0-9_])(?![.,]\d)"  # a number ends here: no letter, digit or decimal part
_YEAR_OPT = rf"(?:,?\s+(?P<y>\d{{4}}){_END})?"
_DASH = r"\s*[-–—]\s*"
_SIGNS = "-−–+"
_MINUS = "-−–"
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
_WORD_DURATION = re.compile(
    r"(?<![\w.,])\d+(?:\.\d+)?\s?(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?"
    r"|months?|years?)(?![A-Za-z])"
)
_SHORT_DURATION = re.compile(r"(?<![\w.,])\d+(?:\.\d+)?(?:ms|hrs?|h|mins?|secs?|s|d)(?![\w])")
# Shapes that are never matched and get no mark at all.
_SHORT_YEAR_DATE = re.compile(r"(?<![\w/.,])\d{1,2}/\d{1,2}/\d{2}(?![\w/])")
_FRACTION = re.compile(r"(?<![\w/.,])\d+/\d+(?![\w/])")
_TIME = re.compile(
    r"(?<![\w:.,])\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s?[AaPp]\.?[Mm]\.?(?![A-Za-z]))?(?![\w:])"
)
_PHONE = re.compile(
    r"(?<![\w.,+])(?:\+\s?)?\(?\d{1,5}\)?(?:[ .-]\(?\d{1,5}\)?){2,}(?![\w])(?![.,]\d)"
)
_PHONE_SEPARATORS = re.compile(r"\d\)?([ .-])\(?(?=\d)")
_URL = re.compile(r"(?:\b(?:https?|ftp)://|\bwww\.)[^\s<>\]]+")
_LINK_TARGET = re.compile(r"\]\([^)\n]*\)")
_TOKEN = re.compile(r"(?<![\w])\d[\d,.]*\d|(?<![\w])\d")
_NUMBER = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
_SUFFIX = re.compile(
    r"(?P<ord>st|nd|rd|th)(?![A-Za-z0-9_])|(?P<scale>bn|k|K|m|M|B)(?![A-Za-z0-9_])"
    r"|\s(?P<word>thousand|million|billion)(?![A-Za-z])"
)
_PERCENT = re.compile(r"\s?%|\s?percent(?![A-Za-z])")
_YEAR_WORD = re.compile(
    rf"(?:(?<![A-Za-z])(?i:in|since|during|of|from|by|until|before|after|through)"
    rf"|(?<![A-Za-z])(?:{_MONTH_NAMES}),?)[ \t]+$"
)
_SCALES = {"k": 1e3, "K": 1e3, "thousand": 1e3, "m": 1e6, "M": 1e6, "million": 1e6,
           "bn": 1e9, "B": 1e9, "billion": 1e9}  # fmt: skip
_FENCE = re.compile(
    r"^[ \t]*(?P<f>(?P<c>[`~])(?P=c){2,})[^\n]*\n.*?^[ \t]*(?P=f)(?P=c)*[ \t]*$", re.M | re.S
)
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


def _plain_mention(start: int, end: int, source: str, **fields) -> Mention:
    """A mention of source[start:end]: a date, or (without fields) a shape never matched."""
    values = dict(value=None, decimals=0, scale=1.0, percent=False, date=None, date_end=None,
                  month_day_only=False, year_only=False)  # fmt: skip
    values.update(fields)
    return Mention(start, end, source[start:end], **values)


def _iso(m: re.Match, text: str) -> Mention | None:
    when = _date(int(m["y"]), int(m["m"]), int(m["d"]))
    return _plain_mention(m.start(), m.end(), text, date=when) if when else None


def _slash(m: re.Match, text: str) -> Mention | None:
    """Day first (the owner's locale); month first only when the second number can't be one."""
    a, b, year = int(m["a"]), int(m["b"]), int(m["y"])
    when = _date(year, b, a) if b <= 12 else _date(year, a, b)
    return _plain_mention(m.start(), m.end(), text, date=when) if when else None


# A word after "May 3" that makes the 3 a count ("May 3 people join?"); joining words and
# words that commonly follow a date don't.
_NOUN_AFTER = re.compile(
    r"[ \t]+(?!(?:and|or|to|through|till|until|at|in|on|from|was|is|were|will|we|i|the|this"
    r"|that|when|with|for|but|by|onwards|only)(?![A-Za-z]))[A-Za-z]+",
    re.I,
)


def _day_month(m: re.Match, text: str, ranged: bool, day_first: bool) -> Mention | None:
    month = _month(m["mon"])
    if not day_first and month == 5 and not m["y"] and _NOUN_AFTER.match(m.string, m.end()):
        return None  # "May 3 people …": a count, not a date
    year = int(m["y"]) if m["y"] else 2000  # a leap year: 29 February stays valid
    first = _date(year, month, int(m["d1"]))
    last = _date(year, month, int(m["d2"])) if ranged else None
    if first is None or (ranged and (last is None or last < first)):
        return None
    return _plain_mention(m.start(), m.end(), text, date=first, date_end=last,
                          month_day_only=not m["y"])  # fmt: skip


def _month_year(m: re.Match, text: str) -> Mention | None:
    when = _date(int(m["y"]), _month(m["mon"]), 1)
    return _plain_mention(m.start(), m.end(), text, date=when, month_year=True) if when else None


def _never(m: re.Match, text: str) -> Mention:
    return _plain_mention(m.start(), m.end(), text)  # no value, no date: always unverified


def _skip(m: re.Match, text: str) -> bool:
    return True  # masked, no mention


def _phone(m: re.Match, text: str) -> bool:
    span = m.group()
    if sum(c.isdigit() for c in span) < 9:
        return False
    separators = set(_PHONE_SEPARATORS.findall(span))
    first = span.lstrip("+( ")
    return (
        "+" in span
        or "(" in span
        or separators in ({"-"}, {"."})
        or (separators == {" "} and first.startswith("0"))
    )


_SKIPS = {_skip, _phone}
_PASSES = (
    (_ISO, _iso),
    (_SLASH, _slash),
    (_SLASH, _skip),  # what is left is not a real date ("13/13/2024"): unmarked text
    (_SHORT_YEAR_DATE, _skip),
    (_FRACTION, _skip),
    (_TIME, _skip),
    (_PHONE, _phone),
    (_DAY_RANGE, lambda m, t: _day_month(m, t, True, True)),
    (_MONTH_RANGE, lambda m, t: _day_month(m, t, True, False)),
    (_DAY_MONTH, lambda m, t: _day_month(m, t, False, True)),
    (_MONTH_DAY, lambda m, t: _day_month(m, t, False, False)),
    (_MONTH_YEAR, _month_year),
    (_DURATION, _never),
    (_WORD_DURATION, _never),
    (_SHORT_DURATION, _never),
)


def _masked(text: str) -> str:
    """The text with code, links and list markers blanked out, offsets unchanged."""
    chars = list(text)

    def blank(start: int, end: int) -> None:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = _MASK

    for pattern in (_FENCE, _INLINE_CODE, _LINK_TARGET, _URL):
        work = "".join(chars)
        for m in pattern.finditer(work):
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
        return _plain_mention(start - 1, end, text)
    # Prefix: a sign directly before the digits (or before the currency sign), a currency sign.
    begin, sign = start, ""
    if begin > 0 and work[begin - 1] in _SIGNS and _boundary(work, begin - 1):
        sign, begin = work[begin - 1], begin - 1
    if begin > 0 and work[begin - 1] in _CURRENCY:
        begin -= 1
        if not sign and begin > 0 and work[begin - 1] in _SIGNS and _boundary(work, begin - 1):
            sign, begin = work[begin - 1], begin - 1
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
        return _plain_mention(begin, stop, text)  # "1.2.3", "1,23": a shape we don't read
    whole, fraction = parsed.group(1).replace(",", ""), parsed.group(2) or ""
    number = float(whole + (f".{fraction}" if fraction else ""))
    if not math.isfinite(number) or not math.isfinite(number * scale):
        return _plain_mention(begin, stop, text)  # too big to compare: always unverified
    if sign in _MINUS and sign:
        number = -number
    digits = len((whole + fraction).lstrip("0"))
    plain = begin == start and stop == end
    if (
        plain
        and len(token) == 4
        and 1900 <= number <= 2100
        and _YEAR_WORD.search(work, max(0, start - 24), start)
    ):
        return _plain_mention(start, end, text, value=number,
                              date=datetime.date(int(number), 1, 1), year_only=True)  # fmt: skip
    return Mention(begin, stop, text[begin:stop], number, len(fraction), scale, percent, None,
                   None, False, False, signed=bool(sign), ordinal=ordinal,
                   digits=digits)  # fmt: skip


def _scan(text: str) -> list[Mention]:
    work = _masked(text)
    found: list[Mention] = []
    for pattern, build in _PASSES:
        taken: list[tuple[int, int]] = []
        for m in pattern.finditer(work):
            if build in _SKIPS:
                if build(m, text):
                    taken.append((m.start(), m.end()))
                continue
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
    r"\s*([-−–])?\s*[£$€¥₹]?\s*([-−–])?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(%)?\s*"
)


def _usable(number: float) -> bool:
    return math.isfinite(number) and abs(number) < MAX_ABS


def _number_or_date(
    raw: object, kind: Kind, page: int | None, percent: bool = False
) -> Reported | None:
    """A number, or a whole-string ISO date or number; None for anything else."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        if abs(raw) >= MAX_ABS:
            return None
        return Reported(float(raw), None, kind, page, percent)
    if isinstance(raw, float):
        return Reported(raw, None, kind, page, percent) if _usable(raw) else None
    if not isinstance(raw, str):
        return None
    if m := _ISO_TEXT.fullmatch(raw):
        when = _date(int(m[1]), int(m[2]), int(m[3]))
        return Reported(None, when, kind, page) if when else None
    if m := _NUMBER_TEXT.fullmatch(raw):
        number = float(m[3].replace(",", "") + (m[4] or ""))
        if not _usable(number):
            return None
        number = -number if (m[1] or m[2]) else number
        return Reported(number, None, kind, page, percent or bool(m[5]))
    return None


def _from_mentions(mentions: Iterable[Mention], kind: Kind, page: int | None) -> Iterator[Reported]:
    """The numbers and full dates written in a named value's text."""
    for m in mentions:
        if m.value is not None:
            number = m.value * m.scale
            if _usable(number):
                yield Reported(number, None, kind, page, m.percent)
        elif m.date is not None and not (m.month_day_only or m.month_year):
            yield Reported(None, m.date, kind, page)
            if m.date_end is not None:
                yield Reported(None, m.date_end, kind, page)


Loaded = list[tuple[RunRecord, list[dict]]]


def _load(runs: RunStore, chat_id: str, turn_id: str) -> Loaded:
    """The evidence runs, newest first, read from the database: this turn's analysis runs and
    every vision run of the chat, with the vision runs' table outputs."""
    loaded: Loaded = []
    for run in runs.runs_for_chat(chat_id, MAX_RUNS):
        if run.purpose != "vision" and run.turn_id != turn_id:
            continue  # an earlier turn's results are not evidence for this reply
        tables: list = []
        if run.purpose == "vision":
            tables = [o.data for o in runs.outputs(run.id) if o.kind == "table"]
        loaded.append((run, [t for t in tables if isinstance(t, dict)]))
    return loaded


def _reported_from(loaded: Loaded) -> Iterator[Reported]:
    """Named results (computed) and read values (document). Printed output and the table cells
    of analysis runs are left out: only named results confirm a number."""
    for run, tables in loaded:
        read = run.purpose == "vision"
        for v in run.values:
            kind: Kind = "document" if read or v.kind == "document" else "computed"
            percent = "%" in (v.unit or "")
            found = _number_or_date(v.value, kind, v.page, percent)
            if found is not None:
                yield found
            elif isinstance(v.value, str):
                yield from _from_mentions(_scan(v.value), kind, v.page)
        for table in tables if read else ():
            rows = table.get("rows")
            for row in rows if isinstance(rows, list) else ():
                for cell in row if isinstance(row, list) else ():
                    found = _number_or_date(cell, "document", None)
                    if found is not None:
                        found.cell = True
                        yield found


def _capped(loaded: Loaded) -> list[Reported]:
    return list(itertools.islice(_reported_from(loaded), MAX_REPORTED))


def reported_values(runs: RunStore, chat_id: str, turn_id: str) -> list[Reported]:
    """The evidence for a reply of this turn, newest runs first, at most MAX_REPORTED."""
    return _capped(_load(runs, chat_id, turn_id))


# ---- matching ------------------------------------------------------------------------------------

# Number lists per form. Forms: "v" the value, "a" |v| of a negative value, "pv"/"pa"
# the same for values whose unit is a percent, "h"/"ha" 100·v and 100·|v| for |v| ≤ 10.
_FORMS = ("v", "a", "pv", "pa", "h", "ha")


@dataclass
class _Side:
    """The values of one kind (computed or document), indexed for lookups."""

    numbers: dict[str, list[tuple[float, int]]] = field(
        default_factory=lambda: {f: [] for f in _FORMS}
    )
    cells: dict[str, list[tuple[float, int]]] = field(
        default_factory=lambda: {"v": [], "a": []}
    )  # vision table cells: no percent or 100·v forms
    named: int = 0  # how many named numbers
    dates: dict[datetime.date, int | None] = field(default_factory=dict)
    month_days: dict[tuple[int, int], int | None] = field(default_factory=dict)
    months: dict[tuple[int, int], int | None] = field(default_factory=dict)
    years: dict[int, int | None] = field(default_factory=dict)

    def add(self, r: Reported) -> None:
        page = r.page or 0
        if r.value is not None and r.cell:
            self.cells["v"].append((r.value, page))
            if r.value < 0:
                self.cells["a"].append((-r.value, page))
        elif r.value is not None:
            self.named += 1
            v = r.value
            forms = [("v", v)]
            if r.percent:
                forms.append(("pv", v))
            if abs(v) <= 10:
                forms.append(("h", v * 100))
            if v < 0:
                forms.append(("a", -v))
                if r.percent:
                    forms.append(("pa", -v))
                if abs(v) <= 10:
                    forms.append(("ha", -v * 100))
            for form, number in forms:
                self.numbers[form].append((number, page))
        if r.date is not None:
            d = r.date
            self.dates.setdefault(d, r.page)
            self.month_days.setdefault((d.month, d.day), r.page)
            self.months.setdefault((d.year, d.month), r.page)
            self.years.setdefault(d.year, r.page)

    def finish(self) -> None:
        for values in (*self.numbers.values(), *self.cells.values()):
            values.sort()

    def number(self, m: Mention) -> tuple[bool, int | None]:
        """Named values within the precision written (exactly, for small numbers and percents
        in a big pool); cells exactly, or within the precision when 3+ digits are written."""
        small = m.percent or (m.decimals == 0 and m.digits <= 2)
        forms = ["pv", "h"] if m.percent else ["v"]
        if not m.signed:
            forms += ["pa", "ha"] if m.percent else ["a"]
        found = _lookup(self.numbers, forms, m, exact=small and self.named > BIG_POOL)
        if found[0] or m.percent:
            return found
        cell_forms = ["v"] if m.signed else ["v", "a"]
        return _lookup(self.cells, cell_forms, m, exact=m.digits < 3)

    def equal(self, m: Mention) -> bool:
        """A named value exactly equal to the number as written (the year path)."""
        return _lookup(self.numbers, ["v"], m, exact=True)[0]

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


def _lookup(
    lists: dict[str, list[tuple[float, int]]], forms: list[str], m: Mention, exact: bool
) -> tuple[bool, int | None]:
    """Half-open [m - t, m + t) for the precision written; `exact`: equal (1e-9 relative)."""
    target = m.value * m.scale  # type: ignore[operator]
    slack = 1e-9 * max(1.0, abs(target))
    if exact:
        low, high, closed = target - slack, target + slack, True
    else:
        tolerance = 0.5 * 10.0 ** (-m.decimals) * m.scale
        low, high, closed = target - tolerance - slack, target + tolerance, False
    for form in forms:
        values = lists[form]
        i = bisect.bisect_left(values, (low, -1))
        if i < len(values) and (values[i][0] <= high if closed else values[i][0] < high):
            return True, values[i][1] or None
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
        # As a number: only an equal named analysis value (no sizes, cells or documents).
        by_value = ("computed", None) if sides[0].equal(m) else None
        found = [r for r in (by_year, by_value) if r is not None]
        return max(found, key=lambda r: _RANK[r[0]]) if found else None
    ends = [m.date] if m.date_end is None else [m.date, m.date_end]
    per_end = [_best(sides, lambda s, d=d: s.day(m, d)) for d in ends]
    if not all(per_end):
        return None
    if all(r[0] == "computed" for r in per_end):  # type: ignore[index]
        return "computed", None
    return next(r for r in per_end if r[0] == "document")  # type: ignore[index]


def _value_key(m: Mention) -> tuple | None:
    """The same number in the same form: "5", "5%" and "5th" are different keys."""
    if m.value is None:
        return None
    return ("n", round(m.value * m.scale, 6), m.percent, m.ordinal)


def _exempt_keys(mentions: Iterable[Mention], by_value: bool) -> set[tuple]:
    """Keys for exempt mentions. `by_value` False (file names): a number counts by value only
    when it has at least two digits; any mention still counts as its identical text."""
    keys: set[tuple] = set()
    for m in mentions:
        keys.add(("t", m.text))
        key = _value_key(m)
        if key is not None and (by_value or sum(c.isdigit() for c in m.text) >= 2):
            keys.add(key)
        if m.date is None:
            continue
        for d in (m.date, m.date_end):
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
    if ("t", m.text) in keys:
        return True
    if m.date is None:
        return _value_key(m) in keys
    if m.year_only:
        return ("y", m.date.year) in keys or _value_key(m) in keys

    def date_key(d: datetime.date) -> tuple:
        if m.month_day_only:
            return ("md", d.month, d.day)
        if m.month_year:
            return ("ym", d.year, d.month)
        return ("d", d)

    ends = [m.date] if m.date_end is None else [m.date, m.date_end]
    return all(date_key(d) in keys for d in ends)


def check_reply(
    reply: str,
    reported: list[Reported],
    exempt: list[str],
    excluded: Sequence[tuple[int, int]] = (),
    file_names: Sequence[str] = (),
) -> list[Mark]:
    """A mark for every mention in the reply that is not exempt or inside an excluded span
    (code points). `exempt` texts (the owner's question) exempt the same number in the same
    form; `file_names` exempt numbers of two or more digits, or the identical token."""
    sides = (_Side(), _Side())
    for r in reported:
        sides[0 if r.kind == "computed" else 1].add(r)
    for side in sides:
        side.finish()
    keys: set[tuple] = set()
    for text in exempt:
        keys |= _exempt_keys(extract_mentions(text), by_value=True)
    for name in file_names:
        keys |= _exempt_keys(extract_mentions(name), by_value=False)
    marks: list[Mark] = []
    for m in extract_mentions(reply):
        if any(m.start < b and m.end > a for a, b in excluded) or _is_exempt(m, keys):
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


def _web_marks(text: str, marks: list[Mark]) -> list[dict]:
    """Mark dicts with UTF-16 offsets (what JavaScript strings index by)."""
    if not marks:
        return []
    limit = max(m.end for m in marks)
    offsets = [0] * (limit + 1)
    units = 0
    for i, ch in enumerate(text[:limit]):
        offsets[i] = units
        units += 2 if ord(ch) > 0xFFFF else 1
    offsets[limit] = units
    return [
        {**dataclasses.asdict(m), "start": offsets[m.start], "end": offsets[m.end]} for m in marks
    ]


class NumberChecker:
    def __init__(self, conn: sqlite3.Connection, runs: RunStore, files: ChatFileStore) -> None:
        self._conn = conn
        self._runs = runs
        self._files = files

    def _inputs(
        self, chat_id: str, turn_id: str, question: str
    ) -> tuple[Loaded, list[str], list[str]] | None:
        chat_files = self._files.list(chat_id)
        if not chat_files:
            return None
        names = [f.name for f in chat_files]
        return _load(self._runs, chat_id, turn_id), [without_attachment_lines(question)], names

    def marks_for(
        self,
        chat_id: str,
        turn_id: str,
        text: str,
        question: str = "",
        excluded: Sequence[tuple[int, int]] = (),
    ) -> list[Mark] | None:
        """The marks of a reply of this turn (code-point offsets), or None when the chat has no
        files."""
        inputs = self._inputs(chat_id, turn_id, question)
        if inputs is None:
            return None
        loaded, exempt, names = inputs
        return check_reply(text, _capped(loaded), exempt, excluded, names)

    async def check(
        self,
        chat_id: str,
        turn_id: str,
        message_id: int,
        question: str,
        reply: str,
        excluded: Sequence[tuple[int, int]] = (),
    ) -> dict | None:
        """Check the reply, store the result for the message (once: a re-check replaces it) and
        return `{"text", "marks"}` with UTF-16 offsets. None when the chat has no files or
        anything goes wrong: the check never stops a reply. The database is read here (its
        connection belongs to this thread); the matching runs in a worker thread."""
        try:
            inputs = self._inputs(chat_id, turn_id, question)
            if inputs is None:
                return None
            loaded, exempt, names = inputs

            def work() -> list[dict]:
                marks = check_reply(reply, _capped(loaded), exempt, excluded, names)
                return _web_marks(reply, marks)

            marks = await asyncio.to_thread(work)
            self._conn.execute(
                "INSERT OR REPLACE INTO number_checks"
                " (message_id, chat_id, turn_id, text, marks, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, chat_id, turn_id, reply, json.dumps(marks), db.utcnow()),
            )
            return {"text": reply, "marks": marks}
        except Exception as exc:
            # Only the type: the message could carry the owner's numbers or file contents.
            _log.warning("number check failed (%s)", type(exc).__name__)
            return None
