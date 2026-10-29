"""Where a script's findings go: one JSON object per line in `<out>/results.jsonl`."""

from __future__ import annotations

import datetime as _dt
import decimal
import json
import math
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd

MAX_TABLE_ROWS = 500
MAX_ITEM_RESULTS = 20  # a dict or Series given to hk.result reports this many items at most
_MANY = (dict, list, tuple, set, frozenset, pd.Series, pd.DataFrame, np.ndarray)
# At most 100 characters, so `files/<name>` fits a plain ustar header (prefix + name).
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")
NOT_A_NUMBER = "not a number"
ROW_INDEX = "hk_row"  # the index name hk.read gives
_NOTED: set[str] = set()  # reading notes already emitted by this script (one process)


def out_dir() -> Path:
    return Path(os.environ.get("HK_OUT", "/out"))


def emit(record: dict) -> None:
    """Append one record to results.jsonl (strict JSON: no NaN or Infinity)."""
    line = json.dumps(record, ensure_ascii=False, allow_nan=False)
    path = out_dir() / "results.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _is_missing(value) -> bool:
    if value is None or value is pd.NaT:
        return True
    if isinstance(value, float | np.floating):
        return not math.isfinite(float(value))
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _from_numpy(value):
    """numpy dates and durations become pandas ones (their .item() can be a bare int)."""
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value)
    if isinstance(value, np.timedelta64):
        return pd.Timedelta(value)
    if isinstance(value, np.generic):
        return value.item()
    return value


def plain(value):
    """A JSON-safe copy of one cell or scalar: numpy → Python, dates → ISO text, NaN → None."""
    value = _from_numpy(value)
    if isinstance(value, np.generic):
        value = value.item()
    if _is_missing(value):
        return None
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, _dt.datetime | _dt.date | _dt.time):
        return value.isoformat()
    if isinstance(value, pd.Timedelta | _dt.timedelta):
        return pd.Timedelta(value).isoformat()  # same as hk.result, e.g. P0DT1H30M0S
    return str(value)


def _value_record(name: str, value, *, unit, note, kind: str, page) -> dict:
    if not isinstance(name, str):
        raise TypeError(
            "hk.result(name, value): the name comes first, e.g. hk.result('Total spent', 98.46)"
        )
    if not name.strip():
        raise ValueError("a result needs a name")
    value = _from_numpy(value)
    is_date = isinstance(value, _dt.date | pd.Timestamp) and value is not pd.NaT
    if _is_missing(value):
        value = None
        note = NOT_A_NUMBER if note is None else f"{note} ({NOT_A_NUMBER})"
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int | float | str):
        pass
    elif isinstance(value, decimal.Decimal):
        value = float(value)
    elif is_date:
        value = value.isoformat()
    elif isinstance(value, _dt.timedelta):
        value = pd.Timedelta(value).isoformat()  # a duration, e.g. P0DT1H30M0S
    else:
        raise TypeError(
            f"hk.result takes a number, a date or text, not {type(value).__name__}; "
            "use hk.table for many values"
        )
    return {
        "type": "value",
        "name": name,
        "value": value,
        "unit": unit,
        "note": note,
        "is_date": bool(is_date),
        "kind": kind,
        "page": page,
    }


def _report(name: str, value, *, unit, note, kind: str, page) -> None:
    """One value, or one result per item of a dict or Series ("<name>: <key>", in order,
    at most 20). Items that aren't single values are skipped with a note."""
    if not isinstance(value, dict | pd.Series):
        emit(_value_record(name, value, unit=unit, note=note, kind=kind, page=page))
        return
    if not isinstance(name, str):
        _value_record(name, 0, unit=None, note=None, kind=kind, page=None)  # raises
    items = list(value.items())
    reported = 0
    skipped: list[str] = []
    for key, item in items:
        if isinstance(_from_numpy(item), _MANY):
            skipped.append(str(key))
            continue
        if reported < MAX_ITEM_RESULTS:
            label = f"{name}: {key}"
            emit(_value_record(label, item, unit=unit, note=note, kind=kind, page=page))
            reported += 1
    if skipped:
        shown = ", ".join(skipped[:5]) + (", …" if len(skipped) > 5 else "")
        cleaned(
            f"Skipped {len(skipped)} value{'' if len(skipped) == 1 else 's'} for {name} that "
            f"aren't single values ({shown}); use hk.table for them."
        )
    singles = len(items) - len(skipped)
    if singles > MAX_ITEM_RESULTS:
        cleaned(
            f"Reported the first {MAX_ITEM_RESULTS} of {singles} values for {name}; "
            "use hk.table for all of them."
        )


def result(name: str, value, *, unit: str | None = None, note: str | None = None) -> None:
    """Report one computed value (a number, a count, a date or a short text). A dict or a
    Series reports one value per item, named "<name>: <key>" (the first 20)."""
    _report(name, value, unit=unit, note=note, kind="computed", page=None)


def document(name: str, value, *, page: int, unit: str | None = None) -> None:
    """Report a value read straight from a document page, not computed (a dict or Series:
    one per item, as hk.result)."""
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("page must be a page number (1 or more)")
    _report(name, value, unit=unit, note=None, kind="document", page=page)


def table(
    name: str, df: pd.DataFrame, *, columns: list[str] | None = None, note: str | None = None
) -> None:
    """Report rows to show the owner (the first 500 are kept; total_rows is the real count).

    row_numbers are the rows in the original file (index + the file row of the first data
    row), so the owner can find them. They are kept only while the rows still carry hk.read's
    index (named "hk_row": filtering, sorting and head keep it); otherwise, e.g. after
    reset_index or groupby, the list is empty.
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError("hk.table takes a pandas DataFrame")
    cols = list(df.columns) if columns is None else list(columns)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"no such column: {missing[0]!r}")
    kept = df[cols].head(MAX_TABLE_ROWS)
    first = df.attrs.get("hk_first_data_row")
    row_numbers: list[int] = []
    if kept.index.name == ROW_INDEX and isinstance(first, int):
        row_numbers = [int(i) + first for i in kept.index]
    rows = [[plain(v) for v in row] for row in kept.itertuples(index=False, name=None)]
    emit(
        {
            "type": "table",
            "name": name,
            "columns": [str(c) for c in cols],
            "rows": rows,
            "row_numbers": row_numbers,
            "note": note,
            "total_rows": int(len(df)),
        }
    )


def file(name: str, data) -> None:
    """Save a file for the owner to download: a DataFrame as .csv (UTF-8 with BOM, so Excel
    reads it) or .xlsx, or bytes/text as they are."""
    if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
        raise ValueError(
            "file names use letters, digits, spaces, dots, dashes and underscores, start with "
            "a letter or digit and are at most 100 characters"
        )
    target = out_dir() / "files" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    lower = name.lower()
    if isinstance(data, pd.DataFrame):
        if lower.endswith(".csv"):
            data.to_csv(target, index=False, encoding="utf-8-sig")
        elif lower.endswith(".xlsx"):
            data.to_excel(target, index=False, engine="openpyxl")
        else:
            raise ValueError("a table is saved as a .csv or .xlsx file")
    elif isinstance(data, bytes | bytearray):
        target.write_bytes(bytes(data))
    elif isinstance(data, str):
        target.write_text(data, encoding="utf-8")
    else:
        raise TypeError("hk.file takes a DataFrame, bytes or text")
    emit({"type": "file", "name": name, "member": f"files/{name}"})


def cleaned(note: str) -> None:
    """Say what was cleaned or assumed (shown to the owner under `How I got this`)."""
    if not isinstance(note, str) or not note.strip():
        raise ValueError("a cleaning note needs text")
    emit({"type": "cleaning", "note": note})


def note_once(note: str) -> None:
    """A cleaning note from reading the files, emitted once per script however often the
    same file is read."""
    if note not in _NOTED:
        _NOTED.add(note)
        cleaned(note)


def section(name: str) -> None:
    emit({"type": "section", "name": name})
