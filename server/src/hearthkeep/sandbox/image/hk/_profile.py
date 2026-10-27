"""`hk.profile`: the at-a-glance look at a table and the problems worth knowing about."""

from __future__ import annotations

import re
from collections import Counter

import pandas as pd

from hk._dates import FORMATS_ATTR, MAX_GAP_RESULTS, find_gaps, report_gaps
from hk._dates import date_format as _date_format
from hk._read import read
from hk._results import cleaned, result, section, table

THOUSANDS = re.compile(r"\(?[-+−]?[£$€]?\s?\d{1,3}(,\d{3})+(\.\d+)?\)?")
NUMBER_TEXT = re.compile(r"\(?[-+−]?[£$€]?\s?[\d][\d,]*(\.\d+)?\s?[£$€%]?\)?")


def _texts(series: pd.Series) -> pd.Series:
    return series[series.map(lambda v: isinstance(v, str) and bool(v.strip()))].map(str.strip)


def _mixed_dates(df: pd.DataFrame, col) -> None:
    texts = _texts(df[col])
    if texts.empty:
        return
    formats = texts.map(_date_format)
    known = formats.dropna()
    if len(known) < max(2, len(texts) // 2):
        return
    counts = Counter(known)
    if len(counts) < 2:
        return
    common = counts.most_common(1)[0][0]
    _report_mixed(df, col, dict(counts.most_common()), formats[formats != common].index)


def _report_mixed(df: pd.DataFrame, col, counts: dict[str, int], odd) -> None:
    common = next(iter(counts))
    listed = ", ".join(f"{name} ({n})" for name, n in counts.items())
    cleaned(f"Column {col} mixes date formats: {listed}.")
    table(
        f"Dates not in the usual format in {col}",
        df.loc[odd],
        note=f"Most dates in {col} are {common}.",
    )


def _numbers_as_text(df: pd.DataFrame, col) -> None:
    texts = _texts(df[col])
    if texts.empty:
        return
    numeric_text = texts[texts.map(lambda s: bool(NUMBER_TEXT.fullmatch(s)))]
    real_numbers = df[col].map(lambda v: isinstance(v, int | float) and not isinstance(v, bool))
    present = int(df[col].notna().sum())
    if numeric_text.empty or len(numeric_text) + int(real_numbers.sum()) < present * 0.5:
        return
    cleaned(f"Column {col} has {len(numeric_text)} numbers stored as text.")
    thousands = numeric_text[numeric_text.map(lambda s: bool(THOUSANDS.fullmatch(s)))]
    if not thousands.empty:
        cleaned(
            f"Column {col} uses thousands separators ({len(thousands)} values, e.g. "
            f"{thousands.iloc[0]})."
        )


GAP_GROUP_VALUES = (2, 20)  # a text column with this many distinct values groups the rows
GAP_GROUP_COLUMNS = 3
GAP_MIN_DAYS = 3


def _date_gaps(df: pd.DataFrame) -> None:
    """Gaps in the first date column, per value of each small text column (at most 3),
    reported only when there are some, so a small model sees them without being asked."""
    dated = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if not dated:
        return
    groups = []
    for col in df.columns:
        series = df[col]
        if series.dtype != object or not series.dropna().map(lambda v: isinstance(v, str)).all():
            continue
        if GAP_GROUP_VALUES[0] <= series.nunique() <= GAP_GROUP_VALUES[1]:
            groups.append(col)
    left = MAX_GAP_RESULTS
    days = df[dated[0]].dt.normalize()
    for col in groups[:GAP_GROUP_COLUMNS]:
        rows = find_gaps(days, df[col], GAP_MIN_DAYS)
        if rows:
            left -= report_gaps(rows, str(dated[0]), str(col), GAP_MIN_DAYS, results_left=left)


def profile(file: str, *, sheet: str | int | None = None) -> None:
    """At a glance: rows, columns, missing values, and notes on dates, numbers stored as
    text, thousands separators, a header below line 1, negative amounts, duplicate rows
    and gaps of 3 or more days in dated rows per group (problem rows reported as
    tables)."""
    df = read(file, sheet=sheet)
    section("glance")
    result("rows", len(df))
    result("columns", len(df.columns))
    for col in df.columns:
        missing = int(df[col].isna().sum())
        if missing:
            result(f"missing in {col}", missing)

    header_row = df.attrs.get("hk_header_row")
    if isinstance(header_row, int) and header_row > 1:
        cleaned(f"The header is on line {header_row}, not line 1; the lines above it were skipped.")

    for col in df.columns:
        series = df[col]
        if series.dtype == object:
            _mixed_dates(df, col)
            _numbers_as_text(df, col)
        elif col in df.attrs.get(FORMATS_ATTR, {}):  # dates hk.read parsed from mixed formats
            seen = df.attrs[FORMATS_ATTR][col]
            _report_mixed(df, col, seen["counts"], seen["odd"])
        elif pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            negative = series[series < 0]
            if not negative.empty:
                n = len(negative)
                cleaned(f"Column {col} has {n} negative value{'' if n == 1 else 's'}.")
                table(f"Negative values in {col}", df.loc[negative.index])

    duplicated = df.duplicated()
    if duplicated.any():
        count = int(duplicated.sum())
        copies = "row is an exact copy" if count == 1 else "rows are exact copies"
        cleaned(f"{count} {copies} of an earlier row.")
        table("Duplicate rows", df[duplicated])

    _date_gaps(df)
