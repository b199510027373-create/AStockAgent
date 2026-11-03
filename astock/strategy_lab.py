from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .backtest import CostConfig, make_signals_params, run_backtest
from .optimize import WalkForwardResult, load_data, oos_figure
from .recommend import parse_universe

STRATEGY_GRIDS = {
    "MA双均线": [
        {"fast": fast, "slow": slow}
        for fast in (3, 5, 10, 20)
        for slow in (10, 20, 30, 60)
        if fast < slow
    ],
    "MACD": [
        {"fast": fast, "slow": slow, "signal": signal}
        for fast in (8, 12, 16)
        for slow in (20, 26, 32)
        for signal in (6, 9, 12)
        if fast < slow
    ],
    "RSI反转": [
        {"period": period, "low": low, "high": high}
        for period in (7, 14, 21)
        for low in (20, 30, 40)
        for high in (60, 70, 80)
        if low < high
    ],
}


@dataclass
class PortfolioResult:
    per_stock: pd.DataFrame
    windows: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict
    status: str
    strategy: str
    params: list[dict] = field(default_factory=list)


def _param_text(params: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in params.items())


def _signals(daily_e: pd.DataFrame, strategy: str, params: dict) -> pd.Series:
    return make_signals_params(daily_e, strategy, params)


def grid_strategy(
    daily_e: pd.DataFrame,
    code: str,
    strategy: str,
    params: list[dict],
    benchmark: pd.Series | None,
    metric: str = "sharpe",
) -> pd.DataFrame:
    cfg = CostConfig()
    rows = []
    for p in params:
        signals = _signals(daily_e, strategy, p)
        result = run_backtest(daily_e, signals, code, cfg, benchmark)
        m = result.metrics
        rows.append(
            {
                "参数": _param_text(p),
                "累计收益%": round(m.get("total_return", 0) * 100, 2),
                "年化收益%": round(m.get("annual_return", 0) * 100, 2),
                "最大回撤%": round(m.get("max_drawdown", 0) * 100, 2),
                "夏普": round(m.get("sharpe", 0), 3) if m.get("sharpe") is not None else None,
                "交易次数": m.get("trades", 0),
                "胜率%": round(m.get("win_rate", 0) * 100, 1) if m.get("win_rate") is not None and math.isfinite(m.get("win_rate", np.nan)) else None,
                "超额%": round(m.get("excess_return", 0) * 100, 2) if m.get("excess_return") is not None and math.isfinite(m.get("excess_return", np.nan)) else None,
            }
        )
    grid = pd.DataFrame(rows)
    if grid.empty:
        raise ValueError("参数网格为空")
    key = "夏普" if metric == "sharpe" else "年化收益%"
    return grid.sort_values(key, ascending=False).reset_index(drop=True)


def walk_forward_strategy(
    daily_e: pd.DataFrame,
    code: str,
    strategy: str,
    params: list[dict],
    benchmark: pd.Series | None,
    train_years: float = 1.5,
    test_months: int = 3,
    metric: str = "sharpe",
) -> WalkForwardResult:
    train_bars = max(120, int(250 * float(train_years)))
    test_bars = max(21, int(21 * int(test_months)))
    if len(daily_e) < train_bars + test_bars:
        raise ValueError(f"{code} 历史长度不足一轮 walk-forward")
    if not params:
        raise ValueError("参数网格为空")

    cfg = CostConfig()
    signals_cache = {
        _param_text(p): _signals(daily_e, strategy, p)
        for p in params
    }
    windows: list[dict] = []
    returns = []
    bench_returns = []
    start = 0
    state: dict | None = None
    prev_equity = cfg.initial_cash
    while start + train_bars + test_bars <= len(daily_e):
        train = daily_e.iloc[start : start + train_bars].reset_index(drop=True)
        test = daily_e.iloc[start + train_bars : start + train_bars + test_bars].reset_index(drop=True)
        train_start = train["trade_date"].iloc[0]
        train_end = train["trade_date"].iloc[-1]
        test_start = test["trade_date"].iloc[0]
        test_end = test["trade_date"].iloc[-1]

        best_params: dict[str, Any] | None = None
        best_value: float = float("-inf")
        for p in params:
            key = _param_text(p)
            sig = signals_cache[key].iloc[start : start + train_bars].reset_index(drop=True)
            result = run_backtest(train, sig, code, cfg, benchmark)
            m = result.metrics
            value = m.get("sharpe") if metric == "sharpe" else m.get("annual_return")
            value = float(value) if value is not None and math.isfinite(float(value)) else -9.99
            if best_params is None or value > best_value:
                best_params, best_value = p, value

        if best_params is None:
            break
        key = _param_text(best_params)
        test_sig = signals_cache[key].iloc[
            start + train_bars : start + train_bars + test_bars
        ].reset_index(drop=True)
        prev_signal = int(signals_cache[key].iloc[start + train_bars - 1])
        test_result = run_backtest(
            test,
            test_sig,
            code,
            cfg,
            benchmark,
            initial_state=state,
            prev_signal=prev_signal,
        )
        state = test_result.state
        tm = test_result.metrics
        test_eq = test_result.equity.set_index("trade_date")["equity"]
        test_ret = test_eq.pct_change().dropna()
        if len(test_eq):
            first_ret = float(test_eq.iloc[0]) / prev_equity - 1
            test_ret = pd.concat(
                [pd.Series({test_eq.index[0]: first_ret}), test_ret]
            ).sort_index()
            window_ret = float(test_eq.iloc[-1]) / prev_equity - 1
            prev_equity = float(test_eq.iloc[-1])
        else:
            window_ret = 0.0
        returns.append(test_ret)

        window_bench = None
        if benchmark is not None and not benchmark.empty:
            b = benchmark.reindex(test["trade_date"]).ffill()
            if b.notna().any():
                vals = b.dropna()
                if len(vals) > 1:
                    window_bench = float(vals.iloc[-1]) / float(vals.iloc[0]) - 1
                bench_returns.append(b.pct_change().dropna())

        windows.append(
            {
                "代码": code,
                "窗口": len(windows) + 1,
                "训练区间": f"{train_start:%Y-%m-%d}~{train_end:%Y-%m-%d}",
                "测试区间": f"{test_start:%Y-%m-%d}~{test_end:%Y-%m-%d}",
                "best": key,
                "训练目标": round(best_value, 3) if math.isfinite(best_value) else None,
                "测试收益%": round(window_ret * 100, 2),
                "测试夏普": round(tm.get("sharpe", 0), 3) if tm.get("sharpe") is not None and math.isfinite(tm.get("sharpe", np.nan)) else None,
                "测试回撤%": round(tm.get("max_drawdown", 0) * 100, 2),
                "测试超额%": round((window_ret - window_bench) * 100, 2) if window_bench is not None else None,
            }
        )
        start += test_bars

    if not windows:
        raise ValueError(f"{code} 没有生成 walk-forward 窗口")

    all_ret = pd.concat(returns).sort_index()
    all_ret = all_ret[~all_ret.index.duplicated(keep="first")]
    nav = (1 + all_ret).cumprod()
    bench_nav = None
    if bench_returns:
        br = pd.concat(bench_returns).sort_index()
        br = br[~br.index.duplicated(keep="first")]
        bench_nav = (1 + br).cumprod()

    equity = pd.DataFrame({"trade_date": nav.index, "strategy": nav.values})
    if bench_nav is not None:
        equity = equity.merge(
            pd.DataFrame({"trade_date": bench_nav.index, "benchmark": bench_nav.values}),
            on="trade_date",
            how="left",
        )
    total = nav.iloc[-1] - 1
    annual = (1 + total) ** (252 / len(nav)) - 1 if total > -1 else np.nan
    daily = all_ret
    vol = daily.std(ddof=0) * math.sqrt(252) if len(daily) else np.nan
    sharpe = daily.mean() / daily.std(ddof=0) * math.sqrt(252) if len(daily) and daily.std(ddof=0) > 0 else np.nan
    dd = nav / nav.cummax() - 1
    maxdd = dd.min()
    bench_total = bench_nav.iloc[-1] - 1 if bench_nav is not None and len(bench_nav) else None
    metrics = {
        "total_return": total,
        "annual_return": annual,
        "annual_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": maxdd,
        "benchmark_return": bench_total,
        "excess_return": total - bench_total if bench_total is not None else None,
        "windows": len(windows),
        "train_bars": train_bars,
        "test_bars": test_bars,
        "continuous": True,
    }
    return WalkForwardResult(
        walk=pd.DataFrame(windows),
        equity=equity,
        metrics=metrics,
        code=code,
        years="",
        metric=metric,
        windows=windows,
        state=state or {},
    )


def portfolio_walk_forward(
    codes: list[str],
    strategy: str,
    years: str = "3年",
    train_years: float = 1.5,
    test_months: int = 3,
    metric: str = "sharpe",
    max_assets: int = 10,
) -> PortfolioResult:
    codes = list(dict.fromkeys(codes))[:max_assets]
    if not codes:
        raise ValueError("股票池为空")
    params = STRATEGY_GRIDS[strategy]
    wfs: dict[str, WalkForwardResult] = {}
    benchmarks: dict[str, pd.Series] = {}
    for code in codes:
        daily, bench = load_data(code, years)
        benchmarks[code] = bench
        wfs[code] = walk_forward_strategy(
            daily, code, strategy, params, bench, train_years, test_months, metric
        )

    per_rows = []
    ret_frames = {}
    windows_all = []
    for code, wf in wfs.items():
        m = wf.metrics
        per_rows.append(
            {
                "代码": code,
                "窗口数": m["windows"],
                "样本外累计%": round(m["total_return"] * 100, 2),
                "年化%": round(m["annual_return"] * 100, 2),
                "夏普": round(m["sharpe"], 3) if m.get("sharpe") is not None and math.isfinite(m["sharpe"]) else None,
                "最大回撤%": round(m["max_drawdown"] * 100, 2),
                "基准%": round((m.get("benchmark_return") or 0) * 100, 2),
                "超额%": round((m.get("excess_return") or 0) * 100, 2),
            }
        )
        eq = wf.equity.set_index("trade_date")
        ret_frames[code] = eq["strategy"].pct_change().dropna()
        w = wf.walk.copy()
        w["代码"] = code
        windows_all.append(w)

    ret_df = pd.concat(ret_frames, axis=1).sort_index()
    port_ret = ret_df.mean(axis=1).dropna()
    nav = (1 + port_ret).cumprod()
    bench_nav = None
    bench_full = benchmarks.get(codes[0]) if benchmarks else None
    if bench_full is not None and not bench_full.empty:
        b = bench_full.reindex(nav.index).ffill().dropna()
        b_ret = b.pct_change().dropna()
        b_ret = b_ret.reindex(nav.index).ffill().fillna(0)
        bench_nav = (1 + b_ret).cumprod()

    equity = pd.DataFrame({"trade_date": nav.index, "strategy": nav.values})
    if bench_nav is not None:
        equity = equity.merge(
            pd.DataFrame({"trade_date": bench_nav.index, "benchmark": bench_nav.values}),
            on="trade_date",
            how="left",
        )
    total = nav.iloc[-1] - 1
    annual = (1 + total) ** (252 / len(nav)) - 1 if total > -1 else np.nan
    sharpe = port_ret.mean() / port_ret.std(ddof=0) * math.sqrt(252) if port_ret.std(ddof=0) > 0 else np.nan
    dd = nav / nav.cummax() - 1
    maxdd = dd.min()
    bench_total = bench_nav.iloc[-1] - 1 if bench_nav is not None and len(bench_nav) else None
    metrics = {
        "total_return": total,
        "annual_return": annual,
        "sharpe": sharpe,
        "max_drawdown": maxdd,
        "benchmark_return": bench_total,
        "excess_return": total - bench_total if bench_total is not None else None,
        "assets": len(codes),
        "windows": min(w.metrics["windows"] for w in wfs.values()),
    }
    status = (
        f"{strategy} · {len(codes)} 只 · 股票池等权组合 · {len(params)} 组参数 · "
        f"训练 {train_years} 年 / 测试 {test_months} 月 · 每只最少 {metrics['windows']} 个窗口 · "
        "测试窗口连续持仓。"
    )
    return PortfolioResult(
        per_stock=pd.DataFrame(per_rows),
        windows=pd.concat(windows_all, ignore_index=True),
        equity=equity,
        metrics=metrics,
        status=status,
        strategy=strategy,
        params=params,
    )


def portfolio_figure(result: PortfolioResult):
    return oos_figure(result.equity, f"{result.strategy} 多标的 Walk-forward 等权净值")
