"""`hk`: the helpers Hearthkeep's analysis scripts use inside the sandbox.

Read the chat's files, report what you found and why, and save files for the owner:

    import hk
    df = hk.read("<file id or name>")
    hk.result("Total spent", df["Amount"].sum(), unit="GBP")
    hk.bar(by_month, "Month", "Amount", title="Spending by month")
    hk.table("Largest payments", df.nlargest(10, "Amount"))
    hk.cleaned("Dropped 2 duplicate rows.")
    df["Date"] = hk.dates(df["Date"])  # only if hk.read left a date column as text
    hk.gaps(df, "Date", by="Region")  # runs of missing days, per region

Everything reported goes to results.jsonl in the run's output folder.
"""

from hk._charts import bar, chart, line, scatter
from hk._dates import dates, gaps
from hk._pdf import pdf_tables, pdf_text
from hk._profile import profile
from hk._read import files, read
from hk._results import cleaned, document, file, result, table

__all__ = [
    "bar",
    "chart",
    "cleaned",
    "dates",
    "document",
    "file",
    "files",
    "gaps",
    "line",
    "pdf_tables",
    "pdf_text",
    "profile",
    "read",
    "result",
    "scatter",
    "table",
]
