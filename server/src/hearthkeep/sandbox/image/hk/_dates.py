"""Dates the way the owner wrote them (`hk.dates`) and the days missing from dated data
(`hk.gaps`)."""

from __future__ import annotations

import datetime as _dt
import re
from collections import Counter

import numpy as np
import pandas as pd

from hk._results import cleaned, note_once, result, table

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
# What hk.read turns into dates by itself: whole values only (a time is allowed, any other
# trailing text is not), with a four-digit year, so codes and version numbers stay text.
AUTO_DATE_SHAPES: tuple[re.Pattern[str], ...] = (
    ISO_DATE,
    re.compile(r"\d{1,2}([/.-])\d{1,2}\1\d{4}( \d{1,2}:\d{2}(:\d{2})?)?"),
    re.compile(r"\d{4}([/-])\d{1,2}\1\d{1,2}"),
)
AUTO_YEARS = (1900, 2100)  # a column that parses to years outside this stays text
FORMATS_ATTR = "hk_date_formats"  # Series.attrs key: which rows were written another way
MAX_GAP_RESULTS = 10
GAP_COLUMNS = ["group", "last_before", "first_missing", "last_missing", "days"]


def date_format(value: str) -> str | None:
    for name, pattern in DATE_FORMATS:
        if pattern.fullmatch(value):
            return name
    return None


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
    decided by the column's own values. What was assumed is added to `notes`; values that
    don't parse stay NaT and are counted in a note. A column whose values say both day first
    and month first comes back as text."""
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

    failed = present & out.isna()
    dmy_read = dmy & out.notna()
    if dmy_read.any():  # only the dates that were read; failures get their own note
        first = matches[dmy_read].iloc[0]
        sep, year = first.group(2), "Y" * len(first.group(4))
        shape = f"DD{sep}MM{sep}{year}" if dayfirst else f"MM{sep}DD{sep}{year}"
        order = "day/month/year" if dayfirst else "month/day/year"
        note = (
            f"Read {_plural(int(dmy_read.sum()), 'date', 'dates')} in {where} written {shape} "
            f"(e.g. {texts[dmy_read].iloc[0]}) as {order}"
        )
        if not (day_first_seen or month_first_seen):
            note += " (no part is over 12, so day first was assumed)"
        iso_read = iso & out.notna()
        if iso_read.any():
            note += (
                "; the others are YYYY-MM-DD"
                if not (other.any() or is_date.any() or failed.any())
                else f"; {int(iso_read.sum())} are YYYY-MM-DD"
            )
        said.append(note + ".")
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


def auto_dates(values: pd.Series, notes: list[str] | None = None) -> pd.Series | None:
    """`values` as dates when hk.read may decide that by itself: every present value is a
    whole date (AUTO_DATE_SHAPES), every one parses and every year is plausible. Otherwise
    None, and the column stays text exactly as it was (codes, version numbers, a date with
    words after it)."""
    texts = values.dropna()
    if texts.empty or not texts.map(lambda v: isinstance(v, str)).all():
        return None
    if not texts.map(lambda s: any(p.fullmatch(s) for p in AUTO_DATE_SHAPES)).all():
        return None
    said: list[str] = []
    parsed = parse_dates(values, said)
    if not pd.api.types.is_datetime64_any_dtype(parsed):
        return None
    if parsed[values.notna()].isna().any():
        return None
    years = parsed.dropna().dt.year
    if ((years < AUTO_YEARS[0]) | (years > AUTO_YEARS[1])).any():
        return None
    if notes is not None:
        notes.extend(said)
    return parsed


def dates(values) -> pd.Series:
    """A column of dates written as text, read as dates: YYYY-MM-DD as ISO, and
    DD/MM/YYYY (or MM/DD/YYYY, DD.MM.YYYY, DD-MM-YYYY) day first or month first as the
    column's own values show. What was assumed becomes a cleaning note; values that aren't
    dates become empty (NaT) and are counted in a note. Use it as
    df["date"] = hk.dates(df["date"]); never pd.to_datetime(..., dayfirst=True)."""
    notes: list[str] = []
    out = parse_dates(values, notes)
    out.attrs.pop(FORMATS_ATTR, None)
    for note in notes:
        note_once(note)
    return out


# ---------------------------------------------------------------- gaps


def _day(value) -> _dt.date:
    return pd.Timestamp(value).date()


def _as_days(df: pd.DataFrame, date_col: str) -> pd.Series:
    when = df[date_col]
    if not pd.api.types.is_datetime64_any_dtype(when):
        when = dates(when)
        if not pd.api.types.is_datetime64_any_dtype(when):
            raise ValueError(f"column {date_col} isn't dates; see the cleaning note")
    return _naive(when).dt.normalize()


def find_gaps(days: pd.Series, keys: pd.Series | None, min_days: int) -> list[dict]:
    """Runs of at least `min_days` missing calendar days per group. Every group is bounded
    by the whole file's first and last date, so a group that starts late or stops early has
    a gap at that edge (its `last_before` is None when the gap starts the file)."""
    present = days.dropna()
    if present.empty:
        return []
    start, end = present.min(), present.max()
    one_day = pd.Timedelta(days=1)
    groups = [("all", days)] if keys is None else list(days.groupby(keys, sort=True))
    by_group = {key: set(group_days.dropna()) for key, group_days in groups}
    rows: list[dict] = []
    for key, seen_set in by_group.items():
        seen = sorted(seen_set)
        if not seen:
            continue
        bounds = [start - one_day, *seen, end + one_day]  # the file's edges as sentinels
        for before, after in zip(bounds, bounds[1:], strict=False):
            missing = (after - before).days - 1
            if missing < min_days:
                continue
            first, last = before + one_day, after - one_day
            others = any(
                any(first <= d <= last for d in other_days)
                for other_key, other_days in by_group.items()
                if other_key != key
            )
            rows.append(
                {
                    "group": key,
                    "last_before": None if before < start else _day(before),
                    "first_missing": _day(first),
                    "last_missing": _day(last),
                    "days": int(missing),
                    "starts_file": before < start,
                    "ends_file": after > end,
                    "others_have_rows": others,
                }
            )
    return rows


def _gap_line(gap: dict, by: str | None) -> str:
    edge = ""
    if gap["starts_file"]:
        edge = ", from the start of the file"
    elif gap["ends_file"]:
        edge = ", to the end of the file"
    span = (
        f"from {gap['first_missing'].isoformat()} to {gap['last_missing'].isoformat()} "
        f"({_plural(gap['days'], 'day', 'days')}{edge})"
    )
    if by is None:
        return f"There are no rows {span}."
    tail = f", while other {by} values do" if gap["others_have_rows"] else ""
    return f"{gap['group']} has no rows {span}{tail}."


def report_gaps(
    rows: list[dict],
    date_col: str,
    by: str | None,
    min_days: int,
    *,
    results_left: int,
    lines_left: int | None = None,
) -> tuple[int, int]:
    """The gaps as a table, one plain line each (while `lines_left` lasts; None means every
    gap), and (while `results_left` lasts) a result for each gap's first missing day, last
    missing day and day count. Returns (gaps given results, lines written)."""
    title = f"Gaps in {date_col}" if by is None else f"Gaps in {date_col} by {by}"
    found = pd.DataFrame([{c: r[c] for c in GAP_COLUMNS} for r in rows], columns=GAP_COLUMNS)
    if not rows:
        note = f"No runs of {min_days} or more missing days."
    elif any(r["starts_file"] or r["ends_file"] for r in rows):
        note = (
            "Every group is checked from the file's first date to its last; a gap at either "
            "end runs to that date, and last_before is empty when the gap starts the file."
        )
    else:
        note = None
    table(title, found, note=note)
    lined = rows if lines_left is None else rows[: max(lines_left, 0)]
    for gap in lined:
        cleaned(_gap_line(gap, by))
    per_group = Counter(str(r["group"]) for r in rows)
    numbered: Counter[str] = Counter()
    reported = 0
    for gap in rows[: max(results_left, 0)]:
        group = str(gap["group"])
        numbered[group] += 1
        label = group if per_group[group] == 1 else f"{group} gap {numbered[group]}"
        result(f"{label}: first missing day", gap["first_missing"])
        result(f"{label}: last missing day", gap["last_missing"])
        result(f"{label}: missing days", gap["days"], unit="days")
        reported += 1
    return reported, len(lined)


def gaps(
    df: pd.DataFrame, date_col: str, *, by: str | None = None, min_days: int = 2
) -> pd.DataFrame:
    """Runs of missing calendar days in `date_col` (per value of `by`), at least `min_days`
    long, each group checked from the file's first date to its last. Reported as the table
    "Gaps in <date_col> by <by>" (columns group, last_before, first_missing, last_missing,
    days), one plain line per gap and, for the first 10 gaps, a result for the first missing
    day, the last missing day and the number of days. Returns the table."""
    if not isinstance(df, pd.DataFrame):
        raise TypeError("hk.gaps takes a pandas DataFrame")
    for col in (date_col,) if by is None else (date_col, by):
        if col not in df.columns:
            raise KeyError(f"no such column: {col!r}")
    if not isinstance(min_days, int) or isinstance(min_days, bool) or min_days < 1:
        raise ValueError("min_days must be a whole number of days (1 or more)")
    days = _as_days(df, date_col)
    rows = find_gaps(days, None if by is None else df[by], min_days)
    report_gaps(rows, date_col, by, min_days, results_left=MAX_GAP_RESULTS)
    result("Gaps found", len(rows))
    return pd.DataFrame([{c: r[c] for c in GAP_COLUMNS} for r in rows], columns=GAP_COLUMNS)
