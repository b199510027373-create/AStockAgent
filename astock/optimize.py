from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .backtest import CostConfig, ma_cross_signals, run_backtest
from .data import enrich, load_daily, load_index_daily

YEAR_DAYS = {"1年": 365, "3年": 365 * 3, "5年": 365 * 5, "全部": None}


@dataclass
class GridResult:
    grid: pd.DataFrame
    best: dict
    code: str
    years: str
    metric: str


@dataclass
class WalkForwardResult:
    walk: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict
    code: str
    years: str
    metric: str
    grid: GridResult | None = None
    windows: list[dict] = field(default_factory=list)


def _load(code: str, years: str):
    end = pd.Timestamp.today().strftime("%Y%m%d")
    days = YEAR_DAYS.get(years)
    start = "20150101" if days is None else (
        pd.Timestamp.today() - pd.Timedelta(days=days)
    ).strftime("%Y%m%d")
    daily = load_daily(code, start=start, end=end, adj="qfq")
    if daily.empty:
        raise ValueError(f"{code} 无日线数据")
    daily_e = enrich(daily)
    bench = load_index_daily("000300.SH", start=start, end=end)
    bench_series = (
        bench.set_index("trade_date")["close"]
        if not bench.empty
        else None
    )
    return daily_e, bench_series


def load_data(code: str, years: str):
    return _load(code, years)


def _params(fast_values, slow_values):
    out = []
    for fast in fast_values:
        for slow in slow_values:
            fast, slow = int(fast), int(slow)
            if 1 < fast < slow:
                out.append((fast, slow))
    return out


def grid_search(
    daily_e: pd.DataFrame,
    code: str,
    fast_values: list[int],
    slow_values: list[int],
    benchmark: pd.Series | None,
    metric: str = "sharpe",
) -> GridResult:
    cfg = CostConfig()
    rows = []
    for fast, slow in _params(fast_values, slow_values):
        signals = ma_cross_signals(daily_e, fast, slow)
        result = run_backtest(daily_e, signals, code, cfg, benchmark)
        m = result.metrics
        rows.append(
            {
                "fast": fast,
                "slow": slow,
                "累计收益%": round(m.get("total_return", 0) * 100, 2),
                "年化收益%": round(m.get("annual_return", 0) * 100, 2),
                "最大回撤%": round(m.get("max_drawdown", 0) * 100, 2),
                "夏普": round(m.get("sharpe", 0), 3) if m.get("sharpe") is not None else None,
                "卡玛": round(m.get("calmar", 0), 3) if m.get("calmar") is not None else None,
                "交易次数": m.get("trades", 0),
                "胜率%": round(m.get("win_rate", 0) * 100, 1) if m.get("win_rate") is not None and math.isfinite(m.get("win_rate", np.nan)) else None,
                "超额%": round(m.get("excess_return", 0) * 100, 2) if m.get("excess_return") is not None and math.isfinite(m.get("excess_return", np.nan)) else None,
            }
        )
    grid = pd.DataFrame(rows)
    if grid.empty:
        raise ValueError("参数网格为空：请检查 fast < slow 的范围")
    key = "夏普" if metric == "sharpe" else "年化收益%"
    grid = grid.sort_values(key, ascending=False).reset_index(drop=True)
    best = grid.iloc[0].to_dict()
    return GridResult(grid=grid, best=best, code=code, years="", metric=metric)


def grid_heatmap(grid: pd.DataFrame, metric: str = "sharpe") -> go.Figure:
    key = "夏普" if metric == "sharpe" else "年化收益%"
    pivot = grid.pivot_table(index="fast", columns="slow", values=key, aggfunc="mean")
    fig = go.Figure(
        data=go.Heatmap(
            z=pivot.values,
            x=[str(c) for c in pivot.columns],
            y=[str(i) for i in pivot.index],
            colorscale="RdYlGn",
            colorbar=dict(title=key),
            hovertemplate="fast=%{y}<br>slow=%{x}<br>" + key + "=%{z:.3f}<extra></extra>",
        )
    )
    fig.update_layout(
        title=f"参数网格（{key}）",
        template="plotly_white",
        height=440,
        xaxis_title="slow",
        yaxis_title="fast",
        margin=dict(t=50, b=40, l=50, r=20),
    )
    return fig


def walk_forward(
    daily_e: pd.DataFrame,
    code: str,
    fast_values: list[int],
    slow_values: list[int],
    benchmark: pd.Series | None,
    train_years: float = 1.5,
    test_months: int = 3,
    metric: str = "sharpe",
) -> WalkForwardResult:
    train_bars = max(120, int(250 * float(train_years)))
    test_bars = max(21, int(21 * int(test_months)))
    combos = _params(fast_values, slow_values)
    if not combos:
        raise ValueError("参数网格为空：请检查 fast < slow 的范围")
    if len(daily_e) < train_bars + test_bars:
        raise ValueError("历史长度不足一轮 walk-forward：请扩大回测区间或缩短训练窗口")

    cfg = CostConfig()
    signals_cache = {
        (fast, slow): ma_cross_signals(daily_e, fast, slow)
        for fast, slow in combos
    }
    key = "夏普" if metric == "sharpe" else "年化收益%"

    windows = []
    returns = []
    bench_returns = []
    start = 0
    while start + train_bars + test_bars <= len(daily_e):
        train = daily_e.iloc[start : start + train_bars].reset_index(drop=True)
        test = daily_e.iloc[start + train_bars : start + train_bars + test_bars].reset_index(drop=True)
        train_start = train["trade_date"].iloc[0]
        train_end = train["trade_date"].iloc[-1]
        test_start = test["trade_date"].iloc[0]
        test_end = test["trade_date"].iloc[-1]

        best = None
        for fast, slow in combos:
            sig = signals_cache[(fast, slow)].iloc[start : start + train_bars].reset_index(drop=True)
            result = run_backtest(train, sig, code, cfg, benchmark)
            m = result.metrics
            value = m.get("sharpe") if metric == "sharpe" else m.get("annual_return")
            if value is None or not math.isfinite(value):
                value = -9.99
            if best is None or value > best["value"]:
                best = {"fast": fast, "slow": slow, "value": value, "train_metrics": m}

        if best is None:
            break
        fast, slow = best["fast"], best["slow"]
        test_sig = signals_cache[(fast, slow)].iloc[
            start + train_bars : start + train_bars + test_bars
        ].reset_index(drop=True)
        test_result = run_backtest(test, test_sig, code, cfg, benchmark)
        tm = test_result.metrics
        test_ret = test_result.equity.set_index("trade_date")["equity"].pct_change().dropna()
        returns.append(test_ret)

        b = None
        if benchmark is not None and not benchmark.empty:
            b = benchmark.reindex(test["trade_date"]).ffill()
            if b.notna().any():
                bench_returns.append(b.pct_change().dropna())

        windows.append(
            {
                "窗口": len(windows) + 1,
                "训练区间": f"{train_start:%Y-%m-%d}~{train_end:%Y-%m-%d}",
                "测试区间": f"{test_start:%Y-%m-%d}~{test_end:%Y-%m-%d}",
                "best_fast": fast,
                "best_slow": slow,
                "训练夏普": round(best["value"], 3) if best["value"] is not None and math.isfinite(best["value"]) else None,
                "测试收益%": round(tm.get("total_return", 0) * 100, 2),
                "测试夏普": round(tm.get("sharpe", 0), 3) if tm.get("sharpe") is not None and math.isfinite(tm.get("sharpe", np.nan)) else None,
                "测试回撤%": round(tm.get("max_drawdown", 0) * 100, 2),
                "测试超额%": round(tm.get("excess_return", 0) * 100, 2) if tm.get("excess_return") is not None and math.isfinite(tm.get("excess_return", np.nan)) else None,
            }
        )
        start += test_bars

    if not windows:
        raise ValueError("没有生成任何 walk-forward 窗口")

    all_ret = pd.concat(returns).sort_index() if returns else pd.Series(dtype=float)
    all_ret = all_ret[~all_ret.index.duplicated(keep="first")]
    nav = (1 + all_ret).cumprod() if len(all_ret) else pd.Series(dtype=float)

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
    total = nav.iloc[-1] - 1 if len(nav) else np.nan
    annual = (1 + total) ** (252 / len(nav)) - 1 if len(nav) and total > -1 else np.nan
    daily = all_ret
    vol = daily.std(ddof=0) * math.sqrt(252) if len(daily) else np.nan
    sharpe = daily.mean() / daily.std(ddof=0) * math.sqrt(252) if len(daily) and daily.std(ddof=0) > 0 else np.nan
    dd = nav / nav.cummax() - 1 if len(nav) else pd.Series(dtype=float)
    maxdd = dd.min() if len(dd) else np.nan
    bench_total = bench_nav.iloc[-1] - 1 if bench_nav is not None and len(bench_nav) else np.nan

    walk = pd.DataFrame(windows)
    metrics = {
        "total_return": total,
        "annual_return": annual,
        "annual_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": maxdd,
        "benchmark_return": bench_total,
        "excess_return": total - bench_total if bench_total is not None and math.isfinite(bench_total) else None,
        "windows": len(windows),
        "test_bars": test_bars,
        "train_bars": train_bars,
    }
    return WalkForwardResult(
        walk=walk,
        equity=equity,
        metrics=metrics,
        code=code,
        years="",
        metric=metric,
        windows=windows,
    )


def oos_figure(equity: pd.DataFrame, title: str = "Walk-forward 样本外净值") -> go.Figure:
    if equity is None or equity.empty:
        return go.Figure()
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=equity["trade_date"],
            y=equity["strategy"],
            mode="lines",
            name="Walk-forward 策略",
            line=dict(color="#1565C0", width=2),
        )
    )
    if "benchmark" in equity.columns:
        fig.add_trace(
            go.Scatter(
                x=equity["trade_date"],
                y=equity["benchmark"],
                mode="lines",
                name="沪深300",
                line=dict(color="#9E9E9E", width=1.2, dash="dash"),
            )
        )
    fig.update_layout(
        title=title,
        template="plotly_white",
        height=420,
        hovermode="x unified",
        margin=dict(t=50, b=30, l=40, r=20),
        legend=dict(orientation="h", y=1.08, x=0),
    )
    fig.update_yaxes(title_text="净值（起点=1）")
    fig.add_hline(y=1, line_color="#999", line_width=1)
    return fig
