"""`hk.profile`: the at-a-glance look at a table and the problems worth knowing about."""

from __future__ import annotations

import re
from collections import Counter

import pandas as pd

from hk._read import read
from hk._results import cleaned, result, section, table

DATE_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("YYYY-MM-DD", re.compile(r"\d{4}-\d{1,2}-\d{1,2}([ T].*)?")),
    ("DD/MM/YYYY or MM/DD/YYYY", re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}( .*)?")),
    ("DD.MM.YYYY", re.compile(r"\d{1,2}\.\d{1,2}\.\d{2,4}( .*)?")),
    ("DD-MM-YYYY", re.compile(r"\d{1,2}-\d{1,2}-\d{2,4}( .*)?")),
    ("YYYY/MM/DD", re.compile(r"\d{4}/\d{1,2}/\d{1,2}( .*)?")),
    ("D Month YYYY", re.compile(r"\d{1,2}[ -][A-Za-z]{3,9}\.?[ -,]+\d{2,4}")),
    ("Month D, YYYY", re.compile(r"[A-Za-z]{3,9}\.? \d{1,2},? \d{4}")),
)
THOUSANDS = re.compile(r"\(?[-+−]?[£$€]?\s?\d{1,3}(,\d{3})+(\.\d+)?\)?")
NUMBER_TEXT = re.compile(r"\(?[-+−]?[£$€]?\s?[\d][\d,]*(\.\d+)?\s?[£$€%]?\)?")


def _date_format(value: str) -> str | None:
    for name, pattern in DATE_FORMATS:
        if pattern.fullmatch(value):
            return name
    return None


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
    listed = ", ".join(f"{name} ({n})" for name, n in counts.most_common())
    cleaned(f"Column {col} mixes date formats: {listed}.")
    odd = formats[formats != common].index
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


def profile(file: str, *, sheet: str | int | None = None) -> None:
    """At a glance: rows, columns, missing values, and notes on dates, numbers stored as
    text, thousands separators, a header below line 1, negative amounts and duplicate
    rows (problem rows reported as tables)."""
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
