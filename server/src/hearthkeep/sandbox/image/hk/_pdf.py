"""Text and tables from PDFs (opened with an empty password when the PDF allows it)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from hk._read import column_names, find

PASSWORD_MESSAGE = "This file is password-protected. Remove the password and attach it again."


class PasswordProtected(Exception):
    """The PDF (or workbook) needs a password to open."""


def is_password_error(exc: BaseException) -> bool:
    """True when `exc` (or anything it wraps) says the file needs a password."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        name = type(e).__name__.lower()
        text = " ".join(str(a) for a in getattr(e, "args", ())).lower()
        if "password" in name or "password" in text or "encrypt" in text:
            return True
        stack += [a for a in getattr(e, "args", ()) if isinstance(a, BaseException)]
        stack += [x for x in (e.__cause__, e.__context__) if x is not None]
    return False


def open_pdf(path: Path):
    import pdfplumber

    try:
        return pdfplumber.open(str(path), password="")
    except Exception as exc:  # pdfplumber wraps pdfminer's errors
        if is_password_error(exc):
            raise PasswordProtected(PASSWORD_MESSAGE) from None
        raise


def _pdf_path(file: str) -> Path:
    entry = find(file)
    if entry.get("kind") != "pdf":
        raise ValueError(f"{file!r} is not a PDF")
    return Path(entry["path"])


def _wanted(pages: list[int] | None, count: int) -> list[int]:
    if pages is None:
        return list(range(1, count + 1))
    return [p for p in pages if isinstance(p, int) and 1 <= p <= count]


def pdf_text(file: str, pages: list[int] | None = None) -> list[dict]:
    """`[{"page", "text"}]` for the given 1-based pages (all by default)."""
    with open_pdf(_pdf_path(file)) as pdf:
        return [
            {"page": n, "text": pdf.pages[n - 1].extract_text() or ""}
            for n in _wanted(pages, len(pdf.pages))
        ]


def pdf_tables(file: str, pages: list[int] | None = None) -> list[dict]:
    """`[{"page", "index", "df"}]`: every table found, its first row used as the header."""
    found = []
    with open_pdf(_pdf_path(file)) as pdf:
        for n in _wanted(pages, len(pdf.pages)):
            for index, rows in enumerate(pdf.pages[n - 1].extract_tables()):
                rows = [list(r) for r in rows if any(c not in (None, "") for c in r)]
                if not rows:
                    continue
                width = max(len(r) for r in rows)
                rows = [(r + [None] * width)[:width] for r in rows]
                df = pd.DataFrame(rows[1:], columns=column_names(rows[0]))
                df.attrs["hk_first_data_row"] = None
                found.append({"page": n, "index": index, "df": df})
    return found
