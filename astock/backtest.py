from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ta
from .data import enrich, load_daily, load_index_daily


@dataclass
class CostConfig:
    initial_cash: float = 500_000.0
    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty: float = 0.0005       # 卖出印花税
    slippage: float = 0.0005         # 单边滑点
    lot_size: int = 100
    risk_free_rate: float = 0.02


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, Any]
    signals: pd.Series
    config: CostConfig
    open_position: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)


def limit_pct(code: str, name: str = "") -> float:
    """涨跌停比例；ST 5%，创业板/科创板 20%，北交所 30%。"""
    if "ST" in str(name).upper():
        return 0.05
    code = code.upper()
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("8", "4")):
        return 0.30
    return 0.10


def _one_price_limit(row: pd.Series, pct: float, side: str, list_date=None) -> bool:
    """一字涨/跌停：全天只有一个价格，无法成交。

    优先使用未复权原始价；复权价只用于收益，不用于交易所限价。
    新股上市首日通常不设涨跌幅，直接视为可成交。
    """
    if list_date is not None and pd.notna(list_date):
        try:
            if pd.Timestamp(row.get("trade_date")).normalize() == pd.Timestamp(list_date).normalize():
                return False
        except Exception:
            pass
    pre = row.get("raw_pre_close", row.get("pre_close"))
    high = row.get("raw_high", row.get("high"))
    low = row.get("raw_low", row.get("low"))
    if pd.isna(pre) or pre <= 0:
        return False
    if high != low:
        return False
    if side == "buy":
        limit_price = round(float(pre) * (1 + pct), 2)
        return float(low) >= limit_price - 0.005
    limit_price = round(float(pre) * (1 - pct), 2)
    return float(high) <= limit_price + 0.005


def run_backtest(
    df: pd.DataFrame,
    signals: pd.Series,
    code: str,
    config: CostConfig | None = None,
    benchmark: pd.Series | None = None,
    name: str = "",
    list_date=None,
    initial_state: dict[str, Any] | None = None,
    prev_signal: int | None = None,
) -> BacktestResult:
    """日线、long-only、信号次日开盘成交的轻量回测。

    ``initial_state`` / ``prev_signal`` 用于 Walk-forward 跨窗口连续持仓：
    传入上一段的现金、持股、成本价和最后信号，测试窗口首日开盘继续执行，
    而不是每段重置现金/空仓。

    交易规则：
    - t 日收盘生成信号，t+1 日开盘成交（避免未来函数）
    - T+1：当日买入不能当日卖出
    - 一字涨停不能买、一字跌停不能卖
    - 佣金双边万 2.5、最低 5 元；印花税卖出万 5；滑点单边万 5
    - 按 100 股整数倍交易，满仓
    """
    cfg = config or CostConfig()
    if df is None or df.empty:
        raise ValueError("回测数据为空")

    data = df.reset_index(drop=True).copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    sig = (
        pd.Series(signals)
        .reset_index(drop=True)
        .reindex(data.index)
        .fillna(0)
        .astype(int)
        .to_numpy()
    )

    pct = limit_pct(code, name)
    initial_state = initial_state or {}
    cash = float(initial_state.get("cash", cfg.initial_cash))
    shares = int(initial_state.get("shares", 0) or 0)
    position = int(initial_state.get("position", 1 if shares > 0 else 0))
    entry_price = float(initial_state.get("entry_price", 0.0) or 0.0)
    entry_date = initial_state.get("entry_date")
    entry_cost = float(initial_state.get("entry_cost", 0.0) or 0.0)
    trades: list[dict[str, Any]] = list(initial_state.get("trades", []))
    equity_rows: list[dict[str, Any]] = []

    for i, row in data.iterrows():
        # 常规回测：执行前一日收盘信号；连续 WF：首日执行上一窗口最后信号
        target = sig[i - 1] if i > 0 else prev_signal
        if target is not None:
            has_volume = pd.notna(row.get("vol")) and float(row["vol"]) > 0
            if target == 1 and position == 0 and has_volume and not _one_price_limit(row, pct, "buy", list_date):
                price = float(row["open"]) * (1 + cfg.slippage)
                lots = int(cash // (price * cfg.lot_size * (1 + cfg.commission_rate)))
                while lots > 0:
                    amount = lots * cfg.lot_size * price
                    fee = max(cfg.min_commission, amount * cfg.commission_rate)
                    if amount + fee <= cash:
                        break
                    lots -= 1
                if lots > 0:
                    amount = lots * cfg.lot_size * price
                    fee = max(cfg.min_commission, amount * cfg.commission_rate)
                    cash -= amount + fee
                    shares = lots * cfg.lot_size
                    position = 1
                    entry_price = price
                    entry_date = row["trade_date"]
                    entry_cost = amount + fee

            elif target == 0 and position == 1 and has_volume and not _one_price_limit(row, pct, "sell", list_date):
                price = float(row["open"]) * (1 - cfg.slippage)
                amount = shares * price
                fee = max(cfg.min_commission, amount * cfg.commission_rate)
                stamp = amount * cfg.stamp_duty
                proceeds = amount - fee - stamp
                cash += proceeds
                pnl = proceeds - entry_cost
                trades.append(
                    {
                        "买入日期": pd.Timestamp(entry_date),
                        "买入价": round(entry_price, 3),
                        "卖出日期": pd.Timestamp(row["trade_date"]),
                        "卖出价": round(price, 3),
                        "股数": int(shares),
                        "持有天数": int((row["trade_date"] - entry_date).days),
                        "净盈亏": round(pnl, 2),
                        "收益率": round(pnl / entry_cost * 100, 2) if entry_cost else np.nan,
                    }
                )
                shares = 0
                position = 0
                entry_price = 0.0
                entry_date = None
                entry_cost = 0.0

        equity_rows.append(
            {
                "trade_date": row["trade_date"],
                "equity": cash + shares * float(row["close"]),
                "position": position,
            }
        )

    equity = pd.DataFrame(equity_rows)
    equity["peak"] = equity["equity"].cummax()
    equity["drawdown"] = equity["equity"] / equity["peak"] - 1

    bench = None
    if benchmark is not None and not benchmark.empty:
        b = benchmark.copy()
        b.index = pd.to_datetime(b.index)
        b = b.sort_index()
        bench = b.reindex(data["trade_date"], method="ffill")
        bench = bench / bench.dropna().iloc[0] * cfg.initial_cash if bench.notna().any() else None
        if bench is not None:
            equity["benchmark"] = bench.to_numpy()

    trades_df = pd.DataFrame(trades)
    metrics = _metrics(equity, trades_df, cfg)

    open_info: dict[str, Any] = {}
    if position == 1:
        last = data.iloc[-1]
        market_value = shares * float(last["close"])
        open_info = {
            "entry_date": entry_date,
            "entry_price": entry_price,
            "shares": shares,
            "market_value": market_value,
            "unrealized_pnl": market_value - entry_cost,
            "unrealized_return": market_value / entry_cost - 1 if entry_cost else np.nan,
        }
    state = {
        "cash": cash,
        "shares": shares,
        "position": position,
        "entry_price": entry_price,
        "entry_date": entry_date,
        "entry_cost": entry_cost,
        "trades": trades,
    }
    return BacktestResult(
        equity=equity,
        trades=trades_df,
        metrics=metrics,
        signals=pd.Series(sig, index=data["trade_date"], name="signal"),
        config=cfg,
        open_position=open_info,
        state=state,
    )


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, cfg: CostConfig) -> dict[str, Any]:
    curve = equity["equity"]
    n = len(curve)
    total = curve.iloc[-1] / curve.iloc[0] - 1 if curve.iloc[0] else np.nan
    years = n / 252 if n else np.nan
    annual = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1 if years and years > 0 else np.nan
    curve_indexed = pd.Series(curve.to_numpy(), index=pd.to_datetime(equity["trade_date"]))
    daily = curve_indexed.pct_change().dropna()
    vol = daily.std(ddof=0) * np.sqrt(252) if len(daily) else np.nan
    excess_daily = daily - cfg.risk_free_rate / 252
    sharpe = (
        excess_daily.mean() / daily.std(ddof=0) * np.sqrt(252)
        if len(daily) and daily.std(ddof=0) > 0
        else np.nan
    )
    maxdd = equity["drawdown"].min() if len(equity) else np.nan
    calmar = annual / abs(maxdd) if maxdd and maxdd < 0 else np.nan

    wins = trades[trades["净盈亏"] > 0] if not trades.empty else trades
    losses = trades[trades["净盈亏"] < 0] if not trades.empty else trades
    win_rate = len(wins) / len(trades) if len(trades) else np.nan
    gross_profit = wins["净盈亏"].sum() if len(wins) else 0.0
    gross_loss = abs(losses["净盈亏"].sum()) if len(losses) else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.nan
    avg_hold = trades["持有天数"].mean() if not trades.empty else np.nan
    exposure = equity["position"].mean() if "position" in equity else np.nan

    metrics: dict[str, Any] = {
        "initial_cash": cfg.initial_cash,
        "final_equity": curve.iloc[-1],
        "total_return": total,
        "annual_return": annual,
        "annual_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": maxdd,
        "calmar": calmar,
        "trades": int(len(trades)),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_holding_days": avg_hold,
        "exposure": exposure,
        "bars": n,
    }

    if "benchmark" in equity and equity["benchmark"].notna().any():
        bench = equity["benchmark"].copy()
        bench.index = pd.to_datetime(equity["trade_date"])
        bench = bench.dropna()
        bench_total = bench.iloc[-1] / bench.iloc[0] - 1 if bench.iloc[0] else np.nan
        bench_ann = (1 + bench_total) ** (252 / len(bench)) - 1 if len(bench) else np.nan
        bench_dd = (bench / bench.cummax() - 1).min()
        metrics["benchmark_return"] = bench_total
        metrics["benchmark_annual"] = bench_ann
        metrics["benchmark_max_drawdown"] = bench_dd
        metrics["excess_return"] = total - bench_total
        bench_daily = bench.pct_change().dropna()
        joined = pd.concat([daily, bench_daily], axis=1, join="inner").dropna()
        if len(joined) > 2:
            b = joined.iloc[:, 1]
            d = joined.iloc[:, 0]
            var_b = b.var(ddof=0)
            beta = d.cov(b, ddof=0) / var_b if var_b > 0 else np.nan
            alpha = annual - (cfg.risk_free_rate + beta * (bench_ann - cfg.risk_free_rate)) if pd.notna(beta) else np.nan
            diff = d - b
            info_ratio = diff.mean() / diff.std(ddof=0) * np.sqrt(252) if diff.std(ddof=0) > 0 else np.nan
            metrics.update(
                {"beta": beta, "alpha": alpha, "info_ratio": info_ratio}
            )
    return metrics


def ma_cross_signals(df: pd.DataFrame, fast: int = 5, slow: int = 20) -> pd.Series:
    f = df["close"].rolling(fast, min_periods=fast).mean()
    s = df["close"].rolling(slow, min_periods=slow).mean()
    sig = (f > s).astype(int)
    sig[s.isna()] = 0
    return sig


def macd_signals(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    m = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
    sig = (m["dif"] > m["dea"]).astype(int)
    sig[m["dea"].isna()] = 0
    return sig


def rsi_reversal_signals(
    df: pd.DataFrame, low: float = 30, high: float = 70, period: int = 14
) -> pd.Series:
    r = ta.rsi(df["close"], period)
    state = 0
    out: list[int] = []
    for value in r:
        if pd.isna(value):
            out.append(0)
            continue
        if state == 0 and value < low:
            state = 1
        elif state == 1 and value > high:
            state = 0
        out.append(state)
    return pd.Series(out, index=df.index)


def boll_breakout_signals(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> pd.Series:
    b = ta.boll(df["close"], n, k)
    state = 0
    out: list[int] = []
    for close, upper, mid in zip(df["close"], b["boll_upper"], b["boll_mid"]):
        if pd.isna(upper) or pd.isna(mid):
            out.append(0)
            continue
        if state == 0 and close > upper:
            state = 1
        elif state == 1 and close < mid:
            state = 0
        out.append(state)
    return pd.Series(out, index=df.index)


STRATEGIES = ["MA双均线", "MACD", "RSI反转", "布林突破", "买入持有"]


def make_signals_params(df: pd.DataFrame, strategy: str, params: dict) -> pd.Series:
    p = dict(params or {})
    if strategy == "MA双均线":
        return ma_cross_signals(df, int(p.get("fast", 5)), int(p.get("slow", 20)))
    if strategy == "MACD":
        return macd_signals(
            df,
            fast=int(p.get("fast", 12)),
            slow=int(p.get("slow", 26)),
            signal=int(p.get("signal", 9)),
        )
    if strategy == "RSI反转":
        return rsi_reversal_signals(
            df,
            low=float(p.get("low", 30)),
            high=float(p.get("high", 70)),
            period=int(p.get("period", 14)),
        )
    if strategy == "布林突破":
        return boll_breakout_signals(df, n=int(p.get("n", 20)), k=float(p.get("k", 2.0)))
    if strategy == "买入持有":
        return pd.Series(1, index=df.index)
    raise ValueError(f"未知策略: {strategy}")


def make_signals(
    df: pd.DataFrame,
    strategy: str,
    fast: int = 5,
    slow: int = 20,
    **kwargs,
) -> pd.Series:
    if strategy == "MA双均线":
        return ma_cross_signals(df, fast, slow)
    if strategy == "MACD":
        return macd_signals(
            df,
            fast=int(kwargs.get("fast", 12)),
            slow=int(kwargs.get("slow", 26)),
            signal=int(kwargs.get("signal", 9)),
        )
    if strategy == "RSI反转":
        return rsi_reversal_signals(
            df,
            low=float(kwargs.get("low", 30)),
            high=float(kwargs.get("high", 70)),
            period=int(kwargs.get("period", 14)),
        )
    if strategy == "布林突破":
        return boll_breakout_signals(
            df,
            n=int(kwargs.get("n", 20)),
            k=float(kwargs.get("k", 2.0)),
        )
    if strategy == "买入持有":
        return pd.Series(1, index=df.index)
    raise ValueError(f"未知策略: {strategy}")


def run_strategy(
    code: str,
    strategy: str,
    years: str = "3年",
    fast: int = 5,
    slow: int = 20,
    initial_cash: float = 500_000.0,
    name: str = "",
    list_date=None,
) -> tuple[BacktestResult, pd.DataFrame]:
    year_days = {"1年": 365, "3年": 365 * 3, "5年": 365 * 5, "全部": None}
    end = pd.Timestamp.today().strftime("%Y%m%d")
    days = year_days.get(years)
    start = "20150101" if days is None else (
        pd.Timestamp.today() - pd.Timedelta(days=days)
    ).strftime("%Y%m%d")

    daily = load_daily(code, start=start, end=end, adj="qfq")
    if daily.empty:
        raise ValueError(f"{code} 无数据")
    daily_e = enrich(daily)
    signals = make_signals(daily_e, strategy, fast, slow)

    try:
        benchmark_df = load_index_daily("000300.SH", start=start, end=end)
    except Exception:
        benchmark_df = pd.DataFrame()
    benchmark = None
    if not benchmark_df.empty:
        benchmark = benchmark_df.set_index("trade_date")["close"]
    cfg = CostConfig(initial_cash=initial_cash)
    result = run_backtest(daily_e, signals, code, cfg, benchmark, name=name, list_date=list_date)
    return result, daily_e
