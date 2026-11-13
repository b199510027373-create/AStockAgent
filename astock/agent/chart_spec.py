"""ChartSpec v1 声明式图表规格：字段 / 枚举 / 长度白名单校验（ADR-016 C1）。

实现依据是 ``docs/AStockAgent_interface_freeze_v1.md`` §3「ChartSpec v1」，字段表见 §3.1、
``data`` 子结构见 §3.2、算子与图层参数见 §3.3。约束要点：

- ``additionalProperties: false``：未知字段一律报错，**不静默忽略**；
- 枚举封闭：``kind`` / ``data.source`` / ``data.period`` / ``data.range`` / ``data.adj`` /
  ``transforms[].op`` / ``layers[].type`` / ``annotations[].type`` / ``theme``；
- 数量上限：``transforms`` ≤ 8、``layers`` ≤ 8、``annotations`` ≤ 10、``title`` ≤ 120 字符、
  ``meta`` JSON 序列化 ≤ 1 KB、inline 数据 ≤ 5000 行 / 1–20 字段；
- 词表消歧（§3.2 评审裁决）：``period`` 的分钟档带 ``min``（``1min``…``60min``），
  ``range`` 的月档带 ``mo``（``1mo``/``3mo``/``6mo``），两者**不共用 ``1m``**；
- 不执行任意 Python：本模块只做白名单校验，``transforms`` 的算子由
  :mod:`astock.agent.chart_render` 用 pandas 显式实现。

与冻结清单的两处**显式兼容**（验收条目 plan-20261107-005 的字段串里有 ``panes``/``style``，
而 §3.1 与 ADR-016 只有 ``layers``/``theme``）：

- ``panes`` 作为 ``layers`` 的别名，``style`` 作为 ``theme`` 的别名（``style`` 允许
  ``"dark"`` 或 ``{"theme": "dark"}``）；
- 别名与规范字段**同时出现**即报字段级错误，避免"两个来源各写一半"的静默覆盖；
- 归一化输出只保留 §3.1 的规范字段，别名不进入归一化结果（因此 artifact 元数据
  里的 ``spec`` 永远是冻结字段集）。

错误一律结构化：:class:`ChartSpecError.issues` 是 ``[{field, reason}]`` 列表，
``field`` 使用 ``$.`` 根记法（如 ``$.encode.y``、``$.transforms[0].op``），可直接回传模型重试。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

__all__ = [
    "ADJ_ALIASES",
    "ADJ_VALUES",
    "ANNOTATION_TYPES",
    "CMP_OPS",
    "DATA_FIELDS",
    "ENCODE_FIELDS",
    "FIELD_RE",
    "KINDS",
    "LAYER_TYPES",
    "MAX_ANNOTATIONS",
    "MAX_INLINE_FIELDS",
    "MAX_INLINE_ROWS",
    "MAX_LAYERS",
    "MAX_META_BYTES",
    "MAX_RANGE_YEARS",
    "MAX_TITLE_CHARS",
    "MAX_TRANSFORMS",
    "MAX_Y_SERIES",
    "METRIC_FUNCS",
    "MINUTE_PERIODS",
    "PERIODS",
    "RANGES",
    "SOURCES",
    "SPEC_VERSION",
    "THEMES",
    "TOP_LEVEL_FIELDS",
    "TRANSFORM_OPS",
    "ChartSpecError",
    "SpecIssue",
    "chart_spec_json_schema",
    "check_chart_spec",
    "normalize_chart_spec",
    "validate_chart_spec",
]

# ---------------------------------------------------------------------------
# 冻结常量（《接口冻结清单》§3；单一定义点，渲染器与工具层必须复用）
# ---------------------------------------------------------------------------

SPEC_VERSION = 1
KINDS = ("candlestick", "line", "bar", "scatter", "heatmap", "histogram")
SOURCES = ("market", "artifact", "inline")
PERIODS = ("day", "week", "month", "1min", "5min", "15min", "30min", "60min")
MINUTE_PERIODS = ("1min", "5min", "15min", "30min", "60min")
RANGES = ("1mo", "3mo", "6mo", "1y", "3y", "5y", "ytd", "max")
ADJ_VALUES = ("qfq", "hfq", "none")
ADJ_ALIASES: dict[str, str] = {"raw": "none"}
THEMES = ("light", "dark")
TRANSFORM_OPS = ("filter", "groupby", "agg", "sort", "limit")
LAYER_TYPES = ("ma", "boll", "volume", "macd", "rsi", "kdj")
ANNOTATION_TYPES = ("hline", "vline", "text")
CMP_OPS = ("gt", "ge", "lt", "le", "eq", "ne", "in")
METRIC_FUNCS = ("sum", "mean", "min", "max", "count", "last")

MAX_TITLE_CHARS = 120
MAX_TRANSFORMS = 8
MAX_LAYERS = 8
MAX_ANNOTATIONS = 10
MAX_META_BYTES = 1024
MAX_INLINE_ROWS = 5000
MAX_INLINE_FIELDS = 20
MAX_Y_SERIES = 4
MAX_RANGE_YEARS = 10

#: 规范字段（§3.1），顺序即归一化输出顺序。
TOP_LEVEL_FIELDS = (
    "version",
    "kind",
    "title",
    "data",
    "encode",
    "transforms",
    "layers",
    "theme",
    "annotations",
    "meta",
)
DATA_FIELDS = ("source", "ref", "rows", "fields", "period", "range", "adj")
ENCODE_FIELDS = ("x", "y", "color")

#: 验收条目使用的别名 → 冻结字段名。
FIELD_ALIASES: dict[str, str] = {"panes": "layers", "style": "theme"}

FIELD_RE = re.compile(r"^[a-z_][a-z0-9_]{0,31}$")
STOCK_REF_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
INDEX_REF_RE = re.compile(r"^000[0-9]{3}\.SH$|^399[0-9]{3}\.SZ$")
ARTIFACT_REF_RE = re.compile(r"^art_[0-9a-f]{16}$")
RANGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")

#: 图层参数白名单：``type -> {参数名: (类型标记, 取值范围)}``。
LAYER_PARAMS: dict[str, dict[str, tuple[str, Any]]] = {
    "ma": {"window": ("int", (2, 250))},
    "boll": {"window": ("int", (5, 250)), "k": ("number", (1, 3))},
    "volume": {},
    "macd": {
        "fast": ("int", (8, 20)),
        "slow": ("int", (21, 60)),
        "signal": ("int", (5, 15)),
    },
    "rsi": {"window": ("int", (2, 60))},
    "kdj": {"n": ("int", (5, 90)), "k": ("int", (3, 3)), "m": ("int", (3, 3))},
}

#: 图层默认参数（缺省即用，不写进 spec 也能复现）。
LAYER_DEFAULTS: dict[str, dict[str, Any]] = {
    "ma": {"window": 20},
    "boll": {"window": 20, "k": 2.0},
    "volume": {},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "rsi": {"window": 14},
    "kdj": {"n": 9, "k": 3, "m": 3},
}

#: transform 参数白名单：``op -> {参数名: (类型标记, 取值范围)}``。
TRANSFORM_PARAMS: dict[str, dict[str, tuple[str, Any]]] = {
    "filter": {
        "field": ("field", None),
        "cmp": ("enum", CMP_OPS),
        "value": ("value", None),
    },
    "groupby": {"by": ("bylist", None), "metrics": ("metrics", None)},
    "agg": {"by": ("bylist", None), "metrics": ("metrics", None)},
    "sort": {"by": ("field", None), "desc": ("bool", None)},
    "limit": {"n": ("int", (1, 5000)), "from": ("enum", ("head", "tail"))},
}

TRANSFORM_REQUIRED: dict[str, tuple[str, ...]] = {
    "filter": ("field", "cmp", "value"),
    "groupby": ("by", "metrics"),
    "agg": ("by", "metrics"),
    "sort": ("by",),
    "limit": ("n",),
}

#: annotation 参数白名单：``type -> {参数名: (类型标记, 取值范围)}``。
ANNOTATION_PARAMS: dict[str, dict[str, tuple[str, Any]]] = {
    "hline": {"y": ("number", None), "label": ("label", None)},
    "vline": {"x": ("xvalue", None), "label": ("label", None)},
    "text": {"x": ("xvalue", None), "y": ("number", None), "label": ("label", None)},
}

ANNOTATION_REQUIRED: dict[str, tuple[str, ...]] = {
    "hline": ("y",),
    "vline": ("x",),
    "text": ("x", "y", "label"),
}


# ---------------------------------------------------------------------------
# 结构化错误
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecIssue:
    """一条字段级校验问题。``field`` 使用 ``$.`` 根记法，便于模型定位重试。"""

    field: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.reason}


class ChartSpecError(ValueError):
    """ChartSpec 校验失败；``issues`` 是字段级问题列表。"""

    def __init__(self, issues: Sequence[SpecIssue | Mapping[str, str]]) -> None:
        self.issues: list[SpecIssue] = [
            item
            if isinstance(item, SpecIssue)
            else SpecIssue(str(item["field"]), str(item["reason"]))
            for item in issues
        ]
        detail = "；".join(f"{issue.field}: {issue.reason}" for issue in self.issues)
        super().__init__(f"ChartSpec 校验失败：{detail}")

    def to_dict(self) -> dict[str, Any]:
        """回传模型的结构化错误（字段 + 原因 + 修正提示）。"""
        return {
            "ok": False,
            "errors": [issue.to_dict() for issue in self.issues],
            "hint": (
                "按 field 修正后重试；字段与枚举白名单见 chart_spec_json_schema()，"
                "未知字段一律拒绝"
            ),
        }


class _Issues:
    """收集器：一次校验尽量收齐全部问题，减少模型往返。"""

    def __init__(self) -> None:
        self.items: list[SpecIssue] = []

    def add(self, field: str, reason: str) -> None:
        self.items.append(SpecIssue(field, reason))

    def require(self, mapping: Mapping[str, Any], key: str, path: str) -> bool:
        if key not in mapping:
            self.add(f"{path}.{key}", "缺少必填字段")
            return False
        return True

    def __bool__(self) -> bool:  # pragma: no cover - 语义辅助
        return bool(self.items)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_unknown_keys(
    mapping: Mapping[str, Any],
    allowed: Sequence[str],
    path: str,
    issues: _Issues,
    *,
    extra: Sequence[str] = (),
) -> None:
    known = set(allowed) | set(extra)
    for key in mapping:
        if key not in known:
            issues.add(
                f"{path}.{key}",
                f"未知字段（允许：{', '.join(list(allowed) + list(extra))}）",
            )


def _check_field_name(
    value: Any, path: str, issues: _Issues, *, what: str = "字段名"
) -> str | None:
    if not isinstance(value, str):
        issues.add(path, f"{what}必须是字符串，实际是 {_type_name(value)}")
        return None
    if not FIELD_RE.match(value):
        issues.add(path, f"{what} {value!r} 非法：需匹配 ^[a-z_][a-z0-9_]{{0,31}}$")
        return None
    return value


# ---------------------------------------------------------------------------
# 参数校验（transform / layer / annotation 共用）
# ---------------------------------------------------------------------------


def _check_range(
    value: Any, bounds: tuple[Any, Any] | None, path: str, issues: _Issues, kind: str
) -> Any | None:
    if kind == "int":
        ok = _is_int(value)
    else:
        ok = _is_number(value)
    if not ok:
        issues.add(path, f"必须是{'整数' if kind == 'int' else '数值'}，实际是 {_type_name(value)}")
        return None
    if bounds is None:  # 注解坐标等无界数值（§3.3：hline.y / text.y 只要求 number）
        return value
    low, high = bounds
    if value < low or value > high:
        issues.add(path, f"取值 {value} 越界：允许 {low}..{high}")
        return None
    return value


def _check_param(
    name: str, value: Any, marker: tuple[str, Any], path: str, issues: _Issues
) -> Any | None:
    kind, bounds = marker
    if kind == "field":
        return _check_field_name(value, path, issues)
    if kind == "enum":
        if not isinstance(value, str) or value not in bounds:
            issues.add(path, f"取值 {value!r} 不在枚举 {list(bounds)} 中")
            return None
        return value
    if kind == "int":
        return _check_range(value, bounds, path, issues, "int")
    if kind == "number":
        return _check_range(value, bounds, path, issues, "number")
    if kind == "label":
        if not isinstance(value, str) or not value.strip():
            issues.add(path, f"标注文字必须是非空字符串，实际是 {_type_name(value)}")
            return None
        if len(value) > 60:
            issues.add(path, f"标注文字长度 {len(value)} > 60")
            return None
        return value
    if kind == "xvalue":
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            issues.add(path, f"x 必须是字符串或数值，实际是 {_type_name(value)}")
            return None
        return value
    if kind == "bylist":
        return _check_bylist(value, path, issues)
    if kind == "metrics":
        return _check_metrics(value, path, issues)
    if kind == "value":
        return _check_filter_value(value, path, issues)
    if kind == "bool":
        if not isinstance(value, bool):
            issues.add(path, f"必须是布尔值，实际是 {_type_name(value)}")
            return None
        return value
    issues.add(path, f"内部错误：未知参数类型标记 {kind!r}")  # pragma: no cover
    return None


def _check_bylist(value: Any, path: str, issues: _Issues) -> list[str] | None:
    if isinstance(value, str):
        field = _check_field_name(value, path, issues)
        return [field] if field else None
    if not isinstance(value, (list, tuple)):
        issues.add(path, f"必须是字段名或字段名数组，实际是 {_type_name(value)}")
        return None
    if not 1 <= len(value) <= 3:
        issues.add(path, f"分组字段数 {len(value)} 越界：允许 1..3")
        return None
    fields = [_check_field_name(item, f"{path}[{i}]", issues) for i, item in enumerate(value)]
    return [f for f in fields if f] if all(fields) else None


def _check_metrics(value: Any, path: str, issues: _Issues) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        issues.add(path, f"metrics 必须是映射 {{字段: 聚合}}，实际是 {_type_name(value)}")
        return None
    if not value:
        issues.add(path, "metrics 不能为空")
        return None
    if len(value) > 8:
        issues.add(path, f"metrics 项数 {len(value)} > 8")
        return None
    out: dict[str, str] = {}
    for field, func in value.items():
        field_path = f"{path}.{field}"
        name = _check_field_name(field, field_path, issues, what="聚合字段名")
        if not isinstance(func, str) or func not in METRIC_FUNCS:
            issues.add(field_path, f"聚合算子 {func!r} 不在白名单 {list(METRIC_FUNCS)} 中")
            continue
        if name:
            out[name] = func
    return out if len(out) == len(value) else None


def _check_filter_value(value: Any, path: str, issues: _Issues) -> Any | None:
    if isinstance(value, (list, tuple)):
        if not value:
            issues.add(path, "value 为数组时不能为空")
            return None
        for i, item in enumerate(value):
            if not (isinstance(item, (str, int, float)) and not isinstance(item, bool)):
                issues.add(f"{path}[{i}]", f"数组元素必须是字符串或数值，实际是 {_type_name(item)}")
                return None
        return list(value)
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return value
    issues.add(path, f"value 必须是数值/字符串/数组，实际是 {_type_name(value)}")
    return None


def _check_params(
    mapping: Mapping[str, Any],
    schema: Mapping[str, tuple[str, Any]],
    required: Sequence[str],
    path: str,
    issues: _Issues,
) -> dict[str, Any]:
    for key in required:
        if key not in mapping:
            issues.add(f"{path}.{key}", "缺少必填参数")
    for key, value in mapping.items():
        key_path = f"{path}.{key}"
        if key not in schema:
            issues.add(key_path, f"未知参数（允许：{', '.join(schema) or '无'}）")
            continue
        _check_param(key, value, schema[key], key_path, issues)
    # filter 的 cmp=in 与 value 形态必须一致
    if "cmp" in mapping and mapping.get("cmp") == "in":
        if "value" in mapping and not isinstance(mapping["value"], (list, tuple)):
            issues.add(f"{path}.value", "cmp=in 时 value 必须是数组")
    return dict(mapping)


# ---------------------------------------------------------------------------
# data / encode / transforms / layers / annotations
# ---------------------------------------------------------------------------


def _validate_data(data: Any, path: str, issues: _Issues) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        issues.add(path, f"必须是 object，实际是 {_type_name(data)}")
        return {}
    _check_unknown_keys(data, DATA_FIELDS, path, issues)
    source = data.get("source")
    if source not in SOURCES:
        issues.add(f"{path}.source", f"取值 {source!r} 不在枚举 {list(SOURCES)} 中")
        source = None

    ref = data.get("ref")
    rows = data.get("rows")
    fields = data.get("fields")
    if source == "market":
        if ref is None:
            issues.add(f"{path}.ref", "source=market 时 ref 必填")
        elif not isinstance(ref, str):
            issues.add(f"{path}.ref", f"必须是字符串，实际是 {_type_name(ref)}")
        elif not (STOCK_REF_RE.match(ref) or INDEX_REF_RE.match(ref)):
            issues.add(f"{path}.ref", f"{ref!r} 不是合法标的代码（如 000001.SZ / 000300.SH）")
        for key in ("rows", "fields"):
            if key in data:
                issues.add(f"{path}.{key}", f"source=market 时不允许 {key}（只对 inline 生效）")
    elif source == "artifact":
        if ref is None:
            issues.add(f"{path}.ref", "source=artifact 时 ref 必填")
        elif not isinstance(ref, str) or not ARTIFACT_REF_RE.match(ref):
            issues.add(f"{path}.ref", f"{ref!r} 不是合法 artifact_id（art_ + 16 位小写十六进制）")
        for key in ("rows", "fields"):
            if key in data:
                issues.add(f"{path}.{key}", f"source=artifact 时不允许 {key}（只对 inline 生效）")
    elif source == "inline":
        if "ref" in data:
            issues.add(f"{path}.ref", "source=inline 时不允许 ref")
        _validate_inline_rows(rows, fields, path, issues)

    period = data.get("period", "day")
    if period not in PERIODS:
        issues.add(f"{path}.period", f"取值 {period!r} 不在枚举 {list(PERIODS)} 中")
        period = "day"
    if period in MINUTE_PERIODS and source is not None and source != "market":
        issues.add(f"{path}.period", f"分钟周期 {period} 只允许 source=market")

    range_value = data.get("range", "1y")
    if not isinstance(range_value, str):
        issues.add(f"{path}.range", f"必须是字符串，实际是 {_type_name(range_value)}")
        range_value = "1y"
    elif range_value not in RANGES:
        match = RANGE_RE.match(range_value)
        if not match:
            issues.add(
                f"{path}.range",
                f"取值 {range_value!r} 不是合法窗口：允许 {list(RANGES)} 或 YYYY-MM-DD:YYYY-MM-DD",
            )
        else:
            start = date.fromisoformat(match.group(1))
            end = date.fromisoformat(match.group(2))
            if start > end:
                issues.add(f"{path}.range", f"起始日 {start} 晚于结束日 {end}")
            elif (end - start).days > MAX_RANGE_YEARS * 366:
                issues.add(f"{path}.range", f"窗口跨度超过 {MAX_RANGE_YEARS} 年")

    adj = data.get("adj", "qfq" if source == "market" else "none")
    if isinstance(adj, str) and adj in ADJ_ALIASES:
        adj = ADJ_ALIASES[adj]
    if adj not in ADJ_VALUES:
        issues.add(
            f"{path}.adj",
            f"取值 {adj!r} 不在枚举 {list(ADJ_VALUES)} 中（raw 是 none 的别名）",
        )
        adj = "none"

    normalized: dict[str, Any] = {"source": source or "market"}
    if ref is not None and source in ("market", "artifact"):
        normalized["ref"] = ref
    if source == "inline":
        normalized["rows"] = [dict(row) for row in rows] if isinstance(rows, list) else []
        normalized["fields"] = list(fields) if isinstance(fields, list) else []
    normalized["period"] = period
    normalized["range"] = range_value
    normalized["adj"] = adj
    return normalized


def _validate_inline_rows(
    rows: Any, fields: Any, path: str, issues: _Issues
) -> None:
    if not isinstance(fields, (list, tuple)) or not fields:
        issues.add(f"{path}.fields", "source=inline 时 fields 必填（1–20 个字段名）")
        field_list: list[str] = []
    else:
        field_list = list(fields)
        if len(field_list) > MAX_INLINE_FIELDS:
            issues.add(f"{path}.fields", f"字段数 {len(field_list)} 越界：允许 1..{MAX_INLINE_FIELDS}")
        for i, field in enumerate(field_list):
            _check_field_name(field, f"{path}.fields[{i}]", issues)
        if len(set(field_list)) != len(field_list):
            issues.add(f"{path}.fields", "字段名重复")
    if not isinstance(rows, (list, tuple)):
        issues.add(f"{path}.rows", f"source=inline 时 rows 必填（list[dict]），实际是 {_type_name(rows)}")
        return
    if len(rows) > MAX_INLINE_ROWS:
        issues.add(f"{path}.rows", f"行数 {len(rows)} 超过上限 {MAX_INLINE_ROWS}")
        return
    if not rows:
        issues.add(f"{path}.rows", "rows 不能为空")
        return
    for i, row in enumerate(rows):
        row_path = f"{path}.rows[{i}]"
        if not isinstance(row, Mapping):
            issues.add(row_path, f"每行必须是 object，实际是 {_type_name(row)}")
            continue
        for key in row:
            if field_list and key not in field_list:
                issues.add(f"{row_path}.{key}", "未知字段（必须在 data.fields 中声明）")
        for field in field_list:
            if field not in row:
                issues.add(f"{row_path}.{field}", "缺少该字段的值")
        for key, value in row.items():
            if not (
                isinstance(value, (str, int, float, bool)) or value is None
            ):
                issues.add(f"{row_path}.{key}", f"值必须是 JSON 标量，实际是 {_type_name(value)}")


def _validate_encode(encode: Any, kind: str | None, path: str, issues: _Issues) -> dict[str, Any]:
    if not isinstance(encode, Mapping):
        issues.add(path, f"必须是 object，实际是 {_type_name(encode)}")
        return {}
    _check_unknown_keys(encode, ENCODE_FIELDS, path, issues)

    x = None
    if "x" not in encode:
        issues.add(f"{path}.x", "缺少必填字段")
    else:
        x = _check_field_name(encode["x"], f"{path}.x", issues)

    y_raw = encode.get("y")
    y: list[str] | None = None
    if y_raw is not None:
        if isinstance(y_raw, str):
            field = _check_field_name(y_raw, f"{path}.y", issues)
            y = [field] if field else None
        elif isinstance(y_raw, (list, tuple)):
            if not 1 <= len(y_raw) <= MAX_Y_SERIES:
                issues.add(f"{path}.y", f"序列数 {len(y_raw)} 越界：允许 1..{MAX_Y_SERIES}")
            else:
                fields = [
                    _check_field_name(item, f"{path}.y[{i}]", issues)
                    for i, item in enumerate(y_raw)
                ]
                y = [f for f in fields if f] if all(fields) else None
        else:
            issues.add(f"{path}.y", f"必须是字段名或字段名数组，实际是 {_type_name(y_raw)}")

    color = None
    if "color" in encode:
        color = _check_field_name(encode["color"], f"{path}.color", issues)

    if kind == "candlestick":
        if y_raw is not None:
            issues.add(f"{path}.y", "candlestick 的 OHLC 来自数据列，不接受 encode.y")
        if color is not None:
            issues.add(f"{path}.color", "candlestick 不支持 encode.color")
        y = None
    elif kind == "heatmap":
        if y_raw is None:
            issues.add(f"{path}.y", "heatmap 需要 encode.y（行分类字段）")
        elif y is not None and len(y) != 1:
            issues.add(f"{path}.y", "heatmap 的 encode.y 只能是单个字段")
        if "color" not in encode:
            issues.add(f"{path}.color", "heatmap 需要 encode.color（数值列，作为色深）")
    elif kind is not None:
        if y_raw is None:
            issues.add(f"{path}.y", f"{kind} 需要 encode.y")
        color = color

    normalized: dict[str, Any] = {}
    if x:
        normalized["x"] = x
    if y:
        normalized["y"] = y[0] if len(y) == 1 else y
    if color:
        normalized["color"] = color
    return normalized


def _validate_transforms(value: Any, path: str, issues: _Issues) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        issues.add(path, f"必须是数组，实际是 {_type_name(value)}")
        return []
    if len(value) > MAX_TRANSFORMS:
        issues.add(path, f"transforms 数量 {len(value)} 超过上限 {MAX_TRANSFORMS}")
        return [dict(item) for item in value if isinstance(item, Mapping)]
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if not isinstance(item, Mapping):
            issues.add(item_path, f"每项必须是 object，实际是 {_type_name(item)}")
            continue
        op = item.get("op")
        if op not in TRANSFORM_OPS:
            issues.add(f"{item_path}.op", f"取值 {op!r} 不在白名单 {list(TRANSFORM_OPS)} 中")
            out.append(dict(item))
            continue
        _check_unknown_keys(item, ("op",) + tuple(TRANSFORM_PARAMS[op]), item_path, issues)
        params = {k: v for k, v in item.items() if k != "op"}
        _check_params(params, TRANSFORM_PARAMS[op], TRANSFORM_REQUIRED[op], item_path, issues)
        out.append({"op": op, **params})
    return out


def _validate_layers(
    value: Any, kind: str | None, path: str, issues: _Issues
) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        issues.add(path, f"必须是数组，实际是 {_type_name(value)}")
        return []
    if len(value) > MAX_LAYERS:
        issues.add(path, f"layers 数量 {len(value)} 超过上限 {MAX_LAYERS}")
        return [dict(item) for item in value if isinstance(item, Mapping)]
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if not isinstance(item, Mapping):
            issues.add(item_path, f"每项必须是 object，实际是 {_type_name(item)}")
            continue
        layer_type = item.get("type")
        if layer_type not in LAYER_TYPES:
            issues.add(f"{item_path}.type", f"取值 {layer_type!r} 不在白名单 {list(LAYER_TYPES)} 中")
            out.append(dict(item))
            continue
        if kind is not None and kind != "candlestick":
            issues.add(item_path, f"layers 只支持 candlestick，{kind} 不接受图层叠加")
            out.append(dict(item))
            continue
        _check_unknown_keys(item, ("type",) + tuple(LAYER_PARAMS[layer_type]), item_path, issues)
        raw_params = {k: v for k, v in item.items() if k != "type"}
        _check_params(raw_params, LAYER_PARAMS[layer_type], (), item_path, issues)
        merged = {**LAYER_DEFAULTS[layer_type], **raw_params}
        out.append({"type": layer_type, **merged})
    return out


def _validate_annotations(value: Any, path: str, issues: _Issues) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        issues.add(path, f"必须是数组，实际是 {_type_name(value)}")
        return []
    if len(value) > MAX_ANNOTATIONS:
        issues.add(path, f"annotations 数量 {len(value)} 超过上限 {MAX_ANNOTATIONS}")
        return [dict(item) for item in value if isinstance(item, Mapping)]
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if not isinstance(item, Mapping):
            issues.add(item_path, f"每项必须是 object，实际是 {_type_name(item)}")
            continue
        ann_type = item.get("type")
        if ann_type not in ANNOTATION_TYPES:
            issues.add(
                f"{item_path}.type", f"取值 {ann_type!r} 不在白名单 {list(ANNOTATION_TYPES)} 中"
            )
            out.append(dict(item))
            continue
        _check_unknown_keys(item, ("type",) + tuple(ANNOTATION_PARAMS[ann_type]), item_path, issues)
        params = {k: v for k, v in item.items() if k != "type"}
        _check_params(params, ANNOTATION_PARAMS[ann_type], ANNOTATION_REQUIRED[ann_type], item_path, issues)
        out.append({"type": ann_type, **params})
    return out


def _validate_title(value: Any, path: str, issues: _Issues) -> str:
    if not isinstance(value, str):
        issues.add(path, f"必须是字符串，实际是 {_type_name(value)}")
        return ""
    title = value.strip()
    if len(title) > MAX_TITLE_CHARS:
        issues.add(path, f"标题长度 {len(title)} > {MAX_TITLE_CHARS}")
    if "<" in title or ">" in title:
        issues.add(path, "标题必须是纯文本（禁止 HTML/脚本标签）")
    return title


def _validate_meta(value: Any, path: str, issues: _Issues) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        issues.add(path, f"必须是 object，实际是 {_type_name(value)}")
        return {}
    try:
        payload = json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        issues.add(path, f"必须可 JSON 序列化（{type(exc).__name__}: {exc}）")
        return {}
    size = len(payload.encode("utf-8"))
    if size > MAX_META_BYTES:
        issues.add(path, f"meta 序列化后 {size} 字节 > {MAX_META_BYTES}")
    return dict(value)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def validate_chart_spec(spec: Any) -> dict[str, Any]:
    """校验并归一化 ChartSpec；失败抛 :class:`ChartSpecError`（含全部字段级问题）。"""
    if not isinstance(spec, Mapping):
        raise ChartSpecError(
            [SpecIssue("$", f"spec 必须是 object，实际是 {_type_name(spec)}")]
        )
    issues = _Issues()

    raw = dict(spec)
    aliased: dict[str, Any] = {}
    for alias, canonical in FIELD_ALIASES.items():
        if alias in raw:
            if canonical in raw:
                issues.add(
                    f"$.{alias}",
                    f"{alias} 与 {canonical} 同时出现：请只使用冻结字段 {canonical}",
                )
            else:
                aliased[canonical] = raw[alias]
    _check_unknown_keys(raw, TOP_LEVEL_FIELDS, "$", issues, extra=tuple(FIELD_ALIASES))

    version = raw.get("version")
    if "version" not in raw:
        issues.add("$.version", "缺少必填字段（必须为 1）")
    elif not _is_int(version) or version != SPEC_VERSION:
        issues.add("$.version", f"必须等于 {SPEC_VERSION}，实际是 {version!r}")

    kind = raw.get("kind")
    if kind is None:
        issues.add("$.kind", "缺少必填字段")
        kind = None
    elif kind not in KINDS:
        issues.add("$.kind", f"取值 {kind!r} 不在枚举 {list(KINDS)} 中")
        kind = None

    title = _validate_title(raw["title"], "$.title", issues) if "title" in raw else ""

    if "data" not in raw:
        issues.add("$.data", "缺少必填字段")
        data_norm: dict[str, Any] = {}
        data_obj: Any = {}
    else:
        data_obj = raw["data"]
        data_norm = _validate_data(data_obj, "$.data", issues)

    if "encode" not in raw:
        issues.add("$.encode", "缺少必填字段")
    encode_norm = _validate_encode(raw.get("encode", {}), kind, "$.encode", issues)

    transforms_norm = _validate_transforms(raw.get("transforms", []), "$.transforms", issues)

    layers_raw = aliased["layers"] if "layers" in aliased else raw.get("layers", [])
    layers_norm = _validate_layers(layers_raw, kind, "$.layers", issues)

    annotations_norm = _validate_annotations(
        raw.get("annotations", []), "$.annotations", issues
    )

    style = aliased["theme"] if "theme" in aliased else raw.get("theme", "light")
    if isinstance(style, Mapping):
        if set(style) != {"theme"}:
            issues.add("$.style", f"style 只支持 {{\"theme\": ...}}，实际字段 {sorted(style)}")
        style = style.get("theme")
    if style is None:
        style = "light"
    if style not in THEMES:
        issues.add("$.theme", f"取值 {style!r} 不在枚举 {list(THEMES)} 中")
        style = "light"

    meta_norm = _validate_meta(raw.get("meta", {}), "$.meta", issues)

    if issues.items:
        raise ChartSpecError(issues.items)

    # 归一化：只保留冻结字段（别名已折叠），顺序稳定，供 artifact 元数据原样复现。
    return {
        "version": SPEC_VERSION,
        "kind": str(kind),
        "title": title,
        "data": data_norm,
        "encode": encode_norm,
        "transforms": transforms_norm,
        "layers": layers_norm,
        "theme": style,
        "annotations": annotations_norm,
        "meta": meta_norm,
    }


def normalize_chart_spec(spec: Any) -> dict[str, Any]:
    """``validate_chart_spec`` 的别名：校验并返回归一化 spec。"""
    return validate_chart_spec(spec)


def check_chart_spec(spec: Any) -> dict[str, Any]:
    """给模型用的容错入口：合法返回 ``{ok: True, spec}``，非法返回 ``{ok: False, errors, hint}``。"""
    try:
        return {"ok": True, "spec": validate_chart_spec(spec)}
    except ChartSpecError as exc:
        return exc.to_dict()


# ---------------------------------------------------------------------------
# 交给模型的 JSON Schema（字段与枚举白名单；细粒度规则仍由 validate_chart_spec 兜底）
# ---------------------------------------------------------------------------

_FIELD_SCHEMA = {"type": "string", "pattern": FIELD_RE.pattern}

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "kind", "data", "encode"],
    "properties": {
        "version": {"type": "integer", "enum": [SPEC_VERSION], "default": SPEC_VERSION},
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string", "maxLength": MAX_TITLE_CHARS},
        "data": {
            "type": "object",
            "additionalProperties": False,
            "required": ["source"],
            "properties": {
                "source": {"type": "string", "enum": list(SOURCES)},
                "ref": {"type": "string"},
                "rows": {"type": "array", "items": {"type": "object"}, "maxItems": MAX_INLINE_ROWS},
                "fields": {
                    "type": "array",
                    "items": _FIELD_SCHEMA,
                    "minItems": 1,
                    "maxItems": MAX_INLINE_FIELDS,
                },
                "period": {"type": "string", "enum": list(PERIODS), "default": "day"},
                "range": {"type": "string", "default": "1y"},
                "adj": {
                    "type": "string",
                    "enum": list(ADJ_VALUES) + sorted(ADJ_ALIASES),
                    "default": "qfq",
                },
            },
        },
        "encode": {
            "type": "object",
            "additionalProperties": False,
            "required": ["x"],
            "properties": {
                "x": _FIELD_SCHEMA,
                "y": {
                    "type": ["string", "array"],
                    "items": _FIELD_SCHEMA,
                    "minItems": 1,
                    "maxItems": MAX_Y_SERIES,
                },
                "color": _FIELD_SCHEMA,
            },
        },
        "transforms": {
            "type": "array",
            "maxItems": MAX_TRANSFORMS,
            "items": {
                "type": "object",
                "required": ["op"],
                "properties": {
                    "op": {"type": "string", "enum": list(TRANSFORM_OPS)},
                    "field": _FIELD_SCHEMA,
                    "cmp": {"type": "string", "enum": list(CMP_OPS)},
                    "value": {"type": ["number", "string", "array"]},
                    "by": {"type": ["string", "array"], "maxItems": 3},
                    "metrics": {"type": "object"},
                    "desc": {"type": "boolean"},
                    "n": {"type": "integer", "minimum": 1, "maximum": 5000},
                    "from": {"type": "string", "enum": ["head", "tail"]},
                },
            },
        },
        "layers": {
            "type": "array",
            "maxItems": MAX_LAYERS,
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": list(LAYER_TYPES)},
                    "window": {"type": "integer", "minimum": 2, "maximum": 250},
                    "k": {"type": "number", "minimum": 1, "maximum": 3},
                    "fast": {"type": "integer", "minimum": 8, "maximum": 20},
                    "slow": {"type": "integer", "minimum": 21, "maximum": 60},
                    "signal": {"type": "integer", "minimum": 5, "maximum": 15},
                    "n": {"type": "integer", "minimum": 5, "maximum": 90},
                    "m": {"type": "integer", "minimum": 3, "maximum": 3},
                },
            },
        },
        "annotations": {
            "type": "array",
            "maxItems": MAX_ANNOTATIONS,
            "items": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": list(ANNOTATION_TYPES)},
                    "x": {"type": ["string", "number"]},
                    "y": {"type": "number"},
                    "label": {"type": "string", "maxLength": 60},
                },
            },
        },
        "theme": {"type": "string", "enum": list(THEMES), "default": "light"},
        "meta": {"type": "object"},
    },
}


def chart_spec_json_schema() -> dict[str, Any]:
    """返回交给模型的 ChartSpec JSON Schema 副本（字段 + 枚举白名单）。"""
    return copy.deepcopy(_JSON_SCHEMA)
