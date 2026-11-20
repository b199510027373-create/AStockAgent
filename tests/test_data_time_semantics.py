"""行情工具的时点与新鲜度语义（plan-20261107-006，ADR-018 C1/C2）。

覆盖验收标准：

- C1：``data_as_of`` / ``data_time`` 只由 ``astock.data.latest_bar_time`` 生产，
  行情工具不得自己拼时间字符串；
- C2：每个行情/指标结果同时带 ``data_as_of``（``YYYY-MM-DD``）与
  ``data_time``（``YYYY-MM-DD HH:MM``），``metadata`` 固定
  ``session`` / ``freshness`` 两键；
- 非交易时段（收盘后 / 周末）返回最近收盘 bar，``freshness=eod``，回答写
  「最近交易日收盘」，不得把昨日收盘当实时；
- ``rt_min`` 超过 2 分钟未更新 → ``freshness=delayed`` 并写明延迟；
- ``rt_min`` 响应超过 2 秒预算 → 同样 ``delayed``（ADR-018 §4）；
- ``rt_min`` 失败 → 回退日线并在 notes 留痕；
- 停牌 / 无成交 → ``freshness=suspended``，回答写明「停牌/无成交」；
- 指标值与 :mod:`astock.indicators` 对同一序列逐值一致；
- 日线指标 ffill 到分钟图只用更早已完成日线（禁止未来值），metadata 记 ffill 段；
- 图题带 freshness 提示（`图上有提示`）；
- 非目标：不预留 tick / Level-2 伪实时字段。
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from astock import data as market
from astock import indicators as ta
from astock.agent.tools import market_tools as M
from astock.tushare_client import TushareError

CODE = "600519.SH"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MINUTE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


# ---------------------------------------------------------------------------
# 固定行情帧与依赖（不触网、不读缓存）
# ---------------------------------------------------------------------------


def _trade_cal(start: str = "2024-01-01", end: str = "2024-03-31") -> pd.DataFrame:
    days = pd.bdate_range(start, end)
    return pd.DataFrame({"cal_date": days, "is_open": 1, "pretrade_date": days})


def _daily_frame(
    days: list[str] | tuple[str, ...],
    *,
    start: float = 100.0,
    suspended: tuple[str, ...] = (),
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        close = start + index
        halted = day in suspended
        rows.append(
            {
                "ts_code": CODE,
                "trade_date": pd.Timestamp(day),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "pre_close": close - 1.0,
                "change": 1.0,
                "pct_chg": 1.0,
                "vol": 0.0 if halted else 1000.0 + index,
                "amount": 0.0 if halted else 5000.0 + index,
            }
        )
    return pd.DataFrame(rows)


def _minute_frame(
    day: str,
    minutes: list[str],
    *,
    start: float = 100.0,
    halted: bool = False,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for index, minute in enumerate(minutes):
        close = start + index * 0.1
        rows.append(
            {
                "ts_code": CODE,
                "trade_date": pd.Timestamp(day),
                "trade_time": pd.Timestamp(f"{day} {minute}"),
                "open": close - 0.05,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "pre_close": close - 0.1,
                "change": 0.1,
                "pct_chg": 0.1,
                "vol": 0.0 if halted else 10.0,
                "amount": 0.0 if halted else 100.0,
            }
        )
    return pd.DataFrame(rows)


class _Deps:
    """测试依赖：固定时钟 / 固定帧 / 记录 ``bar_time`` 调用。"""

    def __init__(
        self,
        *,
        daily: pd.DataFrame | None = None,
        minute: pd.DataFrame | Exception | None = None,
        clock: str = "2024-01-03 10:00",
        calendar: pd.DataFrame | None = None,
        latency: float | None = None,
    ) -> None:
        self.daily_frame = daily if daily is not None else _daily_frame(
            ["2024-01-02", "2024-01-03"]
        )
        self.minute_frame = minute
        self.clock = pd.Timestamp(clock)
        self.calendar = calendar if calendar is not None else _trade_cal()
        self.latency = latency
        self.bar_time_calls: list[str] = []
        self.minute_calls: list[dict[str, Any]] = []
        self.index_calls: list[dict[str, Any]] = []

    def daily(self, ts_code: str, *, start: str, end: str, adj: str | None) -> pd.DataFrame:
        frame = self.daily_frame.copy()
        mask = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y%m%d")
        return frame.loc[(mask >= start) & (mask <= end)].reset_index(drop=True)

    def index_daily(self, ts_code: str, *, start: str, end: str) -> pd.DataFrame:
        self.index_calls.append({"ts_code": ts_code, "start": start, "end": end})
        frame = self.daily_frame.copy()
        mask = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y%m%d")
        return frame.loc[(mask >= start) & (mask <= end)].reset_index(drop=True)

    def minute(
        self, ts_code: str, *, freq: str, days: int, adj: str | None
    ) -> pd.DataFrame:
        self.minute_calls.append({"freq": freq, "days": days})
        if isinstance(self.minute_frame, Exception):
            raise self.minute_frame
        if self.minute_frame is None:
            raise TushareError("rt_min 无返回")
        return self.minute_frame.copy()

    def bar_time(
        self,
        frame: pd.DataFrame,
        *,
        freq: str = "D",
        strict: bool = False,
        notes: list[str] | None = None,
    ) -> tuple[str, str]:
        self.bar_time_calls.append(freq)
        return market.latest_bar_time(frame, freq=freq, strict=strict, notes=notes)

    def deps(self) -> M.MarketDeps:
        kwargs: dict[str, Any] = {}
        if self.latency is not None:
            kwargs["minute_latency_seconds"] = self.latency
        return M.MarketDeps(
            daily=self.daily,
            index_daily=self.index_daily,
            minute=self.minute,
            resample=market.resample_ohlcv,
            bar_time=self.bar_time,
            trade_cal=lambda: self.calendar,
            clock=lambda: self.clock,
            **kwargs,
        )


def _table(result: Any) -> list[dict[str, Any]]:
    """取出工具结果表格（None 视为契约失败）。"""
    assert result.table is not None and result.table, "工具结果应带表格"
    return result.table


def _assert_contract(result: Any) -> None:
    """C2：两个时点字段同时存在且格式正确；metadata 两键取值合法。"""
    assert result.ok, result.error
    assert DAY_RE.match(result.data_as_of), result.data_as_of
    assert MINUTE_RE.match(result.data_time), result.data_time
    assert result.data_time[:10] == result.data_as_of
    assert result.metadata["session"] in M.SESSIONS
    assert result.metadata["freshness"] in M.FRESHNESS


# ---------------------------------------------------------------------------
# C1：唯一时点生产者
# ---------------------------------------------------------------------------


def test_latest_bar_time_is_single_source() -> None:
    """三个工具的 data_as_of/data_time 必须来自 data.latest_bar_time。"""
    context = _Deps(
        daily=_daily_frame([f"2024-01-0{i}" for i in (2, 3, 4, 5)]),
        minute=_minute_frame("2024-01-05", ["09:31", "09:32", "09:35"]),
        clock="2024-01-05 09:36",
    )
    deps = context.deps()

    quote_result = M.quote(CODE, now=context.clock, deps=deps)
    kline_result = M.kline(CODE, period="1min", limit=10, now=context.clock, deps=deps)
    indicator_result = M.indicator(CODE, limit=5, now=context.clock, deps=deps)

    # 每次取数恰好问一次 bar_time，且频率与周期一致。
    assert context.bar_time_calls == ["D", "1min", "1min", "D"]

    expected_day = market.latest_bar_time(context.daily_frame, freq="D")
    expected_minute = market.latest_bar_time(context.minute_frame, freq="1min")
    assert (quote_result.data_as_of, quote_result.data_time) == expected_minute
    assert (kline_result.data_as_of, kline_result.data_time) == expected_minute
    assert (indicator_result.data_as_of, indicator_result.data_time) == expected_day
    for result in (quote_result, kline_result, indicator_result):
        _assert_contract(result)


# ---------------------------------------------------------------------------
# C2：非交易时段 / 延迟 / 停牌
# ---------------------------------------------------------------------------


def test_non_trading_session_marks_eod() -> None:
    """收盘后与周末都标记 eod，并写明「最近交易日收盘」。"""
    daily = _daily_frame(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])

    closed = _Deps(daily=daily, clock="2024-01-05 16:30")
    result = M.quote(CODE, now=closed.clock, deps=closed.deps())
    _assert_contract(result)
    assert result.metadata["session"] == "closed"
    assert result.metadata["freshness"] == "eod"
    assert result.data_time == "2024-01-05 15:00"
    assert "最近交易日收盘" in result.text
    assert "盘中实时" not in result.text

    weekend = _Deps(daily=daily, clock="2024-01-06 10:00")
    result = M.kline(CODE, now=weekend.clock, deps=weekend.deps())
    _assert_contract(result)
    assert result.metadata["session"] == "non_trading_day"
    assert result.metadata["freshness"] == "eod"
    assert result.data_time == "2024-01-05 15:00"
    assert "最近交易日收盘" in result.text


def test_stale_rt_min_marks_delayed() -> None:
    """rt_min 超过 2 分钟未更新 → delayed，并在文字里写明延迟。"""
    minute = _minute_frame("2024-01-03", ["09:31", "09:32", "09:33"])
    context = _Deps(minute=minute, clock="2024-01-03 10:10")
    result = M.quote(CODE, now=context.clock, deps=context.deps())

    _assert_contract(result)
    assert result.metadata["session"] == "open"
    assert result.metadata["freshness"] == "delayed"
    assert result.data_time == "2024-01-03 09:33"
    notes = " ".join(result.metadata["notes"])
    assert "延迟" in notes and "120 秒" in notes
    assert "延迟" in result.text

    # 同一根 bar 刚更新 → live，不得误判为延迟。
    fresh = _Deps(minute=minute, clock="2024-01-03 09:33")
    fresh_result = M.quote(CODE, now=fresh.clock, deps=fresh.deps())
    assert fresh_result.metadata["freshness"] == "live"
    assert "盘中实时" in fresh_result.text


def test_slow_rt_min_marks_delayed() -> None:
    """rt_min 响应超 2 秒预算 → 即便 bar 新鲜也按 delayed 降级（ADR-018 §4）。"""
    minute = _minute_frame("2024-01-03", ["09:31", "09:34"])
    context = _Deps(minute=minute, clock="2024-01-03 09:35", latency=0.0)
    result = M.kline(CODE, period="1min", limit=2, now=context.clock, deps=context.deps())

    _assert_contract(result)
    assert result.data_time == "2024-01-03 09:34"
    assert result.metadata["session"] == "open"
    assert result.metadata["freshness"] == "delayed"
    notes = " ".join(result.metadata["notes"])
    assert "延迟预算" in notes and "按 delayed 降级" in notes
    assert "延迟" in result.text

    # 图题同样带延迟提示（ADR-018「图上有提示」）。
    rendered = SimpleNamespace(
        html="<html>chart</html>",
        artifact_id="art_deadbeefdeadbeef",
        path=None,
        data_as_of=result.data_as_of,
        data_time=result.data_time,
        warnings=[],
    )
    charted = M.kline(
        CODE,
        period="1min",
        limit=2,
        chart=True,
        now=context.clock,
        deps=context.deps(),
        renderer=lambda spec, **kwargs: rendered,
    )
    assert charted.ok
    assert "延迟" in charted.metadata["chart"]["title"]


def test_suspended_marks_freshness_and_note() -> None:
    """停牌 / 无成交显式标注 suspended，指标仍按最后一根 bar 计算。"""
    daily = _daily_frame(
        ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"],
        suspended=("2024-01-09",),
    )
    context = _Deps(daily=daily, clock="2024-01-09 16:30")
    result = M.quote(CODE, now=context.clock, deps=context.deps())

    _assert_contract(result)
    assert result.metadata["freshness"] == "suspended"
    assert result.metadata["suspended"] is True
    assert "停牌" in result.text and "无成交" in result.text

    indicators = M.indicator(
        CODE, indicators=["ma5"], limit=3, now=context.clock, deps=context.deps()
    )
    assert indicators.metadata["freshness"] == "suspended"
    assert _table(indicators)[-1]["ma5"] is not None


def test_minute_failure_falls_back_to_daily() -> None:
    """rt_min / stk_mins 都失败时回退日线，标注 delayed，并在 notes 留痕。"""
    context = _Deps(
        minute=TushareError("rt_min / stk_mins 均无数据"), clock="2024-01-03 10:10"
    )
    result = M.kline(CODE, period="1min", limit=10, now=context.clock, deps=context.deps())

    _assert_contract(result)
    assert result.metadata["requested_period"] == "1min"
    assert result.metadata["period"] == "day"
    assert result.metadata["freshness"] == "delayed"
    assert result.source == "market:kline:daily"
    notes = " ".join(result.metadata["notes"])
    assert "已回退日线" in notes
    assert "不是盘中实时价" in notes
    assert "延迟" in result.text
    assert result.data_time == "2024-01-03 15:00"


def test_daily_period_during_session_is_not_live() -> None:
    """盘中请求日线：日线收盘后更新，必须标 eod 而不是 live（ADR-018 §1/§4）。"""
    daily = _daily_frame(["2024-01-02", "2024-01-03"])
    context = _Deps(daily=daily, clock="2024-01-03 10:10")
    result = M.kline(CODE, period="day", limit=2, now=context.clock, deps=context.deps())

    _assert_contract(result)
    assert result.metadata["session"] == "open"
    assert result.metadata["freshness"] == "eod"
    assert "收盘后更新" in " ".join(result.metadata["notes"])
    assert "盘中实时" not in result.text


def test_trading_session_buckets() -> None:
    """session 五档取值按 trade_cal + 时钟判定。"""
    context = _Deps()
    deps = context.deps()
    cases = {
        "2024-01-03 09:00": "pre",
        "2024-01-03 09:30": "open",
        "2024-01-03 11:30": "open",
        "2024-01-03 12:00": "lunch",
        "2024-01-03 13:00": "open",
        "2024-01-03 15:00": "open",
        "2024-01-03 15:01": "closed",
        "2024-01-06 10:00": "non_trading_day",
    }
    for moment, expected in cases.items():
        assert M.trading_session(moment, deps=deps) == expected, moment


def test_non_trading_day_answer_never_claims_realtime() -> None:
    """回答文本里不得声称「盘中实时」，必须写「最近交易日收盘」（ADR-018 §2/§3）。"""
    daily = _daily_frame(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    context = _Deps(daily=daily, clock="2024-01-06 10:00")
    result = M.quote(CODE, now=context.clock, deps=context.deps())
    assert "盘中实时" not in result.text
    assert "最近交易日收盘" in result.text
    assert "最新价" in result.text
    # 周五的 bar 在，缺 bar 判据不应把周末误判成停牌。
    assert result.metadata["freshness"] == "eod"
    assert result.metadata["suspended"] is False


# ---------------------------------------------------------------------------
# 指标一致性与 ffill（禁止未来值）
# ---------------------------------------------------------------------------


def test_indicator_matches_astock_indicators() -> None:
    """同一序列上指标值与 astock.indicators 逐值一致。"""
    days = list(pd.bdate_range("2023-01-02", periods=300).strftime("%Y-%m-%d"))
    daily = _daily_frame(days, start=80.0)
    context = _Deps(daily=daily, clock="2024-03-15 16:30")
    names = ["ma20", "ema12", "rsi14", "macd", "boll", "kdj", "atr14"]
    result = M.indicator(CODE, indicators=names, limit=3, now=context.clock, deps=context.deps())
    _assert_contract(result)

    close = daily["close"].astype(float)
    expected = {
        "ma20": ta.sma(close, 20),
        "ema12": ta.ema(close, 12),
        "rsi14": ta.rsi(close, 14),
        **{column: ta.macd(close)[column] for column in ("dif", "dea", "macd_hist")},
        **{column: ta.boll(close)[column] for column in ("boll_mid", "boll_upper", "boll_lower")},
        **{column: ta.kdj(daily)[column] for column in ("k", "d", "j")},
        "atr14": ta.atr(daily, 14),
    }
    row = _table(result)[-1]
    assert set(expected) <= set(row)
    for column, series in expected.items():
        assert row[column] == pytest.approx(float(series.iloc[-1]), rel=1e-9, abs=1e-9), column


def test_intraday_overlay_uses_only_completed_daily_bars() -> None:
    """``ma5@day`` 只用更早已完成的日线（allow_exact_matches=False），禁未来值。"""
    days = [
        "2023-12-26",
        "2023-12-27",
        "2023-12-28",
        "2023-12-29",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    daily = _daily_frame(days, start=10.0)
    minute = pd.concat(
        [
            _minute_frame("2024-01-03", ["09:31", "09:32"]),
            _minute_frame("2024-01-04", ["09:31", "09:32"]),
        ],
        ignore_index=True,
    )
    context = _Deps(daily=daily, minute=minute, clock="2024-01-04 09:33")
    result = M.kline(
        CODE, period="1min", layers=["ma5@day"], limit=10, now=context.clock, deps=context.deps()
    )
    _assert_contract(result)

    ma5 = ta.sma(daily["close"].astype(float), 5)
    day_positions = {
        day: index for index, day in enumerate(daily["trade_date"].dt.strftime("%Y-%m-%d"))
    }
    expected_by_day = {
        # 2024-01-03 的 bar 只能看到 2024-01-02 及更早的日线。
        "2024-01-03": float(ma5.iloc[day_positions["2024-01-02"]]),
        # 2024-01-04 的 bar 只能看到 2024-01-03 及更早的日线。
        "2024-01-04": float(ma5.iloc[day_positions["2024-01-03"]]),
    }
    for row in _table(result):
        day = row["trade_time"][:10]
        assert row["ma5_day"] == pytest.approx(expected_by_day[day], rel=1e-9, abs=1e-9), day

    ffill = result.metadata["ffill"]
    assert ffill["fields"] == ["ma5_day"]
    assert ffill["days"] == ["2024-01-03", "2024-01-04"]
    assert ffill["bars"] == 4
    assert "禁止未来值" in ffill["rule"]


def test_overlay_on_daily_period_is_rejected() -> None:
    """``@day`` 叠加层只用于分钟周期，日线上直接报错说明。"""
    context = _Deps()
    with pytest.raises(ValueError, match="叠加层 @day 只用于分钟 K 线"):
        M.kline(CODE, period="day", layers=["ma20@day"], now=context.clock, deps=context.deps())


# ---------------------------------------------------------------------------
# 图表提示（图题带 freshness）
# ---------------------------------------------------------------------------


class _StubRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def __call__(self, spec: dict[str, Any], **kwargs: Any) -> SimpleNamespace:
        self.calls.append((spec, kwargs))
        return SimpleNamespace(
            html="<html>chart</html>",
            artifact_id="art_" + "0" * 16,
            path="artifacts/chart.html",
            data_as_of=kwargs["data_as_of"],
            data_time=kwargs["data_time"],
            warnings=["stub"],
        )


def test_kline_chart_title_carries_freshness_hint() -> None:
    """``chart=True`` 时图题写明 freshness，且图层映射到 ChartSpec。"""
    daily = _daily_frame(["2024-01-02", "2024-01-03"])
    context = _Deps(daily=daily, clock="2024-01-03 16:30")
    renderer = _StubRenderer()
    result = M.kline(
        CODE,
        layers=["ma5", "boll", "volume"],
        chart=True,
        limit=2,
        now=context.clock,
        deps=context.deps(),
        renderer=renderer,
    )
    _assert_contract(result)
    assert result.figure == "<html>chart</html>"
    spec, kwargs = renderer.calls[0]
    # 工具生成的 spec 必须本身通过冻结校验（不让渲染器兜底）。
    from astock.agent.chart_spec import validate_chart_spec

    assert validate_chart_spec(spec)["title"] == spec["title"]
    assert spec["kind"] == "candlestick"
    assert "最近交易日收盘" in spec["title"]
    assert spec["data"] == {
        "source": "market",
        "ref": CODE,
        "period": "day",
        "range": "2024-01-02:2024-01-03",
        "adj": "qfq",
    }
    assert spec["layers"] == [
        {"type": "ma", "window": 5},
        {"type": "boll", "window": 20},
        {"type": "volume"},
    ]
    # 时点必须透传给渲染器（唯一生产者链路不断）。
    assert (kwargs["data_as_of"], kwargs["data_time"]) == (
        result.data_as_of,
        result.data_time,
    )
    assert result.metadata["chart"]["artifact_id"] == "art_" + "0" * 16
    assert "最近交易日收盘" in result.metadata["chart"]["title"]


def test_kline_surfaces_partial_intraday_factor() -> None:
    """ADR-008 第 7 条：盘中当日 bar 用最近已发布因子，工具必须把 partial 透出来。

    数据层在 ``attrs["adjustment"]`` 标 ``partial``/``factor_as_of``；工具侧若不
    消费，图注/表注就永远看不到"盘中近似"（评审 iss-mwoit6on-7d19f2）。
    """
    minute = _minute_frame("2024-01-03", ["09:31", "09:32"])
    minute.attrs["adjustment"] = {
        "adj": "qfq",
        "adj_base": "2025-01-03",
        "base_factor": 4.0,
        "adj_source": "daily",
        "partial": True,
        "factor_as_of": "2024-01-02",
    }
    context = _Deps(minute=minute, clock="2024-01-03 09:33")
    renderer = _StubRenderer()
    result = M.kline(
        CODE,
        period="1min",
        chart=True,
        limit=2,
        now=context.clock,
        deps=context.deps(),
        renderer=renderer,
    )
    _assert_contract(result)
    assert result.metadata["partial"] is True
    assert result.metadata["factor_as_of"] == "2024-01-02"
    assert "盘中近似" in result.text
    spec, _kwargs = renderer.calls[0]
    assert spec["meta"] == {"partial": True, "factor_as_of": "2024-01-02"}

    from astock.agent.chart_render import _caption

    caption = _caption(spec, result.data_as_of, result.data_time, result.data_as_of, result.data_time)
    assert "盘中近似（因子截至 2024-01-02）" in caption


def test_kline_published_factor_has_no_partial_marker() -> None:
    """因子已发布（当日不是 partial）时不得冒出"盘中近似"。"""
    context = _Deps(minute=_minute_frame("2024-01-03", ["09:31"]), clock="2024-01-03 09:32")
    result = M.kline(CODE, period="1min", limit=1, now=context.clock, deps=context.deps())
    _assert_contract(result)
    assert result.metadata["partial"] is False
    assert result.metadata["factor_as_of"] is None
    assert "盘中近似" not in result.text


def test_kline_without_chart_has_no_figure() -> None:
    context = _Deps()
    result = M.kline(CODE, now=context.clock, deps=context.deps())
    assert not result.figure
    assert "chart" not in result.metadata


# ---------------------------------------------------------------------------
# 周 / 月重采样、指数分支与图表窗口上限（评审 rev-20261118-001）
# ---------------------------------------------------------------------------


def test_kline_week_resamples_and_marks_eod_intraday() -> None:
    """周线走 ``resample_ohlcv("W-FRI")``，时点取周期内最后实际交易日的 15:00。"""
    days = [
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
    ]
    context = _Deps(daily=_daily_frame(days, start=50.0), clock="2024-01-10 10:00")
    result = M.kline(CODE, period="week", limit=10, now=context.clock, deps=context.deps())
    _assert_contract(result)

    rows = _table(result)
    assert len(rows) == 2
    assert rows[-1]["trade_date"] == "2024-01-10 00:00"
    assert result.data_time == "2024-01-10 15:00"
    assert result.metadata["period"] == "week"
    assert result.metadata["requested_period"] == "week"
    # 盘中请求周线：周/月线收盘后才更新，必须 eod 并写明原因。
    assert result.metadata["freshness"] == "eod"
    assert any("收盘后更新" in note for note in result.metadata["notes"])


def test_kline_month_resamples_and_runs_real_render(tmp_path: Any) -> None:
    """月线重采样 + 真实渲染管线：range 钳到 ChartSpec 10 年上限后仍能出图。"""
    from astock.agent import chart_service
    from astock.agent.chart_spec import MAX_RANGE_YEARS, validate_chart_spec

    frames: list[pd.DataFrame] = []

    def spy(spec: dict[str, Any], **kwargs: Any) -> Any:
        frames.append(kwargs["frame"])
        return chart_service.render_chart(spec, **kwargs)

    days = pd.bdate_range("2010-01-04", "2021-12-31").strftime("%Y-%m-%d").tolist()
    context = _Deps(daily=_daily_frame(days, start=10.0), clock="2024-01-03 16:30")
    result = M.kline(
        CODE,
        period="month",
        limit=200,
        chart=True,
        now=context.clock,
        deps=context.deps(),
        db_path=str(tmp_path / "agent.db"),
        artifact_root=str(tmp_path / "artifacts"),
        renderer=spy,
    )
    _assert_contract(result)

    assert result.metadata["period"] == "month"
    assert result.data_time == "2021-12-31 15:00"
    # 真实渲染成功：有 HTML、有落盘 artifact，且窗口没有越过 10 年上限。
    assert result.figure and "<html" in result.figure.lower()
    chart = result.metadata["chart"]
    assert chart["artifact_id"].startswith("art_")
    assert chart["data_time"] == result.data_time
    assert any("10 年上限" in warning for warning in chart["warnings"])
    assert "10 年上限" in result.text

    spec = chart["spec"]
    validate_chart_spec(spec)  # 不抛 ChartSpecError
    start, end = spec["data"]["range"].split(":")
    span_days = (pd.Timestamp(end) - pd.Timestamp(start)).days
    assert span_days <= MAX_RANGE_YEARS * 366
    # 图上只画 10 年内的 bar，表格仍是完整 limit 根。
    assert len(_table(result)) == 144
    assert len(frames[0]) < len(_table(result))
    assert pd.to_datetime(frames[0]["trade_date"]).min() >= pd.Timestamp(start)


def test_kline_index_uses_index_daily() -> None:
    """指数走 ``load_index_daily``：source=index_daily，双时点齐全。"""
    context = _Deps(
        daily=_daily_frame(["2024-01-02", "2024-01-03"]), clock="2024-01-03 16:30"
    )
    result = M.kline(
        "000300.SH", period="day", limit=2, now=context.clock, deps=context.deps()
    )
    _assert_contract(result)

    assert context.index_calls and context.index_calls[0]["ts_code"] == "000300.SH"
    assert result.metadata["source"] == "index_daily"
    assert result.source == "market:kline:index_daily"
    assert result.data_as_of == "2024-01-03"
    assert result.data_time == "2024-01-03 15:00"


def test_index_minute_is_rejected() -> None:
    """指数分钟线属 v1 明确不支持的能力，报错文案要写明。"""
    context = _Deps()
    with pytest.raises(ValueError, match="暂无分钟线"):
        M.kline("000300.SH", period="1min", now=context.clock, deps=context.deps())


def test_kline_chart_spec_error_degrades_to_table() -> None:
    """出图被 ChartSpec 拒绝时降级成表格结果，不把工具打成错误。"""
    from astock.agent.chart_spec import ChartSpecError

    def broken(spec: dict[str, Any], **kwargs: Any) -> Any:
        raise ChartSpecError(
            [{"field": "$.data.range", "reason": "窗口跨度超过 10 年"}]
        )

    context = _Deps(clock="2024-01-03 16:30")
    result = M.kline(
        CODE, chart=True, limit=2, now=context.clock, deps=context.deps(), renderer=broken
    )
    _assert_contract(result)

    assert not result.figure
    assert "图表未生成" in result.metadata["chart"]["warning"]
    assert "图表未生成" in result.text
    assert _table(result)


def test_kline_reports_layers_missing_from_chart() -> None:
    """ema/atr 只能进表格：不上图要在 warnings 与文本里说明，不能静默丢线。"""
    days = pd.bdate_range("2023-12-01", "2024-01-03").strftime("%Y-%m-%d").tolist()
    context = _Deps(daily=_daily_frame(days, start=20.0), clock="2024-01-03 16:30")
    renderer = _StubRenderer()
    result = M.kline(
        CODE,
        layers=["ema12", "atr14", "ma5"],
        chart=True,
        limit=len(days),
        now=context.clock,
        deps=context.deps(),
        renderer=renderer,
    )
    _assert_contract(result)

    spec, _ = renderer.calls[0]
    assert spec["layers"] == [{"type": "ma", "window": 5}]
    warnings = result.metadata["chart"]["warnings"]
    assert any("ema12" in warning for warning in warnings)
    assert any("atr14" in warning for warning in warnings)
    assert "仅表格展示" in result.text
    row = _table(result)[-1]
    assert "ema12" in row and "atr14" in row


def test_kline_reports_truncated_chart_layers() -> None:
    """超过 ChartSpec ``MAX_LAYERS`` 的图层要被截断并说明。"""
    from astock.agent.chart_spec import MAX_LAYERS

    layers = [f"ma{window}" for window in range(5, 5 + MAX_LAYERS + 2)]
    context = _Deps(clock="2024-01-03 16:30")
    renderer = _StubRenderer()
    result = M.kline(
        CODE,
        layers=layers,
        chart=True,
        limit=2,
        now=context.clock,
        deps=context.deps(),
        renderer=renderer,
    )
    _assert_contract(result)

    spec, _ = renderer.calls[0]
    assert len(spec["layers"]) == MAX_LAYERS
    warnings = result.metadata["chart"]["warnings"]
    assert any(f"超过 {MAX_LAYERS} 层上限" in warning for warning in warnings)


def test_quote_handles_long_suspension() -> None:
    """长停牌（最后一根 bar 在窗口之外）也要返回 suspended，而不是「无行情数据」。"""
    days = pd.bdate_range("2023-11-01", "2023-12-15").strftime("%Y-%m-%d").tolist()
    context = _Deps(
        daily=_daily_frame(days, start=100.0, suspended=("2023-12-15",)),
        clock="2024-01-04 16:30",
    )
    result = M.quote(CODE, now=context.clock, deps=context.deps())
    _assert_contract(result)

    assert result.metadata["freshness"] == "suspended"
    assert result.metadata["suspended"] is True
    assert result.data_as_of == "2023-12-15"
    assert "停牌" in result.text and "无成交" in result.text
    assert _table(result)[0]["vol"] == 0.0


def _missing_bar_frame() -> pd.DataFrame:
    """真实停牌形态：停牌日**没有行**，最后一根 bar 仍是停牌前的正常成交日。"""
    days = pd.bdate_range("2023-11-01", "2023-12-15").strftime("%Y-%m-%d").tolist()
    return _daily_frame(days, start=100.0)  # 全部 vol>0，之后的日子一行都没有


# 2023-12-15（最后一根 bar）→ 2024-01-03（今天）之间共有 12 个开市日：
# 12-18..12-29 十个 + 01-01、01-02 两个。日历必须从 2023-11 起，否则 note 数不满。
_SUSPENDED_CALENDAR_START = "2023-11-01"
_SUSPENDED_MISSING_DAYS = 12


def _suspended_calendar() -> pd.DataFrame:
    return _trade_cal(_SUSPENDED_CALENDAR_START, "2024-01-31")


def test_quote_marks_suspended_when_bars_are_missing() -> None:
    """停牌 = 缺 bar：三周没有新 bar 时 quote 必须标 suspended，而不是 eod。"""
    context = _Deps(
        daily=_missing_bar_frame(), clock="2024-01-03 16:30", calendar=_suspended_calendar()
    )
    result = M.quote(CODE, now=context.clock, deps=context.deps())
    _assert_contract(result)

    assert result.metadata["freshness"] == "suspended"
    assert result.metadata["suspended"] is True
    assert result.data_as_of == "2023-12-15"
    assert "停牌" in result.text and "无成交" in result.text
    notes = result.metadata["notes"]
    # 审计数字要精确：缺口天数面向用户，不能只匹配「个交易日无行情」子串。
    assert any(
        f"最后一根 bar 为 2023-12-15，其后 {_SUSPENDED_MISSING_DAYS} 个交易日无行情" in note
        for note in notes
    ), notes
    # 最后一根 bar 是正常成交日，vol 不为 0——停牌判据不能只看 0 量行。
    assert _table(result)[0]["vol"] > 0


def test_suspended_missing_bars_intraday_is_not_just_delayed() -> None:
    """盘中同样是缺 bar 形态：不能只报「延迟」，必须写明停牌/无成交。"""
    context = _Deps(
        daily=_missing_bar_frame(), clock="2024-01-03 10:00", calendar=_suspended_calendar()
    )
    result = M.quote(CODE, now=context.clock, deps=context.deps())
    _assert_contract(result)

    assert result.metadata["freshness"] == "suspended"
    assert result.metadata["session"] == "open"
    assert "停牌" in result.text and "无成交" in result.text
    assert any(
        f"其后 {_SUSPENDED_MISSING_DAYS} 个交易日无行情" in note
        for note in result.metadata["notes"]
    )
    # 盘中停牌也不能说成实时行情。
    assert "盘中实时" not in result.text


def test_closed_session_without_today_bar_is_eod_not_suspended() -> None:
    """收盘后当日日线还没发布属于延迟，不能把正常股票误判成停牌。"""
    days = pd.bdate_range("2023-12-01", "2024-01-03").strftime("%Y-%m-%d").tolist()
    context = _Deps(daily=_daily_frame(days), clock="2024-01-04 16:30")
    result = M.quote(CODE, now=context.clock, deps=context.deps())
    _assert_contract(result)

    assert result.metadata["freshness"] == "eod"
    assert result.metadata["suspended"] is False
    assert "停牌" not in result.text


# ---------------------------------------------------------------------------
# 非目标：不预留 tick / Level-2 伪实时字段
# ---------------------------------------------------------------------------


def test_no_pseudo_realtime_fields() -> None:
    """tick / Level-2 属 v1 非目标：schema 与结果都不得出现伪实时字段。"""
    banned = {"bid", "ask", "bid_price", "ask_price", "level2", "tick", "order_book"}
    for schema in (M.QUOTE_SCHEMA, M.KLINE_SCHEMA, M.INDICATOR_SCHEMA):
        assert not banned & set(schema["properties"])

    context = _Deps(minute=_minute_frame("2024-01-03", ["09:31", "09:32"]), clock="2024-01-03 09:32")
    result = M.quote(CODE, now=context.clock, deps=context.deps())
    assert not banned & set(_table(result)[0])
    assert not banned & set(result.metadata)
