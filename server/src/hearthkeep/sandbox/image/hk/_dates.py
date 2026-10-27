"""Dates the way the owner wrote them (`hk.dates`) and the days missing from dated data
(`hk.gaps`)."""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter

import numpy as np
import pandas as pd

from hk._results import note_once, result, table

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?")
# day/month/year or month/day/year with "/", "." or "-" between the parts (and maybe a time)
DMY = re.compile(r"(\d{1,2})([/.-])(\d{1,2})\2(\d{2,4})( .*)?")
DATE_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("YYYY-MM-DD", re.compile(r"\d{4}-\d{1,2}-\d{1,2}([ T].*)?")),
    ("DD/MM/YYYY or MM/DD/YYYY", re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}( .*)?")),
    ("DD.MM.YYYY", re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}( .*)?")),
    ("DD-MM-YYYY", re.compile(r"\d{1,2}-\d{1,2}-\d{2,4}( .*)?")),
    ("YYYY/MM/DD", re.compile(r"\d{4}/\d{1,2}/\d{1,2}( .*)?")),
    ("D Month YYYY", re.compile(r"\d{1,2}[ -][A-Za-z]{3,9}\.?[ ,-]+\d{2,4}")),
    ("Month D, YYYY", re.compile(r"[A-Za-z]{3,9}\.? \d{1,2},? \d{4}")),
)
# The shapes made only of digits and separators: a text column of these is read as dates.
NUMERIC_DATE_FORMATS = DATE_FORMATS[:5]
FORMATS_ATTR = "hk_date_formats"  # Series.attrs key: which rows were written another way
MAX_GAP_RESULTS = 10


def date_format(value: str) -> str | None:
    for name, pattern in DATE_FORMATS:
        if pattern.fullmatch(value):
            return name
    return None


def numeric_dates(texts: pd.Series) -> bool:
    """Every value has one of the all-digit date shapes (YYYY-MM-DD, DD/MM/YYYY, ...)."""
    return bool(texts.map(lambda s: any(p.fullmatch(s) for _, p in NUMERIC_DATE_FORMATS)).all())


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _naive(parsed: pd.Series) -> pd.Series:
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        return parsed.dt.tz_convert(None)
    return parsed


def parse_dates(values: pd.Series, notes: list[str] | None = None) -> pd.Series:
    """`values` as dates: YYYY-MM-DD read as ISO, and day/month/year (or month/day/year)
    decided by the column's own values. What was assumed is added to `notes`. A column
    whose values say both day first and month first comes back as text."""
    if pd.api.types.is_datetime64_any_dtype(values):
        return values
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    where = "the column" if series.name is None else f"column {series.name}"
    said = notes if notes is not None else []
    stripped = series.map(lambda v: v.strip() if isinstance(v, str) else v)
    stripped = stripped.mask(stripped.map(_blank), np.nan)
    present = stripped.notna()
    is_date = stripped.map(lambda v: isinstance(v, _dt.date | pd.Timestamp))
    texts = stripped.map(lambda v: v if isinstance(v, str) else "")
    iso = texts.map(lambda s: bool(ISO_DATE.fullmatch(s)))
    matches = texts.map(lambda s: None if ISO_DATE.fullmatch(s) else DMY.fullmatch(s))
    dmy = matches.notna()
    known = texts.map(lambda s: bool(s) and date_format(s) is not None)
    other = known & ~iso & ~dmy

    firsts = [int(m.group(1)) for m in matches[dmy]]
    seconds = [int(m.group(3)) for m in matches[dmy]]
    day_first_seen = any(n > 12 for n in firsts)
    month_first_seen = any(n > 12 for n in seconds)
    if day_first_seen and month_first_seen:
        day = next(t for t, n in zip(texts[dmy], firsts, strict=True) if n > 12)
        month = next(t for t, n in zip(texts[dmy], seconds, strict=True) if n > 12)
        said.append(
            f"{where[0].upper()}{where[1:]} has dates written day first (e.g. {day}) and "
            f"month first (e.g. {month}), so it was left as text."
        )
        return stripped
    dayfirst = not month_first_seen

    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]", name=series.name)
    parts = (
        (iso, {"format": "ISO8601"}),
        (dmy, {"format": "mixed", "dayfirst": dayfirst}),
        (other, {"format": "mixed", "dayfirst": dayfirst}),
        (is_date, {}),
    )
    for mask, how in parts:
        if mask.any():
            source = stripped[mask] if mask is is_date else texts[mask]
            try:
                parsed = _naive(pd.to_datetime(source, errors="coerce", **how))
                out[mask.to_numpy()] = parsed.astype("datetime64[ns]").to_numpy()
            except (TypeError, ValueError, OverflowError):
                pass  # those values stay NaT and are counted below

    if dmy.any():
        first = matches[dmy].iloc[0]
        sep, year = first.group(2), "YYYY" if len(first.group(4)) == 4 else "YY"
        shape = f"DD{sep}MM{sep}{year}" if dayfirst else f"MM{sep}DD{sep}{year}"
        order = "day/month/year" if dayfirst else "month/day/year"
        note = (
            f"Read {_plural(int(dmy.sum()), 'date', 'dates')} in {where} written {shape} "
            f"(e.g. {texts[dmy].iloc[0]}) as {order}"
        )
        if not (day_first_seen or month_first_seen):
            note += " (no part is over 12, so day first was assumed)"
        if iso.any():
            note += (
                "; the others are YYYY-MM-DD"
                if not (other.any() or is_date.any())
                else f"; {int(iso.sum())} are YYYY-MM-DD"
            )
        said.append(note + ".")
    failed = present & out.isna()
    if failed.any():
        n = int(failed.sum())
        said.append(
            f"{_plural(n, 'value', 'values')} in {where} could not be read as a date "
            f"(e.g. {stripped[failed].iloc[0]}) and {'was' if n == 1 else 'were'} left empty."
        )

    formats = texts[known].map(date_format)
    counts = Counter(formats)
    if len(counts) >= 2:
        common = counts.most_common(1)[0][0]
        out.attrs[FORMATS_ATTR] = {
            "counts": dict(counts.most_common()),
            "odd": list(formats[formats != common].index),
        }
    return out


def dates(values) -> pd.Series:
    """A column of dates written as text, read as dates: YYYY-MM-DD as ISO, and
    DD/MM/YYYY (or MM/DD/YYYY, DD.MM.YYYY, DD-MM-YYYY) day first or month first as the
    column's own values show. What was assumed becomes a cleaning note. Use it as
    df["date"] = hk.dates(df["date"]); never pd.to_datetime(..., dayfirst=True)."""
    notes: list[str] = []
    out = parse_dates(values, notes)
    out.attrs.pop(FORMATS_ATTR, None)
    for note in notes:
        note_once(note)
    return out


def _day(value) -> _dt.date:
    return pd.Timestamp(value).date()


def gaps(
    df: pd.DataFrame, date_col: str, *, by: str | None = None, min_days: int = 2
) -> pd.DataFrame:
    """Runs of missing calendar days in `date_col` (per value of `by`), at least `min_days`
    long. Reported as the table "Gaps in <date_col> by <by>" (columns group, last_before,
    first_missing, last_missing, days) and, for the first 10 gaps, a result for the first
    missing day, the last missing day and the number of days. Returns the table."""
    if not isinstance(df, pd.DataFrame):
        raise TypeError("hk.gaps takes a pandas DataFrame")
    for col in (date_col,) if by is None else (date_col, by):
        if col not in df.columns:
            raise KeyError(f"no such column: {col!r}")
    if not isinstance(min_days, int) or isinstance(min_days, bool) or min_days < 1:
        raise ValueError("min_days must be a whole number of days (1 or more)")
    when = df[date_col]
    if not pd.api.types.is_datetime64_any_dtype(when):
        when = dates(when)
        if not pd.api.types.is_datetime64_any_dtype(when):
            raise ValueError(f"column {date_col} isn't dates; see the cleaning note")
    days = _naive(when).dt.normalize()
    groups = [("all", days)] if by is None else list(days.groupby(df[by], sort=True))
    columns = ["group", "last_before", "first_missing", "last_missing", "days"]
    rows: list[dict] = []
    one_day = pd.Timedelta(days=1)
    for key, group_days in groups:
        seen = sorted(set(group_days.dropna()))
        for before, after in zip(seen, seen[1:], strict=False):
            missing = (after - before).days - 1
            if missing >= min_days:
                rows.append(
                    {
                        "group": key,
                        "last_before": _day(before),
                        "first_missing": _day(before + one_day),
                        "last_missing": _day(after - one_day),
                        "days": int(missing),
                    }
                )
    found = pd.DataFrame(rows, columns=columns)
    title = f"Gaps in {date_col}" if by is None else f"Gaps in {date_col} by {by}"
    note = None if rows else f"No runs of {min_days} or more missing days."
    table(title, found, note=note)
    result("Gaps found", len(rows))
    per_group = Counter(str(r["group"]) for r in rows)
    numbered: Counter[str] = Counter()
    for gap in rows[:MAX_GAP_RESULTS]:
        group = str(gap["group"])
        numbered[group] += 1
        label = group if per_group[group] == 1 else f"{group} gap {numbered[group]}"
        result(f"{label}: first missing day", gap["first_missing"])
        result(f"{label}: last missing day", gap["last_missing"])
        result(f"{label}: missing days", gap["days"], unit="days")
    return found
