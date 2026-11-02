from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .data import non_trading_days

UP = "#EF5350"      # A 股红涨
DOWN = "#26A69A"    # 绿跌
GRID = "rgba(0,0,0,0.06)"

MA_STYLE = {
    "ma5": ("MA5", "#FF8C00", 1.2),
    "ma10": ("MA10", "#FFB300", 1.1),
    "ma20": ("MA20", "#2E8B57", 1.5),
    "ma60": ("MA60", "#8B008B", 1.5),
    "ma120": ("MA120", "#1565C0", 1.6),
    "ma250": ("MA250", "#C62828", 1.6),
}


def _rangebreaks(start, end, intraday: bool = False) -> list[dict]:
    if intraday:
        # 隐藏隔夜与午休：保留 15:00、11:30、13:00 三根边界 K 线
        return [
            dict(bounds=["sat", "mon"]),
            dict(bounds=[15.01, 9.5], pattern="hour"),
            dict(bounds=[11.51, 13], pattern="hour"),
        ]
    return [
        dict(bounds=["sat", "mon"]),
        dict(values=non_trading_days(start, end)),
    ]


def _map_period(period_df: pd.DataFrame, column: str, daily_dates: pd.Series) -> pd.Series:
    if period_df is None or period_df.empty or column not in period_df.columns:
        return pd.Series(np.nan, index=range(len(daily_dates)))
    s = period_df.set_index("trade_date")[column].dropna()
    if s.empty:
        return pd.Series(np.nan, index=range(len(daily_dates)))
    mapped = s.reindex(pd.DatetimeIndex(daily_dates), method="ffill")
    return mapped.reset_index(drop=True)


def empty_figure(text: str = "切换模式后显示") -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=text,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font=dict(size=13, color="#999"),
    )
    fig.update_layout(
        template="plotly_white",
        height=300,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        margin=dict(t=20, b=20, l=20, r=20),
    )
    return fig


def candlestick_figure(
    df: pd.DataFrame,
    title: str,
    *,
    overlay_cols: list[str] | None = None,
    weekly: pd.DataFrame | None = None,
    monthly: pd.DataFrame | None = None,
    show_boll: bool = False,
    show_volume: bool = True,
    show_range_selector: bool = True,
    intraday: bool = False,
    height: int = 700,
) -> go.Figure:
    """单周期 K 线，可叠加日线均线、周线/月线趋势线、布林带。"""
    if df is None or df.empty:
        return empty_figure("无数据")

    x = (
        df["trade_time"]
        if intraday and "trade_time" in df.columns
        else df["trade_date"]
    )

    has_vol = show_volume and "vol" in df.columns
    rows = 2 if has_vol else 1
    row_heights = [0.74, 0.26] if has_vol else [1.0]
    fig = make_subplots(
        rows=rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=row_heights,
    )

    fig.add_trace(
        go.Candlestick(
            x=x,
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

    for col in overlay_cols or []:
        if col not in df.columns or df[col].notna().sum() == 0:
            continue
        label, color, width = MA_STYLE.get(col, (col.upper(), "#607D8B", 1.0))
        fig.add_trace(
            go.Scatter(
                x=x,
                y=df[col],
                mode="lines",
                name=label,
                line=dict(color=color, width=width),
            ),
            row=1,
            col=1,
        )

    if show_boll and {"boll_upper", "boll_lower"}.issubset(df.columns):
        fig.add_trace(
            go.Scatter(
                x=x,
                y=df["boll_upper"],
                mode="lines",
                name="布林上轨",
                line=dict(color="rgba(120,120,120,0.55)", width=1, dash="dash"),
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=x,
                y=df["boll_lower"],
                mode="lines",
                name="布林下轨",
                line=dict(color="rgba(120,120,120,0.55)", width=1, dash="dash"),
                fill="tonexty",
                fillcolor="rgba(120,120,120,0.06)",
            ),
            row=1,
            col=1,
        )

    if weekly is not None and not weekly.empty:
        wma = _map_period(weekly, "ma20", x)
        fig.add_trace(
            go.Scatter(
                x=x,
                y=wma,
                mode="lines",
                name="周线MA20",
                line=dict(color="#FF6F00", width=2.4),
            ),
            row=1,
            col=1,
        )

    if monthly is not None and not monthly.empty:
        m = monthly.copy()
        m["ma12"] = m["close"].rolling(12, min_periods=12).mean()
        mma = _map_period(m, "ma12", x)
        fig.add_trace(
            go.Scatter(
                x=x,
                y=mma,
                mode="lines",
                name="月线MA12",
                line=dict(color="#6A1B9A", width=2.4, dash="dot"),
            ),
            row=1,
            col=1,
        )

    if has_vol:
        colors = [UP if c >= o else DOWN for c, o in zip(df["close"], df["open"])]
        fig.add_trace(
            go.Bar(
                x=x,
                y=df["vol"],
                marker_color=colors,
                marker_line_width=0,
                name="成交量",
                showlegend=False,
            ),
            row=2,
            col=1,
        )
        if "vol_ma20" in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=df["vol_ma20"],
                    mode="lines",
                    name="量MA20",
                    line=dict(color="#FFA726", width=1.4, dash="dot"),
                ),
                row=2,
                col=1,
            )

    rb = _rangebreaks(x.min(), x.max(), intraday=intraday)
    xaxis_kwargs: dict = dict(
        rangebreaks=rb,
        showspikes=True,
        spikemode="across",
        spikesnap="cursor",
        spikethickness=1,
        spikecolor="#999999",
        spikedash="dot",
        showgrid=True,
        gridcolor=GRID,
    )
    if intraday:
        n_days = int(pd.Series(x).dt.normalize().nunique())
        if n_days > 1:
            xaxis_kwargs.update(tickformat="%m-%d %H:%M", nticks=10)
        else:
            xaxis_kwargs.update(tickformat="%H:%M", nticks=8)
    fig.update_xaxes(**xaxis_kwargs)
    fig.update_yaxes(
        side="left",
        showgrid=True,
        gridcolor=GRID,
        zeroline=False,
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    if has_vol:
        fig.update_yaxes(title_text="成交量（手）", row=2, col=1)

    if show_range_selector:
        if intraday:
            n_days = int(pd.Series(x).dt.normalize().nunique())
            if n_days > 1:
                buttons = [
                    dict(count=1, label="1D", step="day", stepmode="backward"),
                    dict(count=3, label="3D", step="day", stepmode="backward"),
                    dict(count=5, label="5D", step="day", stepmode="backward"),
                    dict(step="all", label="ALL"),
                ]
            else:
                buttons = [
                    dict(count=1, label="1H", step="hour", stepmode="backward"),
                    dict(count=2, label="2H", step="hour", stepmode="backward"),
                    dict(count=4, label="4H", step="hour", stepmode="backward"),
                    dict(step="all", label="ALL"),
                ]
        else:
            buttons = [
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(count=1, label="1Y", step="year", stepmode="backward"),
                dict(count=3, label="3Y", step="year", stepmode="backward"),
                dict(count=5, label="5Y", step="year", stepmode="backward"),
                dict(step="all", label="ALL"),
            ]
        fig.update_xaxes(
            rangeselector=dict(
                buttons=buttons,
                x=1,
                xanchor="right",
                y=1.16,
                yanchor="top",
                bgcolor="rgba(250,250,250,0.9)",
                activecolor="#E3F2FD",
                font=dict(size=10),
            ),
            row=1,
            col=1,
        )

    fig.update_layout(
        title=dict(
            text=title,
            x=0.005,
            xanchor="left",
            y=0.985,
            yanchor="top",
            font=dict(size=13 if height < 500 else 15),
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
            borderwidth=0,
        ),
        margin=dict(
            t=58 if show_range_selector else 42,
            b=26,
            l=62,
            r=96 if height < 500 else 104,
        ),
        xaxis_rangeslider_visible=False,
        bargap=0.15,
    )
    return fig


def normalized_compare_figure(
    df: pd.DataFrame,
    code: str,
    windows: dict[str, int] | None = None,
    height: int = 620,
) -> go.Figure:
    """把不同时间区间归一化到同一张图：起点都设为 100。"""
    if df is None or df.empty:
        return empty_figure("无数据")
    windows = windows or {
        "近1月": 20,
        "近3月": 60,
        "近6月": 120,
        "近1年": 250,
        "近3年": 750,
    }
    colors = ["#EF5350", "#FF8C00", "#2E8B57", "#1565C0", "#6A1B9A", "#00838F"]
    fig = go.Figure()
    for i, (label, n) in enumerate(windows.items()):
        if len(df) < max(5, n // 2):
            continue
        sub = df.tail(n).reset_index(drop=True)
        nav = sub["close"] / sub["close"].iloc[0] * 100
        progress = np.linspace(0, 100, len(sub))
        fig.add_trace(
            go.Scatter(
                x=progress,
                y=nav,
                mode="lines",
                name=f"{label}（{len(sub)}日）",
                line=dict(color=colors[i % len(colors)], width=1.8),
                customdata=sub["trade_date"].dt.strftime("%Y-%m-%d"),
                hovertemplate=(
                    "进度 %{x:.0f}%<br>%{customdata}"
                    "<br>净值 %{y:.2f}<extra>%{fullData.name}</extra>"
                ),
            )
        )
    if not fig.data:
        return empty_figure("历史数据不足以做区间对比")
    fig.add_hline(y=100, line_dash="dot", line_color="#999999", line_width=1)
    fig.update_layout(
        title=dict(
            text=f"{code} 多区间归一化对比（起点=100，按区间长度对齐）",
            x=0.005,
            xanchor="left",
            y=0.985,
            yanchor="top",
            font=dict(size=15),
        ),
        height=height,
        template="plotly_white",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            y=1.06,
            x=0,
            xanchor="left",
            font=dict(size=10),
            bgcolor="rgba(255,255,255,0.7)",
        ),
        margin=dict(t=78, b=30, l=20, r=20),
    )
    fig.update_xaxes(
        title_text="区间进度（%），各区间按自身长度展开",
        showgrid=True,
        gridcolor=GRID,
    )
    fig.update_yaxes(
        title_text="归一化净值（起点=100）",
        side="right",
        showgrid=True,
        gridcolor=GRID,
    )
    return fig


def summary_markdown(
    code: str,
    daily: pd.DataFrame,
    weekly: pd.DataFrame,
    monthly: pd.DataFrame,
    start: str,
    end: str,
    adj: str,
) -> str:
    if daily is None or daily.empty:
        return "⚠️ 无数据"
    last = daily.iloc[-1]
    prev = daily.iloc[-2] if len(daily) >= 2 else last

    def num(x, digits=2):
        try:
            x = float(x)
            return "—" if np.isnan(x) else f"{x:.{digits}f}"
        except Exception:
            return "—"

    chg = (last["close"] / prev["close"] - 1) * 100 if prev["close"] else np.nan
    high_250 = daily["high"].tail(250).max()
    low_250 = daily["low"].tail(250).min()
    dist_high = (last["close"] / high_250 - 1) * 100 if high_250 else np.nan
    vol_ratio = last["vol"] / last["vol_ma20"] if last.get("vol_ma20") else np.nan

    day_trend = "多头" if last["close"] > last["ma20"] > last["ma60"] else (
        "空头" if last["close"] < last["ma20"] < last["ma60"] else "震荡"
    )
    week_trend = "—"
    if weekly is not None and not weekly.empty:
        w = weekly.iloc[-1]
        week_trend = "多头" if w["close"] > w["ma20"] > w["ma60"] else (
            "空头" if w["close"] < w["ma20"] < w["ma60"] else "震荡"
        )
    month_trend = "—"
    if monthly is not None and len(monthly) >= 6:
        m = monthly.iloc[-1]
        mma = monthly["close"].tail(6).mean()
        month_trend = "多头" if m["close"] > mma else "空头"

    rows = [
        ("最新收盘", f"{num(last['close'])} 元"),
        ("日涨跌", f"{num(chg)}%"),
        ("MA20 / MA60 / MA250", f"{num(last['ma20'])} / {num(last['ma60'])} / {num(last['ma250'])}"),
        ("RSI14 / MACD柱", f"{num(last['rsi14'], 1)} / {num(last['macd_hist'], 3)}"),
        ("距 52 周高点", f"{num(dist_high)}%"),
        ("52 周区间", f"{num(low_250)} ~ {num(high_250)}"),
        ("量比（对20日均量）", f"{num(vol_ratio)}x"),
        ("日 / 周 / 月趋势", f"{day_trend} / {week_trend} / {month_trend}"),
    ]
    body = "\n".join(f"| {k} | {v} |" for k, v in rows)
    return (
        f"### {code} 多周期概览\n\n"
        f"| 指标 | 数值 |\n|---|---|\n{body}\n\n"
        f"<sub>区间 {pd.Timestamp(start):%Y-%m-%d} ~ {pd.Timestamp(end):%Y-%m-%d}，"
        f"{'前复权' if adj == 'qfq' else '不复权'}，共 {len(daily)} 根日线</sub>"
    )
