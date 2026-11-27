"""因子面板回归测试（plan-20261103-011 的性能改造证据）。

``build_panel`` 为了把 49 只 × 上百个调仓日的面板构建从 ~15s 降到 ~1s，
把逐 (日期, 股票) 的布尔索引换成了 numpy ``searchsorted``。这里用“旧算法”
（逐次 ``df[trade_date <= date].iloc[-1]`` + ``track._forward_return`` 语义）
做逐值对照，防止性能优化改变 IC 输入。

``run_factor_ic`` 的横截面数据源从 ``load_many`` 换成 ``load_daily_panel``
（直接 scan 按 trade_date 分区的数据集），这里用替身锁住调用契约。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from astock import factors as F
from astock.factors import FACTORS, build_panel

CODES = ("000001.SZ", "600519.SH", "300750.SZ")


def _synthetic_frame(code: str, periods: int = 200, *, enriched: bool = True) -> pd.DataFrame:
    """确定性合成日线（含 enrich 产出的指标列），避免随机性让回归测试飘。"""
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=periods)
    seed = sum(ord(ch) for ch in code)
    steps = np.arange(periods, dtype=float)
    close = 10 + seed / 10 + steps * 0.05 + np.sin(steps / 7 + seed) * 0.3
    vol = 1_000 + (steps % 11) * 37 + seed
    frame = pd.DataFrame(
        {
            "ts_code": code,
            "trade_date": dates,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "pre_close": close - 0.05,
            "vol": vol,
            "amount": vol * close,
        }
    )
    if enriched:
        frame["ma20"] = frame["close"].rolling(20, min_periods=1).mean()
        frame["vol_ma20"] = frame["vol"].rolling(20, min_periods=1).mean()
        frame["rsi14"] = 45.0 + np.cos(steps / 5) * 15
        frame["macd_hist"] = np.sin(steps / 9) * 0.2
        frame["atr14"] = 0.3 + np.abs(np.sin(steps / 4)) * 0.1
    return frame


def _reference_factors(df: pd.DataFrame) -> pd.DataFrame:
    """旧实现：把 11 个因子逐列写回 DataFrame。"""
    x = df.sort_values("trade_date").reset_index(drop=True)
    close = x["close"]
    pct = close.pct_change()
    amount = x["amount"]
    x = x.assign(
        momentum_20=close / close.shift(20) - 1,
        momentum_60=close / close.shift(60) - 1,
        reversal_5=-(close / close.shift(5) - 1),
        volatility_20=pct.rolling(20, min_periods=15).std(),
        volume_ratio=x["vol"] / x["vol_ma20"],
        amount_ratio=amount / amount.rolling(20, min_periods=15).mean(),
        amihud_20=(pct.abs() / amount.replace(0, np.nan)).rolling(20, min_periods=15).mean() * 1e8,
        ma_gap_20=close / x["ma20"] - 1,
        rsi_14=x["rsi14"],
        macd_hist=x["macd_hist"],
        atr_pct=x["atr14"] / close,
    )
    return x


def _reference_panel(
    daily_map: dict[str, pd.DataFrame],
    horizon: int = 5,
    rebalance: int = 5,
    *,
    industry_map: dict[str, str] | None = None,
    universe_by_date: dict[pd.Timestamp, set[str]] | None = None,
) -> pd.DataFrame:
    """旧算法：逐 (调仓日, 股票) 取 ``<= date`` 的最后一行，收益按精确日期入场。"""
    frames = {code: _reference_factors(df) for code, df in daily_map.items() if len(df) >= 80}
    all_dates = sorted({d for frame in frames.values() for d in frame["trade_date"].tail(750)})
    dates = all_dates[60:][:: max(1, int(rebalance))]
    membership_dates = sorted(universe_by_date) if universe_by_date else []
    rows: list[dict[str, Any]] = []
    for date in dates:
        date = pd.Timestamp(date)
        allowed = None
        if membership_dates:
            past = [d for d in membership_dates if d <= date]
            if not past:
                continue
            allowed = universe_by_date.get(past[-1], set())
        for code, frame in frames.items():
            if allowed is not None and code not in allowed:
                continue
            hist = frame[frame["trade_date"] <= date]
            if hist.empty:
                continue
            row = hist.iloc[-1]
            entry_rows = frame[frame["trade_date"] == date]
            if entry_rows.empty:
                continue
            entry = int(entry_rows.index[0])
            exit_pos = entry + horizon
            if exit_pos >= len(frame):
                continue
            base = float(frame["close"].iloc[entry])
            if not base or not np.isfinite(base):
                continue
            fwd = float(frame["close"].iloc[exit_pos]) / base - 1
            if not np.isfinite(fwd):
                continue
            values = [float(row[name]) for name in FACTORS]
            if not all(np.isfinite(v) for v in values):
                continue
            item: dict[str, Any] = {
                "date": date,
                "code": code,
                "fwd": fwd,
                "industry": (industry_map or {}).get(code, "其他") or "其他",
                "size": np.nan,
            }
            item.update(dict(zip(FACTORS, values)))
            rows.append(item)
    return pd.DataFrame(rows)


@pytest.fixture()
def daily_map() -> dict[str, pd.DataFrame]:
    return {code: _synthetic_frame(code) for code in CODES}


def test_build_panel_matches_reference_algorithm(daily_map: dict[str, pd.DataFrame]) -> None:
    expected = _reference_panel(daily_map, horizon=5, rebalance=5)
    actual = build_panel(daily_map, horizon=5, rebalance=5)

    assert not expected.empty
    assert len(actual) == len(expected)
    pd.testing.assert_frame_equal(actual, expected, check_dtype=False, check_exact=False, rtol=1e-12)


def test_build_panel_universe_filter_matches_reference(daily_map: dict[str, pd.DataFrame]) -> None:
    stamps = sorted({d for frame in daily_map.values() for d in frame["trade_date"]})
    universe = {
        pd.Timestamp(stamps[100]): set(CODES[:2]),
        pd.Timestamp(stamps[150]): set(CODES[1:]),
    }
    industry = dict.fromkeys(CODES, "银行")

    expected = _reference_panel(daily_map, universe_by_date=universe, industry_map=industry)
    actual = build_panel(daily_map, industry_map=industry, universe_by_date=universe)

    assert not expected.empty
    assert set(actual["code"]) == set(expected["code"])
    assert set(actual["industry"]) == {"银行"}
    pd.testing.assert_frame_equal(actual, expected, check_dtype=False, check_exact=False, rtol=1e-12)


def test_build_panel_without_enriched_columns_is_empty() -> None:
    """未 enrich 的帧没有 vol_ma20/rsi14 等指标：跳过而不是拿 NaN 混进 IC。"""
    raw = {code: _synthetic_frame(code, enriched=False) for code in CODES}
    assert build_panel(raw).empty
    assert build_panel({}).empty


def test_run_factor_ic_loads_panel_through_dataset_scan(
    daily_map: dict[str, pd.DataFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_factor_ic 必须走 ``load_daily_panel``（分区数据集 scan），不再逐股 load_many。"""
    calls: list[dict[str, Any]] = []
    sentinel = object()

    def fake_panel(codes: Any, **kwargs: Any) -> dict[str, pd.DataFrame]:
        calls.append({"codes": list(codes), **kwargs})
        return {code: daily_map[code] for code in codes}

    monkeypatch.setattr(F, "load_daily_panel", fake_panel)
    monkeypatch.setattr(F, "parse_universe", lambda text: list(CODES))
    monkeypatch.setattr(F, "stock_basic", lambda: pd.DataFrame({"ts_code": list(CODES), "industry": ["银行"] * 3}))
    monkeypatch.setattr(F, "compute_ic", lambda *args, **kwargs: sentinel)
    monkeypatch.setattr(F, "build_panel", lambda *args, **kwargs: pd.DataFrame())

    result = F.run_factor_ic(
        universe_text=",".join(CODES),
        years="3年",
        horizon=5,
        rebalance=5,
        universe_mode="自选池",
        neutralize=False,
    )

    assert result is sentinel
    assert calls and calls[0]["codes"] == list(CODES)
    assert calls[0]["years"] == 3
