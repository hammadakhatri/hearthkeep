import json
from pathlib import Path

from hearthkeep.sandbox.results import (
    Chart,
    FileRef,
    ParsedResults,
    Table,
    Value,
    parse_results,
)

FIXTURES = Path(__file__).parent / "fixtures" / "charts"


def lines(*records) -> bytes:
    out = b""
    for r in records:
        out += (r if isinstance(r, bytes) else json.dumps(r, allow_nan=False).encode()) + b"\n"
    return out


def value(**kw) -> dict:
    rec = {
        "type": "value",
        "name": "Total",
        "value": 12.5,
        "unit": "GBP",
        "note": None,
        "is_date": False,
        "kind": "computed",
        "page": None,
    }
    rec.update(kw)
    return rec


def table(**kw) -> dict:
    rec = {
        "type": "table",
        "name": "T",
        "columns": ["a"],
        "rows": [[1]],
        "row_numbers": [],
        "note": None,
        "total_rows": 1,
    }
    rec.update(kw)
    return rec


def chart_spec(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_parses_each_record_type():
    spec = chart_spec("bar")
    raw = lines(
        value(),
        value(name="Closing", kind="document", page=3, value="1,000"),
        {"type": "chart", "title": "Spend", "spec": spec},
        table(
            name="Rows",
            columns=["a", "b"],
            rows=[[1, "x"], [2.5, None]],
            row_numbers=[2, 3],
            note="first rows",
            total_rows=40,
        ),
        {"type": "file", "name": "out.csv", "member": "files/out.csv"},
        {"type": "cleaning", "note": "dropped 2 blank rows"},
        {"type": "section", "name": "Overview"},
    )
    res = parse_results(raw, {"files/out.csv", "results.jsonl"})
    assert isinstance(res, ParsedResults)
    assert res.problems == []
    assert res.values == [
        Value("Total", 12.5, "GBP", None, False, "computed", None),
        Value("Closing", "1,000", "GBP", None, False, "document", 3),
    ]
    assert res.charts == [Chart("Spend", spec)]
    assert res.tables == [
        Table("Rows", ["a", "b"], [[1, "x"], [2.5, None]], [2, 3], "first rows", 40)
    ]
    assert res.files == [FileRef("out.csv", "files/out.csv")]
    assert res.cleaning == ["dropped 2 blank rows"]
    assert res.sections == {"Overview"}


def test_table_accepts_empty_row_numbers():
    res = parse_results(lines(table(rows=[["a"], ["b"]], total_rows=2)), set())
    assert res.problems == []
    assert res.tables[0].row_numbers == []


def test_drops_invalid_lines_with_problem_note():
    raw = lines(
        b"not json",
        b"[1, 2]",
        {"type": "mystery"},
        {"no": "type"},
        value(name=""),
        value(name="n" * 101),
        value(value="s" * 201),
        value(note="n" * 501),
        value(kind="other"),
        value(value=[1]),
        {"type": "cleaning", "note": "ok"},
    )
    res = parse_results(raw, set())
    assert [p.split(":")[0] for p in res.problems] == [f"line {n}" for n in range(1, 11)]
    assert all(": " in p for p in res.problems)
    assert res.values == []
    assert res.cleaning == ["ok"]


def test_blank_lines_are_skipped_but_counted():
    res = parse_results(b"\n" + lines(b"oops"), set())
    assert res.problems[0].startswith("line 2: ")


def test_invalid_utf8_does_not_raise():
    raw = b"\xff\xfe\x00bad\n" + lines({"type": "section", "name": "ok"})
    res = parse_results(raw, set())
    assert len(res.problems) == 1
    assert res.sections == {"ok"}


def test_table_limits():
    bad = [
        table(rows=[[i] for i in range(501)], total_rows=501),
        table(columns=[f"c{i}" for i in range(51)], rows=[[1] * 51]),
        table(rows=[["x" * 201]]),
        table(rows=[[{"a": 1}]]),
        table(rows=[[1, 2]]),
        table(row_numbers=[1, 2]),
        table(row_numbers=["a"]),
        table(columns=["a" * 101]),
        table(columns=[1]),
        table(total_rows="x"),
        table(name=""),
    ]
    res = parse_results(lines(*bad), set())
    assert res.tables == []
    assert len(res.problems) == len(bad)

    ok = [
        table(rows=[[1]] * 500, row_numbers=list(range(500)), total_rows=900),
        table(columns=[f"c{i}" for i in range(50)], rows=[[None] * 50]),
        table(rows=[["x" * 200]]),
        table(rows=[[True]]),
    ]
    res = parse_results(lines(*ok), set())
    assert res.problems == []
    assert len(res.tables) == 4
    assert res.tables[0].total_rows == 900
    assert res.tables[3].rows == [["true"]]


def test_caps_lines_and_bytes():
    many = lines(*[{"type": "section", "name": f"s{i}"} for i in range(2001)])
    res = parse_results(many, set())
    assert len(res.sections) == 2000
    assert res.problems == ["line 2001: too many lines, the rest was ignored"]

    big = b"x" * (5 * 1024 * 1024 + 1)
    res = parse_results(lines({"type": "section", "name": "first"}) + big, set())
    assert res.sections == {"first"}
    assert len(res.problems) == 1
    assert res.problems[0].startswith("line 2: ")
    assert "5 MB" in res.problems[0]

    # exactly 5 MB of one long bad line is read (and rejected as one bad line)
    res = parse_results(b"x" * (5 * 1024 * 1024), set())
    assert len(res.problems) == 1


def test_file_ref_must_exist_in_members():
    raw = lines(
        {"type": "file", "name": "a.csv", "member": "files/a.csv"},
        {"type": "file", "name": "b.csv", "member": "files/b.csv"},
        {"type": "file", "name": "c.csv", "member": "results.jsonl"},
        {"type": "file", "name": "d.csv", "member": "files/../d.csv"},
        {"type": "file", "name": "", "member": "files/"},
    )
    res = parse_results(raw, {"files/a.csv", "results.jsonl", "files/../d.csv", "files/"})
    assert res.files == [FileRef("a.csv", "files/a.csv")]
    assert len(res.problems) == 4


def test_nan_and_inf_become_null():
    def rec(name, v, note="null") -> bytes:
        return (
            f'{{"type":"value","name":"{name}","value":{v},"unit":null,"note":{note},'
            '"is_date":false,"kind":"computed","page":null}'
        ).encode()

    raw = lines(
        rec("Ratio", "NaN"),
        rec("Big", "Infinity", '"capped"'),
        rec("Neg", "-Infinity"),
        b'{"type":"table","name":"T","columns":["a","b"],"rows":[[NaN,1],[Infinity,2]],'
        b'"row_numbers":[],"note":null,"total_rows":2}',
    )
    res = parse_results(raw, set())
    assert res.problems == []
    assert [v.value for v in res.values] == [None, None, None]
    assert res.values[0].note == "not a number"
    assert res.values[1].note == "capped (not a number)"
    assert res.tables[0].rows == [[None, 1], [None, 2]]


def test_null_value_from_hk_keeps_its_note():
    res = parse_results(lines(value(value=None, note="not a number")), set())
    assert res.values[0].value is None
    assert res.values[0].note == "not a number"


def test_chart_failing_validation_is_dropped_with_problem():
    bad = {"$schema": "https://vega.github.io/schema/vega-lite/v6.json", "data": {"url": "x"}}
    good = chart_spec("scatter")
    raw = lines(
        {"type": "chart", "title": "Bad", "spec": bad},
        {"type": "chart", "title": "Good", "spec": good},
        {"type": "chart", "title": "Not a dict", "spec": "x"},
        {"type": "chart", "spec": good},
        {"type": "chart", "title": "T" * 101, "spec": good},
    )
    res = parse_results(raw, set())
    assert [c.title for c in res.charts] == ["Good"]
    assert len(res.problems) == 4
    assert res.problems[0].startswith("line 1: ")


def test_document_values_need_page():
    raw = lines(
        value(kind="document", page=None),
        value(kind="document", page=0),
        value(kind="document", page=True),
        value(kind="document", page="2"),
        value(kind="document", page=2, name="ok"),
        value(kind="computed", page=5, name="Computed with page"),
    )
    res = parse_results(raw, set())
    assert len(res.problems) == 4
    assert [v.name for v in res.values] == ["ok", "Computed with page"]
    assert res.values[0].page == 2
    assert res.values[1].page is None


def test_value_types_and_dates():
    raw = lines(
        value(name="n", value=3),
        value(name="d", value="2025-03-01", is_date=True),
        value(name="t", value="PT1H"),
        value(name="b", value=True),
        value(name="u", unit="u" * 101),
    )
    res = parse_results(raw, set())
    assert [v.value for v in res.values] == [3, "2025-03-01", "PT1H", "true"]
    assert res.values[1].is_date is True
    assert len(res.problems) == 1
