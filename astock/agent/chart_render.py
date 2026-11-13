"""ChartSpec → Plotly Figure → 自包含 HTML / PNG artifact（ADR-016 §3、冻结清单 §3.4）。

职责边界：

- **只接受声明式 spec**：先过 :func:`astock.agent.chart_spec.validate_chart_spec`，
  再按 ``kind`` 分派到固定渲染函数；本模块没有 ``exec``/``eval``，也不拼接可执行代码，
  ``transforms`` 的五个算子全部用 pandas 显式实现（白名单之外的 op 在校验阶段就被拒绝）。
- **不重新发明计算**：MA/BOLL/MACD/RSI/KDJ 全部调用 :mod:`astock.indicators` 的既有实现；
  行情由 :mod:`astock.data` 取，``data_as_of``/``data_time`` 由 ``data.latest_bar_time()``
  统一产出（ADR-018 C1：工具与渲染器不得自行 ``strftime`` 拼时点）。
- **每个产物可回放**：HTML 按 sha256 内容寻址落盘，写入 ``agent_artifacts``，``meta_json``
  记录 ``{spec, params, data_as_of, data_time, source, code_version}`` 与数据快照，
  凭 ``artifact_id`` 可原样复现（ADR-016 C3、冻结清单 §5）。
- **降级不抛异常**：没有 kaleido 时只出 HTML，``png_path=None`` 且 ``degraded=True``，
  调用方通过 :func:`figure_tool_result` 把 ``degraded`` 写进 ``ToolResult.metadata``。
- **时点必须同屏**：图题角标与 ``ToolResult.text`` 同时展示 ``data_as_of`` 与 ``data_time``，
  缺任一写「时点未知」，禁止只写「实时」（ADR-018 §2）。

groupby/agg 的输出列名规则：``"{field}_{func}"``（如 ``amount_sum``），多字段时按
``by`` 列 + 指标列顺序排列；模型在 ``encode.y`` 里要用这些名字（同一条 spec 可复现）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from astock.agent.chart_spec import (
    ChartSpecError,
    SpecIssue,
    validate_chart_spec,
)
from astock.agent.store import (
    DEFAULT_ARTIFACT_ROOT,
    artifact_id_for,
    connect,
    ensure_run,
    get_artifact,
    put_content,
    record_artifact,
)
from astock.agent.types import ToolResult

__all__ = [
    "CODE_VERSION",
    "PRICE_DOWN",
    "PRICE_UP",
    "RenderedFigure",
    "apply_transforms",
    "figure_tool_result",
    "render",
]

#: 记录进 artifact 元数据的代码版本（冻结清单 §5 ``agent_runs.code_version`` 同源）。
CODE_VERSION = "astock-0.1.0"

#: 与 ``astock/charts.py`` 的 ``UP``/``DOWN`` 同值（A 股红涨绿跌）；测试里断言不漂移。
PRICE_UP = "#EF5350"
PRICE_DOWN = "#26A69A"
LINE_COLORS = ("#1F77B4", "#FF7F0E", "#2CA02C", "#9467BD")
GRID = "rgba(0,0,0,0.06)"

MAX_SNAPSHOT_ROWS = 5000
FIGURE_DIV_ID = "astock-chart"
MINUTE_FREQS = ("1min", "5min", "15min", "30min", "60min")
FREQ_ALIASES = {"day": "D", "week": "W", "month": "M"}

#: 数据方来源解析器：签名 ``(data_section) -> DataFrame``，供工具层注入自定义取数。
DataResolver = Callable[[Mapping[str, Any]], pd.DataFrame]


# ---------------------------------------------------------------------------
# 产物对象
# ---------------------------------------------------------------------------


@dataclass
class RenderedFigure:
    """渲染结果（冻结字段在前：``html``/``png_path``/``artifact_id``/``warnings``）。"""

    html: str
    png_path: str | None
    artifact_id: str
    warnings: list[str] = field(default_factory=list)
    path: str = ""
    digest: str = ""
    data_as_of: str = ""
    data_time: str = ""
    source: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    run_id: str = ""
    code_version: str = CODE_VERSION

    @property
    def caption(self) -> str:
        """图注：两个时点 + 价格口径（缺时点写「时点未知」）。"""
        return _caption(self.spec, self.data_as_of, self.data_time)

    def summary_text(self) -> str:
        """一句话摘要，写进 ``ToolResult.text``（与图题角标同口径）。"""
        title = self.spec.get("title") or _default_title(self.spec)
        parts = [f"{title}：{self.caption}"]
        if self.degraded:
            parts.append("PNG 导出降级：仅 HTML（degraded）")
        parts.append(f"artifact_id={self.artifact_id}")
        return "；".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "html": self.html,
            "png_path": self.png_path,
            "artifact_id": self.artifact_id,
            "warnings": list(self.warnings),
            "path": self.path,
            "digest": self.digest,
            "data_as_of": self.data_as_of,
            "data_time": self.data_time,
            "source": self.source,
            "params": dict(self.params),
            "degraded": self.degraded,
            "run_id": self.run_id,
            "code_version": self.code_version,
        }

    def metadata(self) -> dict[str, Any]:
        """写进 ``ToolResult.metadata`` 的固定键集（含 ``degraded`` 标注）。"""
        return {
            "artifact_id": self.artifact_id,
            "artifact_path": self.path,
            "artifact_digest": self.digest,
            "figure_params": dict(self.params),
            "data_as_of": self.data_as_of,
            "data_time": self.data_time,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
            "code_version": self.code_version,
        }


def figure_tool_result(
    rendered: RenderedFigure,
    *,
    text: str | None = None,
    table: list[dict[str, Any]] | Sequence[Mapping[str, Any]] | None = None,
    source: str | None = None,
) -> ToolResult:
    """把渲染结果包成 ``ToolResult``：``figure``=HTML、表与图同屏、metadata 标 ``degraded``。"""
    result = ToolResult(
        text=text or rendered.summary_text(),
        figure=rendered.html,
        table=[dict(row) for row in table] if table is not None else None,
        source=source or rendered.source,
        data_as_of=rendered.data_as_of,
        data_time=rendered.data_time,
        metadata=rendered.metadata(),
    )
    return result.trim_table()


# ---------------------------------------------------------------------------
# 数据解析
# ---------------------------------------------------------------------------


def _frame_from_inline(data: Mapping[str, Any]) -> pd.DataFrame:
    rows = data.get("rows") or []
    fields = list(data.get("fields") or [])
    frame = pd.DataFrame(rows, columns=fields)
    return frame


def _frame_from_snapshot(meta: Mapping[str, Any]) -> pd.DataFrame:
    snapshot = meta.get("data") or {}
    rows = snapshot.get("rows") or []
    fields = list(snapshot.get("fields") or [])
    return pd.DataFrame(rows, columns=fields)


def _range_window(range_value: str, *, end: date | None = None) -> tuple[date, date]:
    """冻结词表 → (start, end)：月档带 ``mo``、年档带 ``y``，不共用 ``1m``。"""
    today = end or date.today()
    if ":" in range_value:
        start_text, end_text = range_value.split(":", 1)
        return date.fromisoformat(start_text), date.fromisoformat(end_text)
    if range_value == "max":
        return date(1990, 1, 1), today
    if range_value == "ytd":
        return date(today.year, 1, 1), today
    months = {"1mo": 1, "3mo": 3, "6mo": 6}
    years = {"1y": 1, "3y": 3, "5y": 5}
    if range_value in months:
        return today - timedelta(days=months[range_value] * 31), today
    if range_value in years:
        return today - timedelta(days=years[range_value] * 366), today
    raise ValueError(f"未知 range：{range_value!r}")  # 校验阶段已拦截，防御性


def _load_market_frame(data: Mapping[str, Any]) -> pd.DataFrame:
    """按冻结词表取行情：日/周/月走 ``load_daily`` + ``period_frame``，分钟走 ``load_minute``。"""
    from astock import data as market  # 延迟导入：模块导入不触发 Tushare 客户端

    ref = str(data["ref"])
    period = str(data.get("period", "day"))
    # 冻结口径 → data 层参数：qfq/hfq 原样传，none（含 raw 别名）必须传 None，
    # 否则 data.load_* 会按非空字符串做前复权（评审 P0）。
    adj_key = str(data.get("adj", "qfq"))
    adj = {"qfq": "qfq", "hfq": "hfq", "none": None}.get(adj_key, adj_key)
    start, end = _range_window(str(data.get("range", "1y")))
    if period in MINUTE_FREQS:
        days = max(1, min(30, (end - start).days + 1))
        frame = market.load_minute(ref, freq=period, days=days, adj=adj)
    else:
        frame = market.load_daily(
            ref, start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"), adj=adj
        )
        if period in ("week", "month") and not frame.empty:
            rule = "W-FRI" if period == "week" else "ME"
            frame = market.resample_ohlcv(frame, rule)
    return frame


def _load_artifact_frame(
    data: Mapping[str, Any], conn: sqlite3.Connection
) -> pd.DataFrame:
    record = get_artifact(conn, str(data["ref"]))
    if record is None:
        raise ChartSpecError(
            [SpecIssue("$.data.ref", f"artifact {data['ref']} 不存在")]
        )
    if record.kind not in ("figure", "table"):
        raise ChartSpecError(
            [
                SpecIssue(
                    "$.data.ref",
                    f"artifact {data['ref']} 的 kind={record.kind}，不能作为图表数据源",
                )
            ]
        )
    frame = _frame_from_snapshot(record.meta)
    if frame.empty:
        raise ChartSpecError(
            [SpecIssue("$.data.ref", f"artifact {data['ref']} 没有数据快照（v1 只回放快照）")]
        )
    snapshot_as_of = str(record.meta.get("data_as_of") or "")
    if snapshot_as_of:
        frame.attrs["data_as_of"] = snapshot_as_of
        frame.attrs["data_time"] = str(record.meta.get("data_time") or "")
    return frame


def _resolve_frame(
    data: Mapping[str, Any],
    *,
    frame: pd.DataFrame | None,
    resolver: DataResolver | None,
    conn: sqlite3.Connection,
) -> tuple[pd.DataFrame, str, list[str]]:
    warnings: list[str] = []
    source = str(data.get("source"))
    if frame is not None:
        resolved = frame.copy()
        warnings.append("使用调用方注入的 DataFrame（source 仅作标注）")
    elif resolver is not None:
        resolved = resolver(data).copy()
    elif source == "inline":
        resolved = _frame_from_inline(data)
    elif source == "market":
        resolved = _load_market_frame(data)
    else:
        resolved = _load_artifact_frame(data, conn)
    return resolved, source, warnings


# ---------------------------------------------------------------------------
# transforms（白名单算子，pandas 显式实现；无 exec/eval）
# ---------------------------------------------------------------------------


def _require_column(frame: pd.DataFrame, column: Any, path: str) -> str:
    if not isinstance(column, str) or column not in frame.columns:
        raise ChartSpecError(
            [
                SpecIssue(
                    path,
                    f"数据里没有列 {column!r}（现有列：{list(frame.columns)}）",
                )
            ]
        )
    return column


def apply_transforms(
    frame: pd.DataFrame, transforms: Sequence[Mapping[str, Any]]
) -> pd.DataFrame:
    """按顺序应用 ``filter/groupby/agg/sort/limit``；其他 op 直接报错。"""
    out = frame.copy()
    for index, transform in enumerate(transforms):
        op = transform.get("op")
        path = f"$.transforms[{index}]"
        if op == "filter":
            out = _transform_filter(out, transform, path)
        elif op in ("groupby", "agg"):
            out = _transform_group(out, transform, path)
        elif op == "sort":
            out = _transform_sort(out, transform, path)
        elif op == "limit":
            out = _transform_limit(out, transform, path)
        else:  # 校验阶段已拦截，这里兜底
            raise ChartSpecError([SpecIssue(f"{path}.op", f"不支持的算子 {op!r}")])
    # pandas 的切片/排序不保证传播 attrs，而 artifact 回放要靠它做时点兜底。
    out.attrs = dict(frame.attrs)
    return out


def _transform_filter(
    frame: pd.DataFrame, transform: Mapping[str, Any], path: str
) -> pd.DataFrame:
    column = _require_column(frame, transform.get("field"), f"{path}.field")
    value = transform.get("value")
    cmp = transform.get("cmp")
    series = frame[column]
    try:
        if cmp == "gt":
            mask = series > value
        elif cmp == "ge":
            mask = series >= value
        elif cmp == "lt":
            mask = series < value
        elif cmp == "le":
            mask = series <= value
        elif cmp == "eq":
            mask = series == value
        elif cmp == "ne":
            mask = series != value
        elif cmp == "in":
            mask = series.isin(list(value) if isinstance(value, (list, tuple)) else [value])
        else:  # pragma: no cover - 校验已拦截
            raise ChartSpecError([SpecIssue(f"{path}.cmp", f"不支持的比较符 {cmp!r}")])
    except TypeError as exc:
        raise ChartSpecError(
            [SpecIssue(f"{path}.value", f"与列 {column!r} 的类型不兼容：{exc}")]
        ) from exc
    return frame[mask.fillna(False)].copy()


def _transform_group(
    frame: pd.DataFrame, transform: Mapping[str, Any], path: str
) -> pd.DataFrame:
    by = transform.get("by")
    by_list = [by] if isinstance(by, str) else list(by or [])
    for i, column in enumerate(by_list):
        _require_column(frame, column, f"{path}.by[{i}]")
    metrics = dict(transform.get("metrics") or {})
    if not metrics:
        raise ChartSpecError([SpecIssue(f"{path}.metrics", "metrics 不能为空")])
    spec: dict[str, tuple[str, str]] = {}
    for field, func in metrics.items():
        _require_column(frame, field, f"{path}.metrics.{field}")
        spec[f"{field}_{func}"] = (field, str(func))
    grouped = frame.groupby(by_list, dropna=False).agg(**spec).reset_index()
    return grouped


def _transform_sort(
    frame: pd.DataFrame, transform: Mapping[str, Any], path: str
) -> pd.DataFrame:
    column = _require_column(frame, transform.get("by"), f"{path}.by")
    return frame.sort_values(column, ascending=not bool(transform.get("desc", False)), kind="stable")


def _transform_limit(
    frame: pd.DataFrame, transform: Mapping[str, Any], path: str
) -> pd.DataFrame:
    n = int(transform.get("n", 0))
    if n <= 0:
        raise ChartSpecError([SpecIssue(f"{path}.n", "n 必须 ≥ 1")])
    if transform.get("from", "head") == "tail":
        return frame.tail(n).copy()
    return frame.head(n).copy()


# ---------------------------------------------------------------------------
# 指标图层（全部复用 astock.indicators）
# ---------------------------------------------------------------------------


def _indicator_frame(frame: pd.DataFrame, layers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """按 layers 计算覆盖列与副图指标，返回 ``{"overlays": {...}, "panes": [...]}``。"""
    from astock import indicators

    overlays: dict[str, pd.Series] = {}
    panes: list[dict[str, Any]] = []
    close = frame["close"] if "close" in frame.columns else None
    ma_windows: list[int] = []
    boll_specs: list[tuple[int, float]] = []
    for layer in layers:
        kind = layer["type"]
        if kind == "ma":
            window = int(layer.get("window", 20))
            if window not in ma_windows:
                ma_windows.append(window)
        elif kind == "boll":
            key = (int(layer.get("window", 20)), float(layer.get("k", 2.0)))
            if key not in boll_specs:
                boll_specs.append(key)
        elif kind in ("macd", "rsi", "kdj"):
            panes.append(dict(layer))
    if close is not None:
        for window in ma_windows:
            overlays[f"ma{window}"] = indicators.sma(close, window)
        for window, k in boll_specs:
            boll = indicators.boll(close, n=window, k=k)
            for column in boll.columns:
                overlays[column] = boll[column]
    return {"overlays": overlays, "panes": panes}


def _pane_series(
    frame: pd.DataFrame, pane: Mapping[str, Any]
) -> dict[str, pd.Series] | pd.DataFrame:
    from astock import indicators

    kind = pane["type"]
    close = frame["close"]
    if kind == "macd":
        return indicators.macd(
            close,
            fast=int(pane.get("fast", 12)),
            slow=int(pane.get("slow", 26)),
            signal=int(pane.get("signal", 9)),
        )
    if kind == "rsi":
        return {"rsi": indicators.rsi(close, n=int(pane.get("window", 14)))}
    if kind == "kdj":
        return indicators.kdj(
            frame,
            n=int(pane.get("n", 9)),
            k_period=int(pane.get("k", 3)),
            d_period=int(pane.get("m", 3)),
        )
    raise ChartSpecError(  # pragma: no cover - 校验已拦截
        [SpecIssue("$.layers", f"不支持的副图指标 {kind!r}")]
    )


# ---------------------------------------------------------------------------
# 图注 / 标题
# ---------------------------------------------------------------------------


def _adj_label(adj: str) -> str:
    if adj == "qfq":
        return "前复权（qfq）"
    if adj == "hfq":
        return "后复权（hfq）"
    if adj == "none":
        return "不复权（原始价格 raw，用于涨跌停/成交异常判断）"
    return f"口径未知（{adj}）"


def _caption(spec: Mapping[str, Any], data_as_of: str, data_time: str) -> str:
    as_of = data_as_of or "时点未知"
    bar = data_time or "时点未知"
    data = spec.get("data") or {}
    parts = [f"数据截止 {as_of}", f"最新 bar {bar}"]
    if data.get("source") == "market":
        parts.append(f"价格口径：{_adj_label(str(data.get('adj', 'qfq')))}")
        parts.append(f"周期：{data.get('period', 'day')}")
    else:
        parts.append(f"数据来源：{data.get('source', 'inline')}")
    return " · ".join(parts)


def _default_title(spec: Mapping[str, Any]) -> str:
    kind = spec.get("kind", "chart")
    labels = {
        "candlestick": "K 线图",
        "line": "折线图",
        "bar": "柱状图",
        "scatter": "散点图",
        "heatmap": "热力图",
        "histogram": "直方图",
    }
    data = spec.get("data") or {}
    ref = data.get("ref")
    name = labels.get(str(kind), "图表")
    return f"{ref} {name}" if ref else name


def _figure_title(spec: Mapping[str, Any], caption: str) -> dict[str, Any]:
    title = str(spec.get("title") or _default_title(spec))
    return {
        "text": f"{title}<br><sup>{caption}</sup>",
        "x": 0.005,
        "xanchor": "left",
        "y": 0.97,
        "yanchor": "top",
        "font": {"size": 15},
    }


# ---------------------------------------------------------------------------
# 时点（唯一生产者：astock.data.latest_bar_time）
# ---------------------------------------------------------------------------


def _period_freq(period: str) -> str:
    return FREQ_ALIASES.get(period, period)


def _time_points(
    frame: pd.DataFrame,
    *,
    period: str,
    data_as_of: str | None,
    data_time: str | None,
    warnings: list[str],
) -> tuple[str, str]:
    if data_as_of is not None or data_time is not None:
        # 显式传参优先；显式空串表示「时点未知」，不再回退猜测（图注写「时点未知」）
        return data_as_of or "", data_time or ""
    from astock import data as market

    # 先按实际渲染的数据重算（transforms 之后调用），artifacts 的旧 meta 只作兜底，
    # 避免把首次渲染的错误时点固化到回放结果里（评审 P1）。
    if not frame.empty:
        probe = frame if _has_bar_time_column(frame) else _probe_time_frame(frame)
        if probe is not None:
            try:
                return market.latest_bar_time(probe, freq=_period_freq(period))
            except ValueError as exc:
                warnings.append(f"无法确定时点：{exc}")
    snapshot_as_of = frame.attrs.get("data_as_of")
    if snapshot_as_of:
        warnings.append("artifact 快照缺少可用时间列，回退到快照记录的时点")
        return str(snapshot_as_of), str(frame.attrs.get("data_time") or "")
    if frame.empty:
        warnings.append("数据为空，时点未知（data_as_of/data_time 置空）")
    else:
        warnings.append("无法确定时点：数据缺少交易日/时间列（data_as_of/data_time 置空）")
    return "", ""


def _has_bar_time_column(frame: pd.DataFrame) -> bool:
    return "trade_date" in frame.columns or "trade_time" in frame.columns


def _probe_time_frame(frame: pd.DataFrame) -> pd.DataFrame | None:
    """inline 数据可能只带业务日期列（如 day），补一列 trade_date 交给唯一生产者。"""
    for column in frame.columns:
        series = frame[column]
        if pd.api.types.is_numeric_dtype(series.dtype):
            continue
        parsed = pd.to_datetime(series, errors="coerce")
        if len(parsed) and bool(parsed.notna().all()):
            probe = frame.copy()
            probe["trade_date"] = parsed
            return probe
    return None


def _intraday_rangebreaks() -> list[dict[str, Any]]:
    """分钟档隐藏隔夜/午休（与 charts.py 的 intraday 口径一致）。"""
    return [
        {"bounds": ["15:00", "09:30"], "pattern": "hour"},
        {"bounds": ["11:30", "13:00"], "pattern": "hour"},
    ]


# ---------------------------------------------------------------------------
# 各 kind 的渲染
# ---------------------------------------------------------------------------


def _prepare_x(frame: pd.DataFrame, x: str) -> pd.Series:
    series = frame[x]
    if series.dtype == object:
        parsed = pd.to_datetime(series, errors="coerce")
        if parsed.notna().all():
            return parsed
    return series


def _ensure_price_columns(frame: pd.DataFrame) -> None:
    missing = [c for c in ("open", "high", "low", "close") if c not in frame.columns]
    if missing:
        raise ChartSpecError(
            [
                SpecIssue(
                    "$.kind",
                    f"candlestick 需要 open/high/low/close 列，缺少 {missing}（现有列：{list(frame.columns)}）",
                )
            ]
        )


def _build_candlestick(
    frame: pd.DataFrame,
    spec: Mapping[str, Any],
    *,
    caption: str,
    period: str,
) -> go.Figure:
    _ensure_price_columns(frame)
    layers = list(spec.get("layers") or [])
    volume_layer = any(layer["type"] == "volume" for layer in layers)
    extras = _indicator_frame(frame, layers)
    pane_specs = extras["panes"]

    rows = 1 + (1 if volume_layer else 0) + len(pane_specs)
    heights = [3.2] + ([1.0] if volume_layer else []) + [1.3] * len(pane_specs)
    figure = make_subplots(
        rows=rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=heights,
    )

    x_field = str(spec["encode"]["x"])
    _require_column(frame, x_field, "$.encode.x")
    x_values = _prepare_x(frame, x_field)

    figure.add_trace(
        go.Candlestick(
            x=x_values,
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name="K 线",
            increasing_line_color=PRICE_UP,
            increasing_fillcolor=PRICE_UP,
            decreasing_line_color=PRICE_DOWN,
            decreasing_fillcolor=PRICE_DOWN,
            showlegend=False,
        ),
        row=1,
        col=1,
    )

    ma_styles = {
        5: ("MA5", "#FF8C00", 1.0),
        10: ("MA10", "#FFB300", 1.0),
        20: ("MA20", "#2E8B57", 1.2),
        60: ("MA60", "#8B008B", 1.2),
        120: ("MA120", "#1565C0", 1.3),
        250: ("MA250", "#C62828", 1.3),
    }
    boll_lower = extras["overlays"].get("boll_lower")
    boll_upper = extras["overlays"].get("boll_upper")
    band_fill = "rgba(96,125,139,0.08)"
    for column, series in extras["overlays"].items():
        if column.startswith("ma"):
            window = int(column[2:])
            label, color, width = ma_styles.get(window, (column.upper(), "#607D8B", 1.0))
            figure.add_trace(
                go.Scatter(
                    x=x_values,
                    y=series,
                    name=label,
                    mode="lines",
                    line={"color": color, "width": width},
                ),
                row=1,
                col=1,
            )
        elif column == "boll_mid":
            figure.add_trace(
                go.Scatter(
                    x=x_values,
                    y=series,
                    name="BOLL 中轨",
                    mode="lines",
                    line={"color": "#607D8B", "width": 1.1, "dash": "dash"},
                ),
                row=1,
                col=1,
            )
        elif column == "boll_lower":
            figure.add_trace(
                go.Scatter(
                    x=x_values,
                    y=series,
                    name="BOLL 下轨",
                    mode="lines",
                    line={"color": "#90A4AE", "width": 1.0, "dash": "dot"},
                ),
                row=1,
                col=1,
            )
            if boll_upper is not None:
                # 上轨填充到「上一根 trace」（下轨），构成 BOLL 通道
                figure.add_trace(
                    go.Scatter(
                        x=x_values,
                        y=boll_upper,
                        name="BOLL 上轨",
                        mode="lines",
                        line={"color": "#90A4AE", "width": 1.0, "dash": "dot"},
                        fill="tonexty",
                        fillcolor=band_fill,
                    ),
                    row=1,
                    col=1,
                )
        elif column == "boll_upper" and boll_lower is None:
            figure.add_trace(
                go.Scatter(
                    x=x_values,
                    y=series,
                    name="BOLL 上轨",
                    mode="lines",
                    line={"color": "#90A4AE", "width": 1.0, "dash": "dot"},
                ),
                row=1,
                col=1,
            )

    current_row = 2
    if volume_layer:
        volume_field = "vol" if "vol" in frame.columns else "volume"
        _require_column(frame, volume_field, "$.layers")
        colors = [
            PRICE_UP if close >= open_ else PRICE_DOWN
            for open_, close in zip(frame["open"], frame["close"])
        ]
        figure.add_trace(
            go.Bar(
                x=x_values,
                y=frame[volume_field],
                name="成交量",
                marker_color=colors,
                opacity=0.75,
                showlegend=False,
            ),
            row=current_row,
            col=1,
        )
        figure.update_yaxes(title_text="成交量", row=current_row, col=1)
        current_row += 1

    for pane in pane_specs:
        series = _pane_series(frame, pane)
        kind = pane["type"]
        if isinstance(series, pd.DataFrame):
            columns = list(series.columns)
        else:
            columns = list(series)
        for index, column in enumerate(columns):
            values = series[column]
            if kind == "macd" and column == "macd_hist":
                colors = [PRICE_UP if v >= 0 else PRICE_DOWN for v in values.fillna(0)]
                figure.add_trace(
                    go.Bar(
                        x=x_values,
                        y=values,
                        name="MACD 柱",
                        marker_color=colors,
                        opacity=0.7,
                        showlegend=False,
                    ),
                    row=current_row,
                    col=1,
                )
                continue
            label = {
                "dif": "DIF",
                "dea": "DEA",
                "rsi": "RSI",
                "k": "K",
                "d": "D",
                "j": "J",
            }.get(column, column.upper())
            figure.add_trace(
                go.Scatter(
                    x=x_values,
                    y=values,
                    name=label,
                    mode="lines",
                    line={"color": LINE_COLORS[index % len(LINE_COLORS)], "width": 1.1},
                    showlegend=False,
                ),
                row=current_row,
                col=1,
            )
        if kind == "rsi":
            for level in (30, 70):
                figure.add_hline(
                    y=level,
                    line_dash="dot",
                    line_color="#9E9E9E",
                    row=current_row,
                    col=1,
                )
        figure.update_yaxes(title_text=kind.upper(), row=current_row, col=1)
        current_row += 1

    _add_annotations(figure, frame, spec, x_values=x_values, row=1)
    if period in MINUTE_FREQS:
        figure.update_xaxes(rangebreaks=_intraday_rangebreaks())

    figure.update_layout(
        template=_template(spec),
        title=_figure_title(spec, caption),
        height=420 + 120 * (rows - 1),
        margin={"l": 52, "r": 18, "t": 78, "b": 36},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02, "x": 0},
        bargap=0.1,
    )
    figure.update_xaxes(showgrid=True, gridcolor=GRID, rangeslider_visible=False)
    figure.update_yaxes(showgrid=True, gridcolor=GRID)
    return figure


def _template(spec: Mapping[str, Any]) -> str:
    return "plotly_dark" if spec.get("theme") == "dark" else "plotly_white"


def _y_columns(spec: Mapping[str, Any]) -> list[str]:
    y = spec["encode"].get("y")
    if y is None:
        return []
    return [y] if isinstance(y, str) else list(y)


def _build_xy(figure_kind: str, frame: pd.DataFrame, spec: Mapping[str, Any], caption: str) -> go.Figure:
    x_field = _require_column(frame, spec["encode"].get("x"), "$.encode.x")
    x_values = _prepare_x(frame, x_field)
    y_columns = _y_columns(spec)
    if not y_columns:
        raise ChartSpecError([SpecIssue("$.encode.y", f"{figure_kind} 需要 encode.y")])
    figure = go.Figure()
    for index, column in enumerate(y_columns):
        _require_column(frame, column, "$.encode.y")
        values = frame[column]
        color = LINE_COLORS[index % len(LINE_COLORS)]
        if figure_kind == "bar":
            trace = go.Bar(x=x_values, y=values, name=column, marker_color=color, opacity=0.85)
        elif figure_kind == "scatter":
            color_field = spec["encode"].get("color")
            if color_field:
                _require_column(frame, color_field, "$.encode.color")
                trace = go.Scatter(
                    x=x_values,
                    y=values,
                    name=column,
                    mode="markers",
                    marker={
                        "size": 8,
                        "color": frame[color_field],
                        "colorscale": "Viridis",
                        "showscale": True,
                        "colorbar": {"title": str(color_field)},
                    },
                )
            else:
                trace = go.Scatter(
                    x=x_values, y=values, name=column, mode="markers", marker={"size": 7, "color": color}
                )
        else:
            trace = go.Scatter(
                x=x_values, y=values, name=column, mode="lines", line={"color": color, "width": 1.6}
            )
        figure.add_trace(trace)

    _add_annotations(figure, frame, spec, x_values=x_values, row=None)
    figure.update_layout(
        template=_template(spec),
        title=_figure_title(spec, caption),
        height=460,
        margin={"l": 56, "r": 20, "t": 78, "b": 40},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.02, "x": 0},
        bargap=0.15,
    )
    figure.update_xaxes(showgrid=True, gridcolor=GRID)
    figure.update_yaxes(showgrid=True, gridcolor=GRID)
    return figure


def _build_heatmap(frame: pd.DataFrame, spec: Mapping[str, Any], caption: str) -> go.Figure:
    encode = spec["encode"]
    x_field = _require_column(frame, encode.get("x"), "$.encode.x")
    y_field = _require_column(frame, encode.get("y"), "$.encode.y")
    value_field = _require_column(frame, encode.get("color"), "$.encode.color")
    pivot = frame.pivot_table(
        index=y_field, columns=x_field, values=value_field, aggfunc="mean"
    )
    figure = go.Figure(
        go.Heatmap(
            z=pivot.to_numpy(),
            x=[str(item) for item in pivot.columns],
            y=[str(item) for item in pivot.index],
            colorscale="RdYlGn",
            colorbar={"title": str(value_field)},
        )
    )
    figure.update_layout(
        template=_template(spec),
        title=_figure_title(spec, caption),
        height=460,
        margin={"l": 64, "r": 20, "t": 78, "b": 46},
    )
    return figure


def _build_histogram(frame: pd.DataFrame, spec: Mapping[str, Any], caption: str) -> go.Figure:
    y_columns = _y_columns(spec)
    if not y_columns:
        raise ChartSpecError([SpecIssue("$.encode.y", "histogram 需要 encode.y")])
    figure = go.Figure()
    for index, column in enumerate(y_columns):
        _require_column(frame, column, "$.encode.y")
        figure.add_trace(
            go.Histogram(
                x=frame[column],
                name=column,
                marker_color=LINE_COLORS[index % len(LINE_COLORS)],
                opacity=0.75,
            )
        )
    figure.update_layout(
        template=_template(spec),
        title=_figure_title(spec, caption),
        height=440,
        margin={"l": 56, "r": 20, "t": 78, "b": 42},
        barmode="overlay",
        legend={"orientation": "h", "y": 1.02, "x": 0},
    )
    return figure


def _build_figure(
    frame: pd.DataFrame, spec: Mapping[str, Any], *, caption: str, period: str
) -> go.Figure:
    kind = str(spec["kind"])
    if kind == "candlestick":
        return _build_candlestick(frame, spec, caption=caption, period=period)
    if kind == "heatmap":
        return _build_heatmap(frame, spec, caption)
    if kind == "histogram":
        return _build_histogram(frame, spec, caption)
    return _build_xy(kind, frame, spec, caption)


def _add_annotations(
    figure: go.Figure,
    frame: pd.DataFrame,
    spec: Mapping[str, Any],
    *,
    x_values: pd.Series,
    row: int | None,
) -> None:
    for annotation in spec.get("annotations") or []:
        kind = annotation["type"]
        label = annotation.get("label")
        kwargs: dict[str, Any] = {
            "line_dash": "dash",
            "line_color": "#E91E63",
            "line_width": 1.2,
        }
        if row is not None:
            kwargs["row"] = row
            kwargs["col"] = 1
        if kind == "hline":
            figure.add_hline(y=float(annotation["y"]), **kwargs)
        elif kind == "vline":
            value = annotation["x"]
            if isinstance(value, str):
                parsed = pd.to_datetime(value, errors="coerce")
                if parsed is not pd.NaT and not pd.isna(parsed):
                    value = parsed.to_pydatetime() if hasattr(parsed, "to_pydatetime") else parsed
            figure.add_vline(x=value, **kwargs)
        elif kind == "text":
            figure.add_annotation(
                x=annotation["x"],
                y=float(annotation["y"]),
                text=str(label or ""),
                showarrow=True,
                arrowhead=2,
                ax=0,
                ay=-28,
                font={"color": "#E91E63", "size": 11},
            )


# ---------------------------------------------------------------------------
# 快照与落盘
# ---------------------------------------------------------------------------


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value else value  # NaN → None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).isoformat()
    if hasattr(value, "item"):
        try:
            return _json_scalar(value.item())
        except (TypeError, ValueError):  # pragma: no cover - 防御
            return str(value)
    return str(value)


def _latest_position(frame: pd.DataFrame) -> int | None:
    """最新 bar 的位置（按时间列取最大值，不依赖行序）；无时间列返回 None。"""
    for column in ("trade_time", "trade_date"):
        if column not in frame.columns:
            continue
        stamps = pd.to_datetime(frame[column], errors="coerce")
        if stamps.notna().any():
            return int(stamps.to_numpy().argmax())
    return None


def data_snapshot(frame: pd.DataFrame, *, limit: int = MAX_SNAPSHOT_ROWS) -> dict[str, Any]:
    """生成可回放的数据快照（JSON 标量；超限取尾部并保证包含最新 bar）。

    截断时必须保留最新一根 bar：图注的 ``data_as_of`` 由它决定，
    否则 ``meta["data_as_of"]`` 会与 ``rows[-1]`` 指向不同交易日（评审 P1）。
    """
    total = int(len(frame))
    if total > limit and limit > 0:
        positions = list(range(total - limit, total))
        latest = _latest_position(frame)
        if latest is not None and latest not in positions:
            # 丢掉最早的一行，把最新行补到末尾（快照仍按时间升序可回放）。
            positions = positions[-(limit - 1) :] + [latest] if limit > 1 else [latest]
        selected = frame.iloc[positions]
    else:
        selected = frame
    fields = [str(column) for column in frame.columns]
    rows = [
        {str(key): _json_scalar(value) for key, value in record.items()}
        for record in selected.to_dict(orient="records")
    ]
    snapshot: dict[str, Any] = {
        "fields": fields,
        "rows": rows,
        "row_count": total,
        "truncated": total > len(rows),
    }
    return snapshot


def _params(spec: Mapping[str, Any]) -> dict[str, Any]:
    data = spec.get("data") or {}
    return {
        "kind": spec.get("kind"),
        "source": data.get("source"),
        "ref": data.get("ref"),
        "period": data.get("period"),
        "range": data.get("range"),
        "adj": data.get("adj"),
        "encode": dict(spec.get("encode") or {}),
        "transforms": [dict(item) for item in spec.get("transforms") or []],
        "layers": [dict(item) for item in spec.get("layers") or []],
        "annotations": [dict(item) for item in spec.get("annotations") or []],
        "theme": spec.get("theme"),
    }


def _try_png(figure: go.Figure, *, width: int, height: int) -> tuple[bytes | None, str | None]:
    """导出 PNG；kaleido 缺失时返回 ``(None, 原因)``，绝不抛异常。"""
    try:
        payload = figure.to_image(format="png", width=width, height=height, scale=1)
    except (ValueError, RuntimeError, ImportError, ModuleNotFoundError) as exc:
        return None, str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return bytes(payload), None


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def render(
    spec: Mapping[str, Any] | Any,
    *,
    frame: pd.DataFrame | None = None,
    resolver: DataResolver | None = None,
    conn: sqlite3.Connection | None = None,
    db_path: str | None = None,
    artifact_root: str | None = None,
    run_id: str | None = None,
    step: int | None = None,
    session_id: str = "chart",
    code_version: str | None = None,
    data_as_of: str | None = None,
    data_time: str | None = None,
    include_plotlyjs: str | bool = "inline",
    export_png: bool = True,
) -> RenderedFigure:
    """``spec`` → Plotly Figure → 自包含 HTML（+ 可选 PNG）→ ``agent_artifacts``。

    非法 spec 抛 :class:`astock.agent.chart_spec.ChartSpecError`（字段 + 原因），
    调用方把 ``exc.to_dict()`` 原样回传模型即可重试。
    """
    normalized = validate_chart_spec(spec)
    warnings: list[str] = []
    owned = conn is None
    connection = conn if conn is not None else connect(db_path)
    try:
        data = normalized["data"]
        period = str(data.get("period", "day"))
        frame_resolved, source, resolve_warnings = _resolve_frame(
            data, frame=frame, resolver=resolver, conn=connection
        )
        warnings.extend(resolve_warnings)

        transformed = apply_transforms(frame_resolved, normalized["transforms"])
        if transformed.empty:
            warnings.append("transforms 之后数据为空，图为空态")

        # 时点必须取自实际落图的 transformed 数据（快照也来自它）；显式参数仍优先。
        as_of, bar_time = _time_points(
            transformed,
            period=period,
            data_as_of=data_as_of,
            data_time=data_time,
            warnings=warnings,
        )

        caption = _caption(normalized, as_of, bar_time)
        figure = _build_figure(transformed, normalized, caption=caption, period=period)
        html = figure.to_html(
            full_html=True,
            include_plotlyjs=include_plotlyjs,
            config={"displaylogo": False, "responsive": True},
            # 固定 div id：plotly 默认随机 uuid 会让同一份 spec 每次产出不同字节，
            # 破坏内容寻址（artifact_id 复现 / digest 去重）。
            div_id=FIGURE_DIV_ID,
        )

        version = code_version or CODE_VERSION
        digest, rel_path, absolute = put_content(
            html,
            filename="chart.html",
            root=artifact_root,
        )
        artifact_id = artifact_id_for(digest, "chart.html")

        png_path: str | None = None
        degraded = False
        if export_png:
            payload, reason = _try_png(
                figure, width=int(figure.layout.width or 960), height=int(figure.layout.height or 520)
            )
            if payload is None:
                degraded = True
                warnings.append(f"PNG 导出降级：{reason}（仅 HTML artifact）")
            else:
                _, _, absolute_png = put_content(
                    payload, filename=f"chart_{artifact_id}.png", root=artifact_root
                )
                png_path = str(absolute_png)

        run_key = run_id or f"run_chart_{artifact_id}"
        ensure_run(
            connection,
            run_key,
            session_id=session_id,
            provider="local",
            model="chart_render",
            code_version=version,
            status="partial" if degraded else "success",
        )

        params = _params(normalized)
        snapshot = data_snapshot(transformed)
        meta = {
            "spec": normalized,
            "params": params,
            "data_as_of": as_of,
            "data_time": bar_time,
            "source": source,
            "code_version": version,
            "degraded": degraded,
            "warnings": list(warnings),
            "png_path": png_path,
            "data": snapshot,
        }
        artifact_id = record_artifact(
            connection,
            run_id=run_key,
            kind="figure",
            path=rel_path,
            digest=digest,
            meta=meta,
            step=step,
        )
    finally:
        if owned:
            connection.close()

    return RenderedFigure(
        html=html,
        png_path=png_path,
        artifact_id=artifact_id,
        warnings=warnings,
        path=rel_path,
        digest=digest,
        data_as_of=as_of,
        data_time=bar_time,
        source=source,
        spec=normalized,
        params=params,
        degraded=degraded,
        run_id=run_key,
        code_version=version,
    )


def artifact_file(rendered: RenderedFigure, *, root: str | None = None) -> str:
    """返回 HTML artifact 的绝对路径（测试与 UI 预览用）。"""
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    return str(base / rendered.path)


def spec_from_artifact(meta: Mapping[str, Any]) -> dict[str, Any]:
    """从 artifact 元数据取回原样 spec（回答引用 artifact_id 时可原样复现）。"""
    spec = meta.get("spec")
    if not isinstance(spec, Mapping):
        raise ChartSpecError([SpecIssue("$.data.ref", "artifact 元数据里没有 spec 快照")])
    return json.loads(json.dumps(dict(spec), ensure_ascii=False))
