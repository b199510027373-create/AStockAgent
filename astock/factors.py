from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .config import BASE_DIR
from .data import load_trade_cal
from .events import stock_basic
from .recommend import load_many, parse_universe
from .track import _forward_return
from .universe import latest_size_map, market_cap_universe, pit_market_cap_universe
from .stats import benjamini_hochberg, effective_n, student_t_p

FACTORS = {
    "momentum_20": "20日动量",
    "momentum_60": "60日动量",
    "reversal_5": "5日反转",
    "volatility_20": "20日波动",
    "volume_ratio": "量比（20日）",
    "amount_ratio": "成交额比（20日）",
    "amihud_20": "Amihud非流动性",
    "ma_gap_20": "MA20乖离",
    "rsi_14": "RSI14",
    "macd_hist": "MACD柱",
    "atr_pct": "ATR占比",
}
HORIZONS = (5, 20)


@dataclass
class FactorResult:
    ic_table: pd.DataFrame
    quantile_table: pd.DataFrame
    ic_series: pd.DataFrame
    panel: pd.DataFrame
    status: str
    horizon: int
    factors: list[str] = field(default_factory=list)
    neutralized: bool = False


def _normal_p(t: float) -> float:
    return float(2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))))


def _add_factors(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    out["momentum_20"] = close / close.shift(20) - 1
    out["momentum_60"] = close / close.shift(60) - 1
    out["reversal_5"] = -(close / close.shift(5) - 1)
    out["volatility_20"] = close.pct_change().rolling(20, min_periods=15).std()
    out["volume_ratio"] = out["vol"] / out["vol_ma20"]
    amount_ma = out["amount"].rolling(20, min_periods=15).mean()
    out["amount_ratio"] = out["amount"] / amount_ma
    out["amihud_20"] = (
        (close.pct_change().abs() / out["amount"].replace(0, np.nan))
        .rolling(20, min_periods=15)
        .mean()
        * 1e8
    )
    out["ma_gap_20"] = close / out["ma20"] - 1
    out["rsi_14"] = out["rsi14"]
    out["atr_pct"] = out["atr14"] / close
    return out


def build_panel(
    daily_map: dict[str, pd.DataFrame],
    horizon: int = 5,
    rebalance: int = 5,
    industry_map: dict[str, str] | None = None,
    size_map: dict[str, float] | None = None,
    universe_by_date: dict[pd.Timestamp, set[str]] | None = None,
    size_by_date: dict[pd.Timestamp, dict[str, float]] | None = None,
) -> pd.DataFrame:
    if not daily_map:
        return pd.DataFrame()
    prepared: dict[str, pd.DataFrame] = {}
    all_dates = None
    for code, df in daily_map.items():
        if df is None or len(df) < 80:
            continue
        x = _add_factors(df).sort_values("trade_date").reset_index(drop=True)
        prepared[code] = x
        dates = set(x["trade_date"].tail(250 * 3))
        all_dates = dates if all_dates is None else all_dates | dates
    if not prepared or not all_dates:
        return pd.DataFrame()

    dates = sorted(all_dates)
    dates = dates[60:]
    dates = dates[:: max(1, int(rebalance))]
    rows = []
    membership_dates = sorted(universe_by_date) if universe_by_date else []
    for date in dates:
        date = pd.Timestamp(date)
        allowed = None
        current_sizes = size_map or {}
        if membership_dates:
            past = [d for d in membership_dates if d <= date]
            if not past:
                continue
            mdate = past[-1]
            allowed = universe_by_date.get(mdate, set())
            current_sizes = (size_by_date or {}).get(mdate, current_sizes)
        for code, df in prepared.items():
            if allowed is not None and code not in allowed:
                continue
            idx = df.index[df["trade_date"] <= date]
            if len(idx) == 0:
                continue
            row = df.iloc[int(idx[-1])]
            fwd = _forward_return(df, date, horizon)
            if fwd is None or not math.isfinite(fwd):
                continue
            item = {
                "date": date,
                "code": code,
                "fwd": fwd,
                "industry": (industry_map or {}).get(code, "其他") or "其他",
                "size": current_sizes.get(code, np.nan),
            }
            ok = True
            for factor in FACTORS:
                value = row.get(factor)
                if value is None or not math.isfinite(float(value)):
                    ok = False
                    break
                item[factor] = float(value)
            if ok:
                rows.append(item)
    return pd.DataFrame(rows)


def _rank_corr(x, y) -> float | None:
    xs = pd.Series(x, dtype=float).rank(method="average")
    ys = pd.Series(y, dtype=float).rank(method="average")
    if xs.std(ddof=0) == 0 or ys.std(ddof=0) == 0:
        return None
    value = xs.corr(ys)
    return float(value) if pd.notna(value) else None


def _rank_ic(values: pd.Series, fwd: pd.Series) -> float | None:
    x = pd.to_numeric(values, errors="coerce")
    y = pd.to_numeric(fwd, errors="coerce")
    mask = x.notna() & y.notna()
    if mask.sum() < 10:
        return None
    x = x[mask].rank(method="average")
    y = y[mask].rank(method="average")
    if x.std(ddof=0) == 0 or y.std(ddof=0) == 0:
        return None
    return float(x.corr(y))


def _neutralize(sub: pd.DataFrame, factor: str) -> pd.Series:
    """横截面残差：对行业哑变量 + log(市值) 回归，取残差。"""
    y = pd.to_numeric(sub[factor], errors="coerce")
    mask = y.notna() & sub["fwd"].notna()
    if mask.sum() < 20 or "industry" not in sub.columns:
        return y
    industry = sub.loc[mask, "industry"].fillna("其他").astype(str)
    dummies = pd.get_dummies(industry, drop_first=True, dtype=float)
    size = pd.to_numeric(sub.loc[mask, "size"], errors="coerce").clip(lower=1)
    log_size = np.log(size)
    med = log_size.median()
    log_size = log_size.fillna(med if pd.notna(med) else 0.0)
    X = pd.concat([dummies, log_size.rename("log_size")], axis=1)
    X.insert(0, "const", 1.0)
    X = X.fillna(0.0)
    try:
        beta = np.linalg.lstsq(X.to_numpy(dtype=float), y[mask].to_numpy(dtype=float), rcond=None)[0]
        resid = y[mask].to_numpy(dtype=float) - X.to_numpy(dtype=float) @ beta
        out = pd.Series(np.nan, index=sub.index)
        out.loc[mask] = resid
        return out
    except Exception:
        return y


def compute_ic(
    panel: pd.DataFrame,
    horizon: int = 5,
    neutralize: bool = True,
    rebalance: int = 5,
) -> FactorResult:
    if panel is None or panel.empty:
        raise ValueError("因子面板为空：股票数量或历史长度不足")
    panel = panel.dropna(subset=["fwd"]).copy()
    active = [
        f for f in FACTORS
        if f in panel.columns and panel[f].notna().sum() >= 10
    ]
    if not active:
        raise ValueError("因子面板中没有可用因子列")
    ic_series = pd.DataFrame(index=sorted(panel["date"].unique()))
    quantile_records: dict[str, list[pd.Series]] = {f: [] for f in active}
    rows = []
    overlap = max(1, int(math.ceil(horizon / max(1, int(rebalance)))))

    for factor in active:
        ics = []
        for date, sub in panel.groupby("date"):
            sub = sub.dropna(subset=[factor, "fwd"])
            if len(sub) < 10:
                continue
            values = _neutralize(sub, factor) if neutralize else sub[factor]
            ic = _rank_ic(values, sub["fwd"])
            if ic is None or not math.isfinite(ic):
                continue
            ics.append((date, ic))
            rank = values.rank(method="first")
            try:
                buckets = pd.qcut(rank, 5, labels=False, duplicates="drop")
            except Exception:
                continue
            q = sub.groupby(buckets)["fwd"].mean().reindex(range(5))
            if q.notna().sum() >= 3:
                quantile_records[factor].append(q)

        if ics:
            series = pd.Series({date: ic for date, ic in ics})
            ic_series[factor] = series
            arr = series.dropna().to_numpy(dtype=float)
            mean = float(arr.mean())
            std = float(arr.std(ddof=1)) if len(arr) > 1 else float("nan")
            icir = mean / std if std and math.isfinite(std) else float("nan")
            t = mean / (std / math.sqrt(len(arr))) if std and math.isfinite(std) else float("nan")
            eff = effective_n(len(arr), overlap)
            p_value = student_t_p(t, max(1.0, eff - 1)) if eff >= 10 and math.isfinite(t) else None
            rows.append(
                {
                    "因子": FACTORS[factor],
                    "IC均值": round(mean, 4),
                    "IC标准差": round(std, 4) if math.isfinite(std) else None,
                    "ICIR": round(icir, 3) if math.isfinite(icir) else None,
                    "IC>0占比%": round(float((arr > 0).mean() * 100), 1),
                    "t值": round(t, 2) if math.isfinite(t) else None,
                    "p值": round(p_value, 4) if p_value is not None else None,
                    "样本日数": len(arr),
                    "有效独立样本": round(eff, 1),
                }
            )
        else:
            ic_series[factor] = np.nan
            rows.append({"因子": FACTORS[factor], "IC均值": None, "样本日数": 0})

    ic_table = pd.DataFrame(rows)
    ic_table["_abs_icir"] = ic_table["ICIR"].abs().fillna(0)
    ic_table = ic_table.sort_values("_abs_icir", ascending=False).drop(columns=["_abs_icir"]).reset_index(drop=True)
    if "p值" in ic_table.columns:
        adjusted = benjamini_hochberg(list(ic_table["p值"]))
        ic_table["p值(BH)"] = [
            round(x, 4) if x is not None and math.isfinite(float(x)) else None
            for x in adjusted
        ]

    qrows = []
    for factor, records in quantile_records.items():
        if not records:
            continue
        qdf = pd.concat(records, axis=1)
        means = qdf.mean(axis=1)
        long_short = means.get(4, np.nan) - means.get(0, np.nan)
        mono = _rank_corr(means.values, np.arange(len(means)))
        qrows.append(
            {
                "因子": FACTORS[factor],
                "Q1(低)": round(float(means.get(0, np.nan)) * 100, 2),
                "Q2": round(float(means.get(1, np.nan)) * 100, 2),
                "Q3": round(float(means.get(2, np.nan)) * 100, 2),
                "Q4": round(float(means.get(3, np.nan)) * 100, 2),
                "Q5(高)": round(float(means.get(4, np.nan)) * 100, 2),
                "Q5-Q1%": round(float(long_short) * 100, 2),
                "单调性": round(float(mono), 3) if mono is not None and math.isfinite(float(mono)) else None,
                "样本日数": len(records),
            }
        )
    quantile_table = pd.DataFrame(qrows)
    if not quantile_table.empty:
        quantile_table = quantile_table.sort_values("Q5-Q1%", ascending=False).reset_index(drop=True)

    usable = int(ic_table["样本日数"].fillna(0).max()) if not ic_table.empty else 0
    max_eff = float(ic_table["有效独立样本"].fillna(0).max()) if "有效独立样本" in ic_table.columns and not ic_table.empty else 0.0
    neutral_text = "行业+市值中性化" if neutralize else "原始因子"
    status = (
        f"因子 {len(active)} 个 · {neutral_text} · 横截面样本 {len(panel)} 行 · "
        f"IC 日数最多 {usable} 天 · 重叠倍数 {overlap} · 有效独立样本最多 {max_eff:.1f} · "
        f"前向窗口 T+{horizon}。"
    )
    if max_eff < 10:
        status += " 有效独立样本 <10，p 值已隐藏。"
    status += " 股票池较小时 IC 噪声较大，仅作研究。"
    return FactorResult(
        ic_table=ic_table,
        quantile_table=quantile_table,
        ic_series=ic_series,
        panel=panel,
        status=status,
        horizon=horizon,
        factors=active,
        neutralized=neutralize,
    )


def run_factor_ic(
    universe_text: str = "",
    years: str = "3年",
    horizon: int = 5,
    rebalance: int = 5,
    universe_mode: str = "自选池",
    neutralize: bool = True,
    progress=None,
) -> FactorResult:
    if progress is not None:
        progress(0.05, desc="读取股票池")
    days = {"1年": 365, "3年": 365 * 3, "5年": 365 * 5}.get(years) or 365 * 5
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=days)
    sizes: dict[str, float] = {}
    membership = None
    size_by_date = None
    pit = "PIT" in universe_mode
    if universe_text.strip():
        codes = parse_universe(universe_text)
    elif pit:
        n = int(re.sub(r"\D", "", universe_mode) or 100)
        cal = load_trade_cal()
        dates = cal[
            (cal["cal_date"] >= cutoff)
            & (cal["cal_date"] <= pd.Timestamp.today())
            & (cal["is_open"] == 1)
        ]["cal_date"].sort_values()
        dates = list(dates[::20])
        if progress is not None:
            progress(0.10, desc=f"构建 PIT 市值前 {n} 股票池")
        union, membership, size_by_date = pit_market_cap_universe(dates, n)
        freq = Counter(code for members in membership.values() for code in members)
        codes = sorted(union, key=lambda c: (-freq.get(c, 0), c))[:400]
    elif universe_mode.startswith("市值前"):
        n = int(re.sub(r"\D", "", universe_mode) or 100)
        codes, sizes = market_cap_universe(n)
    else:
        codes = parse_universe("")
    if not codes:
        raise ValueError("股票池为空")
    year_n = {"1年": 1, "3年": 3, "5年": 5}.get(years, 3)
    if progress is not None:
        progress(0.15, desc=f"加载 {len(codes)} 只股票日线")
    daily_map = load_many(codes, progress=None, years=year_n)
    # 按选择区间截断
    if days:
        daily_map = {
            code: df[df["trade_date"] >= cutoff]
            for code, df in daily_map.items()
            if len(df[df["trade_date"] >= cutoff]) >= 80
        }
    if not sizes and not pit:
        sizes = latest_size_map() if neutralize else {}
    basic = stock_basic()
    industry_map = dict(zip(basic["ts_code"], basic["industry"].fillna("其他")))
    if progress is not None:
        progress(0.55, desc="构建因子面板")
    panel = build_panel(
        daily_map,
        horizon=horizon,
        rebalance=rebalance,
        industry_map=industry_map,
        size_map=sizes,
        universe_by_date=membership,
        size_by_date=size_by_date,
    )
    if progress is not None:
        progress(0.75, desc="计算 RankIC 与分层收益")
    result = compute_ic(panel, horizon=horizon, neutralize=neutralize, rebalance=rebalance)
    if pit:
        result.status += " 股票池为每个调仓日按总市值选前 N（PIT，仍只含当前仍上市股票）。"
    if progress is not None:
        progress(1.0, desc="完成")
    return result


def ic_figure(result: FactorResult) -> go.Figure:
    if result.ic_table is None or result.ic_table.empty:
        return go.Figure()
    df = result.ic_table.dropna(subset=["IC均值"]).iloc[::-1]
    colors = ["#EF5350" if v >= 0 else "#26A69A" for v in df["IC均值"]]
    fig = go.Figure(
        go.Bar(
            x=df["IC均值"],
            y=df["因子"],
            orientation="h",
            marker_color=colors,
            text=[f"{v:.3f}" if pd.notna(v) else "" for v in df["IC均值"]],
            textposition="outside",
        )
    )
    fig.add_vline(x=0, line_color="#999", line_width=1)
    fig.update_layout(
        title=f"RankIC 均值（T+{result.horizon}）",
        template="plotly_white",
        height=380,
        margin=dict(t=50, b=30, l=100, r=30),
        xaxis_title="IC",
    )
    return fig
