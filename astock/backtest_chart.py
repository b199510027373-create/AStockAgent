from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .backtest import BacktestResult
from .data import non_trading_days

UP = "#EF5350"
DOWN = "#26A69A"


def backtest_figure(
    daily: pd.DataFrame,
    result: BacktestResult,
    code: str,
    strategy: str,
    height: int = 920,
) -> go.Figure:
    df = daily.reset_index(drop=True)
    eq = result.equity.reset_index(drop=True)

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.52, 0.28, 0.20],
    )

    fig.add_trace(
        go.Candlestick(
            x=df["trade_date"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="K线",
            increasing_line_color=UP,
            increasing_fillcolor=UP,
            decreasing_line_color=DOWN,
            decreasing_fillcolor=DOWN,
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    for col, color, label in [("ma5", "#FF8C00", "MA5"), ("ma20", "#2E8B57", "MA20")]:
        if col in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=df["trade_date"],
                    y=df[col],
                    mode="lines",
                    name=label,
                    line=dict(color=color, width=1.2),
                ),
                row=1,
                col=1,
            )

    trades = result.trades
    if trades is not None and not trades.empty:
        fig.add_trace(
            go.Scatter(
                x=pd.to_datetime(trades["买入日期"]),
                y=trades["买入价"],
                mode="markers",
                name="买入",
                marker=dict(
                    symbol="triangle-up",
                    size=11,
                    color=UP,
                    line=dict(width=1, color="#B71C1C"),
                ),
                hovertemplate="买入 %{x|%Y-%m-%d}<br>价格 %{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=pd.to_datetime(trades["卖出日期"]),
                y=trades["卖出价"],
                mode="markers",
                name="卖出",
                marker=dict(
                    symbol="triangle-down",
                    size=11,
                    color=DOWN,
                    line=dict(width=1, color="#00695C"),
                ),
                hovertemplate="卖出 %{x|%Y-%m-%d}<br>价格 %{y:.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    eq_norm = eq["equity"] / eq["equity"].iloc[0] * 100
    fig.add_trace(
        go.Scatter(
            x=eq["trade_date"],
            y=eq_norm,
            mode="lines",
            name="策略净值",
            line=dict(color="#1565C0", width=1.8),
        ),
        row=2,
        col=1,
    )
    if "benchmark" in eq.columns and eq["benchmark"].notna().any():
        b = eq["benchmark"]
        base = b.dropna().iloc[0]
        fig.add_trace(
            go.Scatter(
                x=eq["trade_date"],
                y=b / base * 100 if base else b,
                mode="lines",
                name="沪深300",
                line=dict(color="#9E9E9E", width=1.2, dash="dash"),
            ),
            row=2,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=eq["trade_date"],
            y=eq["drawdown"] * 100,
            mode="lines",
            name="回撤",
            line=dict(color="#C62828", width=1.2),
            fill="tozeroy",
            fillcolor="rgba(198,40,40,0.18)",
        ),
        row=3,
        col=1,
    )

    rb = [
        dict(bounds=["sat", "mon"]),
        dict(values=non_trading_days(df["trade_date"].min(), df["trade_date"].max())),
    ]
    fig.update_xaxes(
        rangebreaks=rb,
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikethickness=1,
        spikecolor="#999999",
        showgrid=True,
        gridcolor="rgba(0,0,0,0.06)",
    )
    fig.update_yaxes(
        side="left",
        showgrid=True,
        gridcolor="rgba(0,0,0,0.06)",
        zeroline=False,
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="净值（起点=100）", row=2, col=1)
    fig.update_yaxes(title_text="回撤 %", ticksuffix="%", row=3, col=1)

    fig.update_layout(
        title=dict(
            text=f"{code} 回测 · {strategy}",
            x=0.005,
            xanchor="left",
            y=0.985,
            yanchor="top",
            font=dict(size=15),
        ),
        height=height,
        template="plotly_white",
        hovermode="x unified",
        dragmode="pan",
        showlegend=True,
        legend=dict(
            orientation="v",
            y=1.0,
            x=1.005,
            xanchor="left",
            yanchor="top",
            font=dict(size=10),
            bgcolor="rgba(255,255,255,0.65)",
        ),
        margin=dict(t=56, b=26, l=64, r=104),
        xaxis_rangeslider_visible=False,
    )
    return fig


def _pct(value, digits: int = 2) -> str:
    try:
        if value is None or pd.isna(value):
            return "—"
        return f"{float(value) * 100:.{digits}f}%"
    except Exception:
        return "—"


def _num(value, digits: int = 2) -> str:
    try:
        if value is None or pd.isna(value):
            return "—"
        return f"{float(value):.{digits}f}"
    except Exception:
        return "—"


def metrics_markdown(result: BacktestResult, strategy: str) -> str:
    m = result.metrics
    has_bench = "benchmark_return" in m

    rows = [
        ("累计收益", _pct(m.get("total_return")), _pct(m.get("benchmark_return")) if has_bench else "—"),
        ("年化收益", _pct(m.get("annual_return")), _pct(m.get("benchmark_annual")) if has_bench else "—"),
        ("最大回撤", _pct(m.get("max_drawdown")), _pct(m.get("benchmark_max_drawdown")) if has_bench else "—"),
        ("年化波动", _pct(m.get("annual_vol")), "—"),
        ("夏普比率", _num(m.get("sharpe")), "—"),
        ("卡玛比率", _num(m.get("calmar")), "—"),
        ("胜率", _pct(m.get("win_rate")), "—"),
        ("盈亏比", _num(m.get("profit_factor")), "—"),
        ("交易次数", str(m.get("trades", 0)), "—"),
        ("平均持仓天数", _num(m.get("avg_holding_days"), 1), "—"),
        ("仓位暴露", _pct(m.get("exposure")), "—"),
        ("Beta", _num(m.get("beta")), "—"),
        ("Alpha（年化）", _pct(m.get("alpha")), "—"),
        ("信息比率", _num(m.get("info_ratio")), "—"),
    ]
    body = "\n".join(f"| {a} | {b} | {c} |" for a, b, c in rows)

    extra = []
    if has_bench:
        extra.append(f"**超额收益：{_pct(m.get('excess_return'))}**（相对沪深300）")
    if result.open_position:
        o = result.open_position
        extra.append(
            f"**当前仍持仓**：{pd.Timestamp(o['entry_date']):%Y-%m-%d} 买入 "
            f"{o['entry_price']:.2f}，{o['shares']} 股，浮盈亏 "
            f"{o['unrealized_pnl']:+.2f} 元（{_pct(o['unrealized_return'])}）"
        )
    extra_text = "\n\n" + "  \n".join(extra) if extra else ""

    return (
        f"### 回测结果 · {strategy}\n\n"
        f"| 指标 | 策略 | 沪深300 |\n|---|---|---|\n{body}{extra_text}\n\n"
        "<sub>信号 t 日收盘生成、t+1 开盘成交；T+1；一字涨停不买、一字跌停不卖；"
        "佣金万 2.5（最低 5 元）、印花税卖出万 5、滑点单边万 5。"
        "指标含未平仓浮盈亏，交易明细只列已平仓。</sub>"
    )
