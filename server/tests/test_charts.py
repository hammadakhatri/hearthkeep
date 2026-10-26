import copy
import json
from pathlib import Path

import pytest

from hearthkeep.sandbox.charts import ALLOWED_SCHEMAS, ChartError, validate_chart_spec

FIXTURES = Path(__file__).parent / "fixtures" / "charts"
V5 = "https://vega.github.io/schema/vega-lite/v5.json"
V6 = "https://vega.github.io/schema/vega-lite/v6.json"


def base(**extra) -> dict:
    spec = {
        "$schema": V6,
        "data": {"values": [{"a": 1, "b": 2}]},
        "mark": "bar",
        "encoding": {"x": {"field": "a", "type": "quantitative"}},
    }
    spec.update(extra)
    return spec


def rejected(spec) -> None:
    with pytest.raises(ChartError):
        validate_chart_spec(spec)


def compact_size(spec: dict) -> int:
    return len(json.dumps(spec, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize("name", ["bar", "line_highlight", "scatter"])
def test_accepts_hk_bar_line_scatter_specs(name):
    spec = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert validate_chart_spec(spec) == spec


def test_allowed_schemas():
    assert ALLOWED_SCHEMAS == {V5, V6}
    validate_chart_spec(base(**{"$schema": V5}))


def test_rejects_data_url():
    rejected(base(data={"url": "http://x/y.csv"}))
    rejected(base(data={"values": [{"a": 1}], "url": "x.csv"}))
    rejected(base(data={"name": "remote"}))
    rejected(base(data={"values": [{"a": 1}], "format": {"type": "csv"}}))
    rejected(base(data={"values": "a,b"}))
    rejected(base(data={"values": [1, 2]}))
    rejected(base(layer=[{"data": {"url": "x.json"}, "mark": "point"}]))
    validate_chart_spec(base(data={"name": "main", "values": [{"a": 1}]}))


@pytest.mark.parametrize(
    "text",
    [
        "see https://x",
        "http://x",
        "HTTPS://X",
        "//cdn.example/x",
        "  //cdn.example/x",
        "data:text/csv,a",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://x",
        "java\tscript:alert(1)",
    ],
)
def test_rejects_url_strings_anywhere(text):
    rejected(base(title=text))
    rejected(base(encoding={"x": {"field": "a", "title": text}}))
    rejected(base(data={"values": [{"a": text}]}))
    rejected(base(data={"values": [{text: 1}]}))
    rejected(base(layer=[{"mark": "text", "encoding": {"text": {"value": [text]}}}]))


def test_plain_text_with_colons_is_fine():
    validate_chart_spec(base(title="Profile: Savings, Metadata: ok, a / b, 10:30"))


def test_rejects_image_mark_and_href_channel():
    rejected(base(mark="image"))
    rejected(base(mark={"type": "image", "url": "x"}))
    rejected(base(mark={"type": "IMAGE"}))
    rejected(base(layer=[{"mark": "image"}]))
    rejected(base(encoding={"href": {"field": "a"}}))
    rejected(base(encoding={"url": {"field": "a"}}))
    rejected(base(mark={"type": "text", "href": "x"}))


def test_rejects_lookup_with_url_data():
    rejected(
        base(
            transform=[
                {
                    "lookup": "a",
                    "from": {"data": {"url": "http://x"}, "key": "a", "fields": ["b"]},
                }
            ]
        )
    )
    # even an inline lookup is not an allowed transform
    rejected(
        base(
            transform=[
                {
                    "lookup": "a",
                    "from": {"data": {"values": [{"a": 1}]}, "key": "a", "fields": ["b"]},
                }
            ]
        )
    )


def test_rejects_datasets_usermeta_loader_ref():
    rejected(base(datasets={"d": [{"a": 1}]}))
    rejected(base(usermeta={"x": 1}))
    rejected(base(config={"loader": {"baseURL": "x"}}))
    rejected(base(layer=[{"$ref": "#/definitions/x"}]))
    rejected(base(config={"view": {"usermeta": 1}}))


@pytest.mark.parametrize("key", ["expr", "signal", "calculate", "labelExpr", "titleExpr"])
def test_rejects_expression_keys(key):
    rejected(base(encoding={"x": {"field": "a", key: "datum.a"}}))
    rejected(base(params=[{"name": "p", "value": {key: "1"}}]))


def test_transform_allowlist():
    ok = [
        {"filter": {"field": "a", "gt": 0}},
        {"filter": {"and": [{"field": "a", "gt": 0}, {"not": {"field": "b", "equal": 1}}]}},
        {"aggregate": [{"op": "sum", "field": "a", "as": "s"}], "groupby": ["b"]},
        {"bin": True, "field": "a", "as": "bin_a"},
        {"timeUnit": "month", "field": "a", "as": "m"},
        {"fold": ["a", "b"], "as": ["k", "v"]},
        {"window": [{"op": "rank", "as": "r"}], "sort": [{"field": "a"}]},
        {"joinaggregate": [{"op": "mean", "field": "a", "as": "m"}], "groupby": ["b"]},
    ]
    validate_chart_spec(base(transform=ok))
    for bad in (
        {"filter": "datum.a > 0"},
        {"filter": {"expr": "datum.a > 0"}},
        {"filter": {"param": "p"}},
        {"calculate": "datum.a", "as": "c"},
        {"lookup": "a", "from": {"data": {"values": []}, "key": "a"}},
        {"flatten": ["a"]},
        {"sample": 10},
        {"stack": "a", "groupby": []},
        {"filter": {"field": "a", "gt": 0}, "extra": 1},
        {"filter": {"field": "a", "gt": 0}, "aggregate": []},
        {},
        "filter",
    ):
        rejected(base(transform=[bad]))
    rejected(base(transform={"filter": {"field": "a", "gt": 0}}))


def test_params_with_scale_and_legend_binding_allowed():
    spec = base(
        params=[
            {"name": "zoom", "select": "interval", "bind": "scales"},
            {"name": "legend", "select": {"type": "point", "fields": ["k"]}, "bind": "legend"},
            {"name": "pick", "select": {"type": "interval", "encodings": ["x"]}},
        ],
        encoding={
            "x": {"field": "a", "type": "quantitative"},
            "opacity": {"condition": {"param": "legend", "value": 1}, "value": 0.2},
        },
    )
    validate_chart_spec(spec)


def test_params_other_bindings_rejected():
    rejected(base(params=[{"name": "p", "value": 1, "bind": {"input": "range"}}]))
    rejected(base(params=[{"name": "p", "select": "interval", "bind": "input"}]))
    rejected(base(params=[{"name": "p", "select": {"type": "point", "toggle": "event.shiftKey"}}]))
    rejected(base(params=[{"name": "p", "select": {"type": "point", "on": 5}}]))
    rejected(base(params=[{"name": "p", "select": "lasso"}]))
    rejected(base(params=[{"name": "p", "select": "interval", "expr": "1"}]))


def test_test_only_with_param_inside_condition():
    def color(condition) -> dict:
        return base(encoding={"color": {"condition": condition, "value": "grey"}})

    validate_chart_spec(color({"test": {"param": "p"}, "value": "red"}))
    validate_chart_spec(color([{"test": {"param": "p"}, "value": "red"}]))
    rejected(color({"test": "datum.a > 1", "value": "red"}))
    rejected(color({"test": {"expr": "1"}, "value": "red"}))
    rejected(color({"test": {"param": 5}, "value": "red"}))
    rejected(base(encoding={"color": {"test": {"param": "p"}, "value": "red"}}))
    rejected(base(test={"param": "p"}))


def test_caps_rows_bytes_and_depth():
    rows = [{"a": i} for i in range(5001)]
    rejected(base(data={"values": rows}))
    validate_chart_spec(base(data={"values": rows[:5000]}))

    # each dataset is under 5,000 rows, but together they pass 20,000
    def layer(n: int) -> dict:
        return {"data": {"values": rows[:n]}, "mark": "point"}

    validate_chart_spec({"layer": [layer(5000)] * 4})
    rejected({"layer": [layer(5000)] * 4 + [layer(1)]})

    one_mb = 1024 * 1024
    spec = base(description="")
    spec["description"] = "x" * (one_mb - compact_size(spec))
    assert compact_size(spec) == one_mb
    validate_chart_spec(spec)
    spec["description"] += "x"
    rejected(spec)

    def nested(depth: int) -> dict:
        """A dict whose deepest value sits at exactly `depth` levels."""
        node: dict = {}
        top = {"config": node}  # depth 2 for `node`
        for _ in range(depth - 2):
            node["k"] = {}
            node = node["k"]
        return top

    validate_chart_spec(nested(20))
    rejected(nested(21))
    rejected(nested(200))


def test_unknown_schema_rejected():
    rejected(base(**{"$schema": "https://vega.github.io/schema/vega-lite/v4.json"}))
    rejected(base(**{"$schema": "https://evil.example/v6.json"}))
    rejected(base(**{"$schema": 6}))


def test_not_a_dict_or_not_json():
    for bad in (None, [], "x", 3):
        rejected(bad)
    rejected(base(width=float("nan")))
    rejected(base(width=float("inf")))
    rejected(base(width=object()))
    rejected(base(width=(1, 2)))
    rejected({1: "x"})


def test_returns_copy():
    spec = json.loads((FIXTURES / "line_highlight.json").read_text(encoding="utf-8"))
    original = copy.deepcopy(spec)
    out = validate_chart_spec(spec)
    assert out == spec and out is not spec
    out["data"]["values"].append({"x": 1})
    out["layer"][0]["params"].clear()
    assert spec == original
