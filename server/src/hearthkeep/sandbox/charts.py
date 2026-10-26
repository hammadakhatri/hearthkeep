"""Validation of the Vega-Lite specs a sandbox run emits, before anything is stored or shown.

A spec is untrusted JSON written by model-made code. The browser renders it offline, so the
rule is: nothing in it may make the browser fetch anything, or run an expression. The
checks are a deny-list for the fetch-capable keys plus an allow-list for the shapes that
could still reach one (data, transforms, params, conditions):

* every `data` object is `{"values": [objects], "name"?}`; no `url`, `format`, `datasets`
* no `url`, `href`, `usermeta`, `loader`, `$ref` keys and no `image` mark
* no Vega expressions: the keys `expr`, `signal`, `calculate` and anything ending in
  `expr` (`labelExpr`, ...); `test` only as `{"param": ...}` inside a `condition`
* transforms are limited to filter (field predicates), aggregate, bin, timeUnit, fold,
  window and joinaggregate
* params bind only to `scales` or `legend`
* no string holds a URL (scheme, or leading `//`), in keys or values, except `$schema`
* limits: 1 MiB serialized, 5,000 rows per dataset, 20,000 rows in total, depth 20
"""

from __future__ import annotations

import copy
import json
import math
import re

ALLOWED_SCHEMAS = {
    "https://vega.github.io/schema/vega-lite/v5.json",
    "https://vega.github.io/schema/vega-lite/v6.json",
}

MAX_BYTES = 1024 * 1024
MAX_ROWS_PER_DATASET = 5000
MAX_ROWS_TOTAL = 20000
MAX_DEPTH = 20
MAX_NODES = 500_000

_FORBIDDEN_KEYS = {
    "url",
    "href",
    "datasets",
    "usermeta",
    "loader",
    "$ref",
    "expr",
    "signal",
    "calculate",
}
_URL_SCHEME = re.compile(r"(?<![A-Za-z0-9+.-])(?:https?|ftp|file|data|javascript):", re.IGNORECASE)
_URL_SCHEME_AT_START = re.compile(r"(?:https?|ftp|file|data|javascript):", re.IGNORECASE)
_DROP_FOR_START = re.compile(r"[\s\x00-\x1f\x7f​-‏  ﻿]+")

_TRANSFORMS = {"filter", "aggregate", "bin", "timeUnit", "fold", "window", "joinaggregate"}
_TRANSFORM_COMPANIONS = {"groupby", "field", "as", "frame", "sort", "ignorePeers"}
_PREDICATE_KEYS = {
    "field",
    "timeUnit",
    "equal",
    "lt",
    "lte",
    "gt",
    "gte",
    "range",
    "oneOf",
    "valid",
}
_PARAM_KEYS = {"name", "value", "select", "bind", "views"}
_SELECT_KEYS = {
    "type",
    "fields",
    "encodings",
    "resolve",
    "nearest",
    "clear",
    "toggle",
    "on",
    "mark",
    "translate",
    "zoom",
}
_BIND_VALUES = {"scales", "legend"}


class ChartError(Exception):
    """The chart spec breaks a rule; the message says which, never what the content was."""


def _bad(message: str) -> ChartError:
    return ChartError(message)


def _has_url(text: str) -> bool:
    if _URL_SCHEME.search(text):
        return True
    squeezed = _DROP_FOR_START.sub("", text)
    return bool(_URL_SCHEME_AT_START.match(squeezed)) or squeezed.startswith("//")


class _Walker:
    def __init__(self) -> None:
        self.nodes = 0
        self.rows = 0

    def walk(
        self, node: object, depth: int, *, where: str, data_mode: bool = False, cond: bool = False
    ) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise _bad("the chart has too many elements")
        if isinstance(node, dict):
            self._dict(node, depth + 1, where=where, data_mode=data_mode, cond=cond)
        elif isinstance(node, list):
            if depth + 1 > MAX_DEPTH:
                raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
            for item in node:
                self.walk(item, depth + 1, where=where, data_mode=data_mode, cond=cond)
        elif isinstance(node, str):
            if _has_url(node):
                raise _bad(f"a text in '{where}' looks like a URL")
        elif isinstance(node, bool) or node is None or isinstance(node, int):
            return
        elif isinstance(node, float):
            if not math.isfinite(node):
                raise _bad(f"'{where}' holds a number that is not finite")
        else:
            raise _bad(f"'{where}' holds a value that is not plain JSON")

    def _dict(self, node: dict, depth: int, *, where: str, data_mode: bool, cond: bool) -> None:
        if depth > MAX_DEPTH:
            raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
        for key, child in node.items():
            if not isinstance(key, str):
                raise _bad("the chart has a key that is not text")
            if _has_url(key):
                raise _bad("a key looks like a URL")
            if data_mode:
                self.walk(child, depth, where=where, data_mode=True)
                continue
            lowered = key.lower()
            if lowered in _FORBIDDEN_KEYS or lowered.endswith("expr"):
                raise _bad(f"the key '{key}' is not allowed")
            if key == "test":
                if not cond or not self._is_param_test(child):
                    raise _bad("'test' is only allowed as {param} inside a condition")
                self.walk(child, depth, where=key)
            elif key == "data":
                self._data(child, depth, where=key)
            elif key == "mark":
                self._mark(child)
                self.walk(child, depth, where=key)
            elif key == "transform":
                self._transforms(child)
                self.walk(child, depth, where=key)
            elif key == "params":
                self._params(child)
                self.walk(child, depth, where=key)
            elif key == "bind":
                if not isinstance(child, str) or child not in _BIND_VALUES:
                    raise _bad("a param can only be bound to 'scales' or 'legend'")
            elif key == "condition":
                self.walk(child, depth, where=key, cond=True)
            else:
                self.walk(child, depth, where=key)

    @staticmethod
    def _is_param_test(value: object) -> bool:
        if not isinstance(value, dict) or not isinstance(value.get("param"), str):
            return False
        return set(value) <= {"param", "empty"} and isinstance(value.get("empty", True), bool)

    def _data(self, data: object, depth: int, *, where: str) -> None:
        if not isinstance(data, dict):
            raise _bad("'data' must be an object with inline values")
        if not set(data) <= {"values", "name"} or "values" not in data:
            raise _bad("'data' may only hold inline 'values' (and a 'name')")
        values = data["values"]
        if not isinstance(values, list) or not all(isinstance(row, dict) for row in values):
            raise _bad("'values' must be a list of objects")
        if len(values) > MAX_ROWS_PER_DATASET:
            raise _bad(f"a dataset has more than {MAX_ROWS_PER_DATASET:,} rows")
        self.rows += len(values)
        if self.rows > MAX_ROWS_TOTAL:
            raise _bad(f"the chart has more than {MAX_ROWS_TOTAL:,} rows in total")
        if "name" in data and not isinstance(data["name"], str):
            raise _bad("a data name must be text")
        if depth + 1 > MAX_DEPTH:
            raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
        self.walk(values, depth, where="values", data_mode=True)
        if "name" in data:
            self.walk(data["name"], depth, where="name")

    @staticmethod
    def _mark(mark: object) -> None:
        kind = mark.get("type") if isinstance(mark, dict) else mark
        if isinstance(kind, str) and kind.lower() == "image":
            raise _bad("the image mark is not allowed")

    def _transforms(self, transforms: object) -> None:
        if not isinstance(transforms, list):
            raise _bad("'transform' must be a list")
        for item in transforms:
            if not isinstance(item, dict):
                raise _bad("a transform must be an object")
            ops = set(item) & (_TRANSFORMS | {"calculate", "lookup"})
            if len(ops) != 1 or not ops <= _TRANSFORMS:
                raise _bad("a transform is not one of the allowed kinds")
            if not set(item) <= ops | _TRANSFORM_COMPANIONS:
                raise _bad("a transform has a key that is not allowed")
            if "filter" in item:
                self._predicate(item["filter"])

    def _predicate(self, predicate: object) -> None:
        if not isinstance(predicate, dict):
            raise _bad("a filter must be a field predicate, not an expression")
        combiners = set(predicate) & {"and", "or", "not"}
        if combiners:
            if len(predicate) != 1:
                raise _bad("a filter combiner stands alone")
            (name,) = combiners
            if name == "not":
                self._predicate(predicate["not"])
                return
            parts = predicate[name]
            if not isinstance(parts, list) or not parts:
                raise _bad("'and' and 'or' take a list of predicates")
            for part in parts:
                self._predicate(part)
            return
        if not isinstance(predicate.get("field"), str) or not set(predicate) <= _PREDICATE_KEYS:
            raise _bad("a filter must be a field predicate")

    def _params(self, params: object) -> None:
        if not isinstance(params, list):
            raise _bad("'params' must be a list")
        for param in params:
            if not isinstance(param, dict) or not isinstance(param.get("name"), str):
                raise _bad("a param needs a name")
            if not set(param) <= _PARAM_KEYS:
                raise _bad("a param has a key that is not allowed")
            if "select" in param:
                self._select(param["select"])

    @staticmethod
    def _select(select: object) -> None:
        if isinstance(select, str):
            kind = select
        elif isinstance(select, dict):
            kind = select.get("type")
            if not set(select) <= _SELECT_KEYS:
                raise _bad("a selection has a key that is not allowed")
            for key in ("toggle", "clear", "nearest", "translate", "zoom"):
                if key in select and not isinstance(select[key], bool):
                    raise _bad(f"selection '{key}' must be true or false")
            if "on" in select and not isinstance(select["on"], str):
                raise _bad("selection 'on' must be an event name")
        else:
            raise _bad("a selection must be 'interval', 'point' or an object")
        if kind not in ("interval", "point"):
            raise _bad("a selection is 'interval' or 'point'")


def validate_chart_spec(spec: object) -> dict:
    """Return a deep copy of `spec` if it is safe to store and render, else raise ChartError."""
    if not isinstance(spec, dict):
        raise _bad("a chart spec is a JSON object")
    body = dict(spec)
    if "$schema" in body:
        schema = body.pop("$schema")
        if not isinstance(schema, str) or schema not in ALLOWED_SCHEMAS:
            raise _bad("the chart schema is not Vega-Lite v5 or v6")
    try:
        _Walker().walk(body, 0, where="spec")
        size = len(json.dumps(spec, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except RecursionError:
        raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels") from None
    except (TypeError, ValueError) as exc:
        raise _bad("the chart is not plain JSON") from exc
    if size > MAX_BYTES:
        raise _bad("the chart is larger than 1 MB")
    return copy.deepcopy(spec)
