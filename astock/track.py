from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .config import BASE_DIR
from .data import (
    last_open_date,
    load_daily,
    load_index_daily,
    load_trade_cal,
    next_open_date,
)
from .stats import (
    MIN_EFFECTIVE_N,
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
BANNER_HORIZON = 5
ALPHA_LEVEL = 0.05
ALPHA_BANNER_STATES = ("insufficient", "unavailable", "no_alpha", "positive", "negative")


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


# ---------------------------------------------------------------------------
# 「无显著 alpha」横幅：ledger 实盘 tracking → stats 聚合
# 数字必须实时算，UI 只渲染；样本不足时绝不展示 p 值。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlphaBanner:
    """推荐页顶部的实时显著性状态。

    ``state`` 取值见 :data:`ALPHA_BANNER_STATES`：

    - ``insufficient``：有效独立样本 < 10，展示样本量但隐藏 p；
    - ``unavailable``：样本达标但 Newey-West 无法给出 p（如方差为 0）；
    - ``no_alpha``：样本达标、p >= 0.05，未发现显著差异；
    - ``positive``：样本达标、p < 0.05 且 Top2 优于其余；
    - ``negative``：样本达标、p < 0.05 但 Top2 弱于其余。
    """

    state: str
    horizon: int
    live_days: int
    independent_windows: int
    effective_n: float
    p_value: float | None
    top2_mean: float | None
    rest_mean: float | None

    @property
    def trustworthy(self) -> bool:
        """是否有可信统计（p 可展示）。"""
        return self.p_value is not None and self.state in (
            "no_alpha",
            "positive",
            "negative",
        )


def _finite_float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def alpha_banner(
    rows,
    *,
    horizon: int = BANNER_HORIZON,
    min_effective: float = MIN_EFFECTIVE_N,
    alpha: float = ALPHA_LEVEL,
) -> AlphaBanner:
    """Aggregate realised tracking rows into the banner state.

    ``rows`` are ``ledger.tracking_rows`` dicts (``data_as_of``/``rank``/
    ``excess``)。每个推荐日先算 Top2 与其余的平均超额，再对逐日差值做
    Newey-West（lag=horizon-1）+ Student-t；有效独立样本不足时 p 置空。
    """
    horizon = max(1, int(horizon))
    by_date: dict[str, dict[str, list[float]]] = {}
    for row in rows or []:
        excess = _finite_float(row.get("excess"))
        if excess is None:
            continue
        date = str(row.get("data_as_of") or row.get("entry_date") or "").strip()
        if not date:
            continue
        bucket = by_date.setdefault(date, {"top2": [], "rest": []})
        rank = _finite_float(row.get("rank"))
        if rank is None:
            continue
        bucket["top2" if rank <= 2 else "rest"].append(excess)

    live_days = len(by_date)
    top2_daily: list[float] = []
    rest_daily: list[float] = []
    diffs: list[float] = []
    for bucket in by_date.values():
        if not bucket["top2"] or not bucket["rest"]:
            continue
        top = float(np.mean(bucket["top2"]))
        rest = float(np.mean(bucket["rest"]))
        top2_daily.append(top)
        rest_daily.append(rest)
        diffs.append(top - rest)

    n_diffs = len(diffs)
    independent = int(math.ceil(n_diffs / horizon)) if n_diffs else 0
    effective = effective_n(n_diffs, horizon) if n_diffs else 0.0
    nw = newey_west(diffs, horizon=horizon) if n_diffs else None
    p_value = nw.p if nw is not None and effective >= min_effective else None
    top2_mean = float(np.mean(top2_daily)) if top2_daily else None
    rest_mean = float(np.mean(rest_daily)) if rest_daily else None

    if effective < min_effective:
        state = "insufficient"
    elif p_value is None:
        state = "unavailable"
    elif p_value < alpha and (top2_mean or 0.0) > (rest_mean or 0.0):
        state = "positive"
    elif p_value < alpha:
        state = "negative"
    else:
        state = "no_alpha"

    return AlphaBanner(
        state=state,
        horizon=horizon,
        live_days=live_days,
        independent_windows=independent,
        effective_n=effective,
        p_value=p_value,
        top2_mean=top2_mean,
        rest_mean=rest_mean,
    )


def alpha_banner_snapshot(*, horizon: int = BANNER_HORIZON) -> AlphaBanner:
    """Read the ledger and build the banner; ledger failure means no sample."""
    try:
        from .ledger import tracking_rows

        rows = tracking_rows(horizon)
    except Exception:  # noqa: BLE001 - 横幅不能拖垮推荐页
        rows = []
    return alpha_banner(rows, horizon=horizon)


def _format_p(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0.0001:
        return "<0.0001"
    return f"{value:.4f}"


def _banner_box(body: str, *, background: str, border: str) -> str:
    return (
        f'<div style="background:{background};border-left:4px solid {border};'
        f'padding:10px 14px;border-radius:4px;margin:4px 0">{body}</div>'
    )


def alpha_banner_markdown(banner: AlphaBanner) -> str:
    """Render the banner as Markdown with inline HTML background.

    口径固定写进横幅：数字来自 ledger 实盘 tracking 记录，p 只由 stats 产出。
    """
    title = f"**T+{banner.horizon} 实盘样本**"
    numbers = (
        f"实盘样本 **{banner.live_days}** 个交易日 · "
        f"独立窗口≈**{banner.independent_windows}** · "
        f"有效独立样本 {banner.effective_n:.1f}"
    )
    method = (
        "口径：ledger tracking 实盘记录（仅 source=live），"
        f"T+{banner.horizon} 超额 vs 沪深300，Newey-West lag={banner.horizon - 1}、"
        "Student-t；历史样本内检验，不构成交易建议。"
    )

    if banner.state == "insufficient":
        head = f"🧪 {title} · 样本不足"
        detail = "**样本不足，不显示显著性**（有效独立样本 < 10）。"
        body = f"{head}<br>{numbers}<br>{detail}<br><sub>{method}</sub>"
        return _banner_box(body, background="#f1f3f5", border="#868e96")

    if banner.state == "unavailable":
        head = f"🧪 {title} · p 暂不可计算"
        detail = "p 值暂不可计算（方差为 0 或样本结构异常），**不显示显著性结论**。"
        body = f"{head}<br>{numbers}<br>{detail}<br><sub>{method}</sub>"
        return _banner_box(body, background="#f1f3f5", border="#868e96")

    p_text = _format_p(banner.p_value)
    stats = f"{numbers} · Top2−其余 p=**{p_text}**"
    if banner.state == "positive":
        head = f"✅ {title} · 样本内正向观察"
        detail = "Top2 在历史样本内暂优于其余（p < 0.05）；样本仍有限，不构成交易建议。"
        body = f"{head}<br>{stats}<br>{detail}<br><sub>{method}</sub>"
        return _banner_box(body, background="#ebfbee", border="#2f9e44")

    if banner.state == "negative":
        head = f"⚠️ {title} · Top2 弱于其余"
        detail = "Top2 显著弱于其余（p < 0.05），未发现正向 alpha。"
        body = f"{head}<br>{stats}<br>{detail}<br><sub>{method}</sub>"
        return _banner_box(body, background="#fff5f5", border="#e03131")

    head = f"🧪 {title} · 未发现显著 alpha"
    detail = "Top2 与其余的差异不显著（p ≥ 0.05），暂无可宣称的 alpha。"
    body = f"{head}<br>{stats}<br>{detail}<br><sub>{method}</sub>"
    return _banner_box(body, background="#f1f3f5", border="#868e96")


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


def _trading_days(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """SSE 交易日序列（不拿指数当日 bar 冒充交易日历）。"""
    try:
        cal = load_trade_cal()
    except Exception:
        return pd.DatetimeIndex([])
    if cal is None or cal.empty or "cal_date" not in cal.columns or "is_open" not in cal.columns:
        return pd.DatetimeIndex([])
    days = pd.to_datetime(cal.loc[cal["is_open"] == 1, "cal_date"]).sort_values()
    return pd.DatetimeIndex(days[(days >= start) & (days <= end)])


def _batch_plans(detail: pd.DataFrame, top_n: int) -> list[dict]:
    """One plan per 入场日: the top-N rows of the batch, bought at tradable_from open.

    ``skip_reason`` is set when the batch cannot be ordered (no 排名/综合分) or has
    fewer than ``top_n`` components; ``portfolio_nav`` records those as dropped.
    """
    if detail is None or detail.empty or "代码" not in detail.columns:
        return []
    plans: list[dict] = []
    for as_of, group in detail.groupby("推荐日", sort=True, dropna=True):
        as_of_ts = _as_ts(as_of)
        if as_of_ts is None:
            continue
        rows = group.copy()
        order = (
            pd.to_numeric(rows["排名"], errors="coerce")
            if "排名" in rows.columns
            else pd.Series(np.nan, index=rows.index)
        )
        skip_reason = None
        if order.notna().any():
            rows["_order"] = order
        elif "综合分" in rows.columns and pd.to_numeric(
            rows["综合分"], errors="coerce"
        ).notna().any():
            # 旧 JSON / 回填批次可能缺排名：按综合分从高到低兜底
            rows["_order"] = -pd.to_numeric(rows["综合分"], errors="coerce")
        else:
            skip_reason = "批次缺少排名与综合分，无法确定 Top5"
        codes: list[str] = []
        if skip_reason is None:
            # 无排名的行不得占用 Top5 名额（否则会按文件行序补位却仍宣称 Top5）
            rows = rows[rows["_order"].notna()]
            rows = rows.sort_values("_order", na_position="last").drop_duplicates("代码")
            rows = rows.head(top_n)
            codes = [str(code) for code in rows["代码"].tolist() if str(code)]
            if len(codes) < top_n:
                skip_reason = f"成分不足 Top5（仅 {len(codes)} 只）"
        entry = None
        if "入场日" in group.columns:
            for value in group["入场日"].tolist():
                entry = _as_ts(value)
                if entry is not None:
                    break
        if entry is None:
            # 没有 tradable_from 记录时，与 compute_tracking 同口径回退到下一交易日
            entry = next_open_date(as_of_ts)
        if entry is None:
            continue
        plans.append(
            {
                "推荐日": as_of_ts,
                "入场日": entry.normalize(),
                "codes": codes,
                "skip_reason": skip_reason,
            }
        )
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

    trading_days = _trading_days(start_ts, end_ts)

    if bench is None:
        try:
            bench = load_index_daily("000300.SH", start=start_s, end=end_s)
        except Exception:
            bench = pd.DataFrame()
    bench_bars = _bar_frame(bench)

    daily: dict[str, pd.DataFrame] = {}
    included: list[dict] = []
    dropped: list[dict] = []
    for plan in plans:
        entry = plan["入场日"]
        reason = plan.get("skip_reason")
        if reason is None and len(trading_days) and entry not in trading_days:
            # 交易日判定用交易日历，而不是“指数当日是否已有 bar”
            reason = f"入场日 {entry:%Y-%m-%d} 不是交易日"
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
    # 组合交易日 = 成分股实际有 bar 的日期（并集），不再被沪深300 的更新进度截断
    union: set[pd.Timestamp] = set()
    for item in included:
        for frame in item["frames"].values():
            union.update(d for d in frame.index if d >= first_entry)
    calendar = pd.DatetimeIndex(sorted(union))
    if len(trading_days):
        calendar = calendar.intersection(trading_days)
    if not len(calendar):
        return empty_nav, dropped_df

    bench_nav: pd.Series | None = None
    bench_last: pd.Timestamp | None = None
    bench_anchor: pd.Timestamp | None = None
    if len(bench_bars) and "close" in bench_bars.columns:
        bench_close = pd.to_numeric(bench_bars["close"], errors="coerce")
        bench_close = bench_close[bench_close > 0]
        if len(bench_close):
            bench_last = pd.Timestamp(bench_close.index.max())
            # 基准起算点分三种情况，不能用“入场日之后第一根 bar 的收盘”直接当基：
            # (a) 入场日有 bar → 该日开盘为基（与组合同口径）；
            # (b) 入场日缺 bar、其后有 bar → 以其后第一根 bar 的收盘为基（anchor 日 = 1.0），
            #     入场日到该日之间留 NaN（不拿 ffill 前收去除以未来的基）；
            # (c) 入场日及其后都没有 bar → 无可用基准线，状态行注明“数据截至 X，尚未覆盖入场日”。
            later = bench_close[bench_close.index > first_entry]
            anchor: pd.Timestamp | None = None
            if first_entry in bench_bars.index and (
                _frame_price(bench_bars, first_entry) is not None
                or _frame_price(bench_bars, first_entry, "close") is not None
            ):
                anchor = first_entry
            elif len(later):
                anchor = pd.Timestamp(later.index[0])
            bench_anchor = anchor
            if anchor is not None:
                aligned = (
                    bench_close.reindex(bench_close.index.union(calendar))
                    .ffill()
                    .reindex(calendar)
                )
                # 基准末端缺 bar 时保留 NaN，让曲线停在最后有效日，而不是把旧值延伸成新值
                aligned = aligned.where(calendar <= bench_last)
                if anchor == first_entry:
                    base = _frame_price(bench_bars, anchor) or _frame_price(
                        bench_bars, anchor, "close"
                    )
                else:
                    # 入场日缺 bar：anchor 日按收盘起算（两侧同为收盘口径），
                    # anchor 之前不补前收、不把当日盘中涨幅算进超额
                    aligned = aligned.where(calendar >= anchor)
                    base = _frame_price(bench_bars, anchor, "close") or _frame_price(
                        bench_bars, anchor
                    )
                if base is not None and base > 0:
                    bench_nav = aligned / base

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
            if base is None or "close" not in frame.columns:
                continue
            # 值级 ffill：索引缺行与 close 为 NaN 都按“持上一收盘、当日收益 0”处理，
            # 避免 concat().mean() 静默跳过 NaN、把权重临时重分配给其余成分
            closes = pd.to_numeric(frame["close"], errors="coerce").ffill()
            closes = closes.reindex(segment).ffill()
            ratio = closes / base
            if ratio.isna().all():
                continue
            ratios.append(ratio.fillna(1.0))
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
    nav.attrs["bench_last"] = bench_last
    nav.attrs["bench_anchor"] = bench_anchor
    return nav, dropped_df


def nav_status_text(nav: pd.DataFrame, dropped: pd.DataFrame | None = None) -> str:
    """首屏净值区的一句统计口径说明（含剔除批次提示）。"""
    has_nav = nav is not None and not nav.empty
    if not has_nav:
        text = "组合净值：暂无可拼接的批次（需要至少一个批次在 tradable_from 有完整 Top5 开盘 bar）。"
    else:
        start = pd.Timestamp(nav["交易日"].iloc[0])
        end = pd.Timestamp(nav["交易日"].iloc[-1])
        n_batches = int(nav["入场日"].nunique())
        port = float(nav["组合净值"].iloc[-1])
        bench_series = pd.to_numeric(nav["沪深300净值"], errors="coerce")
        bench_values = bench_series.dropna()
        bench_anchor = nav.attrs.get("bench_anchor") if hasattr(nav, "attrs") else None
        parts = [
            f"组合净值：{n_batches} 个批次 / {len(nav)} 个交易日"
            f"（{start:%Y-%m-%d} → {end:%Y-%m-%d}）",
            f"Top5 等权累计 {port - 1:+.2%}",
        ]
        if len(bench_values):
            last_idx = bench_values.index[-1]
            bench = float(bench_values.iloc[-1])
            port_at_bench = float(nav.loc[last_idx, "组合净值"])
            # 基准可能从 anchor 日起算（case b）：组合净值需同样在 anchor 日归一，
            # 否则会把 start→anchor 的组合涨跌算进超额，与「此前不参与超额」矛盾。
            # case (a)（anchor == start）不归一：入场日组合与基准都从开盘起算，
            # 当日收益属于正常超额，不能拿收盘净值再除一次。
            port_base = 1.0
            if bench_anchor is not None and pd.Timestamp(bench_anchor) > start:
                anchor_ts = pd.Timestamp(bench_anchor)
                after_anchor = nav.index[nav["交易日"] >= anchor_ts]
                if len(after_anchor):
                    port_base = float(nav.loc[after_anchor[0], "组合净值"])
            last_bench_date = pd.Timestamp(nav.loc[last_idx, "交易日"])
            parts.append(f"沪深300 {bench - 1:+.2%}")
            if port_base > 0:
                excess = (port_at_bench / port_base - 1.0 - (bench - 1.0)) * 100
            else:
                excess = (port_at_bench - bench) * 100
            parts.append(f"超额 {excess:+.2f}pp")
            if last_bench_date < end:
                parts.append(
                    f"沪深300 数据截至 {last_bench_date:%Y-%m-%d}，超额只算到该日"
                )
        else:
            bench_last = nav.attrs.get("bench_last") if hasattr(nav, "attrs") else None
            if bench_last is not None:
                parts.append(
                    f"沪深300 数据截至 {pd.Timestamp(bench_last):%Y-%m-%d}，尚未覆盖入场日"
                )
            else:
                parts.append("沪深300 本次无可用数据")
        text = "，".join(parts) + "。"
        text += (
            " 口径：每个批次在 tradable_from 开盘等权买入 Top5（qfq 收盘计价），"
            "持有到下一批次入场日开盘换仓；"
        )
        if not len(bench_values):
            text += "沪深300 本次无可用数据，不展示基准与超额；不计交易成本。"
        elif bench_anchor is not None and pd.Timestamp(bench_anchor) > start:
            text += (
                f"沪深300 自 {pd.Timestamp(bench_anchor):%Y-%m-%d} 收盘起算"
                f"（{start:%Y-%m-%d} 缺基准 bar），此前不参与超额；不计交易成本。"
            )
        else:
            text += "沪深300 从首个入场日开盘买入持有；不计交易成本。"
        if n_batches < 20:
            text += " 批次 < 20，净值仅作跟踪记录，不构成 alpha 证据。"
    if dropped is not None and not dropped.empty:
        items = []
        for _, row in dropped.head(3).iterrows():
            items.append(f"{pd.Timestamp(row['入场日']):%Y-%m-%d}（{row['原因']}）")
        more = f" 等 {len(dropped)} 个" if len(dropped) > 3 else ""
        text += f" 已剔除 {len(dropped)} 个批次：" + "；".join(items) + more
        if has_nav:
            text += "（被剔除批次不建仓，原持仓延续到下一批）"
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


def compute_tracking() -> tuple[pd.DataFrame, pd.DataFrame, str]:
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
