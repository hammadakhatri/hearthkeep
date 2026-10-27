"""Validation of the Vega-Lite specs a sandbox run emits, before anything is stored or shown.

A spec is untrusted JSON written by model-made code. The browser renders it offline, so the
rule is: nothing in it may make the browser fetch anything, or run an expression. Shapes that
could reach a fetch or an expression are allow-listed (data, transforms, params, conditions,
paint values, tooltips); a deny-list of keys and strings backs them up:

* Vega-Lite only: the top level has one of `mark layer hconcat vconcat concat facet repeat
  spec`, and raw Vega keys (`signals marks scales axes legends projections on update init
  events`, any case) are refused at any depth outside data rows; the one exception is a
  selection's `on`, which may only be `false`
* no `config` (the app supplies its own theme at render time) and no legacy `selection`
* every `data` object is `{"values": [objects], "name"?}`; no `url`, `format`, `datasets`
* no `url`, `href`, `usermeta`, `loader`, `$ref` keys; no `image` mark; `image` keys are
  rejected both in data rows and everywhere else; no string `image` outside data rows
  (tooltip images through derived fields)
* no Vega expressions: the keys `expr`, `signal`, `calculate` and anything ending in
  `expr` (`labelExpr`, ...); `test` only as `{"param": ...}` inside a `condition`
* transforms are limited to filter (field predicates), aggregate, bin, timeUnit, fold,
  window and joinaggregate, each with only its own companion keys
* params bind only to `scales` or `legend`; selection `on` and `clear` are `false` only;
  `toggle`, `translate` and `zoom` are booleans (no event or expression strings)
* paint values (`color fill stroke fillOpacity strokeOpacity strokeDash strokeWidth opacity
  size shape`, as encoding channels, conditions and mark properties): a `value` or `datum`
  string is a hex colour, `rgb()/rgba()/hsl()/hsla()` of digits, a colour keyword, or (for
  `shape`) a Vega shape name; numbers and number lists are fine. Their scales are never
  `null`, `false`, `identity`, or a `range` that names a `field`, in the channel or any
  condition, and the string items of a `range` list pass the same allow-list.
* colour-like keys (any key ending in `color` or `background`, such as `labelColor`,
  `gridColor`, `fillColor`, `subtitleColor`): their strings pass the colour allow-list too
* `cursor` (any depth) is one of the standard CSS cursor keywords; no `image-set()` or URL
* a `tooltip` encoding is a field definition, a list of them, `null` or `false` (a `value`
  is never an object or list), and so is each of its conditions (dict or list, nested);
  a mark `tooltip` is a boolean, `null` or `{"content": ...}`
* `formatType` is `number`, `time` or `utc`; `format` is short text, no `( [ {`, no 4+ digit run
* no string outside data rows holds a URL (`http(s)://`, `ftp://`, `file://`, `javascript:`,
  `data:` with a type or `,;`, a start of `http:`/`ftp:`/`file:` or of two slashes) or a CSS
  `url(` (also written with escapes), except `$schema`. String values in data rows may look
  like URLs (Vega never loads them) but their keys may not. A backslash plus `(` is refused
  except in an encoding channel's `field` and in `params[].select.fields` (column names).
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
    "config",
    "selection",
    "expr",
    "signal",
    "calculate",
}
# Raw Vega (not Vega-Lite) keys: event streams, signal updates and marks the web app would
# otherwise run if a spec reached Vega directly.
_RAW_VEGA_KEYS = {
    "signals",
    "marks",
    "scales",
    "axes",
    "legends",
    "projections",
    "on",
    "update",
    "init",
    "events",
}
_VEGA_LITE_SHAPES = ("mark", "layer", "hconcat", "vconcat", "concat", "facet", "repeat", "spec")
_BOUNDARY = r"(?<![A-Za-z0-9+.-])"
_URL_SCHEME = re.compile(
    _BOUNDARY + r"(?:(?:https?|ftp|file)://|javascript:|data:\s*(?:[\w.+-]+/|[,;]))", re.IGNORECASE
)
_SCHEME_START = re.compile(r"(?:https?|ftp|file):(?=\S)", re.IGNORECASE)
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\n\r\f]?|\\(.)", re.DOTALL)
_DIGIT_RUN = re.compile(r"\d{4,}")
_PAINT_KEYS = {
    "color",
    "fill",
    "stroke",
    "fillopacity",
    "strokeopacity",
    "strokedash",
    "strokewidth",
    "opacity",
    "size",
    "shape",
}
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}")
_FUNC_COLOR = re.compile(r"(?:rgba?|hsla?)\([\d.,% ]*\)", re.IGNORECASE)
_KEYWORD_COLOR = re.compile(r"[a-zA-Z]{3,20}")
_SHAPES = {
    "circle",
    "square",
    "cross",
    "diamond",
    "triangle-up",
    "triangle-down",
    "triangle-right",
    "triangle-left",
    "stroke",
    "arrow",
    "wedge",
    "triangle",
}
_CURSORS = frozenset(
    "auto default none context-menu help pointer progress wait cell crosshair text"
    " vertical-text alias copy move no-drop not-allowed grab grabbing all-scroll col-resize"
    " row-resize n-resize e-resize s-resize w-resize ne-resize nw-resize se-resize sw-resize"
    " ew-resize ns-resize nesw-resize nwse-resize zoom-in zoom-out".split()
)
_PROTOCOL_RELATIVE = re.compile(r"[/\\]{2}")
_STRIP_IN_SCHEME = re.compile(r"[\t\r\n\x00]")
_LEADING_JUNK = re.compile(r"^[\s\x00-\x1f\x7f\u200b-\u200f\u2028\u2029\ufeff]+")
_CSS_URL = re.compile(r"url\s*\(", re.IGNORECASE)
_FORMAT_TYPES = {"number", "time", "utc"}
MAX_FORMAT = 40
QUOTE = 40

_TRANSFORM_KEYS = {
    "filter": {"filter"},
    "aggregate": {"aggregate", "groupby"},
    "bin": {"bin", "field", "as"},
    "timeUnit": {"timeUnit", "field", "as"},
    "fold": {"fold", "as"},
    "window": {"window", "groupby", "frame", "sort", "ignorePeers"},
    "joinaggregate": {"joinaggregate", "groupby"},
}
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
_SELECT_BOOLEANS = ("on", "clear", "toggle", "translate", "zoom", "nearest")
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
    """The chart spec breaks a rule; the message names it, quoting at most 40 characters."""


def _bad(message: str) -> ChartError:
    return ChartError(message)


def _q(text: object) -> str:
    """At most 40 characters of a key or string, for an error message."""
    s = text if isinstance(text, str) else repr(text)
    s = s[:QUOTE] + ("..." if len(s) > QUOTE else "")
    return repr(s.encode("ascii", "backslashreplace").decode("ascii"))


def _has_url(text: str) -> bool:
    plain = _STRIP_IN_SCHEME.sub("", text)
    if _URL_SCHEME.search(plain):
        return True
    start = _LEADING_JUNK.sub("", plain)
    return bool(_PROTOCOL_RELATIVE.match(start) or _SCHEME_START.match(start))


def _css_unescape(text: str) -> str:
    def one(m: re.Match) -> str:
        if m.group(1):
            code = int(m.group(1), 16)
            return chr(code) if 0 < code <= 0x10FFFF else "\ufffd"
        return m.group(2)

    return _CSS_ESCAPE.sub(one, text)


def _has_css_url(text: str, *, column: bool = False) -> bool:
    """A CSS url( written plainly or with escapes (u\\72l(, url\\28 ), or a backslash with ( )."""
    if _CSS_URL.search(text):
        return True
    if "\\" not in text:
        return False
    if "(" in text and not column:  # column names (`field`) legitimately hold hk's escapes
        return True
    return bool(_CSS_URL.search(_css_unescape(text)))


def _check_text(text: str, *, what: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad(f"{what} is not valid text: {_q(text)}") from None


class _Walker:
    def __init__(self) -> None:
        self.nodes = 0
        self.rows = 0

    def walk(
        self,
        node: object,
        depth: int,
        *,
        where: str,
        data_mode: bool = False,
        cond: bool = False,
        ctx: str | None = None,
        column: bool = False,
    ) -> None:
        """`ctx` says what a dict (or the items of a list) is: the "encoding" map, a "channel"
        definition, the "params" list, one "select" object. `column` marks a column name."""
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise _bad("the chart has too many elements")
        if isinstance(node, dict):
            self._dict(node, depth + 1, where=where, data_mode=data_mode, cond=cond, ctx=ctx)
        elif isinstance(node, list):
            if depth + 1 > MAX_DEPTH:
                raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
            for item in node:
                self.walk(
                    item,
                    depth + 1,
                    where=where,
                    data_mode=data_mode,
                    cond=cond,
                    ctx=ctx,
                    column=column,
                )
        elif isinstance(node, str):
            _check_text(node, what=f"a text in {_q(where)}")
            if data_mode:
                return
            if _has_url(node):
                raise _bad(f"a text in {_q(where)} looks like a URL: {_q(node)}")
            if _has_css_url(node, column=column):
                raise _bad(f"a text in {_q(where)} has a CSS url(): {_q(node)}")
            if node.strip().casefold() == "image":
                raise _bad(f"the text 'image' in {_q(where)} is not allowed")
        elif isinstance(node, bool) or node is None or isinstance(node, int):
            return
        elif isinstance(node, float):
            if not math.isfinite(node):
                raise _bad(f"{_q(where)} holds a number that is not finite")
        else:
            raise _bad(f"{_q(where)} holds a value that is not plain JSON")

    def _dict(
        self,
        node: dict,
        depth: int,
        *,
        where: str,
        data_mode: bool,
        cond: bool,
        ctx: str | None,
    ) -> None:
        if depth > MAX_DEPTH:
            raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
        for key, child in node.items():
            if not isinstance(key, str):
                raise _bad("the chart has a key that is not text")
            _check_text(key, what="a key")
            if _has_url(key) or _CSS_URL.search(key):
                raise _bad(f"the key {_q(key)} looks like a URL")
            if data_mode:
                if key.lower() == "image":
                    raise _bad("a data column named 'image' is not allowed")
                self.walk(child, depth, where=where, data_mode=True)
                continue
            lowered = key.lower()
            if lowered in _FORBIDDEN_KEYS or lowered.endswith("expr"):
                raise _bad(f"the key {_q(key)} is not allowed")
            if lowered.strip() in _RAW_VEGA_KEYS and not (key == "on" and ctx == "select"):
                raise _bad(f"the key {_q(key)} is raw Vega, not Vega-Lite")
            if lowered.strip() == "image" or key.strip().casefold() == "image":
                raise _bad("an 'image' key is not allowed")
            if lowered.strip() == "cursor":
                if not isinstance(child, str) or child.strip().lower() not in _CURSORS:
                    raise _bad(f"a cursor must be a CSS cursor keyword: {_q(child)}")
            if lowered == "tooltip":
                self._tooltip(child, in_mark=where == "mark")
            elif lowered in _PAINT_KEYS:
                self._paint(lowered, child, in_mark=where == "mark")
            elif lowered.endswith(("color", "background")):
                self._colour_like(child)
            if lowered.endswith("formattype"):
                if not isinstance(child, str) or child not in _FORMAT_TYPES:
                    raise _bad("formatType must be 'number', 'time' or 'utc'")
            elif lowered.endswith("format"):
                self._format(child)
            if key == "title":
                self._title(child)
            if key == "test":
                if not cond or not self._is_param_test(child):
                    raise _bad("'test' is only allowed as {param} inside a condition")
                self.walk(child, depth, where=key)
            elif key == "encoding":
                self.walk(child, depth, where=key, ctx="encoding")
            elif key == "field" and ctx == "channel":
                self.walk(child, depth, where=key, column=True)
            elif key == "select" and ctx == "param":
                self.walk(child, depth, where=key, ctx="select")
            elif key == "fields" and ctx == "select":
                self.walk(child, depth, where=key, column=True)
            elif key == "data":
                self._data(child, depth)
            elif key == "mark":
                self._mark(child)
                self.walk(child, depth, where=key)
            elif key == "transform":
                self._transforms(child)
                self.walk(child, depth, where=key)
            elif key == "params":
                self._params(child)
                self.walk(child, depth, where=key, ctx="param")
            elif key == "bind":
                if not isinstance(child, str) or child not in _BIND_VALUES:
                    raise _bad("a param can only be bound to 'scales' or 'legend'")
            elif key == "condition":
                self.walk(child, depth, where=key, cond=True, ctx="channel")
            else:
                self.walk(child, depth, where=key, ctx="channel" if ctx == "encoding" else None)

    @staticmethod
    def _scale(name: str, defn: dict) -> None:
        """A paint channel keeps a real scale: not null, not identity, no range from a field."""
        if "scale" not in defn:
            return
        scale = defn["scale"]
        if scale is None or scale is False:
            raise _bad("a paint channel needs a scale (not null or false)")
        if isinstance(scale, dict):
            if str(scale.get("type")).strip().lower() == "identity":
                raise _bad("an identity scale is not allowed on a paint channel")
            if isinstance(scale.get("range"), dict) and "field" in scale["range"]:
                raise _bad("a paint scale range cannot come from a field")
            if isinstance(scale.get("range"), list):
                for item in scale["range"]:
                    if isinstance(item, str):
                        _Walker._paint_string(name, item)

    @staticmethod
    def _paint_string(name: str, text: str) -> None:
        if name == "shape":
            ok = text.strip().lower() in _SHAPES
        else:
            ok = bool(
                _HEX_COLOR.fullmatch(text)
                or _FUNC_COLOR.fullmatch(text)
                or _KEYWORD_COLOR.fullmatch(text)
            )
        if not ok:
            raise _bad(f"a {_q(name)} value is not a plain colour or shape: {_q(text)}")

    def _paint_value(self, name: str, value: object) -> None:
        if isinstance(value, str):
            self._paint_string(name, value)
        elif value is None or isinstance(value, bool | int | float):
            return
        elif isinstance(value, list) and all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in value
        ):
            return
        else:
            raise _bad(f"a {_q(name)} value must be a colour, shape, number or list of numbers")

    def _paint(self, name: str, child: object, *, in_mark: bool) -> None:
        if isinstance(child, dict):
            if in_mark:
                raise _bad(f"the mark property {_q(name)} must be a plain value")
            self._paint_def(name, child, 0)
        else:
            self._paint_value(name, child)

    def _paint_def(self, name: str, defn: dict, level: int) -> None:
        if level > MAX_DEPTH:
            raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
        self._scale(name, defn)
        for key in ("value", "datum"):
            if key in defn:
                self._paint_value(name, defn[key])
        if "condition" in defn:
            cond = defn["condition"]
            for item in cond if isinstance(cond, list) else [cond]:
                if not isinstance(item, dict):
                    raise _bad("a condition must be an object")
                self._paint_def(name, item, level + 1)

    def _colour_like(self, child: object) -> None:
        """A key such as `labelColor` or `background`: its colour strings are allow-listed."""
        if isinstance(child, dict):
            self._paint_def("color", child, 0)
        elif isinstance(child, list):
            for item in child:
                self._colour_like(item)
        else:
            self._paint_value("color", child)

    @staticmethod
    def _tooltip(child: object, *, in_mark: bool, level: int = 0) -> None:
        if in_mark:
            if child is None or isinstance(child, bool):
                return
            if isinstance(child, dict) and set(child) == {"content"}:
                if child["content"] in ("data", "encoding"):
                    return
            raise _bad("a mark tooltip is true, false, null or {content: data|encoding}")
        if level > MAX_DEPTH:
            raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
        if child is None or child is False:
            return
        for item in child if isinstance(child, list) else [child]:
            if not isinstance(item, dict):
                raise _bad("a tooltip is a field definition, a list of them, null or false")
            if isinstance(item.get("value"), dict | list):
                raise _bad("a tooltip value must be plain text or a number")
            if "condition" in item:
                _Walker._tooltip(item["condition"], in_mark=False, level=level + 1)

    @staticmethod
    def _format(value: object) -> None:
        if not isinstance(value, str) or len(value) > MAX_FORMAT or any(c in value for c in "([{"):
            raise _bad(f"a format is text of at most {MAX_FORMAT} characters without ( [ {{")
        if _DIGIT_RUN.search(value):
            raise _bad("a format may not hold a run of 4 or more digits")

    @staticmethod
    def _title(value: object) -> None:
        for title in value if isinstance(value, list) else [value]:
            if isinstance(title, str) and title.strip().lower() == "image":
                raise _bad("a title 'image' is not allowed")

    @staticmethod
    def _is_param_test(value: object) -> bool:
        if not isinstance(value, dict) or not isinstance(value.get("param"), str):
            return False
        return set(value) <= {"param", "empty"} and isinstance(value.get("empty", True), bool)

    def _data(self, data: object, depth: int) -> None:
        """`depth` is that of the dict holding the `data` key; `data` itself is one deeper."""
        if not isinstance(data, dict):
            raise _bad("'data' must be an object with inline values")
        if depth + 1 > MAX_DEPTH:
            raise _bad(f"the chart is nested deeper than {MAX_DEPTH} levels")
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
        self.walk(values, depth + 1, where="values", data_mode=True)
        if "name" in data:
            self.walk(data["name"], depth + 1, where="name")

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
            ops = set(item) & (set(_TRANSFORM_KEYS) | {"calculate", "lookup"})
            if len(ops) != 1 or not ops <= set(_TRANSFORM_KEYS):
                raise _bad("a transform is not one of the allowed kinds")
            (op,) = ops
            if not set(item) <= _TRANSFORM_KEYS[op]:
                raise _bad(f"a {op} transform has a key that is not allowed")
            if op == "filter":
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
            for key in ("on", "clear"):
                if key in select and select[key] is not False:
                    raise _bad(f"selection '{key}' can only be false")
            for key in _SELECT_BOOLEANS:
                if key in select and not isinstance(select[key], bool):
                    raise _bad(f"selection '{key}' must be true or false, not an event string")
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
    if not any(key in body for key in _VEGA_LITE_SHAPES):
        raise _bad("a chart spec is Vega-Lite: it needs a mark, layer, concat, facet or repeat")
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
