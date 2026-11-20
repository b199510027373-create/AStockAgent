"""行情与指标工具（plan-20261107-006，ADR-016 / ADR-018）。

三个只读工具（Agent 白名单）：

- ``quote``：最新价 / 涨跌幅 / 成交额，盘中优先用 ``rt_min`` 的最新 1 分钟
  bar，盘后 / 非交易日退回最近交易日收盘；
- ``kline``：日 / 周 / 月 + 1/5/15/30/60 分钟 K 线表，可叠加 MA/BOLL/成交量
  等指标列，并可出图（图题写明 freshness 提示）；
- ``indicator``：MA/EMA/RSI/MACD/BOLL/KDJ/ATR 等，全部复用
  :mod:`astock.indicators` 的向量化实现——同一序列必须与
  ``add_all`` / 单指标函数逐值一致。

时点契约（ADR-018 C1/C2，唯一生产者）：

- ``data_as_of`` / ``data_time`` 只由 :func:`astock.data.latest_bar_time` 生产，
  本模块不拼接任何时间字符串；
- 每个成功结果都同时带 ``data_as_of``（``YYYY-MM-DD``）与 ``data_time``
  （``YYYY-MM-DD HH:MM``），缺一即视为契约失败；
- ``metadata`` 固定 ``session``（``pre/open/lunch/closed/non_trading_day``）与
  ``freshness``（``live/delayed/eod/suspended``）两键，UI 与图题只按这两键
  渲染提示。

freshness 判定（ADR-018 §4，冻结口径）：

- 非交易时段（含盘前 / 午休 / 收盘后 / 非交易日）一律 ``eod``，回答写明
  「最近交易日收盘」，不把昨日收盘当实时；
- 交易时段内最后一根 bar 超过 ``MINUTE_STALE_SECONDS``（2 分钟）未更新，
  一律 ``delayed``：写明延迟秒数并退回 ``stk_mins`` / 日线；
- ``rt_min`` 单次响应超过 ``MINUTE_LATENCY_SECONDS``（2 秒）同样降级为
  ``delayed`` 并在 notes 写明耗时；
- 最新交易日成交量为 0（停牌 / 无成交）一律 ``suspended``，回答写明
  「停牌/无成交」，指标仍按最后一根 bar 计算。

非目标（ADR-018 §6）：tick / 逐笔 / Level-2 是 v1 非目标，本模块不预留
任何伪实时字段（没有 bid/ask、没有秒级时间戳、没有 WebSocket）。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time as clock_time
from functools import partial
from typing import Any

import pandas as pd

from astock import data as market
from astock import indicators as ta
from astock.agent.chart_spec import (
    ADJ_VALUES,
    INDEX_REF_RE,
    MAX_LAYERS,
    MAX_RANGE_YEARS,
    MINUTE_PERIODS,
    PERIODS,
    STOCK_REF_RE,
    ChartSpecError,
)
from astock.agent.registry import Permission, ToolRegistry, ToolSpec
from astock.agent.types import MAX_TEXT_CHARS, ToolResult
from astock.tushare_client import TushareError

__all__ = [
    "DEFAULT_INDICATOR_LIMIT",
    "DEFAULT_KLINE_LIMIT",
    "FRESHNESS",
    "MAX_LIMIT",
    "MINUTE_LATENCY_SECONDS",
    "MINUTE_STALE_SECONDS",
    "SESSIONS",
    "MarketDeps",
    "MarketSeries",
    "build_market_registry",
    "indicator",
    "kline",
    "load_series",
    "quote",
    "register_market_tools",
    "trading_session",
]

# ---------------------------------------------------------------------------
# 冻结常量（与 docs/AStockAgent_interface_freeze_v1.md §6、ADR-018 C2 对齐）
# ---------------------------------------------------------------------------

#: rt_min 超过该秒数未更新即判 ``delayed``（ADR-018 §4 的「2 分钟」）。
MINUTE_STALE_SECONDS = 120.0

#: rt_min 单次响应超过该秒数（2 秒预算）即按 ``delayed`` 降级（ADR-018 §4）。
MINUTE_LATENCY_SECONDS = 2.0

#: 元数据 ``session`` 的冻结取值（ADR-018 C2）。
SESSIONS = ("pre", "open", "lunch", "closed", "non_trading_day")

#: 元数据 ``freshness`` 的冻结取值（ADR-018 C2）。
FRESHNESS = ("live", "delayed", "eod", "suspended")

#: 工具一次最多返回的 bar 行数（与 ``MAX_TABLE_ROWS`` 对齐，避免二次截断）。
MAX_LIMIT = 200
DEFAULT_KLINE_LIMIT = 120
DEFAULT_INDICATOR_LIMIT = 30

#: 指标预热：MA250 / EMA26 / 长周期指标需要更长的历史才能与
#: :mod:`astock.indicators` 逐值一致（工具只展示最近 ``limit`` 根）。
INDICATOR_WARMUP_BARS = 260

#: ``quote`` 的日线回看根数：停牌股要能取到「最后一根 bar」才判得出 suspended
#: （ADR-018 §4），窗口太短会让长停牌标的直接报「无行情数据」。
QUOTE_LOOKBACK_BARS = 60

#: 冻结周期 → ``latest_bar_time(freq=...)`` 的取值。
PERIOD_FREQ = {
    "day": "D",
    "week": "W",
    "month": "M",
    **{period: period for period in MINUTE_PERIODS},
}

#: 一个交易日的分钟 bar 数（1 分钟 240 根 = 4 小时），用于换算取数天数。
BARS_PER_DAY = {"1min": 240, "5min": 48, "15min": 16, "30min": 8, "60min": 4}

#: 周期 → 日历天数倍率（多取一些余量，长假不会把窗口切空）。
LOOKBACK_FACTOR = {"day": 2, "week": 10, "month": 45}

PERIOD_LABELS = {
    "day": "日线",
    "week": "周线",
    "month": "月线",
    "1min": "1 分钟",
    "5min": "5 分钟",
    "15min": "15 分钟",
    "30min": "30 分钟",
    "60min": "60 分钟",
}

_ADJ_LABELS = {"qfq": "前复权（qfq）", "hfq": "后复权（hfq）", "none": "不复权"}

#: 指标名文法：``ma20`` / ``ema12`` / ``rsi14`` / ``boll20`` / ``atr14``，
#: 以及无窗口的 ``macd`` / ``kdj`` / ``obv`` / ``cci`` / ``vr`` / ``psy``。
_INDICATOR_RE = re.compile(r"^(ma|ema|rsi|boll|atr)(\d{1,3})?$")
_BARE_INDICATORS = ("macd", "kdj", "obv", "cci", "vr", "psy")

#: K 线叠加层文法：指标名 + 可选 ``@day``（把日线指标 ffill 到分钟图，
#: 只用更早已完成日线，禁止未来值），外加 ``volume`` 成交量图层。
_LAYER_RE = re.compile(r"^(ma|ema|rsi|boll|atr|macd|kdj|volume)(\d{1,3})?(?:@(day))?$")

#: 指标默认集合：覆盖验收要求的 MA/EMA/RSI/MACD/BOLL/KDJ/ATR。
DEFAULT_INDICATORS = (
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ema12",
    "ema26",
    "rsi14",
    "macd",
    "boll",
    "kdj",
    "atr14",
)


# ---------------------------------------------------------------------------
# 依赖注入（测试用固定时钟 / 固定行情帧，生产用真实 data 层）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketDeps:
    """行情工具的外部依赖；默认全部指向 :mod:`astock.data` 的真实实现。"""

    daily: Callable[..., pd.DataFrame] = market.load_daily
    index_daily: Callable[..., pd.DataFrame] = market.load_index_daily
    minute: Callable[..., pd.DataFrame] = market.load_minute
    resample: Callable[..., pd.DataFrame] = market.resample_ohlcv
    bar_time: Callable[..., tuple[str, str]] = market.latest_bar_time
    trade_cal: Callable[..., pd.DataFrame] = market.load_trade_cal
    clock: Callable[[], Any] = pd.Timestamp.now
    minute_stale_seconds: float = MINUTE_STALE_SECONDS
    minute_latency_seconds: float = MINUTE_LATENCY_SECONDS


DEFAULT_DEPS = MarketDeps()


@dataclass
class MarketSeries:
    """一次行情取数的结果：帧 + 时点 + 会话 / 新鲜度标注。"""

    code: str
    period: str
    requested_period: str
    source: str
    frame: pd.DataFrame
    session: str
    freshness: str
    data_as_of: str
    data_time: str
    adj: str
    suspended: bool
    notes: list[str] = field(default_factory=list)
    #: 分钟盘中当日 bar 用最近已发布因子复权（ADR-008 补充裁决第 7 条）时为 True，
    #: 此时 ``factor_as_of`` 给出该因子的实际交易日，图注/表注据此标「盘中近似」。
    partial: bool = False
    factor_as_of: str | None = None

    def metadata(self) -> dict[str, Any]:
        """写进 ``ToolResult.metadata`` 的固定键集（C2 两键必须存在）。"""
        return {
            "session": self.session,
            "freshness": self.freshness,
            "adjustment": self.adj,
            "adjustment_label": _ADJ_LABELS.get(self.adj, self.adj),
            "source": self.source,
            "period": self.period,
            "requested_period": self.requested_period,
            "suspended": self.suspended,
            "notes": list(self.notes),
            "partial": self.partial,
            "factor_as_of": self.factor_as_of,
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _now(now: Any, deps: MarketDeps) -> pd.Timestamp:
    return pd.Timestamp(deps.clock() if now is None else now)


def _normalize_code(value: str) -> str:
    code = str(value or "").strip().upper()
    if STOCK_REF_RE.match(code) or INDEX_REF_RE.match(code):
        return code
    raise ValueError(
        f"ts_code 必须是 000001.SZ / 600519.SH / 000300.SH 形式，实际 {value!r}"
    )


def _validate_adj(adj: str) -> str:
    key = str(adj or "").strip().lower()
    if key not in ADJ_VALUES:
        raise ValueError(f"adj 必须是 {list(ADJ_VALUES)} 之一，实际 {adj!r}")
    return key


def _validate_period(period: str) -> str:
    key = str(period or "").strip().lower()
    if key not in PERIODS:
        raise ValueError(f"period 必须是 {list(PERIODS)} 之一，实际 {period!r}")
    return key


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}{suffix}"


def _fmt_signed(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}"


def _fmt_amount(amount_k: float | None) -> str:
    """成交额按 ADR-008 的千元口径展示（≥1 亿元用亿元，否则万元）。"""
    if amount_k is None:
        return "—"
    if abs(amount_k) >= 1e5:
        return f"{amount_k / 1e5:.2f} 亿元"
    if abs(amount_k) >= 10:
        return f"{amount_k / 10:.2f} 万元"
    return f"{amount_k:,.2f} 千元"


def _freshness_label(freshness: str, session: str, data_time: str) -> str:
    """人读标注：非交易时段必须写「最近交易日收盘」，不得写「实时」（ADR-018 §3）。"""
    if freshness == "live":
        return "盘中实时"
    if freshness == "delayed":
        return f"延迟（最后一根 bar {data_time}）"
    if freshness == "suspended":
        return "停牌/无成交"
    if session == "non_trading_day":
        return "非交易日，最近交易日收盘"
    return "最近交易日收盘（非实时）"


def _time_column(frame: pd.DataFrame) -> str:
    return "trade_time" if "trade_time" in frame.columns else "trade_date"


def _as_day_text(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _series_window_start(period: str, now: pd.Timestamp, bars: int) -> str:
    if period in BARS_PER_DAY:
        days = max(1, math.ceil(bars / BARS_PER_DAY[period]))
        start = now - pd.Timedelta(days=days + 3)
        return start.strftime("%Y%m%d")
    factor = LOOKBACK_FACTOR.get(period, 3)
    start = now - pd.Timedelta(days=max(7, int(bars * factor)))
    return start.strftime("%Y%m%d")


def _rows(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    digits: int | None = 3,
) -> list[dict[str, Any]]:
    """DataFrame → ``list[dict]``：NaN/inf → ``None``，时间统一成字符串。"""
    rows: list[dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        row: dict[str, Any] = {}
        for column in columns:
            if column not in record:
                continue
            value = record[column]
            if isinstance(value, (pd.Timestamp,)) or value.__class__.__name__ == "datetime":
                row[column] = pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")
                continue
            number = _num(value)
            if number is not None:
                row[column] = round(number, digits) if digits is not None else number
            elif isinstance(value, str):
                row[column] = value
            else:
                row[column] = None
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 交易时段与新鲜度（ADR-018 §3/§4，C2 冻结键）
# ---------------------------------------------------------------------------


def trading_session(now: Any = None, *, deps: MarketDeps = DEFAULT_DEPS) -> str:
    """按 trade_cal + 当前时钟判定会话：``pre/open/lunch/closed/non_trading_day``。"""
    stamp = _now(now, deps)
    cal = deps.trade_cal()
    if cal is None or len(cal) == 0:
        open_today = stamp.weekday() < 5
    else:
        cal_days = pd.to_datetime(cal["cal_date"]).dt.normalize()
        is_open = pd.to_numeric(cal["is_open"], errors="coerce").fillna(0).astype(int)
        open_today = bool(((cal_days == stamp.normalize()) & (is_open == 1)).any())
    if not open_today:
        return "non_trading_day"
    clock = stamp.time()
    if clock < clock_time(9, 30):
        return "pre"
    if clock <= clock_time(11, 30):
        return "open"
    if clock < clock_time(13, 0):
        return "lunch"
    if clock <= clock_time(15, 0):
        return "open"
    return "closed"


def _freshness_of(
    session: str,
    data_time: str,
    now: pd.Timestamp,
    stale_seconds: float,
) -> tuple[str, float | None]:
    """会话 + 最后一根 bar 的滞后秒数 → ``freshness``（非交易时段一律 eod）。"""
    if session != "open" or not data_time:
        return "eod", None
    lag = (now - pd.Timestamp(data_time)).total_seconds()
    return ("live" if lag <= stale_seconds else "delayed"), lag


def _is_suspended(frame: pd.DataFrame) -> bool:
    """最新交易日全部 bar 的 vol/amount 都为 0 → 停牌 / 无成交（ADR-018 §4）。"""
    if frame is None or frame.empty or "vol" not in frame.columns:
        return False
    dates = pd.to_datetime(frame["trade_date"]).dt.normalize()
    latest = dates.max()
    mask = dates == latest
    vol = pd.to_numeric(frame.loc[mask, "vol"], errors="coerce").fillna(0.0)
    if "amount" in frame.columns:
        amount = pd.to_numeric(frame.loc[mask, "amount"], errors="coerce").fillna(0.0)
    else:
        amount = pd.Series(0.0, index=vol.index)
    return bool((vol <= 0).all() and (amount <= 0).all())


def _missing_open_days(calendar: Any, stamp: pd.Timestamp, data_as_of: str) -> int:
    """``data_as_of`` 之后、今天之前缺失的交易日数，即停牌交易日数（ADR-018 §4）。

    真实日线里停牌日**没有行**（``astock/track.py:688``、``astock/recommend.py:704``
    都按「缺 bar」判停牌），所以只看 vol/amount 是否为 0 会漏掉三周停牌这类形态：
    最后一根 bar 仍是停牌前的成交日、量不为 0，data_as_of 却已经落后很多天。

    这里把「今天」排除在缺口之外：收盘后（15:00 之后）当日日线常常还没发布，
    不能因为今天这根 bar 没到就把正常股票判成停牌——今天缺 bar 属于 eod/delayed
    的延迟语义，只有更早的交易日整段缺失才说明标的停牌。
    """
    if not data_as_of:
        return 0
    try:
        last_bar = pd.Timestamp(data_as_of).normalize()
    except (TypeError, ValueError):  # pragma: no cover - data_as_of 由契约保证格式
        return 0
    if calendar is None or len(calendar) == 0 or "cal_date" not in getattr(
        calendar, "columns", []
    ):
        return 0
    days = pd.to_datetime(calendar["cal_date"]).dt.normalize()
    is_open = pd.to_numeric(calendar["is_open"], errors="coerce").fillna(0).astype(int)
    opens = pd.Series(days[is_open == 1].unique()).sort_values()
    today = stamp.normalize()
    return int(sum(1 for day in opens if pd.Timestamp(day) < today and day > last_bar))


# ---------------------------------------------------------------------------
# 取数：唯一入口 load_series（时点只由 latest_bar_time 生产）
# ---------------------------------------------------------------------------


def load_series(
    ts_code: str,
    period: str = "day",
    *,
    adj: str = "qfq",
    bars: int = DEFAULT_KLINE_LIMIT,
    now: Any = None,
    deps: MarketDeps = DEFAULT_DEPS,
) -> MarketSeries:
    """按冻结词表取一段行情，并算好 ``data_as_of`` / ``data_time`` / freshness。

    - 分钟周期先走 ``rt_min``（``data.load_minute(days=1..N)``，内部 30s 缓存）；
      ``rt_min`` 失败（:class:`TushareError`）时退回日线，并把 freshness 标成
      ``delayed``（bar 时间陈旧）；
    - 日 / 周 / 月走 ``load_daily``（默认 qfq）或 ``load_index_daily``，
      周 / 月用 ``resample_ohlcv`` 重采样；
    - ``data_as_of`` / ``data_time`` 只从 :func:`astock.data.latest_bar_time`
      取，本函数不拼接时间字符串。
    """
    code = _normalize_code(ts_code)
    period = _validate_period(period)
    adj = _validate_adj(adj)
    stamp = _now(now, deps)
    bars = max(1, int(bars))
    session = trading_session(stamp, deps=deps)
    adj_arg = None if adj == "none" else adj
    is_index = bool(INDEX_REF_RE.match(code))
    notes: list[str] = []
    requested_period = period
    source = "daily"
    degraded = False
    slow = False
    elapsed: float | None = None

    if period in MINUTE_PERIODS:
        if is_index:
            raise ValueError(f"指数 {code} 暂无分钟线（v1 指数只支持 day/week/month）")
        try:
            started = time.perf_counter()
            frame = deps.minute(
                code,
                freq=period,
                days=max(1, min(30, math.ceil(bars / BARS_PER_DAY[period]))),
                adj=adj_arg,
            )
            elapsed = time.perf_counter() - started
            slow = elapsed > deps.minute_latency_seconds
            source = "minute"
        except TushareError as exc:
            notes.append(f"{period} 分钟线不可用（{exc}），已回退日线")
            frame = deps.daily(
                code,
                start=_series_window_start("day", stamp, bars),
                end=stamp.strftime("%Y%m%d"),
                adj=adj_arg,
            )
            period = "day"
            source = "daily"
            degraded = True
    else:
        start = _series_window_start(period, stamp, bars)
        end = stamp.strftime("%Y%m%d")
        if is_index:
            frame = deps.index_daily(code, start=start, end=end)
            source = "index_daily"
        else:
            frame = deps.daily(code, start=start, end=end, adj=adj_arg)
        if period == "week":
            frame = deps.resample(frame, "W-FRI")
        elif period == "month":
            frame = deps.resample(frame, "ME")

    if frame is None or len(frame) == 0:
        raise TushareError(f"{code} 无 {requested_period} 行情数据")

    frame = frame.copy()
    time_col = _time_column(frame)
    frame[time_col] = pd.to_datetime(frame[time_col])
    frame = frame.sort_values(time_col).reset_index(drop=True)

    data_as_of, data_time = deps.bar_time(frame, freq=PERIOD_FREQ[period], notes=notes)
    suspended = _is_suspended(frame)
    # 停牌第二判据：真实日线停牌日缺行（不是 0 量行）。指数不存在停牌，跳过。
    missing_days = 0 if is_index else _missing_open_days(deps.trade_cal(), stamp, data_as_of)
    if missing_days > 0:
        suspended = True
    freshness: str
    lag: float | None = None
    if suspended:
        freshness = "suspended"
    else:
        freshness, lag = _freshness_of(
            session, data_time, stamp, deps.minute_stale_seconds
        )
    # 盘中拿到的是日/周/月 bar：日线在收盘后更新，不能当实时（ADR-018 §1/§4）。
    if not suspended and session == "open" and period not in MINUTE_PERIODS:
        if degraded:
            freshness = "delayed"
        else:
            freshness = "eod"
            notes.append(
                "日线/周线/月线在收盘后更新：盘中该周期不是实时价，实时行情请用分钟周期"
            )
    # rt_min 响应超预算同样按 delayed 降级（ADR-018 §4 的 2 秒预算）。
    if slow and not suspended:
        freshness = "delayed"

    if session != "open" and freshness == "eod":
        notes.append("非交易时段：以下为最近交易日收盘数据，不是实时价")
    if freshness == "delayed":
        if slow:
            notes.append(
                f"rt_min 响应 {elapsed:.2f} 秒，超过 {deps.minute_latency_seconds:.0f} 秒"
                "延迟预算，按 delayed 降级"
            )
        elif lag is not None and lag > 0:
            notes.append(
                f"实时行情延迟：最后一根 bar 为 {data_time}，距今 {lag:.0f} 秒"
                f"（阈值 {deps.minute_stale_seconds:.0f} 秒），按 delayed 降级并退回最近已完成 bar"
            )
        else:
            notes.append(
                f"分钟行情不可用或未及时更新：当前展示 {data_time} 的日线 bar，"
                "不是盘中实时价，按 delayed 降级"
            )
    if suspended:
        if missing_days > 0:
            notes.append(
                f"停牌/无成交：最后一根 bar 为 {data_as_of}，其后 {missing_days} 个交易日无行情，"
                "指标按最后一根 bar 计算"
            )
        else:
            notes.append(
                "停牌/无成交：最新交易日成交量与成交额为 0，指标按最后一根 bar 计算"
            )
    if period != requested_period:
        notes.append(f"请求的 {requested_period} 不可用，实际返回 {period} 数据")

    # ADR-008 补充裁决第 7 条：分钟盘中当日 bar 用最近已发布因子复权，
    # 数据层在 attrs["adjustment"] 标 partial/factor_as_of，工具侧必须把它
    # 透出到 metadata + notes，否则"盘中近似"只存在于没人读的地方（评审 P2）。
    adjust_meta = dict(getattr(frame, "attrs", {}).get("adjustment") or {})
    partial = bool(adjust_meta.get("partial"))
    factor_as_of = str(adjust_meta["factor_as_of"]) if adjust_meta.get("factor_as_of") else None
    if partial:
        notes.append(
            f"盘中近似：当日 bar 按最近已发布因子（截至 {factor_as_of or '最近交易日'}）复权，"
            "收盘后因子发布才变为终值"
        )

    return MarketSeries(
        code=code,
        period=period,
        requested_period=requested_period,
        source=source,
        frame=frame,
        session=session,
        freshness=freshness,
        data_as_of=data_as_of,
        data_time=data_time,
        adj=adj,
        suspended=suspended,
        notes=notes,
        partial=partial,
        factor_as_of=factor_as_of,
    )


# ---------------------------------------------------------------------------
# 指标：全部复用 astock.indicators（同一序列必须逐值一致）
# ---------------------------------------------------------------------------


def _indicator_series(frame: pd.DataFrame, name: str) -> dict[str, pd.Series]:
    """单个指标名 → ``{列名: Series}``，只做转发不做重算。"""
    key = str(name or "").strip().lower()
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
    if key in _BARE_INDICATORS:
        if key == "macd":
            table = ta.macd(close)
            return {column: table[column] for column in table.columns}
        if key == "kdj":
            table = ta.kdj(frame)
            return {column: table[column] for column in table.columns}
        if key == "obv":
            return {"obv": ta.obv(frame)}
        if key == "cci":
            return {"cci20": ta.cci(frame)}
        if key == "vr":
            return {"vr26": ta.vr(frame)}
        return {"psy12": ta.psy(close)}
    match = _INDICATOR_RE.match(key)
    if not match:
        raise ValueError(
            f"未知指标 {name!r}：支持 {list(_BARE_INDICATORS)} 或 ma20/ema12/rsi14/boll20/atr14 形式"
        )
    kind, raw_window = match.group(1), match.group(2)
    window = int(raw_window) if raw_window else None
    if kind == "ma":
        n = window or 20
        return {f"ma{n}": ta.sma(close, n)}
    if kind == "ema":
        n = window or 12
        return {f"ema{n}": ta.ema(close, n)}
    if kind == "rsi":
        n = window or 14
        return {f"rsi{n}": ta.rsi(close, n)}
    if kind == "boll":
        table = ta.boll(close, window or 20)
        return {column: table[column] for column in table.columns}
    n = window or 14
    return {f"atr{n}": ta.atr(frame, n)}


def _indicator_columns(
    frame: pd.DataFrame, names: Sequence[str]
) -> tuple[dict[str, pd.Series], list[str]]:
    """按顺序算指标列；``volume`` 这类图层次只进图不进表，记进 skipped。"""
    columns: dict[str, pd.Series] = {}
    skipped: list[str] = []
    for name in names:
        key = str(name or "").strip().lower()
        if key == "volume":
            skipped.append(key)
            continue
        columns.update(_indicator_series(frame, key))
    return columns, skipped


def _parse_indicator_names(names: Any) -> list[str]:
    if names is None:
        return list(DEFAULT_INDICATORS)
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)) or not names:
        raise ValueError("indicators 必须是非空字符串数组，例如 [\"ma20\", \"rsi14\", \"macd\"]")
    return [str(item) for item in names]


def _parse_layer_names(layers: Any) -> list[str]:
    if layers is None:
        return []
    if isinstance(layers, str):
        layers = [layers]
    if not isinstance(layers, (list, tuple)):
        raise TypeError("layers 必须是字符串数组，例如 [\"ma5\", \"ma20\", \"volume\"]")
    out: list[str] = []
    for item in layers:
        key = str(item or "").strip().lower()
        match = _LAYER_RE.match(key)
        if not match:
            raise ValueError(
                f"未知叠加层 {item!r}：支持 ma20 / ema12 / rsi14 / boll / atr14 / macd / kdj / volume，"
                "分钟图可用 ma20@day 叠加日线指标"
            )
        out.append(key)
    return out


def _overlay_on_bars(
    bars: pd.DataFrame,
    daily: pd.DataFrame,
    columns: Mapping[str, pd.Series],
) -> tuple[dict[str, pd.Series], list[str]]:
    """低频指标按交易日 ffill 到高频 bar；只用更早已完成的日线（禁止未来值）。"""
    bar_days = pd.to_datetime(bars["trade_date"]).dt.normalize()
    day_column = pd.to_datetime(daily["trade_date"]).dt.normalize()
    out: dict[str, pd.Series] = {}
    ffill_days = sorted({pd.Timestamp(day).strftime("%Y-%m-%d") for day in bar_days.unique()})
    for name, series in columns.items():
        right = pd.DataFrame({"trade_date": day_column, "value": series.to_numpy()})
        right = right.dropna(subset=["value"]).sort_values("trade_date")
        if right.empty:
            out[name] = pd.Series([None] * len(bars), index=bars.index, dtype=float)
            continue
        left = pd.DataFrame({"trade_date": bar_days}).sort_values("trade_date")
        merged = pd.merge_asof(
            left,
            right,
            on="trade_date",
            direction="backward",
            allow_exact_matches=False,
        )
        values = pd.Series(merged["value"].to_numpy(), index=merged.index)
        out[name] = values.reindex(bars.index)
    return out, ffill_days


# ---------------------------------------------------------------------------
# 工具 1：quote —— 最新价 / 涨跌幅 / 成交额
# ---------------------------------------------------------------------------


def quote(
    ts_code: str,
    *,
    adj: str = "qfq",
    now: Any = None,
    deps: MarketDeps = DEFAULT_DEPS,
) -> ToolResult:
    """最新价 / 涨跌幅 / 成交额；非交易时段明确标注「最近交易日收盘」。"""
    code = _normalize_code(ts_code)
    adj = _validate_adj(adj)
    stamp = _now(now, deps)
    session = trading_session(stamp, deps=deps)
    # 日线窗口要覆盖停牌期：太长停牌时也要拿到最后一根 bar 才能标 suspended。
    daily = load_series(code, "day", adj=adj, bars=QUOTE_LOOKBACK_BARS, now=stamp, deps=deps)
    series = daily
    notes: list[str] = []
    if session == "open":
        try:
            series = load_series(
                code, "1min", adj=adj, bars=BARS_PER_DAY["1min"], now=stamp, deps=deps
            )
        except TushareError as exc:
            notes.append(f"1 分钟实时行情不可用（{exc}），已退回日线并标注 delayed")
    notes = notes + series.notes

    rows = _today_or_last(series)
    last = rows.iloc[-1]
    day_row = daily.frame.iloc[-1]
    price = _num(last.get("close"))
    suspended = series.suspended
    freshness = series.freshness

    base = _num(day_row.get("close"))
    if _as_day_text(day_row.get("trade_date")) == series.data_as_of:
        base = _num(day_row.get("pre_close"))
    pct_chg = None
    if price is not None and base:
        pct_chg = (price / base - 1.0) * 100.0

    if _time_column(rows) == "trade_time":
        amount = _num(pd.to_numeric(rows["amount"], errors="coerce").fillna(0.0).sum())
        vol = _num(pd.to_numeric(rows["vol"], errors="coerce").fillna(0.0).sum())
        open_price = _num(rows["open"].iloc[0])
        high = _num(pd.to_numeric(rows["high"], errors="coerce").max())
        low = _num(pd.to_numeric(rows["low"], errors="coerce").min())
    else:
        amount = _num(day_row.get("amount"))
        vol = _num(day_row.get("vol"))
        open_price = _num(day_row.get("open"))
        high = _num(day_row.get("high"))
        low = _num(day_row.get("low"))

    label = _freshness_label(freshness, session, series.data_time)
    text = (
        f"{code} 最新价 {_fmt(price)} 元（{_fmt_signed(pct_chg)}%）"
        f"｜成交额 {_fmt_amount(amount)}｜成交量 {_fmt(vol)} 手"
        f"｜数据截至 {series.data_as_of} {series.data_time[11:16]}（{label}）"
    )
    if notes:
        text = f"{text}；" + "；".join(notes)

    table = [
        {
            "ts_code": code,
            "data_as_of": series.data_as_of,
            "data_time": series.data_time,
            "open": _round(open_price),
            "high": _round(high),
            "low": _round(low),
            "close": _round(price),
            "pre_close": _round(base),
            "pct_chg": _round(pct_chg),
            "vol": _round(vol),
            "amount": _round(amount),
            "session": session,
            "freshness": freshness,
        }
    ]
    metadata = series.metadata()
    metadata.update(
        {
            "session": session,
            "freshness": freshness,
            "notes": notes,
            "adjustment": adj,
            "adjustment_label": _ADJ_LABELS[adj],
            "suspended": suspended,
        }
    )
    return ToolResult(
        text=_clip(text),
        table=table,
        data_as_of=series.data_as_of,
        data_time=series.data_time,
        source=f"market:quote:{series.source}",
        metadata=metadata,
    )


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _today_or_last(series: MarketSeries) -> pd.DataFrame:
    """盘中取当日 bar（成交量/成交额按日累计），停牌/盘后取最后一根 bar。"""
    frame = series.frame
    dates = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    today = frame.loc[dates == series.data_as_of]
    if today.empty:
        return frame.tail(1).reset_index(drop=True)
    return today.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 工具 2：kline —— K 线表 + 可选叠加层 + 可选图（图题带 freshness 提示）
# ---------------------------------------------------------------------------


def kline(
    ts_code: str,
    *,
    period: str = "day",
    adj: str = "qfq",
    limit: int = DEFAULT_KLINE_LIMIT,
    layers: Sequence[str] | None = None,
    chart: bool = False,
    title: str | None = None,
    now: Any = None,
    deps: MarketDeps = DEFAULT_DEPS,
    db_path: str | None = None,
    artifact_root: str | None = None,
    renderer: Callable[..., Any] | None = None,
) -> ToolResult:
    """K 线表（日/周/月 + 分钟）+ MA/BOLL/成交量等叠加层；``chart=True`` 时出图。"""
    code = _normalize_code(ts_code)
    period = _validate_period(period)
    adj = _validate_adj(adj)
    limit = _bounded_limit(limit)
    layer_names = _parse_layer_names(layers)
    stamp = _now(now, deps)
    series = load_series(code, period, adj=adj, bars=limit, now=stamp, deps=deps)

    columns: dict[str, pd.Series] = {}
    skipped: list[str] = []
    ffill_days: list[str] = []
    ffill_fields: list[str] = []
    intraday_layers = [name for name in layer_names if name.endswith("@day")]
    local_layers = [name for name in layer_names if not name.endswith("@day")]
    if intraday_layers:
        if series.period not in MINUTE_PERIODS:
            raise ValueError("叠加层 @day 只用于分钟 K 线（日线指标 ffill 到分钟 bar）")
        daily = load_series(
            code,
            "day",
            adj=adj,
            bars=INDICATOR_WARMUP_BARS,
            now=stamp,
            deps=deps,
        )
        daily_columns, _ = _indicator_columns(
            daily.frame, [name.split("@")[0] for name in intraday_layers]
        )
        mapped, ffill_days = _overlay_on_bars(series.frame, daily.frame, daily_columns)
        for daily_column, values in mapped.items():
            columns[f"{daily_column}_day"] = values
            ffill_fields.append(f"{daily_column}_day")
    if local_layers:
        local_columns, skipped = _indicator_columns(series.frame, local_layers)
        columns.update(local_columns)

    display = series.frame.tail(limit).reset_index(drop=True)
    for name, values in columns.items():
        display[name] = values.tail(limit).to_numpy()

    time_col = _time_column(display)
    table_columns = [time_col, "open", "high", "low", "close", "pct_chg", "vol", "amount"]
    table_columns.extend(columns)
    table = _rows(display, table_columns)

    latest = display.iloc[-1]
    parts = [
        f"{code} {PERIOD_LABELS[period]} K线 共 {len(display)} 根",
        f"最新 {series.data_as_of} {series.data_time[11:16]} 收盘 {_fmt(_num(latest.get('close')))}",
    ]
    for name in columns:
        if name in display.columns:
            value = _num(display[name].iloc[-1])
            if value is not None:
                parts.append(f"{name} {_fmt(value, 3)}")
    label = _freshness_label(series.freshness, series.session, series.data_time)
    parts.append(label)
    text = "；".join(parts)
    if skipped:
        text = f"{text}；图层次 {skipped} 只画图不列表"
    if series.notes:
        text = f"{text}；" + "；".join(series.notes)

    metadata = series.metadata()
    if ffill_fields:
        metadata["ffill"] = {
            "fields": ffill_fields,
            "days": ffill_days,
            "bars": len(display),
            "rule": "日线指标按交易日 ffill 到分钟 bar，只用更早已完成日线（禁止未来值）",
        }
    metadata["layers"] = layer_names

    figure = None
    if chart:
        figure, chart_meta = _render_kline(
            series,
            display,
            adj=adj,
            period=period,
            layers=layer_names,
            title=title,
            db_path=db_path,
            artifact_root=artifact_root,
            renderer=renderer,
        )
        metadata["chart"] = chart_meta
        chart_warnings = [str(item) for item in chart_meta.get("warnings") or []]
        if chart_warnings:
            text = f"{text}；" + "；".join(chart_warnings)

    return ToolResult(
        text=_clip(text),
        table=table,
        figure=figure,
        data_as_of=series.data_as_of,
        data_time=series.data_time,
        source=f"market:kline:{series.source}",
        metadata=metadata,
    )


def _bounded_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"limit 必须是整数，实际 {value!r}") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须在 1..{MAX_LIMIT} 之间，实际 {limit}")
    return limit


def _chart_layers(layer_names: Sequence[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """工具层名 → (ChartSpec 图层, 未上图说明)。

    ChartSpec 只支持 ma/boll/volume/macd/rsi/kdj 六类且最多 ``MAX_LAYERS`` 层：
    ema/atr 只能算在表里，超限的层会被截断——这两种情况都要显式告知调用方，
    不能画少线却什么都不说（评审 iss-mwm6tv83）。
    """
    built: list[tuple[str, dict[str, Any]]] = []
    dropped: list[str] = []
    for name in layer_names:
        base = name.split("@")[0]
        match = re.match(r"^(ma|ema|rsi|boll|atr|macd|kdj|volume)(\d{1,3})?$", base)
        if not match:
            dropped.append(f"{name}（无法识别的图层名，未上图）")
            continue
        kind, window = match.group(1), match.group(2)
        if kind in ("ema", "atr"):
            dropped.append(f"{name}（ChartSpec 暂不支持 {kind} 图层，仅表格展示）")
            continue
        if kind == "volume":
            layer: dict[str, Any] = {"type": "volume"}
        elif kind == "ma":
            layer = {"type": "ma", "window": int(window) if window else 20}
        elif kind == "boll":
            layer = {"type": "boll", "window": int(window) if window else 20}
        else:
            layer = {"type": kind}
        built.append((name, layer))
    if len(built) > MAX_LAYERS:
        overflow = "、".join(name for name, _ in built[MAX_LAYERS:])
        dropped.append(f"{overflow}（超过 {MAX_LAYERS} 层上限，未上图）")
        built = built[:MAX_LAYERS]
    return [layer for _, layer in built], dropped


def _chart_range(
    display: pd.DataFrame,
) -> tuple[str, str | None, pd.Timestamp | None]:
    """落图窗口的 ``range``，并钳到 ChartSpec 的 ``MAX_RANGE_YEARS`` 上限。

    月线 ``limit=200`` 能覆盖十几年，直接拼首尾日期会被 ``validate_chart_spec``
    判「窗口跨度超过 10 年」并让整个工具失败（评审 iss-mwm6tovm）。
    返回的第三项是裁剪下限：调用方据此裁掉图上更早的 bar，让图注与图上内容一致。
    """
    stamps = pd.to_datetime(display["trade_date"])
    first = pd.Timestamp(stamps.iloc[0]).normalize()
    last = pd.Timestamp(stamps.iloc[-1]).normalize()
    floor = last - pd.DateOffset(years=MAX_RANGE_YEARS)
    if first >= floor:
        return f"{first:%Y-%m-%d}:{last:%Y-%m-%d}", None, None
    shown = int((stamps >= floor).sum())
    warning = (
        f"图表窗口超过 {MAX_RANGE_YEARS} 年上限，已裁到 {floor:%Y-%m-%d} 之后"
        f"（图上 {shown} 根，表格仍返回 {len(display)} 根）"
    )
    return f"{floor:%Y-%m-%d}:{last:%Y-%m-%d}", warning, floor


def _render_kline(
    series: MarketSeries,
    display: pd.DataFrame,
    *,
    adj: str,
    period: str,
    layers: Sequence[str],
    title: str | None,
    db_path: str | None,
    artifact_root: str | None,
    renderer: Callable[..., Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """用渲染管线出图；图题写明 freshness 提示（ADR-018 §4「图上有提示」）。

    出图失败不把工具打成错误：``ChartSpecError`` 收成 ``figure=None`` +
    ``metadata['chart']['warning']``，表格数据仍然可用（评审 iss-mwm6tovm）。
    """
    if renderer is None:
        from astock.agent import chart_service as renderer_module

        # Q5：渲染 + 审计的唯一入口；chart_render 只是纯渲染器。
        renderer = renderer_module.render_chart
    label = _freshness_label(series.freshness, series.session, series.data_time)
    chart_title = title or f"{series.code} {PERIOD_LABELS[period]} K线 · {label}"
    layer_spec, layer_warnings = _chart_layers(layers)
    chart_range, range_warning, floor = _chart_range(display)
    plot = display
    if floor is not None:
        plot = display.loc[pd.to_datetime(display["trade_date"]) >= floor].reset_index(
            drop=True
        )
    warnings = list(layer_warnings)
    if range_warning:
        warnings.append(range_warning)
    spec: dict[str, Any] = {
        "version": 1,
        "kind": "candlestick",
        "title": chart_title,
        "data": {
            "source": "market",
            "ref": series.code,
            "period": period,
            "range": chart_range,
            "adj": adj,
        },
        "encode": {"x": "trade_time" if period in MINUTE_PERIODS else "trade_date"},
        "layers": layer_spec,
    }
    if series.partial:
        # spec.meta 是自由对象（chart_spec._validate_meta），不占 data 白名单；
        # 图注据此标「盘中近似（因子截至 X）」。
        spec["meta"] = {"partial": True, "factor_as_of": series.factor_as_of}
    meta: dict[str, Any] = {
        "title": chart_title,
        "freshness_label": label,
        "partial": series.partial,
        "factor_as_of": series.factor_as_of,
    }
    # ADR-017 §7.1：runtime 通过 ToolContext 告诉工具本次调用的 run_id/step，
    # 图表 artifact 才能挂到真实 run（source="agent"）；UI 直接渲染无上下文，
    # 仍按 source="render" 记录（Q5/D9）。
    from astock.agent.registry import current_tool_context

    context = current_tool_context()
    audit_kwargs: dict[str, Any] = {"session_id": "market_tools"}
    if context is not None:
        audit_kwargs = {
            "run_id": context.run_id,
            "step": context.step,
            "session_id": context.session_id or "market_tools",
        }
    try:
        rendered = renderer(
            spec,
            frame=plot.copy(),
            db_path=db_path,
            artifact_root=artifact_root,
            data_as_of=series.data_as_of,
            data_time=series.data_time,
            export_png=False,
            **audit_kwargs,
        )
    except ChartSpecError as exc:
        warning = f"图表未生成（{exc}），K 线表仍可读"
        warnings.append(warning)
        meta.update({"warning": warning, "warnings": warnings, "spec": spec})
        return None, meta
    return rendered.html, {
        **meta,
        "artifact_id": rendered.artifact_id,
        "artifact_path": rendered.path,
        "data_as_of": rendered.data_as_of,
        "data_time": rendered.data_time,
        # Q1：视图时点只做展示；行情时点仍是上面的 data_*（runtime/gateway 继续读它）。
        # 注入的 renderer 可能是轻量替身：缺字段时按「未知」处理，不因为展示字段炸掉。
        "view_as_of": getattr(rendered, "view_as_of", ""),
        "view_time": getattr(rendered, "view_time", ""),
        # Q7：pre=集合竞价 / post=盘后固定价 / continuous / close（日线）。
        "session": getattr(rendered, "session", ""),
        "degraded": bool(getattr(rendered, "degraded", False)),
        "warnings": warnings + list(rendered.warnings),
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# 工具 3：indicator —— 复用 astock.indicators
# ---------------------------------------------------------------------------


def indicator(
    ts_code: str,
    *,
    period: str = "day",
    adj: str = "qfq",
    indicators: Sequence[str] | None = None,
    limit: int = DEFAULT_INDICATOR_LIMIT,
    now: Any = None,
    deps: MarketDeps = DEFAULT_DEPS,
) -> ToolResult:
    """MA/EMA/RSI/MACD/BOLL/KDJ/ATR 等指标；数值与 ``astock.indicators`` 逐值一致。"""
    code = _normalize_code(ts_code)
    period = _validate_period(period)
    adj = _validate_adj(adj)
    limit = _bounded_limit(limit)
    names = _parse_indicator_names(indicators)
    stamp = _now(now, deps)
    series = load_series(
        code,
        period,
        adj=adj,
        bars=limit + INDICATOR_WARMUP_BARS,
        now=stamp,
        deps=deps,
    )

    columns, skipped = _indicator_columns(series.frame, names)
    display = series.frame.tail(limit).reset_index(drop=True)
    for name, values in columns.items():
        display[name] = values.tail(limit).to_numpy()

    time_col = _time_column(display)
    table = _rows(display, [time_col, "close", *columns], digits=None)

    latest = display.iloc[-1]
    parts = [
        (
            f"{code} {PERIOD_LABELS[period]} 指标（最近 {len(display)} 根，"
            f"截至 {series.data_as_of} {series.data_time[11:16]}）"
        )
    ]
    for name in columns:
        value = _num(latest.get(name))
        parts.append(f"{name}={_fmt(value, 3)}")
    label = _freshness_label(series.freshness, series.session, series.data_time)
    parts.append(label)
    text = "；".join(parts)
    if skipped:
        text = f"{text}；{skipped} 是图层次，指标工具不返回"
    if series.notes:
        text = f"{text}；" + "；".join(series.notes)

    metadata = series.metadata()
    metadata["indicators"] = list(columns)
    metadata["warmup_bars"] = INDICATOR_WARMUP_BARS
    metadata["indicator_basis"] = "复权收盘价（受 adj 控制），与 astock.indicators 同一序列"

    return ToolResult(
        text=_clip(text),
        table=table,
        data_as_of=series.data_as_of,
        data_time=series.data_time,
        source=f"market:indicator:{series.source}",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# 注册表（Agent 白名单：quote / kline / indicator）
# ---------------------------------------------------------------------------

_TS_CODE_SCHEMA = {
    "type": "string",
    "description": "标的代码，如 600519.SH / 000001.SZ / 000300.SH（指数只支持日/周/月）",
    "minLength": 8,
    "maxLength": 12,
}
_ADJ_SCHEMA = {
    "type": "string",
    "enum": list(ADJ_VALUES),
    "default": "qfq",
    "description": "复权口径：qfq 前复权（默认）/ hfq 后复权 / none 不复权",
}
_PERIOD_SCHEMA = {
    "type": "string",
    "enum": list(PERIODS),
    "default": "day",
    "description": "周期：day / week / month / 1min / 5min / 15min / 30min / 60min",
}

QUOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"ts_code": _TS_CODE_SCHEMA, "adj": _ADJ_SCHEMA},
    "required": ["ts_code"],
}

KLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ts_code": _TS_CODE_SCHEMA,
        "period": _PERIOD_SCHEMA,
        "adj": _ADJ_SCHEMA,
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_KLINE_LIMIT,
            "description": "返回的 bar 行数（1..200）",
        },
        "layers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "叠加层：ma5/ma20/ema12/rsi14/boll/atr14/macd/kdj/volume；分钟图可用 ma20@day（日线指标 ffill）",
        },
        "chart": {
            "type": "boolean",
            "default": False,
            "description": "是否出图（图题写明 live/delayed/eod/suspended 提示）",
        },
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
    },
    "required": ["ts_code"],
}

INDICATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ts_code": _TS_CODE_SCHEMA,
        "period": _PERIOD_SCHEMA,
        "adj": _ADJ_SCHEMA,
        "indicators": {
            "type": "array",
            "items": {"type": "string"},
            "description": "指标名：ma5/ma20/ema12/rsi14/boll20/atr14/macd/kdj/obv/cci/vr/psy",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "default": DEFAULT_INDICATOR_LIMIT,
            "description": "返回最近多少根 bar（1..200）",
        },
    },
    "required": ["ts_code"],
}


def register_market_tools(
    registry: ToolRegistry,
    *,
    deps: MarketDeps = DEFAULT_DEPS,
    db_path: str | None = None,
    artifact_root: str | None = None,
) -> ToolRegistry:
    """把 quote / kline / indicator 注册进给定注册表（便于与文件工具合并）。"""
    registry.register(
        ToolSpec(
            name="quote",
            description=(
                "查询标的的最新价、涨跌幅、成交额与成交量。盘中用最新分钟 bar，"
                "非交易时段返回最近交易日收盘并标注 eod，停牌标注 suspended。"
                "结果同时带 data_as_of（YYYY-MM-DD）与 data_time（YYYY-MM-DD HH:MM）。"
            ),
            parameters=QUOTE_SCHEMA,
            handler=partial(quote, deps=deps),
            permissions=frozenset({Permission.MARKET_READ}),
        )
    )
    registry.register(
        ToolSpec(
            name="kline",
            description=(
                "取 K 线：日/周/月或 1/5/15/30/60 分钟，可叠加 MA/BOLL/成交量等；"
                "chart=true 时出图，图题写明 live/delayed/eod/suspended。"
                "结果同时带 data_as_of 与 data_time。"
            ),
            parameters=KLINE_SCHEMA,
            handler=partial(
                kline, deps=deps, db_path=db_path, artifact_root=artifact_root
            ),
            permissions=frozenset({Permission.MARKET_READ, Permission.CHART_RENDER}),
        )
    )
    registry.register(
        ToolSpec(
            name="indicator",
            description=(
                "在复权价上计算 MA/EMA/RSI/MACD/BOLL/KDJ/ATR 等指标（与 astock.indicators "
                "同一实现），返回最近 N 根 bar 的指标值，带 data_as_of 与 data_time。"
            ),
            parameters=INDICATOR_SCHEMA,
            handler=partial(indicator, deps=deps),
            permissions=frozenset({Permission.MARKET_READ, Permission.INDICATOR_COMPUTE}),
        )
    )
    return registry


def build_market_registry(
    *,
    deps: MarketDeps = DEFAULT_DEPS,
    db_path: str | None = None,
    artifact_root: str | None = None,
    table_row_limit: int | None = None,
) -> ToolRegistry:
    """独立行情注册表（测试与单独装配用）。"""
    kwargs: dict[str, Any] = {}
    if table_row_limit is not None:
        kwargs["table_row_limit"] = table_row_limit
    registry = ToolRegistry(**kwargs)
    return register_market_tools(
        registry, deps=deps, db_path=db_path, artifact_root=artifact_root
    )
