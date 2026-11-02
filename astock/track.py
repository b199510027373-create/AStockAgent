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
        means = []
        for _ in range(n_boot):
            idx: list[int] = []
            while len(idx) < n:
                start = int(rng.integers(0, n))
                idx.extend((start + np.arange(block)) % n)
            means.append(float(arr[np.asarray(idx[:n])].mean()))
        means = np.asarray(means)
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



def _load_batches() -> list[dict]:
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
    detail = detail.sort_values(["推荐日", "排名"], ascending=[False, True]).reset_index(drop=True)
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
