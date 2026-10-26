"""Charts as Vega-Lite v6 specs with their data inline (the app renders them offline)."""

from __future__ import annotations

import json
import re

import pandas as pd

from hk._results import emit, plain

SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
MAX_CHART_ROWS = 5000
_FIELD_SPECIALS = re.compile(r"([\\.\[\]])")


def field(name: str) -> str:
    """A column name as a Vega-Lite `field`: `.` and `[...]` mean nested access there, so
    they (and the backslash) are escaped. Data keys stay as they are."""
    return _FIELD_SPECIALS.sub(r"\\\1", str(name))


def _too_many(spec) -> bool:
    if isinstance(spec, dict):
        values = spec.get("values")
        if isinstance(values, list) and len(values) > MAX_CHART_ROWS:
            return True
        return any(_too_many(v) for v in spec.values())
    if isinstance(spec, list):
        return any(_too_many(v) for v in spec)
    return False


def chart(spec: dict, *, title: str) -> None:
    """Report a Vega-Lite spec (inline `values` only, at most 5,000 rows per dataset)."""
    if not isinstance(spec, dict):
        raise TypeError("a chart spec is a dict (Vega-Lite JSON)")
    if _too_many(spec):
        raise ValueError(f"a chart can show at most {MAX_CHART_ROWS:,} rows; aggregate first")
    json.dumps(spec, allow_nan=False)  # must be plain JSON
    emit({"type": "chart", "title": title, "spec": spec})


def _field_type(series: pd.Series, *, mark: str) -> str:
    if pd.api.types.is_datetime64_any_dtype(series):
        return "temporal"
    if pd.api.types.is_bool_dtype(series):
        return "nominal"
    if pd.api.types.is_numeric_dtype(series):
        return "ordinal" if mark == "bar" else "quantitative"
    return "nominal"


def _highlight_layers(highlight, x: str, x_type: str) -> list[dict]:
    layers = []
    for item in highlight or []:
        if not isinstance(item, dict) or "label" not in item:
            raise ValueError('each highlight is {"x", "label"} or {"from", "to", "label"}')
        label = str(item["label"])
        if "x" in item:
            values = [{x: plain(item["x"]), "hk_label": label}]
            layers.append(
                {
                    "data": {"values": values},
                    "mark": {"type": "rule", "strokeDash": [4, 4]},
                    "encoding": {
                        "x": {"field": field(x), "type": x_type},
                        "tooltip": [{"field": "hk_label", "type": "nominal", "title": "Note"}],
                    },
                }
            )
            layers.append(
                {
                    "data": {"values": values},
                    "mark": {"type": "text", "align": "left", "dx": 4, "dy": -4, "y": 0},
                    "encoding": {
                        "x": {"field": field(x), "type": x_type},
                        "text": {"field": "hk_label"},
                    },
                }
            )
        elif "from" in item and "to" in item:
            values = [
                {"hk_from": plain(item["from"]), "hk_to": plain(item["to"]), "hk_label": label}
            ]
            layers.append(
                {
                    "data": {"values": values},
                    "mark": {"type": "rect", "opacity": 0.15},
                    "encoding": {
                        "x": {"field": "hk_from", "type": x_type, "title": x},
                        "x2": {"field": "hk_to"},
                        "tooltip": [{"field": "hk_label", "type": "nominal", "title": "Note"}],
                    },
                }
            )
        else:
            raise ValueError('each highlight is {"x", "label"} or {"from", "to", "label"}')
    return layers


def _spec(mark: str, df: pd.DataFrame, x: str, y: str, color, title: str, highlight) -> dict:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("charts take a pandas DataFrame")
    if len(df) > MAX_CHART_ROWS:
        raise ValueError(
            f"a chart can show at most {MAX_CHART_ROWS:,} rows (this has {len(df):,}); "
            "aggregate first"
        )
    fields = [x, y] + ([color] if color else [])
    for name in fields:
        if name not in df.columns:
            raise KeyError(f"no such column: {name!r}")
    values = [
        {str(k): plain(v) for k, v in zip(fields, row, strict=True)}
        for row in df[fields].itertuples(index=False, name=None)
    ]
    x_type = _field_type(df[x], mark=mark)
    encoding: dict = {
        "x": {"field": field(x), "type": x_type},
        "y": {"field": field(y), "type": "quantitative"},
        "tooltip": [
            {"field": field(f), "type": _field_type(df[f], mark="tooltip")} for f in fields
        ],
    }
    params: list[dict] = []
    if mark in ("line", "point"):
        params.append({"name": "zoom", "select": "interval", "bind": "scales"})
    if color:
        encoding["color"] = {"field": field(color), "type": "nominal"}
        encoding["opacity"] = {"condition": {"param": "legend", "value": 1}, "value": 0.2}
        params.append(
            {
                "name": "legend",
                "select": {"type": "point", "fields": [field(color)]},
                "bind": "legend",
            }
        )
    main: dict = {"mark": {"type": mark}, "encoding": encoding}
    if mark == "line":
        main["mark"]["point"] = len(df) <= 200
    if params:
        main["params"] = params
    return {
        "$schema": SCHEMA,
        "title": title,
        "width": "container",
        "data": {"values": values},
        "layer": [main, *_highlight_layers(highlight, x, x_type)],
    }


def bar(
    df, x: str, y: str, *, color: str | None = None, title: str, highlight: list[dict] | None = None
) -> None:
    chart(_spec("bar", df, x, y, color, title, highlight), title=title)


def line(
    df, x: str, y: str, *, color: str | None = None, title: str, highlight: list[dict] | None = None
) -> None:
    chart(_spec("line", df, x, y, color, title, highlight), title=title)


def scatter(
    df, x: str, y: str, *, color: str | None = None, title: str, highlight: list[dict] | None = None
) -> None:
    chart(_spec("point", df, x, y, color, title, highlight), title=title)
