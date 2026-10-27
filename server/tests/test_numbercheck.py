import datetime
import json
import logging
import math
import time

import pytest

from hearthkeep import numbercheck
from hearthkeep.db import connect
from hearthkeep.files.store import ChatFileStore
from hearthkeep.numbercheck import (
    UNVERIFIED_NOTE,
    NumberChecker,
    Reported,
    check_reply,
    extract_mentions,
    reply_spans,
    reply_text,
    reported_values,
)
from hearthkeep.sandbox.runs import RunStore

CHAT = "c" * 32
D = datetime.date


def num(value, kind="computed", page=None, source="value", percent=False):
    return Reported(float(value), None, kind, page, source, percent)


def cell(value):
    return num(value, source="table")


def day(iso, kind="computed", page=None):
    return Reported(None, D.fromisoformat(iso), kind, page)


def marks_by_text(marks):
    return {m.text: m for m in marks}


# ---- connection-backed fixtures ------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "hk.db")
    yield c
    c.close()


@pytest.fixture
def runs(conn, tmp_path):
    return RunStore(conn, tmp_path)


@pytest.fixture
def files(conn, tmp_path):
    return ChatFileStore(conn, tmp_path)


_seq = iter(range(1, 10**6))


def add_file(conn, name, chat_id=CHAT):
    n = next(_seq)
    conn.execute(
        "INSERT INTO chat_files (id, chat_id, name, kind, mime, size, status, source, file_name,"
        " created_at) VALUES (?, ?, ?, 'csv', 'text/csv', 1, 'ready', 'upload', ?, ?)",
        (f"f{n}", chat_id, name, f"f{n}.csv", f"2026-01-01T00:00:{n % 60:02d}"),
    )


def add_run(conn, *, values=(), stdout="", purpose="analysis", tables=(), chat_id=CHAT,
            created_at=None):  # fmt: skip
    n = next(_seq)
    run_id = f"r{n}"
    results = {"values": [dict(v) for v in values], "cleaning": [], "sections": []}
    conn.execute(
        "INSERT INTO runs (id, chat_id, turn_id, purpose, script, stdout, stdout_truncated,"
        " status, error, duration_ms, results, problems, created_at)"
        " VALUES (?, ?, 't0', ?, 'x', ?, 0, 'ok', NULL, 1, ?, '[]', ?)",
        (run_id, chat_id, purpose, stdout, json.dumps(results),
         created_at or f"2026-01-01T00:{n // 60 % 60:02d}:{n % 60:02d}"),
    )  # fmt: skip
    for t in tables:
        body = json.dumps(t)
        conn.execute(
            "INSERT INTO outputs (id, run_id, chat_id, kind, name, mime, size, data, created_at)"
            " VALUES (?, ?, ?, 'table', 'T', 'application/json', ?, ?, '2026-01-01')",
            (f"o{next(_seq)}", run_id, chat_id, len(body), body),
        )
    return run_id


def value(name, v, *, kind="computed", page=None, is_date=False):
    return {"name": name, "value": v, "unit": None, "note": None, "is_date": is_date,
            "kind": kind, "page": page}  # fmt: skip


# ---- matching --------------------------------------------------------------------------------


def test_formats_match_reported_values():
    reported = [num(-0.3112), num(1187), num(4210.5), num(-3), day("2024-03-12"),
                day("2024-03-24")]  # fmt: skip
    reply = "Sales fell 31%, 1.2k orders, £4,210.50 in refunds, −3 items, no orders 12–24 March."
    marks = check_reply(reply, reported, [])
    assert [(m.text, m.kind) for m in marks] == [
        ("31%", "computed"),
        ("1.2k", "computed"),
        ("£4,210.50", "computed"),
        ("−3", "computed"),
        ("12–24 March", "computed"),
    ]
    for m in marks:
        assert reply[m.start : m.end] == m.text
    wrong = check_reply("£4,210.60 in refunds", reported, [])
    assert [(m.text, m.kind, m.note) for m in wrong] == [
        ("£4,210.60", "unverified", UNVERIFIED_NOTE)
    ]


def test_rounding_tolerance_follows_written_precision():
    reported = [num(31.12, percent=True), num(2.6)]
    assert [m.kind for m in check_reply("31.1%", reported, [])] == ["computed"]
    assert [m.kind for m in check_reply("31.2%", reported, [])] == ["unverified"]
    assert [m.kind for m in check_reply("about 3 items", reported, [])] == ["computed"]
    # More digits written means a tighter match.
    assert [m.kind for m in check_reply("2.60", reported, [])] == ["computed"]
    assert [m.kind for m in check_reply("2.61", reported, [])] == ["unverified"]


def test_sign_written_must_match_sign_reported():
    reported = [num(3)]
    assert [m.kind for m in check_reply("−3 items", reported, [])] == ["unverified"]
    assert [m.kind for m in check_reply("-3 items", [num(-3)], [])] == ["computed"]
    # A plain number may state the size of a negative value ("fell 3").
    assert [m.kind for m in check_reply("fell by 3", [num(-3)], [])] == ["computed"]


def test_scales_and_percent_words():
    reported = [num(2_400_000), num(3_100_000_000), num(0.125), num(12_000)]
    marks = check_reply(
        "2.4m users, 3.1bn rows, 12.5 percent, 12 thousand lines, 2.4 million again", reported, []
    )
    assert [(m.text, m.kind) for m in marks] == [
        ("2.4m", "computed"),
        ("3.1bn", "computed"),
        ("12.5 percent", "computed"),
        ("12 thousand", "computed"),
        ("2.4 million", "computed"),
    ]


def test_exempt_numbers_from_question_and_file_names():
    reported = [num(1)]
    marks = check_reply(
        "In March 2024 the total in 2024 was 7; sales-2023.csv covers 2023.",
        reported,
        ["what happened in March 2024?"],
        file_names=["sales-2023.csv"],
    )
    texts = [m.text for m in marks]
    assert "2024" not in texts and "March 2024" not in texts
    assert "2023" not in texts
    assert texts == ["7"]


def test_exempt_text_drops_attachment_lines(conn, runs, files):
    add_file(conn, "a.csv")
    add_run(conn, values=[value("total", 1)])
    checker = NumberChecker(conn, runs, files)
    question = "Attached file: 123\nAttached photo: 456\nHow many rows?"
    # The id in the attachment line would otherwise exempt the "123" in the reply.
    marks = checker.marks_for(CHAT, "123 rows", question=question)
    assert [(m.text, m.kind) for m in marks] == [("123", "unverified")]


def test_document_values_marked_with_page():
    reported = [num(1200, "document", 2), day("2024-01-31", "document", 3), num(5)]
    marks = check_reply("Opening balance 1,200.00 on 2024-01-31, 5 rows.", reported, [])
    assert [(m.text, m.kind, m.page) for m in marks] == [
        ("1,200.00", "document", 2),
        ("2024-01-31", "document", 3),
        ("5", "computed", None),
    ]
    # A computed match wins over a document one.
    both = check_reply("1,200", [num(1200, "document", 2), num(1200)], [])
    assert [(m.kind, m.page) for m in both] == [("computed", None)]


def test_unverified_has_note():
    marks = check_reply("The total is 99.", [num(1)], [])
    assert len(marks) == 1
    assert marks[0].kind == "unverified"
    assert marks[0].note == UNVERIFIED_NOTE
    assert UNVERIFIED_NOTE == "Not found in any result from this chat's analysis."
    assert marks[0].page is None


def test_list_markers_code_and_identifiers_ignored():
    reply = (
        "1. First point\n"
        "  2) Second point\n"
        "Use `df.head(5)` and Q4 sales in col_2 of H2O.\n"
        "```\nprint(42)\nx = 7\n```\n"
        "Then 8 rows."
    )
    mentions = extract_mentions(reply)
    assert [m.text for m in mentions] == ["8"]


def test_counts_and_dates_are_numbers():
    reply = (
        "There were 12 orders on 2024-03-12, the 12th, 3/4/2024, March 12, 12 March 2024, "
        "in March 2024 and in 2024."
    )
    mentions = extract_mentions(reply)
    by_text = {m.text: m for m in mentions}
    assert by_text["12"].value == 12
    assert by_text["2024-03-12"].date == D(2024, 3, 12)
    assert by_text["12th"].value == 12
    assert by_text["March 12"].month_day_only
    assert by_text["12 March 2024"].date == D(2024, 3, 12)
    assert not by_text["12 March 2024"].month_day_only
    assert by_text["2024"].year_only and by_text["2024"].value == 2024
    assert by_text["March 2024"].date.month == 3

    reported = [num(12), day("2024-03-12"), day("2024-04-03")]
    marks = marks_by_text(check_reply(reply, reported, []))
    assert all(m.kind == "computed" for m in marks.values()), [
        (m.text, m.kind) for m in marks.values()
    ]
    # Slash dates are read day first.
    assert check_reply("on 3/4/2024", [day("2024-04-03")], [])[0].kind == "computed"
    assert check_reply("on 3/4/2024", [day("2024-03-04")], [])[0].kind == "unverified"
    # A month-day date ignores the year; month and year dates match any day in them.
    assert check_reply("on 12 March", [day("2019-03-12")], [])[0].kind == "computed"
    assert check_reply("in March 2024", [day("2023-03-01")], [])[0].kind == "unverified"
    # A bare year matches an equal number or any date in that year.
    assert check_reply("in 2023", [num(2023)], [])[0].kind == "computed"
    assert check_reply("in 2023", [day("2024-01-01")], [])[0].kind == "unverified"
    # A range needs both ends.
    assert check_reply("12–24 March", [day("2024-03-12")], [])[0].kind == "unverified"


def test_durations_are_always_unverified():
    reported = [num(90), num(2), num(1), num(3)]
    marks = check_reply(
        "It took 1h 30m, or 90 minutes; another took 45min, then 2 weeks, 1 year and 3 days.",
        reported,
        [],
    )
    assert [(m.text, m.kind) for m in marks] == [
        ("1h 30m", "unverified"),
        ("90 minutes", "unverified"),
        ("45min", "unverified"),
        ("2 weeks", "unverified"),
        ("1 year", "unverified"),
        ("3 days", "unverified"),
    ]


def test_odd_number_shapes_are_flagged_not_misread():
    marks = check_reply("version 1.2.3 and 1,23", [num(1), num(1.2), num(23)], [])
    assert [(m.text, m.kind) for m in marks] == [("1.2.3", "unverified"), ("1,23", "unverified")]


def test_reply_text_joins_like_the_web_app():
    assert reply_text(["Let me look.", "  ", "", "It is 5."]) == "Let me look.\n\nIt is 5."


# ---- reported values ---------------------------------------------------------------------------


def test_reported_values_from_values_tables_and_printed_output(conn, runs):
    add_run(
        conn,
        values=[
            value("total", 1187),
            value("when", "2024-03-12", is_date=True),
            value("flag", True),
            value("too big", None),
            value("took", "P0DT1H30M0S"),
            value("opening", "1,200.00", kind="document", page=2),
        ],
        stdout="rows: 4,210\nmean 2.5\n",
        tables=[{"columns": ["day", "n", "label"],
                 "rows": [["2024-03-24", 7, "x"], ["2024-03-25T10:00:00", 8.5, None]]}],
    )  # fmt: skip
    add_run(conn, values=[value("seen", 31.5)], purpose="vision",
            tables=[{"columns": ["v"], "rows": [[44]]}])  # fmt: skip
    got = reported_values(runs, CHAT)
    numbers = {(r.value, r.kind, r.page) for r in got if r.value is not None}
    sources = {(r.value, r.source) for r in got if r.value is not None}
    assert (1187.0, "value") in sources
    assert (4210.0, "printed") in sources
    assert (7.0, "table") in sources
    dates = {(r.date, r.kind) for r in got if r.date is not None}
    assert (1187.0, "computed", None) in numbers
    assert (1200.0, "document", 2) in numbers
    assert (4210.0, "computed", None) in numbers
    assert (2.5, "computed", None) in numbers
    assert (7.0, "computed", None) in numbers
    assert (8.5, "computed", None) in numbers
    assert (31.5, "document", None) in numbers  # a vision run's value is read, not computed
    assert (44.0, "document", None) in numbers
    assert (D(2024, 3, 12), "computed") in dates
    assert (D(2024, 3, 24), "computed") in dates
    assert (D(2024, 3, 25), "computed") in dates
    assert not any(r.value in (0.0, 1.0, 30.0) for r in got)  # no bool, no duration parts


def test_reported_values_newest_first_and_capped(conn, runs, monkeypatch):
    monkeypatch.setattr(numbercheck, "MAX_REPORTED", 3)
    add_run(conn, values=[value("old", 1)], created_at="2026-01-01T00:00:00")
    add_run(conn, values=[value("a", 2), value("b", 3), value("c", 4), value("d", 5)],
            created_at="2026-01-02T00:00:00")  # fmt: skip
    got = reported_values(runs, CHAT)
    assert [r.value for r in got] == [2.0, 3.0, 4.0]


def test_cap_is_fifty_thousand():
    assert numbercheck.MAX_REPORTED == 50_000
    assert numbercheck.MAX_REPLY_CHARS == 20_000


def test_extract_mentions_reads_at_most_twenty_thousand_characters():
    text = "x " * 10_000 + "77"
    assert extract_mentions(text) == []


async def test_whole_check_is_fast_for_large_inputs(conn, runs, files):
    add_file(conn, "sales.csv")
    numbers = [value(f"v{i}", i * 1.37) for i in range(49_000)]
    dates = [value(f"d{i}", (D(2000, 1, 1) + datetime.timedelta(days=i)).isoformat(),
                   is_date=True) for i in range(1000)]  # fmt: skip
    add_run(conn, values=numbers + dates, stdout="mean 4.5\n" * 20_000)
    chunk = "Totals 1,234.56 and 98.6% on 2024-03-12, 12–24 March, 3/4/2024 and 7 rows. "
    reply = (chunk * (20_000 // len(chunk) + 1))[:20_000]
    checker = NumberChecker(conn, runs, files)
    started = time.perf_counter()
    result = await checker.check(CHAT, "t1", 1, "what about 2024?", reply)
    elapsed = time.perf_counter() - started
    assert result is not None and result["marks"]
    assert elapsed < 3.0, elapsed


# ---- the checker ----------------------------------------------------------------------------


async def test_no_check_without_files(conn, runs, files):
    add_run(conn, values=[value("total", 5)])
    checker = NumberChecker(conn, runs, files)
    assert checker.marks_for(CHAT, "5 rows") is None
    assert await checker.check(CHAT, "t1", 1, "q", "5 rows") is None
    assert conn.execute("SELECT COUNT(*) FROM number_checks").fetchone()[0] == 0


async def test_check_stores_one_row_per_message(conn, runs, files):
    add_file(conn, "sales.csv")
    add_run(conn, values=[value("total", 5)])
    checker = NumberChecker(conn, runs, files)
    first = await checker.check(CHAT, "t1", 7, "how many?", "5 rows and 6 columns")
    assert first == {
        "text": "5 rows and 6 columns",
        "marks": [
            {"start": 0, "end": 1, "text": "5", "kind": "computed", "page": None, "note": None},
            {"start": 11, "end": 12, "text": "6", "kind": "unverified", "page": None,
             "note": UNVERIFIED_NOTE},
        ],
    }  # fmt: skip
    again = await checker.check(CHAT, "t1", 7, "how many?", "5 rows")
    rows = conn.execute("SELECT message_id, chat_id, turn_id, text, marks FROM number_checks")
    rows = rows.fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "5 rows"
    assert json.loads(rows[0]["marks"]) == again["marks"]


async def test_checker_errors_never_raise(conn, files, caplog):
    add_file(conn, "sales.csv")

    class BrokenRuns:
        def runs_for_chat(self, chat_id, limit=200):
            raise RuntimeError("secret detail 4242")

    checker = NumberChecker(conn, BrokenRuns(), files)
    with caplog.at_level(logging.WARNING):
        assert await checker.check(CHAT, "t1", 1, "q", "5 rows") is None
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
    assert conn.execute("SELECT COUNT(*) FROM number_checks").fetchone()[0] == 0


def test_migration_5_creates_number_checks(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(number_checks)")]
    assert columns == ["message_id", "chat_id", "turn_id", "text", "marks", "created_at"]


# ---- fix round 1 -----------------------------------------------------------------------------


def kinds(reply, reported, exempt=(), file_names=()):
    marks = check_reply(reply, reported, list(exempt), file_names=list(file_names))
    return [(m.text, m.kind) for m in marks]


def test_table_cells_and_printed_numbers_need_exact_or_precise_matches():
    assert kinds("3 items", [cell(2.6)]) == [("3", "unverified")]
    assert kinds("3 items", [num(2.6)]) == [("3", "computed")]
    assert kinds("4,210 rows", [cell(4209.6)]) == [("4,210", "computed")]
    assert kinds("3 items", [cell(3)]) == [("3", "computed")]
    assert kinds("3 items", [num(2.6, source="printed")]) == [("3", "unverified")]
    assert kinds("12.5 kg", [num(12.46, source="printed")]) == [("12.5", "computed")]


DESCRIBE = """       amount
count    8.000000
mean    42.500000
std     12.247449
min     20.000000
25%     35.000000
50%     45.000000
75%     50.000000
max     60.000000
"""


def test_describe_output_does_not_confirm_unrelated_numbers(conn, runs, files):
    add_file(conn, "orders.csv")
    add_run(conn, stdout=DESCRIBE)
    checker = NumberChecker(conn, runs, files)
    marks = checker.marks_for(CHAT, "Half of orders (50%) were cheap, and 2 had 75% off.")
    assert [(m.text, m.kind) for m in marks] == [
        ("50%", "unverified"),
        ("2", "unverified"),
        ("75%", "unverified"),
    ]


def test_excluded_spans_get_no_marks():
    reply = "Found 5 rows.\n\nI stopped (1 tool steps)."
    start = reply.index("I stopped")
    marks = check_reply(reply, [num(5)], [], excluded=[(start, len(reply))])
    assert [(m.text, m.kind) for m in marks] == [("5", "computed")]


def test_reply_spans_follow_the_join():
    parts = ["Found 5.", "  ", "Stopped (1 step)."]
    text = reply_text(parts)
    spans = reply_spans(parts, {2})
    assert [text[a:b] for a, b in spans] == ["Stopped (1 step)."]


async def test_offsets_sent_to_the_web_are_utf16(conn, runs, files):
    add_file(conn, "sales.csv")
    add_run(conn, values=[value("growth", 0.31)])
    checker = NumberChecker(conn, runs, files)
    result = await checker.check(CHAT, "t1", 1, "", "📈 Sales 31%")
    assert [(m["start"], m["end"], m["text"]) for m in result["marks"]] == [(9, 12, "31%")]
    # Internally the offsets are code points.
    assert [(m.start, m.end) for m in check_reply("📈 Sales 31%", [num(0.31)], [])] == [(8, 11)]


def test_explicit_sign_never_matches_the_size_of_a_value():
    assert kinds("+3 items", [num(-3)]) == [("+3", "unverified")]
    assert kinds("+3 items", [num(3)]) == [("+3", "computed")]
    assert kinds("−3 items", [num(3)]) == [("−3", "unverified")]
    assert kinds("+31%", [num(-0.31)]) == [("+31%", "unverified")]


async def test_non_finite_values_are_dropped(conn, runs, files):
    add_file(conn, "sales.csv")
    add_run(
        conn,
        values=[value("huge", 10**400), value("big text", "9" * 400), value("n", 5)],
        tables=[{"columns": ["a"], "rows": [[10**400]]}],
        stdout="1" + "0" * 400 + "\n",
    )
    got = reported_values(runs, CHAT)
    assert all(r.value is None or math.isfinite(r.value) for r in got)
    checker = NumberChecker(conn, runs, files)
    huge = "1" + "0" * 400
    result = await checker.check(CHAT, "t1", 1, "", f"5 rows and {huge} more")
    assert [(m["text"], m["kind"]) for m in result["marks"]] == [
        ("5", "computed"),
        (huge, "unverified"),
    ]


def test_bare_years_are_dates_only_after_a_date_word():
    reported = [day("2024-05-01")]
    assert kinds("2024 orders", reported) == [("2024", "unverified")]
    assert kinds("in 2024", reported) == [("2024", "computed")]
    assert kinds("Since 2024", reported) == [("2024", "computed")]
    assert kinds("2024 orders", [num(2024)]) == [("2024", "computed")]
    assert not extract_mentions("2024 orders")[0].year_only


def test_percent_candidates():
    assert kinds("0.31%", [num(0.31)]) == [("0.31%", "unverified")]
    assert kinds("31%", [num(31)]) == [("31%", "unverified")]
    assert kinds("31%", [num(0.31)]) == [("31%", "computed")]
    assert kinds("31%", [num(31, percent=True)]) == [("31%", "computed")]
    assert kinds("31%", [num(31, source="printed")]) == [("31%", "unverified")]


def test_slash_dates_read_day_first():
    assert kinds("on 3/12/2024", [day("2024-03-12")]) == [("3/12/2024", "unverified")]
    assert kinds("on 3/12/2024", [day("2024-12-03")]) == [("3/12/2024", "computed")]
    # Only when no month could be second is it read month first.
    assert kinds("on 3/25/2024", [day("2024-03-25")]) == [("3/25/2024", "computed")]


def test_exemption_matches_the_same_form_only():
    assert kinds("5 rows", [num(1)], ["top 5?"]) == []
    assert kinds("5% of rows", [num(1)], ["top 5?"]) == [("5%", "unverified")]
    assert kinds("the 5th row", [num(1)], ["top 5?"]) == [("5th", "unverified")]


def test_file_names_exempt_longer_numbers_or_identical_tokens():
    names = ["budget 2023 (3).csv"]
    assert kinds("in 2023", [num(1)], file_names=names) == []
    assert kinds("3 rows", [num(1)], file_names=names) == []  # the identical token
    assert kinds("3.0 rows", [num(1)], file_names=names) == [("3.0", "unverified")]
    assert kinds("2,023 rows", [num(1)], file_names=names) == []  # 4 digits: by value


def test_tolerance_is_half_open():
    assert kinds("3 items", [num(2.5)]) == [("3", "computed")]
    assert kinds("3 items", [num(3.5)]) == [("3", "unverified")]
    assert kinds("3 items", [num(3.4999)]) == [("3", "computed")]


def test_fences_close_with_a_longer_fence_and_unclosed_fences_mask_nothing():
    closed = "```\nprint(42)\n`````\nThen 8 rows."
    assert [m.text for m in extract_mentions(closed)] == ["8"]
    unclosed = "```\nprint(42)\nThen 8 rows."
    assert [m.text for m in extract_mentions(unclosed)] == ["42", "8"]


def test_urls_and_link_targets_skipped():
    text = "See [the 2 charts](https://example.org/a/2024/7) or https://x.example/p?id=55 for 9."
    assert [m.text for m in extract_mentions(text)] == ["2", "9"]


def test_times_fractions_short_dates_and_phone_numbers_skipped():
    text = "At 10:30, 1/3 of rows, on 3/12/24, call +44 20 7946 0958 or 555-123-4567; 6 left."
    assert [m.text for m in extract_mentions(text)] == ["6"]


def test_may_is_a_month_next_to_a_day_or_before_a_year():
    assert [m.text for m in extract_mentions("May 12 and May 2024")] == ["May 12", "May 2024"]
    assert [m.text for m in extract_mentions("12 May 2024")] == ["12 May 2024"]
    assert [m.text for m in extract_mentions("12 March")] == ["12 March"]
    assert [m.text for m in extract_mentions("on 12 May.")] == ["12 May"]
    assert [m.text for m in extract_mentions("May 3, 2024 people")] == ["May 3, 2024"]


def test_12_may_is_a_date():
    assert kinds("on 12 May", [day("2024-05-12")]) == [("12 May", "computed")]


def test_may_before_a_count_and_a_noun_is_not_a_date():
    mentions = extract_mentions("May 3 people join?")
    assert [m.text for m in mentions] == ["3"]
    assert all(m.date is None for m in mentions)


async def test_check_runs_in_a_worker_thread(conn, runs, files, monkeypatch):
    add_file(conn, "sales.csv")
    add_run(conn, values=[value("n", 5)])
    called = []
    real = numbercheck.asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        called.append(fn)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(numbercheck.asyncio, "to_thread", spy)
    result = await NumberChecker(conn, runs, files).check(CHAT, "t1", 1, "", "5 rows")
    assert called and result["marks"][0]["kind"] == "computed"


def test_document_mark_with_unknown_page_has_null_page():
    marks = check_reply("31.5 units", [num(31.5, kind="document")], [])
    assert [(m.kind, m.page) for m in marks] == [("document", None)]
