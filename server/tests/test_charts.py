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


@pytest.mark.parametrize("name", ["bar", "line_highlight", "scatter", "line_paren_column"])
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
    rejected(base(layer=[{"mark": "point", "usermeta": 1}]))


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
        top = {"mark": "point", "description_k": node}  # depth 2 for `node`
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


# ---- fix round 1 ----


def test_url_looking_data_values_are_allowed_but_not_keys():
    rows = [{"a": "https://shop.example/x", "b": "javascript:1", "c": "url(x)", "d": "//h"}]
    validate_chart_spec(base(data={"values": rows}))
    rejected(base(data={"values": [{"https://x": 1}]}))
    rejected(base(data={"values": [{"a": {"https://x": 1}}]}))


def test_scheme_check_needs_slashes_for_http_ftp_file():
    for title in ("Data: monthly", "File: statement", "Http: notes", "ftp: x", "Source data: bank"):
        validate_chart_spec(base(title=title))
    for title in (
        "a javascript:alert(1)",
        "x data:text/csv,a",
        "JavaScript:1",
        "\t java\tscript:1",
    ):
        rejected(base(title=title))
    rejected(base(title="see file:///etc/passwd"))


def test_rejects_config_entirely():
    rejected(base(config={}))
    rejected(base(config={"view": {"stroke": None}}))
    rejected(base(config={"selection": {"interval": {"toggle": "datum.a"}}}))
    rejected(base(config={"customFormatTypes": True}))
    rejected(base(config={"numberFormatType": "evil"}))
    rejected(base(layer=[{"mark": "point", "config": {}}]))


@pytest.mark.parametrize("key", ["on", "clear", "toggle", "translate", "zoom"])
def test_selection_event_strings_rejected(key):
    for bad in ("click[datum.a > 0]", "mousedown", "event.shiftKey", "true"):
        rejected(base(params=[{"name": "p", "select": {"type": "point", key: bad}}]))
        rejected(base(params=[{"name": "p", "select": {"type": "interval", key: bad}}]))
    validate_chart_spec(base(params=[{"name": "p", "select": {"type": "interval", key: False}}]))
    if key in ("on", "clear"):
        rejected(base(params=[{"name": "p", "select": {"type": "interval", key: True}}]))
    else:
        validate_chart_spec(base(params=[{"name": "p", "select": {"type": "interval", key: True}}]))


def test_format_and_format_type_restricted():
    def axis(**kw) -> dict:
        return base(encoding={"x": {"field": "a", "type": "quantitative", "axis": kw}})

    for ok in ("number", "time", "utc"):
        validate_chart_spec(axis(formatType=ok))
    for bad in ("evil", "NUMBER", 5, "", "number ", {"x": 1}):
        rejected(axis(formatType=bad))
    validate_chart_spec(axis(format=",.2f"))
    validate_chart_spec(axis(format="%Y-%m-%d"))
    validate_chart_spec(axis(format="x" * 40))
    for bad in ("x" * 41, "f(x)", "a[0]", "{a}", 5, {"a": 1}, ["a"]):
        rejected(axis(format=bad))
    rejected(base(encoding={"text": {"field": "a", "format": "d3(1)"}}))
    rejected(base(encoding={"text": {"field": "a", "labelFormatType": "evil"}}))


def test_tooltip_image_routes_rejected():
    rejected(base(data={"values": [{"image": "x"}]}))
    rejected(base(data={"values": [{"a": 1, "Image": "x"}]}))
    rejected(base(data={"values": [{"a": {"IMAGE": "x"}}]}))
    rejected(base(encoding={"tooltip": {"field": "a", "title": "image"}}))
    rejected(base(encoding={"tooltip": [{"field": "a", "title": "IMAGE"}]}))
    validate_chart_spec(base(data={"values": [{"images": "x", "my image": 1}]}))
    validate_chart_spec(base(encoding={"tooltip": {"field": "a", "title": "Image count"}}))


@pytest.mark.parametrize("text", ["url(x)", "URL (x)", "background: Url\t(x)", "a url( b"])
def test_css_urls_rejected_outside_data_values(text):
    rejected(base(title=text))
    rejected(base(encoding={"x": {"field": "a", "title": text}}))
    rejected(base(data={"values": [{text: 1}]}))
    validate_chart_spec(base(data={"values": [{"a": text}]}))


def test_lone_surrogates_rejected():
    rejected(base(title="bad \ud800 text"))
    rejected(base(data={"values": [{"a": "\udc00"}]}))
    rejected(base(data={"values": [{"\ud800": 1}]}))


def test_error_text_quotes_at_most_40_chars():
    long = "x" * 3000
    for spec in (
        base(**{long + "expr": 1}),
        base(encoding={long + "expr": 1}),
        base(title="https://" + long),
        base(**{"https://" + long: 1}),
        base(data={"values": [{"https://" + long: 1}]}),
        base(encoding={"x": {"field": long, "formatType": long}}),
        base(encoding={"x": {"field": "a", "format": long}}),
        base(encoding={"x": {"field": "a", "title": "url(" + long}}),
        base(**{long: {"url": 1}}),
    ):
        with pytest.raises(ChartError) as info:
            validate_chart_spec(spec)
        assert len(str(info.value)) < 200
        assert long[:41] not in str(info.value)


def test_data_dict_counts_in_depth():
    def chain(layers: int, row_value: object) -> dict:
        node: dict = {"data": {"values": [{"a": row_value}]}, "mark": "point"}
        for _ in range(layers):
            node = {"layer": [node]}
        return node

    def depth_of(n: object, d: int = 0) -> int:
        if isinstance(n, dict):
            return max([d + 1] + [depth_of(v, d + 1) for v in n.values()])
        if isinstance(n, list):
            return max([d + 1] + [depth_of(v, d + 1) for v in n])
        return d

    seen = set()
    for layers in range(0, 12):
        for leaf in (1, [1], {"b": 1}):
            spec = chain(layers, leaf)
            depth = depth_of(spec)
            seen.add(depth)
            if depth <= 20:
                validate_chart_spec(spec)
            else:
                rejected(spec)
    assert {19, 20, 21, 22} <= seen


def test_transform_companions_are_per_op():
    for bad in (
        {"filter": {"field": "a", "gt": 0}, "groupby": ["a"]},
        {"aggregate": [{"op": "sum", "field": "a", "as": "s"}], "field": "a"},
        {"bin": True, "field": "a", "as": "b", "groupby": ["a"]},
        {"fold": ["a"], "as": ["k", "v"], "field": "a"},
        {"timeUnit": "month", "field": "a", "as": "m", "frame": [None, 0]},
        {"joinaggregate": [{"op": "sum", "field": "a", "as": "s"}], "sort": []},
        {"window": [{"op": "rank", "as": "r"}], "as": "x"},
    ):
        rejected(base(transform=[bad]))


# ---- fix round 2 ----


def test_derived_image_fields_rejected():
    rejected(base(transform=[{"fold": ["a", "b"], "as": ["k", "image"]}]))
    rejected(base(transform=[{"window": [{"op": "rank", "as": "image"}]}]))
    rejected(base(transform=[{"joinaggregate": [{"op": "sum", "field": "a", "as": "image"}]}]))
    rejected(base(transform=[{"aggregate": [{"op": "sum", "field": "a", "as": " Image "}]}]))
    rejected(base(transform=[{"bin": True, "field": "a", "as": "IMAGE"}]))
    rejected(base(transform=[{"timeUnit": "month", "field": "a", "as": "image"}]))
    rejected(base(transform=[{"fold": ["image"], "as": ["k", "v"]}]))
    rejected(base(transform=[{"aggregate": [], "groupby": ["image"]}]))
    rejected(base(encoding={"tooltip": [{"field": "image"}]}))
    rejected(base(encoding={"tooltip": {"field": "a", "title": " image\n"}}))
    rejected(base(mark={"type": "text", "text": "IMAGE"}))
    validate_chart_spec(base(encoding={"tooltip": [{"field": "images"}, {"field": "my image"}]}))
    validate_chart_spec(base(data={"values": [{"a": "image"}]}))  # a row value is only data


@pytest.mark.parametrize("key", ["selection", "Selection"])
def test_legacy_selection_rejected(key):
    rejected(base(**{key: {"brush": {"type": "interval", "on": "[mousedown[datum.a>1], x]"}}}))
    rejected(base(layer=[{"mark": "point", key: {"b": {"type": "interval"}}}]))


@pytest.mark.parametrize(
    "text",
    [
        "u\\72l(x)",
        "\\75\\72\\6c(x)",
        "ur\\6c (x)",
        "url\\28 x)",
        "\\75rl\\28x\\29",
        "a\\b(c)",
    ],
)
def test_css_escapes_rejected_outside_rows(text):
    rejected(base(title=text))
    rejected(base(encoding={"x": {"field": "a", "title": text}}))
    validate_chart_spec(base(data={"values": [{"a": text}]}))
    validate_chart_spec(base(title="Cost\\.total", encoding={"x": {"field": "a\\[1\\]"}}))


@pytest.mark.parametrize("channel", ["color", "fill", "stroke", "shape", "opacity", "size"])
def test_null_scale_on_paint_channels_rejected(channel):
    rejected(base(encoding={channel: {"field": "a", "scale": None}}))
    rejected(base(encoding={channel: {"field": "a", "scale": {"type": "identity"}}}))
    validate_chart_spec(base(encoding={channel: {"field": "a", "scale": {"scheme": "blues"}}}))
    validate_chart_spec(base(encoding={"x": {"field": "a", "scale": None}}))


@pytest.mark.parametrize(
    "text",
    [
        "https:evil",
        "HTTP:\\\\evil",
        "  https:/x",
        "\t\nhttps:x",
        "ftp:x",
        "FILE:x",
        "file:\\\\x",
        "\\\\server\\share",
        "data:text/csv;base64,AAA",
        "data: text/html,x",
        "data:,x",
        "data:  ,x",
        "x data:image/png;base64,AA",
    ],
)
def test_schemes_outside_rows_rejected(text):
    rejected(base(title=text))
    validate_chart_spec(base(data={"values": [{"a": text}]}))


def test_plain_titles_with_scheme_words_pass():
    for title in ("Data: monthly", "File: statement", "Data:  weekly totals", "Http: notes"):
        validate_chart_spec(base(title=title))


def test_selection_triggers_only_false():
    for key in ("on", "clear"):
        validate_chart_spec(base(params=[{"name": "p", "select": {"type": "point", key: False}}]))
        rejected(base(params=[{"name": "p", "select": {"type": "point", key: True}}]))


def test_format_rejects_long_digit_runs():
    def axis(f: str) -> dict:
        return base(encoding={"x": {"field": "a", "axis": {"format": f}}})

    rejected(axis("0>99999,"))
    rejected(axis("1234"))
    validate_chart_spec(axis("0>999,.2f"))
    validate_chart_spec(axis("%Y-%m-%d"))


def test_field_names_may_carry_hk_escapes_with_parentheses():
    esc = "cost (usd)" + chr(92) + ".total"
    validate_chart_spec(base(encoding={"y": {"field": esc, "type": "quantitative"}}))
    validate_chart_spec(
        base(
            params=[
                {"name": "legend", "select": {"type": "point", "fields": [esc]}, "bind": "legend"}
            ]
        )
    )
    # still checked by every other rule
    for bad in (
        "url" + chr(92) + "28 x" + chr(92) + "29",
        "u" + chr(92) + "72l(x)",
        "image",
        "https://x",
        "x" * 3 + chr(0xD800),
    ):
        rejected(base(encoding={"y": {"field": bad}}))
        rejected(
            base(
                params=[
                    {"name": "p", "select": {"type": "point", "fields": [bad]}, "bind": "legend"}
                ]
            )
        )
    # other strings keep the backslash plus parenthesis rule
    rejected(base(title=esc))
    rejected(base(encoding={"y": {"field": "a", "title": esc}}))


# ---- fix round 3 ----

PAINT = ["color", "fill", "stroke", "fillOpacity", "strokeOpacity", "strokeDash", "strokeWidth"]
PAINT += ["opacity", "size", "shape"]


def test_tooltip_value_objects_rejected():
    rejected(base(encoding={"tooltip": {"value": {"image": "x"}}}))
    rejected(base(encoding={"tooltip": [{"field": "a"}, {"value": {"a": 1}}]}))
    rejected(base(encoding={"tooltip": {"value": ["a"]}}))
    rejected(base(encoding={"tooltip": True}))
    rejected(base(encoding={"tooltip": "a"}))
    rejected(base(encoding={"tooltip": [1]}))
    validate_chart_spec(base(encoding={"tooltip": None}))
    validate_chart_spec(base(encoding={"tooltip": False}))
    validate_chart_spec(base(encoding={"tooltip": {"value": "text"}}))
    validate_chart_spec(base(encoding={"tooltip": [{"field": "a"}, {"value": 3}]}))


def test_mark_tooltip_shapes():
    for ok in (True, False, None, {"content": "data"}, {"content": "encoding"}):
        validate_chart_spec(base(mark={"type": "bar", "tooltip": ok}))
    for bad in ("x", {"content": "other"}, {"content": "data", "x": 1}, {"image": "x"}, [1]):
        rejected(base(mark={"type": "bar", "tooltip": bad}))


def test_image_key_rejected_at_any_depth_outside_rows():
    rejected(base(encoding={"tooltip": {"value": {" Image ": "x"}}}))
    rejected(base(layer=[{"mark": "point", "image": 1}]))
    rejected(base(params=[{"name": "p", "value": {"IMAGE": 1}}]))
    validate_chart_spec(base(data={"values": [{"images": 1}]}))


def test_scale_checked_inside_conditions():
    cond = {"param": "p", "field": "a"}
    for channel in ("color", "fill", "opacity"):
        rejected(base(encoding={channel: {"condition": {**cond, "scale": None}, "value": "red"}}))
        rejected(base(encoding={channel: {"condition": [{**cond, "scale": None}], "value": "red"}}))
        rejected(
            base(
                encoding={
                    channel: {
                        "condition": {"param": "p", "condition": {**cond, "scale": None}},
                        "value": "red",
                    }
                }
            )
        )
        rejected(
            base(
                encoding={
                    channel: {
                        "condition": [{"param": "q", "scale": {"type": "Identity"}}],
                        "value": "red",
                    }
                }
            )
        )
    validate_chart_spec(
        base(
            encoding={
                "color": {"condition": {**cond, "scale": {"scheme": "blues"}}, "value": "red"}
            }
        )
    )


def test_scale_range_from_field_rejected():
    rejected(base(encoding={"color": {"field": "a", "scale": {"range": {"field": "a"}}}}))
    rejected(base(encoding={"size": {"field": "a", "scale": {"type": "IDENTITY"}}}))
    validate_chart_spec(
        base(encoding={"color": {"field": "a", "scale": {"range": ["red", "blue"]}}})
    )


@pytest.mark.parametrize("channel", PAINT)
def test_paint_strings_are_allow_listed(channel):
    bad_values = [
        "url(x)",
        "red;background:url(x)",
        "var(--x)",
        "#12",
        "#12345678a",
        "rgb(1,2,3);x",
        "rgb(a)",
        "calc(1px)",
        "a b",
        "toolongcolourkeywordname",
        "",
        "M0,0L1,1",
        {"x": 1},
        ["red"],
        [1, "x"],
    ]
    for bad in bad_values:
        rejected(base(encoding={channel: {"field": "a", "value": bad}}))
        rejected(base(encoding={channel: {"datum": bad}}))
        rejected(base(encoding={channel: {"condition": {"param": "p", "value": bad}}}))
        rejected(base(mark={"type": "point", channel: bad}))
    rejected(base(mark={"type": "point", channel: {"gradient": "linear"}}))


def test_paint_good_values():
    for good in ("red", "Steelblue", "#abc", "#aabbccdd", "rgb(1, 2, 3)", "RGBA(1,2,3,0.5)"):
        validate_chart_spec(base(encoding={"color": {"value": good}}))
        validate_chart_spec(base(mark={"type": "point", "fill": good, "stroke": good}))
    validate_chart_spec(base(encoding={"fill": {"datum": "hsl(120, 50%, 50%)"}}))
    validate_chart_spec(base(encoding={"opacity": {"value": 0.5}, "size": {"datum": 3}}))
    validate_chart_spec(base(mark={"type": "rule", "strokeDash": [4, 4], "opacity": 0.2}))
    validate_chart_spec(base(encoding={"strokeDash": {"value": [2, 3]}}))
    validate_chart_spec(
        base(encoding={"color": {"condition": {"param": "p", "value": "#fff"}, "value": "grey"}})
    )


def test_shape_values_are_a_fixed_list():
    for ok in ("circle", "square", "cross", "diamond", "triangle-up", "triangle-down"):
        validate_chart_spec(base(encoding={"shape": {"value": ok}}))
    for ok in ("triangle-right", "triangle-left", "stroke", "arrow", "wedge", "triangle"):
        validate_chart_spec(base(mark={"type": "point", "shape": ok}))
    for bad in ("red", "M0,0L1,1", "hexagon"):
        rejected(base(encoding={"shape": {"value": bad}}))
        rejected(base(mark={"type": "point", "shape": bad}))


@pytest.mark.parametrize(
    "text", ["/\\x", "\\/x", "//x", "\\\\x", "  /\\x", "\t\\/x", "data:;base64,AA"]
)
def test_protocol_relative_and_data_semicolon_rejected(text):
    rejected(base(title=text))
    validate_chart_spec(base(data={"values": [{"a": text}]}))


def test_single_slash_titles_pass():
    for title in ("a/b", "/ path", "Q1/Q2", "\\ one"):
        validate_chart_spec(base(title=title))


def test_backslash_paren_exemption_is_narrow():
    esc = "cost (usd)" + chr(92) + ".total"
    validate_chart_spec(base(encoding={"y": {"field": esc}}))
    validate_chart_spec(base(encoding={"tooltip": [{"field": esc}]}))
    validate_chart_spec(
        base(params=[{"name": "p", "select": {"type": "point", "fields": [esc]}, "bind": "legend"}])
    )
    validate_chart_spec(
        base(encoding={"color": {"condition": {"param": "p", "field": esc}, "value": "red"}})
    )
    # a key called field/fields elsewhere is not a column reference
    rejected(base(transform=[{"fold": [esc], "as": ["k", "v"]}]))
    rejected(base(transform=[{"bin": True, "field": esc, "as": "b"}]))
    rejected(base(transform=[{"filter": {"field": esc, "gt": 1}}]))
    rejected(base(layer=[{"mark": "point", "field": esc}]))
    rejected(base(params=[{"name": "p", "select": {"type": "point", "encodings": [esc]}}]))
    rejected(base(facet={"field": esc}, spec={"mark": "point"}))


CURSORS = "auto default none context-menu help pointer progress wait cell crosshair text"
CURSORS += " vertical-text alias copy move no-drop not-allowed grab grabbing all-scroll col-resize"
CURSORS += " row-resize n-resize e-resize s-resize w-resize ne-resize nw-resize se-resize"
CURSORS += " sw-resize ew-resize ns-resize nesw-resize nwse-resize zoom-in zoom-out"


@pytest.mark.parametrize(
    "bad",
    [
        'image-set("/api/x" 1x), auto',
        '-webkit-image-set("/api/x" 1x), auto',
        "//evil.example/a.png",
        "/api/x.png, auto",
        "pointer, auto",
        "hand",
        "",
        {"value": "pointer"},
        ["pointer"],
        1,
    ],
)
def test_cursor_values_are_keywords_only(bad):
    rejected(base(mark={"type": "point", "cursor": bad}))
    rejected(base(params=[{"name": "b", "select": {"type": "interval", "mark": {"cursor": bad}}}]))
    rejected(base(layer=[{"mark": {"type": "rule", "Cursor": bad}}]))
    rejected(base(legend={"cursor": bad}))


def test_cursor_keywords_pass():
    for keyword in CURSORS.split():
        validate_chart_spec(base(mark={"type": "point", "cursor": keyword}))
    validate_chart_spec(base(mark={"type": "point", "cursor": "pointer"}))
    # a data column called cursor is data
    validate_chart_spec(base(data={"values": [{"cursor": "image-set(x)"}]}))


COLOUR_KEYS = [
    ("axis", "labelColor"),
    ("axis", "gridColor"),
    ("axis", "domainColor"),
    ("axis", "tickColor"),
    ("axis", "titleColor"),
    ("legend", "fillColor"),
    ("legend", "strokeColor"),
    ("legend", "labelColor"),
    ("header", "labelColor"),
    ("header", "titleColor"),
    ("title", "subtitleColor"),
]


@pytest.mark.parametrize("bad", ["var(--x)", 'image-set("/api/x" 1x)', "url(x)", "a b", ["x y"]])
def test_colour_like_keys_are_allow_listed(bad):
    for parent, key in COLOUR_KEYS:
        enc = {"x": {"field": "a", parent: {key: bad}}}
        if parent == "title":
            rejected(base(title={"text": "t", key: bad}))
        else:
            rejected(base(encoding=enc))
            rejected(base(encoding={"x": {"field": "a", parent: {key.upper(): bad}}}))
    rejected(base(background=bad))
    rejected(base(view={"fill": "red", "Background": bad}))
    rejected(
        base(
            encoding={
                "x": {
                    "field": "a",
                    "axis": {
                        "labelColor": {"condition": {"param": "p", "value": bad}, "value": "red"}
                    },
                }
            }
        )
    )


def test_colour_like_keys_good_values():
    for good in ("#fff", "steelblue"):
        for parent, key in COLOUR_KEYS:
            if parent == "title":
                validate_chart_spec(base(title={"text": "t", key: good}))
            else:
                validate_chart_spec(base(encoding={"x": {"field": "a", parent: {key: good}}}))
        validate_chart_spec(base(background=good))
    validate_chart_spec(base(data={"values": [{"labelColor": "var(--x)", "background": "x y"}]}))


@pytest.mark.parametrize("bad", ["var(--x)", 'image-set("/api/x" 1x)', "url(x)", "hexagon x"])
def test_paint_scale_range_strings_allow_listed(bad):
    for channel in ("color", "fill", "stroke"):
        rejected(base(encoding={channel: {"field": "a", "scale": {"range": ["red", bad]}}}))
        rejected(
            base(
                encoding={
                    channel: {
                        "condition": {"param": "p", "field": "a", "scale": {"range": [bad]}},
                        "value": "red",
                    }
                }
            )
        )
    rejected(base(encoding={"shape": {"field": "a", "scale": {"range": ["circle", bad]}}}))


def test_paint_scale_range_good_values():
    validate_chart_spec(
        base(encoding={"color": {"field": "a", "scale": {"range": ["#fff", "steelblue"]}}})
    )
    validate_chart_spec(
        base(encoding={"shape": {"field": "a", "scale": {"range": ["circle", "square"]}}})
    )
    validate_chart_spec(base(encoding={"size": {"field": "a", "scale": {"range": [10, 100]}}}))
    validate_chart_spec(
        base(encoding={"strokeDash": {"field": "a", "scale": {"range": [[1, 0], [4, 4]]}}})
    )


@pytest.mark.parametrize("channel", PAINT)
def test_scale_false_on_paint_channels_rejected(channel):
    rejected(base(encoding={channel: {"field": "a", "scale": False}}))
    rejected(base(encoding={channel: {"condition": {"param": "p", "field": "a", "scale": False}}}))
    validate_chart_spec(base(encoding={"x": {"field": "a", "scale": {"zero": False}}}))


def test_tooltip_conditions_shape_checked():
    for value in ({"image": "x"}, ["x"], {"a": 1}):
        cond = {"param": "p", "value": value}
        rejected(base(encoding={"tooltip": {"field": "a", "condition": cond}}))
        rejected(base(encoding={"tooltip": {"field": "a", "condition": [cond]}}))
        rejected(base(encoding={"tooltip": [{"field": "a", "condition": [cond]}]}))
        rejected(
            base(
                encoding={"tooltip": {"field": "a", "condition": {"param": "q", "condition": cond}}}
            )
        )
    rejected(base(encoding={"tooltip": {"field": "a", "condition": "x"}}))
    rejected(base(encoding={"tooltip": {"field": "a", "condition": ["x"]}}))
    validate_chart_spec(
        base(encoding={"tooltip": {"field": "a", "condition": {"param": "p", "value": "hi"}}})
    )
    validate_chart_spec(
        base(encoding={"tooltip": {"condition": [{"param": "p", "field": "b"}], "value": 3}})
    )


def test_docstring_names_image_keys_everywhere():
    import hearthkeep.sandbox.charts as charts

    doc = " ".join(charts.__doc__.split())
    assert "`image` key" in doc
    assert "both in data rows and everywhere else" in doc


RAW_VEGA_PROBE = {
    "$schema": V6,
    "signals": [{"name": "s", "value": 1, "on": [{"events": "click", "update": "1"}]}],
    "marks": [{"type": "symbol", "encode": {"update": {"x": {"value": 1}}}}],
}


def test_raw_vega_probe_rejected():
    rejected(RAW_VEGA_PROBE)
    rejected({k: v for k, v in RAW_VEGA_PROBE.items() if k != "$schema"})


@pytest.mark.parametrize(
    "key", ["signals", "marks", "scales", "axes", "legends", "projections", "on", "update",
            "init", "events", "Signals", "MARKS"],
)  # fmt: skip
def test_raw_vega_keys_rejected_anywhere(key):
    rejected(base(**{key: []}))
    rejected(base(layer=[{"mark": "point", key: []}]))
    rejected(base(encoding={"x": {"field": "a", "type": "quantitative", key: {}}}))
    # Data rows may use any column name.
    validate_chart_spec(base(data={"values": [{key: 1}]}))


def test_selection_on_false_still_allowed():
    validate_chart_spec(base(params=[{"name": "p", "select": {"type": "point", "on": False}}]))
    rejected(base(params=[{"name": "p", "value": 1, "on": False}]))


def test_requires_a_vega_lite_shape():
    rejected({"$schema": V6, "data": {"values": [{"a": 1}]}})
    rejected({"data": {"values": [{"a": 1}]}, "encoding": {}})
    for shape in ("layer", "hconcat", "vconcat", "concat"):
        validate_chart_spec({"data": {"values": [{"a": 1}]}, shape: [{"mark": "point"}]})
    validate_chart_spec(
        {"data": {"values": [{"a": 1}]}, "facet": {"field": "a"}, "spec": {"mark": "point"}}
    )
    validate_chart_spec(
        {"data": {"values": [{"a": 1}]}, "repeat": ["a"], "spec": {"mark": "point"}}
    )
