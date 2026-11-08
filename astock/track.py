from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .config import BASE_DIR
from .data import last_open_date, load_daily, load_index_daily, next_open_date
from .stats import (
    benjamini_hochberg,
    effective_n,
    newey_west,
    normal_p,
    student_t_p,
)

REC_DIR = BASE_DIR / "data" / "recommendations"
HORIZONS = (1, 5, 20)
TOP_N = 5
NAV_COLUMNS = ("交易日", "组合净值", "沪深300净值", "推荐日", "入场日", "换仓")


def _normal_p(t: float) -> float:
    return normal_p(t)


def _block_bootstrap_ci(
    values, block: int = 1, n_boot: int = 2000, seed: int = 42
) -> tuple[float, float, float]:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = len(arr)
    if n < 2:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    if block <= 1 or n < 2 * block:
        means = rng.choice(arr, size=(n_boot, n), replace=True).mean(axis=1)
    else:
        boot_means: list[float] = []
        for _ in range(n_boot):
            idx: list[int] = []
            while len(idx) < n:
                start = int(rng.integers(0, n))
                idx.extend((start + np.arange(block)) % n)
            boot_means.append(float(arr[np.asarray(idx[:n])].mean()))
        means = np.asarray(boot_means)
    low, high = np.percentile(means, [2.5, 97.5])
    p_boot = min(1.0, 2 * min(float((means <= 0).mean()), float((means >= 0).mean())))
    return float(low), float(high), p_boot


def _newey_west(values, lags: int) -> tuple[float | None, float | None]:
    arr = pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(dtype=float)
    n = len(arr)
    if n < 3:
        return None, None
    mean = float(arr.mean())
    e = arr - mean
    gamma0 = float((e * e).sum() / n)
    var = gamma0
    for lag in range(1, min(lags, n - 1) + 1):
        gamma = float((e[lag:] * e[:-lag]).sum() / n)
        var += 2 * (1 - lag / (lags + 1)) * gamma
    if var <= 0:
        return None, None
    se = math.sqrt(var / n)
    t = mean / se if se > 0 else None
    return (t, _normal_p(t) if t is not None else None)


def _test_series(values, horizon: int) -> dict:
    arr = pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(dtype=float)
    n = len(arr)
    if n < 2:
        return {
            "n": n, "independent": n, "usable": False, "mean": None, "std": None, "t": None,
            "p": None, "nw_t": None, "nw_p": None, "ci_low": None,
            "ci_high": None, "p_boot": None, "block": 1,
        }
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    t = mean / (std / math.sqrt(n)) if std > 0 else float("nan")
    # Newey-West 的 lag 必须覆盖重叠窗口：T+20 => 19
    nw = newey_west(arr, horizon=horizon)
    eff = effective_n(n, horizon)
    usable = eff >= 10.0
    block = max(1, min(int(horizon), max(1, n - 1)))
    low, high, p_boot = _block_bootstrap_ci(arr, block=block)
    return {
        "n": n,
        "independent": int(math.ceil(n / max(1, horizon))),
        "effective": eff,
        "usable": usable,
        "mean": mean,
        "std": std,
        "t": t,
        "p": (student_t_p(t, n - 1) if math.isfinite(t) else None) if usable else None,
        "nw_t": nw.t if usable else None,
        "nw_p": nw.p if usable else None,
        "ci_low": low,
        "ci_high": high,
        "p_boot": p_boot if usable else None,
        "block": block,
    }


def significance_table(detail: pd.DataFrame) -> pd.DataFrame:
    if detail is None or detail.empty:
        return pd.DataFrame()
    if "推荐日" not in detail.columns:
        detail = detail.copy()
        detail["推荐日"] = np.arange(len(detail))
    ranks = pd.to_numeric(detail["排名"], errors="coerce")
    groups = {
        "Top2": ranks <= 2,
        "其余": ranks > 2,
        "全部": pd.Series(True, index=detail.index),
    }
    rows = []
    for group, mask in groups.items():
        for n in HORIZONS:
            series = (
                detail.loc[mask]
                .groupby("推荐日")[f"超额T+{n}"]
                .mean()
                .dropna()
            )
            stats = _test_series(series, n)
            rows.append(
                {
                    "分组": group,
                    "周期": f"T+{n}",
                    "样本日数": stats["n"],
                    "独立窗口(约)": stats["independent"],
                    "有效独立样本": round(stats["effective"], 1),
                    "p值可用": "是" if stats["usable"] else "否",
                    "平均超额%": round(stats["mean"], 2) if stats["mean"] is not None else None,
                    "标准差%": round(stats["std"], 2) if stats["std"] is not None else None,
                    "t值": round(stats["t"], 2) if stats["t"] is not None and math.isfinite(stats["t"]) else None,
                    "p值(normal)": round(stats["p"], 4) if stats["p"] is not None else None,
                    "NW t值": round(stats["nw_t"], 2) if stats["nw_t"] is not None else None,
                    "NW p值": round(stats["nw_p"], 4) if stats["nw_p"] is not None else None,
                    "95%CI下%": round(stats["ci_low"], 2) if stats["ci_low"] is not None and math.isfinite(stats["ci_low"]) else None,
                    "95%CI上%": round(stats["ci_high"], 2) if stats["ci_high"] is not None and math.isfinite(stats["ci_high"]) else None,
                    "p值(block bootstrap)": round(stats["p_boot"], 4) if stats["p_boot"] is not None else None,
                    "block": stats["block"],
                }
            )

    for n in HORIZONS:
        top = detail.loc[ranks <= 2].groupby("推荐日")[f"超额T+{n}"].mean()
        rest = detail.loc[ranks > 2].groupby("推荐日")[f"超额T+{n}"].mean()
        series = (top - rest).dropna()
        stats = _test_series(series, n)
        if stats["n"] >= 2:
            rows.append(
                {
                    "分组": "Top2 - 其余",
                    "周期": f"T+{n}",
                    "样本日数": stats["n"],
                    "独立窗口(约)": stats["independent"],
                    "有效独立样本": round(stats["effective"], 1),
                    "p值可用": "是" if stats["usable"] else "否",
                    "平均超额%": round(stats["mean"], 2),
                    "标准差%": round(stats["std"], 2) if stats["std"] is not None else None,
                    "t值": round(stats["t"], 2) if stats["t"] is not None and math.isfinite(stats["t"]) else None,
                    "p值(normal)": round(stats["p"], 4) if stats["p"] is not None else None,
                    "NW t值": round(stats["nw_t"], 2) if stats["nw_t"] is not None else None,
                    "NW p值": round(stats["nw_p"], 4) if stats["nw_p"] is not None else None,
                    "95%CI下%": round(stats["ci_low"], 2) if stats["ci_low"] is not None and math.isfinite(stats["ci_low"]) else None,
                    "95%CI上%": round(stats["ci_high"], 2) if stats["ci_high"] is not None and math.isfinite(stats["ci_high"]) else None,
                    "p值(block bootstrap)": round(stats["p_boot"], 4) if stats["p_boot"] is not None else None,
                    "block": stats["block"],
                }
            )
    result = pd.DataFrame(rows)
    if not result.empty:
        adjusted = benjamini_hochberg(list(result["p值(normal)"]))
        result["p值(BH)"] = [
            round(x, 4) if x is not None and math.isfinite(float(x)) else None
            for x in adjusted
        ]
    return result



def _from_ledger() -> list[dict]:
    try:
        from .ledger import recommendation_batches
    except Exception:
        return []
    batches: list[dict] = []
    for data in recommendation_batches(limit=500):
        try:
            as_of_ts = pd.Timestamp(data.get("as_of"))
            opened = last_open_date(as_of_ts)
            if opened is not None:
                as_of_ts = opened
            tradable = (
                pd.Timestamp(data["tradable_from"])
                if data.get("tradable_from")
                else next_open_date(as_of_ts)
            )
        except Exception:
            continue
        batches.append(
            {
                "path": None,
                "as_of": as_of_ts.normalize(),
                "tradable_from": tradable.normalize() if tradable is not None else None,
                "generated_at": data.get("generated_at", ""),
                "source": data.get("source", "live"),
                "run_id": data.get("run_id", ""),
                "rows": data.get("rows", []),
            }
        )
    by_date: dict[pd.Timestamp, dict] = {}
    for batch in sorted(batches, key=lambda x: x["generated_at"]):
        by_date[batch["as_of"]] = batch
    return sorted(by_date.values(), key=lambda x: x["as_of"], reverse=True)


def _load_batches() -> list[dict]:
    # Prefer the SQLite ledger; fall back to JSON only if the ledger is empty.
    ledger_batches = _from_ledger()
    if ledger_batches:
        return ledger_batches
    if not REC_DIR.exists():
        return []
    batches: list[dict] = []
    for path in sorted(REC_DIR.glob("recommend_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = data.get("table") or []
        if not rows:
            continue
        as_of = data.get("as_of")
        data_as_of = data.get("data_as_of") or as_of
        if not data_as_of:
            # 旧版推荐文件没有记录数据截止日，无法可靠跟踪，跳过
            continue
        generated = data.get("generated_at") or ""
        try:
            as_of_ts = pd.Timestamp(data_as_of)
            opened = last_open_date(as_of_ts)
            if opened is not None:
                as_of_ts = opened
            tradable_raw = data.get("tradable_from")
            tradable = pd.Timestamp(tradable_raw) if tradable_raw else next_open_date(as_of_ts)
        except Exception:
            continue
        batches.append(
            {
                "path": path,
                "as_of": as_of_ts.normalize(),
                "tradable_from": tradable.normalize() if tradable is not None else None,
                "generated_at": generated,
                "source": data.get("source", "live"),
                "run_id": data.get("run_id", ""),
                "rows": rows,
            }
        )
    # 同一天多次生成，只保留最后一次
    by_date: dict[pd.Timestamp, dict] = {}
    for batch in sorted(batches, key=lambda x: x["generated_at"]):
        by_date[batch["as_of"]] = batch
    return sorted(by_date.values(), key=lambda x: x["as_of"], reverse=True)


def _forward_return(
    df: pd.DataFrame,
    entry_date: pd.Timestamp,
    n: int,
    entry: str = "close",
) -> float | None:
    """Return from an exact entry bar to the n-th following close.

    entry='close' is the legacy close-to-close convention (tests/backfill).
    entry='open' is used for live tracking: buy at next open, then hold N bars.
    A missing bar on entry_date returns None instead of silently moving the entry.
    """
    if df is None or df.empty:
        return None
    exact = df.index[df["trade_date"] == pd.Timestamp(entry_date)]
    if len(exact) == 0:
        return None
    i = int(exact[0])
    j = i + n
    if j >= len(df):
        return None
    price_col = "open" if entry == "open" else "close"
    if price_col not in df.columns:
        price_col = "close"
    base = float(df[price_col].iloc[i])
    if not base or not math.isfinite(base):
        return None
    return float(df["close"].iloc[j]) / base - 1


def _as_ts(value) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.normalize()


def _positive(value) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and num > 0 else None


def _frame_price(frame: pd.DataFrame, date: pd.Timestamp, column: str = "open") -> float | None:
    if frame is None or frame.empty or column not in frame.columns or date not in frame.index:
        return None
    return _positive(frame.at[date, column])


def _bar_frame(df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalize a daily frame to a trade_date-indexed frame (unique, sorted)."""
    if df is None or df.empty or "trade_date" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.normalize()
    out = out.drop_duplicates("trade_date").sort_values("trade_date")
    return out.set_index("trade_date")


def _batch_plans(detail: pd.DataFrame, top_n: int) -> list[dict]:
    """One plan per 入场日: the top-N rows of the batch, bought at tradable_from open."""
    if detail is None or detail.empty or "代码" not in detail.columns:
        return []
    has_rank = "排名" in detail.columns
    plans: list[dict] = []
    for as_of, group in detail.groupby("推荐日", sort=True, dropna=True):
        as_of_ts = _as_ts(as_of)
        if as_of_ts is None:
            continue
        rows = group.copy()
        rows["_rank"] = (
            pd.to_numeric(rows["排名"], errors="coerce") if has_rank else np.nan
        )
        rows = rows.sort_values("_rank", na_position="last").drop_duplicates("代码")
        rows = rows.head(top_n)
        codes = [str(code) for code in rows["代码"].tolist() if str(code)]
        if not codes:
            continue
        entry = None
        if "入场日" in rows.columns:
            for value in rows["入场日"].tolist():
                entry = _as_ts(value)
                if entry is not None:
                    break
        if entry is None:
            # 没有 tradable_from 记录时，与 track_recommendations 同口径回退到下一交易日
            entry = next_open_date(as_of_ts)
        if entry is None:
            continue
        plans.append({"推荐日": as_of_ts, "入场日": entry.normalize(), "codes": codes})
    # 同一入场日只保留最后一次生成
    by_entry: dict[pd.Timestamp, dict] = {}
    for plan in plans:
        prev = by_entry.get(plan["入场日"])
        if prev is None or plan["推荐日"] >= prev["推荐日"]:
            by_entry[plan["入场日"]] = plan
    return sorted(by_entry.values(), key=lambda p: p["入场日"])


def portfolio_nav(
    detail: pd.DataFrame,
    bench: pd.DataFrame | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    top_n: int = TOP_N,
    require_full_batch: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """等权买入每批 Top5、按交易日拼接组合净值，并与沪深300对齐。

    口径（与 ADR-005 / DATA-03 一致）：
    - 每个批次在 tradable_from 的**开盘**等权买入 Top5，持有到下一批次入场日开盘换仓；
    - 批次之间按收盘 → 开盘拼接，不计隔夜跳空与交易成本；
    - 成分股当日缺 bar（停牌）按上一收盘持有，当日收益记 0；入场日缺 bar 的批次剔除，
      其持仓延续到下一个可用批次（不后移入场日、不使用未来数据）；
    - 沪深300 从首个批次入场日开盘买入持有，作为对照。
    """
    empty_nav = pd.DataFrame(columns=list(NAV_COLUMNS))
    empty_dropped = pd.DataFrame(columns=["推荐日", "入场日", "原因"])
    plans = _batch_plans(detail, top_n)
    if not plans:
        return empty_nav, empty_dropped

    start_ts = (
        pd.Timestamp(start) if start is not None else plans[0]["入场日"] - pd.Timedelta(days=10)
    )
    end_ts = pd.Timestamp(end) if end is not None else pd.Timestamp.today().normalize()
    start_s, end_s = start_ts.strftime("%Y%m%d"), end_ts.strftime("%Y%m%d")

    if bench is None:
        try:
            bench = load_index_daily("000300.SH", start=start_s, end=end_s)
        except Exception:
            bench = pd.DataFrame()
    bench_bars = _bar_frame(bench)
    bench_dates = bench_bars.index if len(bench_bars) else pd.DatetimeIndex([])

    daily: dict[str, pd.DataFrame] = {}
    included: list[dict] = []
    dropped: list[dict] = []
    for plan in plans:
        entry = plan["入场日"]
        reason = None
        if len(bench_dates) and entry not in bench_dates:
            reason = f"入场日 {entry:%Y-%m-%d} 不是沪深300交易日（缺基准 bar）"
        frames: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        if reason is None:
            for code in plan["codes"]:
                if code not in daily:
                    try:
                        daily[code] = _bar_frame(
                            load_daily(code, start=start_s, end=end_s, adj="qfq")
                        )
                    except Exception:
                        daily[code] = pd.DataFrame()
                frame = daily[code]
                if _frame_price(frame, entry) is None:
                    missing.append(code)
                else:
                    frames[code] = frame
            if missing and require_full_batch:
                shown = "、".join(missing[:3]) + ("等" if len(missing) > 3 else "")
                reason = f"入场日 {entry:%Y-%m-%d} 缺 bar：{shown}"
            elif not frames:
                reason = f"入场日 {entry:%Y-%m-%d} 无可用成分股（全部缺 bar）"
        if reason is not None:
            dropped.append(
                {"推荐日": plan["推荐日"], "入场日": entry, "原因": reason}
            )
            continue
        included.append({"plan": plan, "frames": frames, "missing": missing})

    dropped_df = pd.DataFrame(dropped) if dropped else empty_dropped
    if not included:
        return empty_nav, dropped_df

    first_entry = included[0]["plan"]["入场日"]
    bench_nav: pd.Series | None = None
    if len(bench_dates):
        calendar = bench_dates[bench_dates >= first_entry]
        base = _frame_price(bench_bars, first_entry)
        if base is None:
            base = _frame_price(bench_bars, first_entry, "close")
        if base is not None and len(calendar):
            closes = pd.to_numeric(
                bench_bars["close"].reindex(calendar, method="ffill"), errors="coerce"
            )
            bench_nav = closes / base
    else:
        # 基准缺失时仍给出组合曲线（沪深300 列留空），日期用成分股并集
        union: set[pd.Timestamp] = set()
        for item in included:
            for frame in item["frames"].values():
                union.update(d for d in frame.index if d >= first_entry)
        calendar = pd.DatetimeIndex(sorted(union))

    rows: list[dict] = []
    nav_base = 1.0
    for idx, item in enumerate(included):
        entry = item["plan"]["入场日"]
        next_entry = (
            included[idx + 1]["plan"]["入场日"] if idx + 1 < len(included) else None
        )
        segment = calendar[calendar >= entry]
        if next_entry is not None:
            segment = segment[segment < next_entry]
        if not len(segment):
            continue
        ratios: list[pd.Series] = []
        for frame in item["frames"].values():
            base = _frame_price(frame, entry)
            if base is None:
                continue
            closes = pd.to_numeric(
                frame["close"].reindex(segment, method="ffill"), errors="coerce"
            )
            ratios.append(closes / base)
        if not ratios:
            continue
        segment_nav = nav_base * pd.concat(ratios, axis=1).mean(axis=1)
        for trade_date, value in segment_nav.items():
            bench_value = np.nan
            if bench_nav is not None and trade_date in bench_nav.index:
                bench_value = float(bench_nav.loc[trade_date])
            rows.append(
                {
                    "交易日": trade_date,
                    "组合净值": float(value),
                    "沪深300净值": bench_value,
                    "推荐日": item["plan"]["推荐日"],
                    "入场日": entry,
                    "换仓": bool(trade_date == entry),
                }
            )
        nav_base = float(segment_nav.iloc[-1])

    if not rows:
        return empty_nav, dropped_df
    nav = pd.DataFrame(rows).sort_values("交易日").reset_index(drop=True)
    return nav, dropped_df


def nav_status_text(nav: pd.DataFrame, dropped: pd.DataFrame | None = None) -> str:
    """首屏净值区的一句统计口径说明（含剔除批次提示）。"""
    if nav is None or nav.empty:
        text = "组合净值：暂无可拼接的批次（需要至少一个批次在 tradable_from 有完整 Top5 开盘 bar）。"
    else:
        start = pd.Timestamp(nav["交易日"].iloc[0])
        end = pd.Timestamp(nav["交易日"].iloc[-1])
        n_batches = int(nav["入场日"].nunique())
        port = float(nav["组合净值"].iloc[-1])
        bench_values = pd.to_numeric(nav["沪深300净值"], errors="coerce").dropna()
        parts = [
            f"组合净值：{n_batches} 个批次 / {len(nav)} 个交易日"
            f"（{start:%Y-%m-%d} → {end:%Y-%m-%d}）",
            f"Top5 等权累计 {port - 1:+.2%}",
        ]
        if len(bench_values):
            bench = float(bench_values.iloc[-1])
            parts.append(f"沪深300 {bench - 1:+.2%}")
            parts.append(f"超额 {port - bench:+.2f}pp")
        else:
            parts.append("沪深300 数据缺失，仅显示组合")
        text = "，".join(parts) + "。"
        text += (
            " 口径：每个批次在 tradable_from 开盘等权买入 Top5（qfq 收盘计价），"
            "持有到下一批次入场日开盘换仓；沪深300 从首个入场日开盘买入持有；不计交易成本。"
        )
        if n_batches < 20:
            text += " 批次 < 20，净值仅作跟踪记录，不构成 alpha 证据。"
    if dropped is not None and not dropped.empty:
        items = []
        for _, row in dropped.head(3).iterrows():
            items.append(f"{pd.Timestamp(row['入场日']):%Y-%m-%d}（{row['原因']}）")
        more = f" 等 {len(dropped)} 个" if len(dropped) > 3 else ""
        text += (
            f" 已剔除 {len(dropped)} 个批次（入场日缺 bar，原持仓延续到下一批）："
            + "；".join(items)
            + more
        )
    return text


def nav_figure(nav: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if nav is None or nav.empty:
        fig.update_layout(
            title="Top5 等权组合净值 vs 沪深300（暂无可用批次）",
            template="plotly_white",
            height=420,
        )
        return fig
    dates = pd.to_datetime(nav["交易日"])
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=nav["组合净值"],
            mode="lines",
            name="Top5 等权组合",
            line=dict(color="#d62728", width=2),
            hovertemplate="%{x|%Y-%m-%d}<br>组合净值 %{y:.4f}<extra></extra>",
        )
    )
    bench = pd.to_numeric(nav["沪深300净值"], errors="coerce")
    if bench.notna().any():
        fig.add_trace(
            go.Scatter(
                x=dates,
                y=bench,
                mode="lines",
                name="沪深300",
                line=dict(color="#7f7f7f", width=2, dash="dot"),
                hovertemplate="%{x|%Y-%m-%d}<br>沪深300 %{y:.4f}<extra></extra>",
            )
        )
    rebalance = nav[nav["换仓"] == True]  # noqa: E712
    if len(rebalance):
        fig.add_trace(
            go.Scatter(
                x=pd.to_datetime(rebalance["交易日"]),
                y=rebalance["组合净值"],
                mode="markers",
                name="批次换仓",
                marker=dict(symbol="triangle-up", size=9, color="#1f77b4"),
                hovertemplate="批次 %{x|%Y-%m-%d}<br>净值 %{y:.4f}<extra></extra>",
            )
        )
    fig.add_hline(y=1.0, line_color="#bbb", line_width=1)
    fig.update_layout(
        title="Top5 等权组合净值 vs 沪深300（起点 = 首个批次入场日开盘）",
        template="plotly_white",
        height=420,
        margin=dict(t=50, b=30, l=40, r=20),
        legend=dict(orientation="h", y=1.08, x=0),
        hovermode="x unified",
    )
    fig.update_yaxes(title="净值", tickformat=".3f")
    return fig


def track_recommendations() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    batches = _load_batches()
    if not batches:
        return (
            pd.DataFrame(),
            pd.DataFrame(),
            "暂无历史推荐。先运行一次「每日推荐」，或等收盘后的自动任务生成。",
        )

    earliest = min(b["as_of"] for b in batches)
    start = (earliest - pd.Timedelta(days=10)).strftime("%Y%m%d")
    end = pd.Timestamp.today().strftime("%Y%m%d")

    bench = load_index_daily("000300.SH", start=start, end=end)
    if not bench.empty:
        bench = bench.sort_values("trade_date").reset_index(drop=True)

    records: list[dict] = []
    daily_cache: dict[str, pd.DataFrame] = {}
    for batch in batches:
        as_of = batch["as_of"]
        entry_date = batch.get("tradable_from") or as_of
        for row in batch["rows"]:
            code = str(row.get("代码", ""))
            if not code:
                continue
            if code not in daily_cache:
                try:
                    daily_cache[code] = load_daily(code, start=start, end=end, adj="qfq")
                except Exception:
                    daily_cache[code] = pd.DataFrame()
            df = daily_cache[code]
            rec = {
                "推荐日": as_of,
                "入场日": entry_date,
                "run_id": batch.get("run_id", ""),
                "来源": batch.get("source", "live"),
                "代码": code,
                "名称": row.get("名称", ""),
                "排名": row.get("排名"),
                "综合分": row.get("综合分"),
                "建议": row.get("建议", ""),
            }
            for n in HORIZONS:
                r = _forward_return(df, entry_date, n, entry="open")
                b = _forward_return(bench, entry_date, n, entry="open") if not bench.empty else None
                rec[f"T+{n}"] = r * 100 if r is not None else None
                rec[f"超额T+{n}"] = (r - b) * 100 if r is not None and b is not None else None
            records.append(rec)

    detail = pd.DataFrame(records)
    if detail.empty:
        return pd.DataFrame(), pd.DataFrame(), "历史推荐文件里没有可跟踪的记录。"

    def summarize(name: str, mask: pd.Series) -> dict:
        sub = detail[mask]
        out = {"分组": name, "样本数": int(len(sub))}
        for n in HORIZONS:
            vals = pd.to_numeric(sub[f"超额T+{n}"], errors="coerce").dropna()
            out[f"T+{n}平均超额%"] = round(float(vals.mean()), 2) if len(vals) else None
            out[f"T+{n}胜率%"] = round(float((vals > 0).mean() * 100), 1) if len(vals) else None
            out[f"T+{n}样本"] = int(len(vals))
        return out

    ranks = pd.to_numeric(detail["排名"], errors="coerce")
    scores = pd.to_numeric(detail["综合分"], errors="coerce")
    summary = pd.DataFrame(
        [
            summarize("Top2", ranks <= 2),
            summarize("其余", ranks > 2),
            summarize("综合分≥75", scores >= 75),
            summarize("综合分<75", scores < 75),
            summarize("全部", pd.Series(True, index=detail.index)),
        ]
    )

    usable = int(pd.to_numeric(detail["超额T+1"], errors="coerce").notna().sum())
    max_n = max(
        int(pd.to_numeric(detail[f"超额T+{n}"], errors="coerce").notna().sum())
        for n in HORIZONS
    )
    live_n = int((detail.get("来源", "") == "live").sum()) if "来源" in detail.columns else len(detail)
    backfill_n = int((detail.get("来源", "") == "backfill_technical").sum()) if "来源" in detail.columns else 0
    status = (
        f"已加载 {len(batches)} 个批次 / {len(detail)} 条记录"
        f"（实盘/实时 {live_n} 条，技术分回填 {backfill_n} 条）；"
        f"有 T+1 前向数据的记录 {usable} 条。"
    )
    if backfill_n:
        status += " 回填批次事件分固定 50，只能检验技术分，不等同于完整推荐。"
    if usable == 0:
        status += " 目前还没有 T+1 收盘数据，等下一个交易日收盘后再刷新。"
    if max_n < 20:
        status += " 有效样本不足 20 条，t/p 值与置信区间仅作参考，不宜据此下结论。"
    n_dates = int(detail["推荐日"].nunique()) if "推荐日" in detail.columns else 0
    if n_dates:
        status += (
            f" 日期数 {n_dates}；T+20 的独立窗口约 {max(1, -(-n_dates // 20))} 个，"
            "重叠严重时 p 值只能作参考。"
        )
        if n_dates / 5 < 10 or n_dates / 20 < 10:
            status += " T+5/T+20 有效独立样本 <10，p 值已隐藏。"
    status += " 跟踪口径：T+1 起从 tradable_from（下一交易日）开盘买入，持有到第 N 个交易日收盘；超额=个股收益-沪深300。"
    status += " 数据源：SQLite ledger（缺失时回退 JSON）。"
    detail = detail.sort_values(["推荐日", "排名"], ascending=[False, True]).reset_index(drop=True)
    try:
        from .ledger import save_tracking

        for run_id, group in detail.groupby("run_id"):
            if not run_id:
                continue
            rows = []
            for _, r in group.iterrows():
                for n in HORIZONS:
                    ret = pd.to_numeric(pd.Series([r.get(f"T+{n}")]), errors="coerce").iloc[0]
                    exc = pd.to_numeric(pd.Series([r.get(f"超额T+{n}")]), errors="coerce").iloc[0]
                    rows.append(
                        {
                            "code": r.get("代码"),
                            "horizon": n,
                            "entry_date": str(r.get("入场日")),
                            "exit_date": None,
                            "ret": None if pd.isna(ret) else float(ret) / 100.0,
                            "excess": None if pd.isna(exc) else float(exc) / 100.0,
                        }
                    )
            save_tracking(str(run_id), rows)
    except Exception:
        pass
    return detail, summary, status


def track_figure(summary: pd.DataFrame) -> go.Figure:
    if summary is None or summary.empty:
        return go.Figure()
    groups = [g for g in ["Top2", "其余", "全部"] if g in set(summary["分组"])]
    data = summary[summary["分组"].isin(groups)]
    fig = go.Figure()
    for n in HORIZONS:
        fig.add_trace(
            go.Bar(
                name=f"T+{n} 平均超额%",
                x=data["分组"],
                y=data[f"T+{n}平均超额%"],
                text=[f"{v:.2f}%" if pd.notna(v) else "" for v in data[f"T+{n}平均超额%"]],
                textposition="outside",
            )
        )
    fig.add_hline(y=0, line_color="#999", line_width=1)
    fig.update_layout(
        title="不同分组的平均超额收益（%）",
        template="plotly_white",
        height=360,
        barmode="group",
        margin=dict(t=50, b=30, l=40, r=20),
        legend=dict(orientation="h", y=1.08, x=0),
    )
    fig.update_yaxes(ticksuffix="%")
    return fig
