"""ChartSpec → Plotly Figure → 自包含 HTML（+ PNG bytes）：**纯渲染，不碰审计库**。

职责边界（Q5，2026-11-14 裁决）：

- **只接受声明式 spec**：先过 :func:`astock.agent.chart_spec.validate_chart_spec`，
  再按 ``kind`` 分派到固定渲染函数；本模块没有 ``exec``/``eval``，也不拼接可执行代码，
  ``transforms`` 的算子全部用 pandas / :mod:`astock.stats` 显式实现。
- **不重新发明计算**：MA/BOLL/MACD/RSI/KDJ 全部调用 :mod:`astock.indicators`；
  分组聚合调用 :func:`astock.stats.aggregate_groups`（D11：图上均值与表同口径）；
  ``data_as_of``/``data_time`` 由 ``data.latest_bar_time()`` 统一产出（ADR-018 C1）。
- **不写库、不落盘**：本模块只返回 HTML / PNG bytes / 数据快照 / meta 草稿；
  artifact 身份、blob 写入与 ``record_artifact`` 全部由
  :mod:`astock.agent.chart_service` 负责（唯一审计入口）。因此这里**不能** import
  ``record_artifact``/``put_content``/``connect`` 等写路径，测试会静态扫描守住。
- **时点分两套**（Q1）：``data_as_of``/``data_time`` 是 transforms **之前**的行情时点，
  ``view_as_of``/``view_time`` 是图上的视图时点；图注同时展示（未知写「时点未知」，禁止猜）。
- **D10 不猜时间列**：只认 spec 显式声明的 ``data.time_field``（或 ``encode.x`` 指向的、
  值可证明为日期时间的列）；``code=20240104``、``label="2024-01-04 收盘"`` 一律不解析。
- **Q7 接受特殊时段 bar**：09:25 集合竞价、盘后固定价都能画（``session`` 标注在 meta），
  连续指标只用连续竞价 bar；盘前只有集合竞价数据时不回退前一日。

groupby/agg 的输出列名规则：``"{field}_{func}"``（如 ``amount_sum``），多字段时按
``by`` 列 + 指标列顺序排列；模型在 ``encode.y`` 里要用这些名字（同一条 spec 可复现）。
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from astock import __version__ as _PKG_VERSION
from astock import data as market
from astock import indicators, stats
from astock.agent.chart_spec import (
    ChartSpecError,
    SpecIssue,
    validate_chart_spec,
)
from astock.agent.store import (
    PNG_DEGRADED_WARNING_PREFIX,
    compute_env_fingerprint,
)
from astock.agent.types import ToolResult

__all__ = [
    "CODE_VERSION",
    "DEFAULT_FONT_FAMILY",
    "FIGURE_DIV_ID",
    "PRICE_DOWN",
    "PRICE_UP",
    "PNGExportError",
    "RenderedFigure",
    "apply_transforms",
    "embed_html",
    "figure_tool_result",
    "font_family",
    "render_figure",
    "reset_png_probe_cache",
]

#: 记录进 artifact 元数据的代码版本（``ASTOCK_CODE_VERSION`` 可覆盖，默认取包版本）。
#: 冻结清单 §5 ``agent_runs.code_version`` 同源；不写死常量，避免版本与代码脱节。
CODE_VERSION = os.getenv("ASTOCK_CODE_VERSION") or f"astock-{_PKG_VERSION}"

#: 与 ``astock/charts.py`` 的 ``UP``/``DOWN`` 同值（A 股红涨绿跌）；测试里断言不漂移。
PRICE_UP = "#EF5350"
PRICE_DOWN = "#26A69A"
LINE_COLORS = ("#1F77B4", "#FF7F0E", "#2CA02C", "#9467BD")
GRID = "rgba(0,0,0,0.06)"

MAX_SNAPSHOT_ROWS = 5000

#: 规范 HTML 的固定 div id（Q3）：artifact 字节必须稳定，所以**不**按图派生 id。
#: 同页多图/重复嵌入选 :func:`embed_html` 换成每个 DOM 实例唯一的 id。
FIGURE_DIV_ID = "astock-chart"
_DIV_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

#: artifact meta 的 schema 版本（§3.6）：没有该键的历史 meta 一律视为 legacy。
CHART_META_SCHEMA_VERSION = "chart_meta_v2"
MINUTE_FREQS = ("1min", "5min", "15min", "30min", "60min")
FREQ_ALIASES = {"day": "D", "week": "W", "month": "M"}

#: 中文字体栈（CSS ``font-family``，从左到右取第一个已安装字体）。
#: Linux CI 装 fonts-noto-cjk，Windows 开发机用雅黑/黑体；可用 ``ASTOCK_CHART_FONT`` 覆盖。
#: 只设 ``font.size`` 不设 family 时，浏览器会回退到无 CJK 字形的字体 → 中文变方块（评审 P1）。
DEFAULT_FONT_FAMILY = "Noto Sans CJK SC, Microsoft YaHei, SimHei, sans-serif"

#: PNG 导出模式：``off`` 不尝试、``auto`` 失败降级、``require`` 失败抛 :class:`PNGExportError`。
PNG_MODES = ("off", "auto", "require")

#: 运行期环境类失败（缺 kaleido / 找不到 Chrome / 超时）：``auto`` 模式缓存后不再逐图重试。
_PNG_ENV_FAILURE_HINTS = (
    "kaleido",
    "chrome",
    "chromium",
    "browser",
    "executable",
    "not found",
    "no such file",
    "timed out",
    "timeout",
    "cannot connect",
)

#: 进程级探测缓存：首次环境类失败的原因；``reset_png_probe_cache()`` 清空。
_PNG_PROBE_FAILURE: str | None = None


class PNGExportError(RuntimeError):
    """``png_mode="require"`` 下 PNG 导出失败（显式请求不得静默降级）。"""


def font_family() -> str:
    """返回图表字体栈；``ASTOCK_CHART_FONT`` 可覆盖（例如只装了思源黑体的环境）。"""
    return (os.getenv("ASTOCK_CHART_FONT") or "").strip() or DEFAULT_FONT_FAMILY


def reset_png_probe_cache() -> None:
    """清空 PNG 环境探测缓存（测试或运行期更换 Chrome/字体后调用）。"""
    global _PNG_PROBE_FAILURE
    _PNG_PROBE_FAILURE = None
    _chrome_identity.cache_clear()


def _is_env_failure(reason: str | None) -> bool:
    text = (reason or "").lower()
    return any(hint in text for hint in _PNG_ENV_FAILURE_HINTS)


def _kaleido_version() -> str:
    try:
        return _pkg_version("kaleido")
    except PackageNotFoundError:
        return "unavailable"


@lru_cache(maxsize=1)
def _chrome_identity() -> tuple[str | None, str]:
    """返回 ``(chrome_path, chrome_version)``，用于 PNG artifact 追溯。

    kaleido 0.2.x 自带浏览器 → ``("bundled", "bundled")``；1.x 由 choreographer 发现系统
    Chrome / CI 预装 Chrome。仓库禁子进程调用（架构门禁），故不执行 ``chrome --version``：
    Windows 安装路径里带版本目录（``...\\Chrome\\Application\\126.0.6478.127\\chrome.exe``）时
    解析出 ``126.0.6478.127``，否则记 ``unknown`` 并保留路径。
    """
    version = _kaleido_version()
    if version.startswith("0."):
        return "bundled", "bundled"
    if version == "unavailable":
        return None, "unavailable"
    path: str | None = None
    try:  # pragma: no cover - 依赖 kaleido 1.x 的私有布局
        from choreographer.browsers import Chromium

        path = Chromium.find_browser(skip_local=False)
    except Exception:  # noqa: BLE001 - 探测失败不能影响渲染
        path = None
    path = path or os.environ.get("BROWSER_PATH")
    # Linux 的 chrome 路径不带版本号；CI 用 ``google-chrome --version`` 显式传入。
    override = (os.environ.get("ASTOCK_CHROME_VERSION") or "").strip()
    if override:
        return str(path) if path else None, override
    if not path:
        return None, "unknown"
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", str(path))
    return str(path), match.group(1) if match else "unknown"


def embed_html(html: str, div_id: str) -> str:
    """把规范 HTML 的固定 div id 换成该 DOM 实例唯一的 id（Q3，2026-11-14 裁决）。

    规范 HTML 用固定 :data:`FIGURE_DIV_ID` 保证字节稳定（同 spec 同数据 → 同
    ``content_digest``/``artifact_id``）；同页放两张图、或同一 artifact 重复嵌入时，
    必须各自换 id，否则 Plotly 只会渲染最后一个 div。

    契约：

    - ``div_id`` 必须匹配 ``^[A-Za-z][A-Za-z0-9_-]{0,63}$``（Plotly/HTML id 约束）；
    - 替换**全部** ``astock-chart`` 出现处（div id 与脚本里的 ``getElementById``）；
    - 一处都没替换到时抛 ``ValueError``——说明传入的不是本渲染器的规范 HTML，
      静默返回会让两张图共享 DOM id（宁可报错）。
    """
    target = str(div_id or "").strip()
    if not _DIV_ID_RE.match(target):
        raise ValueError(f"非法 div_id {div_id!r}：必须匹配 ^[A-Za-z][A-Za-z0-9_-]{{0,63}}$")
    if target == FIGURE_DIV_ID:
        return html
    replaced = html.replace(FIGURE_DIV_ID, target)
    if replaced == html or html.count(FIGURE_DIV_ID) == 0:
        raise ValueError(
            f"HTML 里找不到 {FIGURE_DIV_ID!r}，不能换成 {target!r}（只有 chart_render 产出的规范 HTML 支持 embed_html）"
        )
    return replaced


def runtime_versions() -> dict[str, str]:
    """记录复现所需的运行环境版本（plotly/kaleido 输出字节随版本漂移）。

    PNG 追溯要求「字体 + 版本」可查（评审 P1）：HTML 复现看 plotly，PNG 复现看
    plotly + kaleido + Chrome + font.family，缺一项换环境就可能得到不同像素。
    """
    try:
        plotly_version = _pkg_version("plotly")
    except PackageNotFoundError:  # pragma: no cover - plotly 是硬依赖
        plotly_version = "unknown"
    chrome_path, chrome_version = _chrome_identity()
    return {
        "plotly_version": plotly_version,
        "python_version": platform.python_version(),
        "kaleido_version": _kaleido_version(),
        "chrome_version": chrome_version,
        "chrome_path": chrome_path or "",
        "font_family": font_family(),
    }


def env_fingerprint(code_version: str | None = None) -> str:
    """v2 artifact 环境身份：plotly + python + code_version（规格 §4.4）。"""
    versions = runtime_versions()
    return compute_env_fingerprint(
        plotly_version=str(versions.get("plotly_version") or ""),
        python_version=str(versions.get("python_version") or ""),
        code_version=code_version or CODE_VERSION,
    )


#: 数据方来源解析器：签名 ``(data_section) -> DataFrame``，供工具层注入自定义取数。
DataResolver = Callable[[Mapping[str, Any]], pd.DataFrame]


# ---------------------------------------------------------------------------
# 产物对象
# ---------------------------------------------------------------------------


@dataclass
class RenderedFigure:
    """渲染结果（冻结字段在前：``html``/``png_path``/``artifact_id``/``warnings``）。

    ``artifact_id``/``artifact_version``/``meta_digest``/``path``/``digest`` 由
    :mod:`astock.agent.chart_service` 在写入审计库后回填；纯渲染阶段为空/None。
    """

    html: str
    png_path: str | None
    artifact_id: str = ""
    warnings: list[str] = field(default_factory=list)
    path: str = ""
    digest: str = ""
    #: 行情时点（transforms **之前**，Q1）：``data_as_of`` 为交易日，``data_time`` 为 bar 时间。
    data_as_of: str = ""
    data_time: str = ""
    #: 视图时点（transforms **之后**，Q1）：聚合后没有时间列时为空，图注写「视图时点未知」。
    view_as_of: str = ""
    view_time: str = ""
    #: bar 时段（Q7）：``pre``/``continuous``/``post``/``closed``，日线为 ``close``。
    session: str = ""
    source: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    run_id: str | None = None
    code_version: str = CODE_VERSION
    artifact_version: int | None = None
    meta_digest: str | None = None
    #: 待写入/已写入的 v2 meta（``schema_version="chart_meta_v2"``）与可回放数据快照。
    meta: dict[str, Any] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    #: PNG 字节（纯渲染阶段只在内存里；落盘由 chart_service 负责）。
    png_bytes: bytes | None = None
    #: 旧版 artifact（meta 无 schema_version）：图注只能写「旧版记录时点（语义未区分）」。
    legacy: bool = False
    #: 视图时点为空的原因（§3.2 归因）：``aggregated``=transforms 丢了时间列，
    #: ``none``=数据声明 ``time_semantics=none``，空串=其他未知。
    view_unknown_reason: str = ""
    #: 渲染出的 plotly Figure（仅测试/调用方数值核对用，不落库、不进 meta）。
    figure: Any = field(default=None, repr=False, compare=False)

    @property
    def caption(self) -> str:
        """图注：行情时点 + 视图时点 + 口径；缺时点写「时点未知」，旧记录单独标注。"""
        return _caption(
            self.spec,
            self.data_as_of,
            self.data_time,
            self.view_as_of,
            self.view_time,
            legacy=self.legacy,
            session=self.session,
            warnings=self.warnings,
            view_unknown_reason=self.view_unknown_reason,
        )

    def summary_text(self) -> str:
        """一句话摘要，写进 ``ToolResult.text``（与图题角标同口径）。"""
        title = self.spec.get("title") or _default_title(self.spec)
        parts = [f"{title}：{self.caption}"]
        if self.degraded:
            parts.append("PNG 导出降级：仅 HTML（degraded）")
        if self.artifact_id:
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
            "view_as_of": self.view_as_of,
            "view_time": self.view_time,
            "session": self.session,
            "source": self.source,
            "params": dict(self.params),
            "degraded": self.degraded,
            "run_id": self.run_id,
            "code_version": self.code_version,
            "artifact_version": self.artifact_version,
            "meta_digest": self.meta_digest,
        }

    def metadata(self) -> dict[str, Any]:
        """写进 ``ToolResult.metadata`` 的固定键集（含 ``degraded`` 标注）。

        Q1：``data_as_of``/``data_time`` 保持行情时点（runtime/gateway 继续读它）；
        ``view_as_of``/``view_time`` 只做展示；``session`` 标注集合竞价/盘后（Q7）。
        """
        return {
            "artifact_id": self.artifact_id,
            "artifact_path": self.path,
            "artifact_digest": self.digest,
            "figure_params": dict(self.params),
            "data_as_of": self.data_as_of,
            "data_time": self.data_time,
            "view_as_of": self.view_as_of,
            "view_time": self.view_time,
            "session": self.session,
            "degraded": self.degraded,
            "warnings": list(self.warnings),
            "code_version": self.code_version,
            "artifact_version": self.artifact_version,
            "meta_digest": self.meta_digest,
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
    ref = str(data["ref"])
    period = str(data.get("period", "day"))
    # 冻结口径 → data 层参数：qfq/hfq 原样传，none 必须传 None，
    # 否则 data.load_* 会按非空字符串做前复权（评审 P0）。
    adj_key = str(data.get("adj", "qfq"))
    adj = {"qfq": "qfq", "hfq": "hfq", "none": None}.get(adj_key, adj_key)
    start, end = _range_window(str(data.get("range", "1y")))
    if period in MINUTE_FREQS:
        days = max(1, min(30, (end - start).days + 1))
        frame = market.load_minute(ref, freq=period, days=days, adj=adj)
    else:
        frame = market.load_daily(ref, start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"), adj=adj)
        if period in ("week", "month") and not frame.empty:
            rule = "W-FRI" if period == "week" else "ME"
            frame = market.resample_ohlcv(frame, rule)
    return frame


def _resolve_frame(
    data: Mapping[str, Any],
    *,
    frame: pd.DataFrame | None,
    resolver: DataResolver | None,
) -> tuple[pd.DataFrame, str, list[str]]:
    """取数：注入的 frame > 注入的 resolver > inline/market。

    ``data.source="artifact"`` 必须由调用方注入 resolver（:mod:`astock.agent.chart_service`
    用只读 ``ArtifactReader`` 实现）——渲染器不再直接读审计库（Q5）。
    """
    warnings: list[str] = []
    source = str(data.get("source"))
    if frame is not None:
        resolved = frame.copy()
        warnings.append("使用调用方注入的 DataFrame（source 仅作标注）")
    elif source == "artifact":
        if resolver is None:
            raise ChartSpecError(
                [
                    SpecIssue(
                        "$.data.ref",
                        "data.source=artifact 需要注入只读 resolver（chart_service.ArtifactReader），渲染器不直接读审计库",
                    )
                ]
            )
        resolved = resolver(data).copy()
    elif source == "inline":
        resolved = _frame_from_inline(data)
    elif source == "market":
        resolved = _load_market_frame(data)
    else:
        raise ChartSpecError(
            [SpecIssue("$.data.source", f"未知 source {source!r}")]
        )
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
    frame: pd.DataFrame,
    transforms: Sequence[Mapping[str, Any]],
    notes: list[str] | None = None,
) -> pd.DataFrame:
    """按顺序应用 ``filter/groupby/agg/sort/limit``；其他 op 直接报错。

    ``notes`` 非空时收集聚合层的显式告警（空组/NaN 组/输出改名，D11），
    由调用方写进图注与 artifact meta 的 ``warnings``。
    """
    out = frame.copy()
    for index, transform in enumerate(transforms):
        op = transform.get("op")
        path = f"$.transforms[{index}]"
        if op == "filter":
            out = _transform_filter(out, transform, path)
        elif op in ("groupby", "agg"):
            out = _transform_group(out, transform, path, notes)
        elif op == "sort":
            out = _transform_sort(out, transform, path)
        elif op == "limit":
            out = _transform_limit(out, transform, path)
        else:  # 校验阶段已拦截，这里兜底
            raise ChartSpecError([SpecIssue(f"{path}.op", f"不支持的算子 {op!r}")])
    # pandas 的切片/排序不保证传播 attrs，而 artifact 回放要靠它做时点兜底。
    out.attrs = dict(frame.attrs)
    return out


def _transform_filter(frame: pd.DataFrame, transform: Mapping[str, Any], path: str) -> pd.DataFrame:
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
        raise ChartSpecError([SpecIssue(f"{path}.value", f"与列 {column!r} 的类型不兼容：{exc}")]) from exc
    return frame[mask.fillna(False)].copy()


def _transform_group(
    frame: pd.DataFrame,
    transform: Mapping[str, Any],
    path: str,
    notes: list[str] | None = None,
) -> pd.DataFrame:
    by = transform.get("by")
    by_list = [by] if isinstance(by, str) else list(by or [])
    for i, column in enumerate(by_list):
        _require_column(frame, column, f"{path}.by[{i}]")
    metrics = dict(transform.get("metrics") or {})
    if not metrics:
        raise ChartSpecError([SpecIssue(f"{path}.metrics", "metrics 不能为空")])
    for column in metrics:
        _require_column(frame, column, f"{path}.metrics.{column}")
    # D11：聚合口径只在 astock.stats 里实现一次，图上均值必须与统计表一致。
    try:
        result = stats.aggregate_groups(frame, by=by_list, metrics=metrics)
    except ValueError as exc:
        raise ChartSpecError([SpecIssue(f"{path}.metrics", str(exc))]) from exc
    if notes is not None:
        notes.extend(result.warnings)
    return result.frame


def _transform_sort(frame: pd.DataFrame, transform: Mapping[str, Any], path: str) -> pd.DataFrame:
    column = _require_column(frame, transform.get("by"), f"{path}.by")
    return frame.sort_values(column, ascending=not bool(transform.get("desc", False)), kind="stable")


def _transform_limit(frame: pd.DataFrame, transform: Mapping[str, Any], path: str) -> pd.DataFrame:
    n = int(transform.get("n", 0))
    if n <= 0:
        raise ChartSpecError([SpecIssue(f"{path}.n", "n 必须 ≥ 1")])
    if transform.get("from", "head") == "tail":
        return frame.tail(n).copy()
    return frame.head(n).copy()


# ---------------------------------------------------------------------------
# 指标图层（全部复用 astock.indicators）
# ---------------------------------------------------------------------------


_INDICATOR_LAYER_TYPES = ("ma", "boll", "macd", "rsi", "kdj")


def _requests_indicators(layers: Sequence[Mapping[str, Any]]) -> bool:
    """layers 里是否请求了连续指标（MA/BOLL/MACD/RSI/KDJ）。

    用途：指标样本为空时必须显式写 warning，不能静默全 NaN（评审 P0）。
    """
    return any(str(layer.get("type")) in _INDICATOR_LAYER_TYPES for layer in layers)


def _indicator_frame(
    frame: pd.DataFrame,
    layers: Sequence[Mapping[str, Any]],
    *,
    source_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """按 layers 计算覆盖列与副图指标，返回 ``{"overlays": {...}, "panes": [...]}``。

    ``source_frame`` 是计算指标的样本（Q7：只含连续竞价 bar）。画图仍用 ``frame``
    （含 09:25/盘后 bar），指标按索引对齐回 ``frame``，非连续 bar 处为 NaN。
    """
    base = frame if source_frame is None else source_frame
    overlays: dict[str, pd.Series] = {}
    panes: list[dict[str, Any]] = []
    close = base["close"] if "close" in base.columns else None
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
            overlays[f"ma{window}"] = indicators.sma(close, window).reindex(frame.index)
        for window, k in boll_specs:
            boll = indicators.boll(close, n=window, k=k)
            for column in boll.columns:
                overlays[column] = boll[column].reindex(frame.index)
    return {"overlays": overlays, "panes": panes, "indicator_frame": base}


def _pane_series(
    frame: pd.DataFrame,
    pane: Mapping[str, Any],
    *,
    source_frame: pd.DataFrame | None = None,
) -> dict[str, pd.Series] | pd.DataFrame:
    """副图指标序列；``source_frame``（Q7：只含连续竞价 bar）用于计算，按索引对齐回图上。"""
    base = frame if source_frame is None else source_frame
    index = frame.index
    kind = pane["type"]
    close = base["close"]
    if kind == "macd":
        macd = indicators.macd(
            close,
            fast=int(pane.get("fast", 12)),
            slow=int(pane.get("slow", 26)),
            signal=int(pane.get("signal", 9)),
        )
        return {name: series.reindex(index) for name, series in macd.items()}
    if kind == "rsi":
        return {"rsi": indicators.rsi(close, n=int(pane.get("window", 14))).reindex(index)}
    if kind == "kdj":
        kdj = indicators.kdj(
            base,
            n=int(pane.get("n", 9)),
            k_period=int(pane.get("k", 3)),
            d_period=int(pane.get("m", 3)),
        )
        return {name: series.reindex(index) for name, series in kdj.items()}
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
        return "不复权（原始价格，用于涨跌停/成交异常判断）"
    return f"口径未知（{adj}）"


def _caption(
    spec: Mapping[str, Any],
    data_as_of: str,
    data_time: str,
    view_as_of: str = "",
    view_time: str = "",
    *,
    legacy: bool = False,
    session: str = "",
    warnings: Sequence[str] = (),
    view_unknown_reason: str = "",
) -> str:
    """图注：行情时点 + 视图时点（Q1），并标注竞价/盘后时段（Q7）。

    组合口径见变更说明 §3.2（``行情 X · 视图截止 Y`` / ``行情 X（视图一致）`` /
    ``视图时点未知（聚合后无时间列）`` / ``行情时点未知 · 视图 Y`` /
    旧版「旧版记录时点 X（语义未区分）」）。

    ``view_unknown_reason`` 只影响「视图为空」时的括号归因（评审 P2）：
    只有 transforms 真的丢了时间列才写「聚合后无时间列」，声明
    ``time_semantics=none`` 时写「声明 none」，其余不加括号原因。
    """
    data = spec.get("data") or {}
    period = str(data.get("period") or "day")
    market = data_time or data_as_of
    view = view_time or view_as_of
    parts: list[str] = []
    if legacy:
        parts.append(f"旧版记录时点 {market or '未知'}（语义未区分）")
    elif not market and not view:
        parts.append("行情时点未知 · 视图时点未知")
    elif not market:
        parts.append(f"行情时点未知 · 视图 {view}")
    elif not view:
        if view_unknown_reason == "aggregated":
            parts.append(f"行情 {market} · 视图时点未知（聚合后无时间列）")
        elif view_unknown_reason == "none":
            parts.append(f"行情 {market} · 视图时点未知（声明 time_semantics=none）")
        else:
            parts.append(f"行情 {market} · 视图时点未知")
    elif view_as_of and view_as_of != data_as_of:
        parts.append(f"行情 {market} · 视图截止 {view_as_of}")
    elif view == market:
        parts.append(f"行情 {market}（视图一致）")
    else:
        parts.append(f"行情 {market} · 视图 {view}")
    tag = {"pre": "集合竞价（09:15–09:29）", "post": "盘后固定价（15:00 后）"}.get(session)
    if tag and not legacy:
        parts.append(tag)
    if session == "pre" and period not in ("day", "week", "month") and not legacy:
        parts.append("盘前，暂无连续竞价数据")
    if data.get("source") == "market":
        parts.append(f"价格口径：{_adj_label(str(data.get('adj', 'qfq')))}")
        meta = spec.get("meta") or {}
        if isinstance(meta, Mapping) and meta.get("partial"):
            # ADR-008 第 7 条：盘中当日 bar 用最近已发布因子，图注必须说明。
            as_of = str(meta.get("factor_as_of") or "最近交易日")
            parts.append(f"盘中近似（因子截至 {as_of}）")
        parts.append(f"周期：{period}")
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


#: D10：可证明的时间字面量——只认 ``YYYY-MM-DD`` / ``YYYY-MM-DD HH:MM[:SS]``。
#: ``20240104``（字符串代码）、``20240104``（整数）、``2024-01-04 收盘``（文本标签）
#: 一律不猜，必须由 spec 显式声明 ``data.time_field``。
_TIME_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")


def _is_provable_time_column(frame: pd.DataFrame, column: str) -> bool:
    """整列都能证明是时间：datetime 类型，或全部是 ISO 日期/时间字面量（D10）。"""
    if column not in frame.columns:
        return False
    series = frame[column]
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return True
    values = [value for value in series.tolist() if not _is_missing(value)]
    if not values:
        return False
    return all(isinstance(value, str) and bool(_TIME_VALUE_RE.match(value.strip())) for value in values)


def _is_missing(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):  # pragma: no cover - 非标量
        return False


def resolve_time_column(
    frame: pd.DataFrame,
    data: Mapping[str, Any],
    *,
    encode: Mapping[str, Any] | None = None,
    path: str = "$.data",
) -> str | None:
    """确定时间列（D10 唯一入口）：**只接受可证明的时间列，绝不猜**。

    返回 ``None`` 表示 ``data.time_semantics="none"``（数据本身没有时间语义，
    图注只能写「视图时点未知」，且不得编造 ``data_as_of``）。

    拒绝的情形（抛 :class:`ChartSpecError`，不出图）：

    - ``data.time_field`` 指向不存在 / 不能证明是时间的列；
    - 未声明 ``time_field``，且 ``encode.x`` / ``trade_date`` / ``trade_time`` 里
      没有可证明的时间列（含 ``code="20240104"``、``label="2024-01-04 收盘"``）。
    """
    semantics = str(data.get("time_semantics") or "bar_time")
    declared = data.get("time_field")
    if declared:
        name = str(declared)
        if name not in frame.columns:
            raise ChartSpecError(
                [
                    SpecIssue(
                        f"{path}.time_field",
                        f"time_field={name!r} 不在数据列 {list(frame.columns)} 里",
                    )
                ]
            )
        if not _is_provable_time_column(frame, name):
            raise ChartSpecError(
                [
                    SpecIssue(
                        f"{path}.time_field",
                        f"列 {name!r} 不是可证明的时间列：只接受 datetime 或 "
                        "YYYY-MM-DD[ HH:MM[:SS]]，字符串代码/文本标签/纯数字不猜",
                    )
                ]
            )
        return name
    if semantics == "none":
        return None
    candidates: list[str] = []
    if encode:
        x = encode.get("x")
        if x:
            candidates.append(str(x))
    candidates.extend(["trade_time", "trade_date"])
    for candidate in candidates:
        if _is_provable_time_column(frame, candidate):
            return candidate
    raise ChartSpecError(
        [
            SpecIssue(
                f"{path}.time_field",
                "无法证明时间语义：未声明 data.time_field，且 encode.x/trade_date/"
                "trade_time 都不是可证明的时间列；请显式指定 time_field，"
                '或声明 data.time_semantics="none"',
            )
        ]
    )


def _period_freq(period: str) -> str:
    return FREQ_ALIASES.get(period, period)


def _time_probe(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    """把已确认的时间列复制成 ``trade_date``/``trade_time``，交给唯一生产者。"""
    probe = frame.copy()
    stamps = pd.to_datetime(probe[column], errors="coerce")
    probe["trade_date"] = stamps
    probe["trade_time"] = stamps
    return probe


def _bar_session(frame: pd.DataFrame, *, period: str, column: str | None) -> str:
    """最新一根 bar 的时段：分钟档按 :func:`astock.data.classify_bar_time`，日线 ``close``。"""
    if column is None or frame.empty:
        return ""
    key = _period_freq(period)
    stamps = pd.to_datetime(frame[column], errors="coerce").dropna()
    if stamps.empty:
        return ""
    return market.classify_bar_time(stamps.max(), key)


def _points_from_frame(
    frame: pd.DataFrame,
    *,
    period: str,
    column: str | None,
    as_of: str | None,
    bar_time: str | None,
    warnings: list[str],
    label: str,
    unknown_note: str,
) -> tuple[str, str]:
    """从一帧数据取 ``(as_of, time)``；显式参数优先，取不到就置空（不猜）。"""
    if as_of is not None or bar_time is not None:
        return as_of or "", bar_time or ""
    if column is None:
        return "", ""
    if frame.empty:
        warnings.append(f"{label}为空（transforms 过滤后无行），{unknown_note}")
        return "", ""
    try:
        return market.latest_bar_time(_time_probe(frame, column), freq=_period_freq(period), notes=warnings)
    except ValueError as exc:
        warnings.append(f"{label}：{exc}")
        return "", ""


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
    indicator_frame: pd.DataFrame | None = None,
) -> go.Figure:
    _ensure_price_columns(frame)
    layers = list(spec.get("layers") or [])
    volume_layer = any(layer["type"] == "volume" for layer in layers)
    extras = _indicator_frame(frame, layers, source_frame=indicator_frame)
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
            # 图例保留中文「K 线」条目：ADR-016 Q6=A 要求中文图例在 PNG 中可读，
            # 不设 showlegend=False，图例位置见下方 update_layout。
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
        colors = [PRICE_UP if close >= open_ else PRICE_DOWN for open_, close in zip(frame["open"], frame["close"])]
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
        series = _pane_series(frame, pane, source_frame=indicator_frame)
        kind = pane["type"]
        columns = list(series.columns) if isinstance(series, pd.DataFrame) else list(series)
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
        legend={"orientation": "h", "y": 1.02, "x": 1, "xanchor": "right"},
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
            trace = go.Scatter(x=x_values, y=values, name=column, mode="lines", line={"color": color, "width": 1.6})
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
    pivot = frame.pivot_table(index=y_field, columns=x_field, values=value_field, aggfunc="mean")
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
    frame: pd.DataFrame,
    spec: Mapping[str, Any],
    *,
    caption: str,
    period: str,
    indicator_frame: pd.DataFrame | None = None,
) -> go.Figure:
    kind = str(spec["kind"])
    if kind == "candlestick":
        figure = _build_candlestick(
            frame,
            spec,
            caption=caption,
            period=period,
            indicator_frame=indicator_frame,
        )
    elif kind == "heatmap":
        figure = _build_heatmap(frame, spec, caption)
    elif kind == "histogram":
        figure = _build_histogram(frame, spec, caption)
    else:
        figure = _build_xy(kind, frame, spec, caption)
    # 统一注入字体栈（标题/坐标轴/图例/annotation 都继承 layout.font）：
    # 中文必须命中 CJK 字体，否则浏览器回退成方块（评审 P1；只设 size 不够）。
    figure.update_layout(font={"family": font_family(), "size": 14})
    return figure


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
        return None if value != value else value  # noqa: PLR0124 - NaN 自比较是标准判定
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
    total = len(frame)
    if total > limit > 0:
        positions = list(range(total - limit, total))
        latest = _latest_position(frame)
        if latest is not None and latest not in positions:
            # 丢掉最早的一行，把最新行补到末尾（快照仍按时间升序可回放）。
            positions = [*positions[-(limit - 1) :], latest] if limit > 1 else [latest]
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


def _params(
    spec: Mapping[str, Any],
    *,
    code_version: str = CODE_VERSION,
    include_plotlyjs: str | bool = "inline",
) -> dict[str, Any]:
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
        # 复现前提：同 spec 同数据在不同 plotly 版本下 HTML 字节不同（D4/Q3）。
        "include_plotlyjs": include_plotlyjs,
        **runtime_versions(),
        "code_version": code_version,
    }


#: kaleido 1.5.1 + Chrome 首次渲染偶发的页面竞态（``ReferenceError: kaleido_scopes is not defined``）；
#: 重试一次即可恢复，属引擎噪音而非环境缺失，不能直接判 degraded（Windows/CI 均有观测）。
_TRANSIENT_PNG_ERROR_HINTS = ("kaleido_scopes is not defined", "timed out")


def _is_transient_png_error(reason: str) -> bool:
    lowered = reason.lower()
    return any(hint in lowered for hint in _TRANSIENT_PNG_ERROR_HINTS)


def _try_png(figure: go.Figure, *, width: int, height: int) -> tuple[bytes | None, str | None]:
    """导出 PNG；kaleido 缺失时返回 ``(None, 原因)``，绝不抛异常。

    kaleido 1.x 偶发首帧竞态（见 ``_TRANSIENT_PNG_ERROR_HINTS``）时重试一次：
    这类失败与「没装 kaleido / 没 Chrome」不同，重试成功就不该降级。
    """
    reason = ""
    for attempt in range(2):
        try:
            payload = figure.to_image(format="png", width=width, height=height, scale=1)
        except (ValueError, RuntimeError, ImportError, ModuleNotFoundError) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            if attempt == 0 and _is_transient_png_error(reason):
                continue
            return None, reason
        return bytes(payload), None
    return None, reason


def _remember_png_failure(reason: str | None) -> None:
    """``auto`` 模式记住环境类失败，避免每张图重复起 Chrome（评审 P2）。"""
    global _PNG_PROBE_FAILURE
    _PNG_PROBE_FAILURE = reason or "unknown"


def _png_meta(payload: bytes, *, width: int, height: int, elapsed_ms: int) -> dict[str, Any]:
    """PNG 的追溯元数据（落盘路径/digest 由 chart_service 写入后回填）。"""
    chrome_path, chrome_version = _chrome_identity()
    return {
        "png_bytes": len(payload),
        "png_width": width,
        "png_height": height,
        "png_scale": 1,
        "png_elapsed_ms": elapsed_ms,
        "font_family": font_family(),
        "kaleido_version": _kaleido_version(),
        "chrome_version": chrome_version,
        "chrome_path": chrome_path or "",
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def render_figure(
    spec: Mapping[str, Any] | Any,
    *,
    frame: pd.DataFrame | None = None,
    resolver: DataResolver | None = None,
    data_as_of: str | None = None,
    data_time: str | None = None,
    view_as_of: str | None = None,
    view_time: str | None = None,
    include_plotlyjs: str | bool = "inline",
    export_png: bool = True,
    png_mode: str | None = None,
    code_version: str | None = None,
) -> RenderedFigure:
    """``spec`` → Plotly Figure → 自包含 HTML（+ PNG 字节）；**纯渲染，不写审计库**（Q5）。

    审计落库（artifact/version/link/digest/版本号）由唯一入口
    :func:`astock.agent.chart_service.render_chart` 负责，本函数不 import 任何
    ``agent_store`` 写路径，因此也永远不会合成 ``run_chart_*`` 假 run（D9/§7）。

    时点（Q1）：``data_*`` 取自 transforms **之前**的行情帧（行情时点），
    ``view_*`` 取自 transforms **之后**的绘图帧（视图时点）；显式参数只覆盖自己那一组。

    时间语义（D10）：时间列必须由 ``data.time_field`` 显式声明，或 ``encode.x``/
    ``trade_date``/``trade_time`` 是**可证明**的时间列；否则抛
    :class:`astock.agent.chart_spec.ChartSpecError`，绝不猜第一个可解析列。

    ``png_mode`` 三态（ADR-016 Q6=A）：``auto``（默认，失败降级 ``degraded=True``）/
    ``require``（失败抛 :class:`PNGExportError`）/ ``off``。``export_png`` 是兼容别名。
    """
    mode = (png_mode or ("auto" if export_png else "off")).strip().lower()
    if mode not in PNG_MODES:
        raise ValueError(f"png_mode 必须是 {PNG_MODES} 之一，实际 {png_mode!r}")
    normalized = validate_chart_spec(spec)
    warnings: list[str] = []
    data = normalized["data"]
    period = str(data.get("period", "day"))
    freq = _period_freq(period)

    source_frame, source, resolve_warnings = _resolve_frame(data, frame=frame, resolver=resolver)
    warnings.extend(resolve_warnings)
    recorded_as_of = str(source_frame.attrs.get("data_as_of") or "")
    recorded_time = str(source_frame.attrs.get("data_time") or "")
    try:
        time_column = resolve_time_column(
            source_frame, data, encode=normalized.get("encode")
        )
    except ChartSpecError:
        # artifact 快照可能已被聚合（没有可证明的时间列）：行情时点只能采信记录值
        # （§3.5「data_* 信任 meta，不反推」）；inline/market 仍必须报契约错误。
        if not recorded_as_of:
            raise
        time_column = None
        warnings.append(
            "artifact 快照没有可证明的时间列：行情时点采信记录值（不回推 data_as_of）"
        )
    if time_column is None and not recorded_as_of:
        warnings.append("数据声明 time_semantics=none，行情时点未知（不编造 data_as_of）")

    as_of, bar_time = _points_from_frame(
        source_frame,
        period=period,
        column=time_column,
        as_of=data_as_of if data_as_of is not None else (recorded_as_of or None),
        bar_time=data_time if data_time is not None else (recorded_time or None),
        warnings=warnings,
        label="行情数据",
        unknown_note="行情时点未知",
    )
    transformed = apply_transforms(source_frame, normalized["transforms"], notes=warnings)
    if transformed.empty:
        warnings.append("transforms 之后数据为空，图为空态")
    view_column = time_column if time_column and time_column in transformed.columns else None
    # §3.2 归因：视图为空可能是「聚合丢了时间列」，也可能是数据本身声明了
    # time_semantics=none（评审 P2：不要把所有情况都写成「聚合后无时间列」）。
    time_semantics = str(data.get("time_semantics") or "")
    if view_column is not None:
        view_unknown_reason = ""
    elif time_column is None:
        view_unknown_reason = "none" if time_semantics == "none" else "unknown"
    elif normalized["transforms"]:
        view_unknown_reason = "aggregated"
        warnings.append("视图时点未知（聚合后无时间列）")
    else:
        view_unknown_reason = "unknown"
    v_as_of, v_time = _points_from_frame(
        transformed,
        period=period,
        column=view_column,
        as_of=view_as_of,
        bar_time=view_time,
        warnings=warnings,
        label="视图数据",
        unknown_note="视图时点未知",
    )
    session_frame = transformed if view_column else source_frame
    session = _bar_session(session_frame, period=period, column=view_column or time_column)
    if session == "pre":
        warnings.append("最新 bar 属集合竞价（session=pre）：K 线照常绘制，连续指标不含该 bar")
    elif session == "post":
        warnings.append("最新 bar 属盘后固定价（session=post）：K 线照常绘制，连续指标不含该 bar")

    caption = _caption(
        normalized,
        as_of,
        bar_time,
        v_as_of,
        v_time,
        session=session,
        warnings=warnings,
        view_unknown_reason=view_unknown_reason,
    )
    # Q7：分钟周期才有集合竞价/盘后固定价，连续指标只吃连续竞价 bar；
    # 日/周/月线每根都是收盘 bar（classify_bar_time → "close"），必须全量参与，
    # 否则 MA/BOLL/MACD/RSI/KDJ 会静默恒为 NaN（评审 P0）。
    if freq in MINUTE_FREQS:
        if view_column is None:
            indicator_frame = None
        else:
            probe = (
                transformed
                if view_column in ("trade_time", "trade_date")
                else _time_probe(transformed, view_column)
            )
            indicator_frame = market.continuous_bars(probe, freq=freq)
    else:
        indicator_frame = transformed
    if _requests_indicators(normalized["layers"]):
        if indicator_frame is None or indicator_frame.empty:
            warnings.append(
                "连续指标无可用 bar（集合竞价/盘后固定价不参与），MA/BOLL/MACD/RSI/KDJ 为空"
            )
        elif "close" in indicator_frame.columns and indicator_frame["close"].isna().all():
            warnings.append("连续指标样本的 close 全为空，MA/BOLL/MACD/RSI/KDJ 为空")
    figure = _build_figure(
        transformed,
        normalized,
        caption=caption,
        period=period,
        indicator_frame=indicator_frame,
    )
    version = code_version or CODE_VERSION
    # 规范 HTML 固定 div id（同 spec 同数据 → 字节稳定 → content_digest 可复现）；
    # 每张图/每次嵌入的唯一 id 由 embed_html 在展示时替换（Q3）。
    html = figure.to_html(
        full_html=True,
        include_plotlyjs=include_plotlyjs,
        config={"displaylogo": False, "responsive": True},
        div_id=FIGURE_DIV_ID,
    )
    snapshot = data_snapshot(transformed)

    width = int(figure.layout.width or 960)
    height = int(figure.layout.height or 520)
    png_bytes: bytes | None = None
    png_meta: dict[str, Any] = {
        "png_bytes": None,
        "png_width": None,
        "png_height": None,
        "png_scale": None,
        "png_elapsed_ms": None,
        "font_family": font_family(),
        "kaleido_version": _kaleido_version(),
        "chrome_version": _chrome_identity()[1],
        "chrome_path": _chrome_identity()[0] or "",
    }
    degraded = False
    png_error: str | None = None
    if mode == "require":
        started = time.perf_counter()
        payload, reason = _try_png(figure, width=width, height=height)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if payload is None:
            raise PNGExportError(f"PNG 导出失败（png_mode=require）：{reason or '未知原因'}")
        png_bytes = payload
        png_meta = _png_meta(payload, width=width, height=height, elapsed_ms=elapsed_ms)
    elif mode == "auto":
        if _PNG_PROBE_FAILURE:
            degraded = True
            png_error = _PNG_PROBE_FAILURE
            warnings.append(
                f"{PNG_DEGRADED_WARNING_PREFIX}：{_PNG_PROBE_FAILURE}"
                "（进程内已探测失败，跳过重试；png_mode='require' 可强制重试）"
            )
        else:
            started = time.perf_counter()
            payload, reason = _try_png(figure, width=width, height=height)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if payload is None:
                degraded = True
                png_error = reason
                warnings.append(f"{PNG_DEGRADED_WARNING_PREFIX}：{reason or '未知原因'}（仅 HTML artifact）")
                if _is_env_failure(reason):
                    _remember_png_failure(reason)
            else:
                png_bytes = payload
                png_meta = _png_meta(payload, width=width, height=height, elapsed_ms=elapsed_ms)

    params = _params(normalized, code_version=version, include_plotlyjs=include_plotlyjs)
    meta: dict[str, Any] = {
        "schema_version": CHART_META_SCHEMA_VERSION,
        "spec": normalized,
        "params": params,
        "data_as_of": as_of,
        "data_time": bar_time,
        "view_as_of": v_as_of,
        "view_time": v_time,
        "session": session,
        "source": source,
        "code_version": version,
        "degraded": degraded,
        "warnings": list(warnings),
        "png_mode": mode,
        "png_error": png_error,
        **png_meta,
        # meta 只留数据摘要；快照本身由 chart_service 落独立 blob（Q2）。
        "data_rows": int(snapshot.get("row_count", 0)),
        "data_truncated": bool(snapshot.get("truncated", False)),
    }
    return RenderedFigure(
        html=html,
        png_path=None,
        warnings=warnings,
        data_as_of=as_of,
        data_time=bar_time,
        view_as_of=v_as_of,
        view_time=v_time,
        session=session,
        source=source,
        spec=normalized,
        params=params,
        degraded=degraded,
        code_version=version,
        meta=meta,
        snapshot=snapshot,
        png_bytes=png_bytes,
        view_unknown_reason=view_unknown_reason,
        figure=figure,
    )


def spec_from_artifact(meta: Mapping[str, Any]) -> dict[str, Any]:
    """从 artifact 元数据取回原样 spec（回答引用 artifact_id 时可原样复现）。"""
    spec = meta.get("spec")
    if not isinstance(spec, Mapping):
        raise ChartSpecError([SpecIssue("$.data.ref", "artifact 元数据里没有 spec 快照")])
    return json.loads(json.dumps(dict(spec), ensure_ascii=False))
