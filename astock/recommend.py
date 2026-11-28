from __future__ import annotations

import json
import math
import os
import re
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import llm
from .config import BASE_DIR
from .contracts import (
    ContractError,
    action_short,
    normalize_action,
    validate_recommendation_run,
)
from .data import enrich, last_completed_trade_date, load_daily, load_index_daily, load_trade_cal, next_open_date
from .events import (
    evidence_for_stock,
    fetch_announcements,
    fetch_major_news,
    fetch_news,
    fetch_policy,
    fetch_reports,
    score_events,
    sentiment,
    stock_basic,
)
from .ledger import make_evidence_id, normalize_pub_time
from .tushare_client import TushareClient
from .utils import is_code, normalize_code

REC_DIR = BASE_DIR / "data" / "recommendations"
REC_DIR.mkdir(parents=True, exist_ok=True)
SCHEMA_VERSION = 2
# 展示技术分 = 绝对技术分与批内排名分的固定混合；详情/简报必须披露这两步，
# 否则「因子贡献 → 展示技术分」无法复算。
TECHNICAL_BLEND = {"absolute": 0.45, "rank": 0.55}
MISSING_PERCENTILE = "—"
_TLS = threading.local()


class StaleDataError(RuntimeError):
    """Data is older than the last completed trading day; do not publish a batch."""


@dataclass
class Recommendation:
    table: pd.DataFrame
    report: str
    evidence_md: str
    status: str
    csv_path: str
    json_path: str
    items: list[dict] = field(default_factory=list)
    market: str = ""
    evidence: dict[str, list[dict]] = field(default_factory=dict)
    as_of: str = ""
    data_as_of: str = ""
    tradable_from: str = ""
    run_id: str = ""
    diagnostics: dict = field(default_factory=dict)


def _thread_client() -> TushareClient:
    client = getattr(_TLS, "client", None)
    if client is None:
        client = TushareClient()
        _TLS.client = client
    return client


def parse_universe(text: str = "") -> list[str]:
    basic = stock_basic()
    by_name = {str(r["name"]): str(r["ts_code"]) for _, r in basic.iterrows()}
    by_alias: dict[str, str] = {}
    if "aliases" in basic.columns:
        for _, r in basic.iterrows():
            for alias in str(r.get("aliases") or "").split("|"):
                if alias and alias not in by_alias:
                    by_alias[alias] = str(r["ts_code"])
    valid = set(basic["ts_code"].astype(str))

    raw = (text or "").strip()
    if raw:
        parts = [p for p in re.split(r"[,，;；\s]+", raw) if p]
    else:
        path = BASE_DIR / "stocks.csv"
        if path.exists():
            df = pd.read_csv(path)
            parts = df["ts_code"].astype(str).tolist() if "ts_code" in df.columns else []
        else:
            parts = []

    codes: list[str] = []
    for part in parts:
        code = normalize_code(part)
        if is_code(code) and code in valid:
            codes.append(code)
            continue
        named = by_name.get(part.strip()) or by_alias.get(part.strip())
        if named:
            codes.append(named)
    return list(dict.fromkeys(codes))[:60]


def _load_one(code: str, start: str, end: str) -> tuple[str, pd.DataFrame | None, str]:
    try:
        df = load_daily(
            code,
            start=start,
            end=end,
            adj="qfq",
            client=_thread_client(),
        )
        return code, (enrich(df) if not df.empty else None), ""
    except ContractError:
        # schema 漂移不是"单票缺数据"：直接失败整轮 run，不静默退出候选池。
        raise
    except Exception as exc:  # noqa: BLE001 - 单票加载失败只降级为诊断，不退出整轮候选池（ContractError 已单独 raise）
        return code, None, f"{type(exc).__name__}: {exc}"


def load_many(
    codes: list[str],
    progress=None,
    years: int = 3,
    diagnostics: list[dict] | None = None,
) -> dict[str, pd.DataFrame]:
    end = pd.Timestamp.today().strftime("%Y%m%d")
    start = (
        pd.Timestamp.today() - pd.Timedelta(days=365 * max(1, int(years)))
    ).strftime("%Y%m%d")
    out: dict[str, pd.DataFrame] = {}
    total = max(1, len(codes))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_load_one, code, start, end): code for code in codes}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                code, df, error = future.result()
            except ContractError:
                raise
            except Exception as exc:  # noqa: BLE001 - future.result 的非 ContractError 异常降级为该票诊断，单票失败不拖垮整批
                code, df, error = futures[future], None, f"{type(exc).__name__}: {exc}"
            if df is not None and not df.empty:
                out[code] = df
            elif diagnostics is not None:
                diagnostics.append({"code": code, "error": error or "empty dataframe"})
            if progress is not None:
                progress(min(0.30, 0.05 + 0.30 * i / total), desc=f"加载日线 {i}/{total}")
    return out


def _contribution(
    factor: str,
    label: str,
    delta: float,
    value=None,
    unit: str = "",
    detail: str = "",
) -> dict:
    """One auditable technical-score delta.

    ``value`` is the indicator value that triggered the rule; ``unit`` tells the
    detail page how to format it (``pct``/``ratio``/``raw``).  Keeping the deltas
    next to the score changes means the arithmetic can be replayed from the JSON
    archive without re-reading prices.
    """
    return {
        "factor": factor,
        "label": label,
        "delta": round(float(delta), 4),
        "value": None if value is None else round(float(value), 6),
        "unit": unit,
        "detail": detail,
    }


def technical_score(df: pd.DataFrame) -> dict | None:
    if df is None or len(df) < 60:
        return None
    last = df.iloc[-1]
    close = float(last["close"])
    if not math.isfinite(close):
        return None

    ma20 = float(last.get("ma20", np.nan))
    ma60 = float(last.get("ma60", np.nan))
    ma120 = float(last.get("ma120", np.nan))
    rsi = float(last.get("rsi14", np.nan))
    dif = float(last.get("dif", np.nan))
    dea = float(last.get("dea", np.nan))
    hist = float(last.get("macd_hist", np.nan))
    atr = float(last.get("atr14", np.nan))
    vol_ratio = float(last["vol"] / last["vol_ma20"]) if last.get("vol_ma20") else np.nan

    ret20 = close / float(df["close"].iloc[-21]) - 1 if len(df) >= 21 else np.nan
    ret60 = close / float(df["close"].iloc[-61]) - 1 if len(df) >= 61 else np.nan
    high250 = float(df["high"].tail(250).max())
    drawdown = close / high250 - 1 if high250 else np.nan
    atr_pct = atr / close if close and math.isfinite(atr) else np.nan

    score = 50.0
    contributions = [_contribution("base", "基础分", 50.0, detail="起始 50 分")]
    reasons: list[str] = []

    if math.isfinite(ma20):
        if close > ma20:
            score += 8
            contributions.append(
                _contribution(
                    "ma20", "站上MA20", 8, close / ma20 - 1 if ma20 else None,
                    "ratio", f"收盘 {close:.2f} / MA20 {ma20:.2f}",
                )
            )
            reasons.append("站上MA20")
        if math.isfinite(ma60) and ma20 > ma60:
            score += 8
            contributions.append(
                _contribution(
                    "ma20_ma60", "MA20>MA60", 8, (ma20 - ma60) / ma60 if ma60 else None,
                    "ratio", f"MA20 {ma20:.2f} / MA60 {ma60:.2f}",
                )
            )
            reasons.append("MA20>MA60")
        if math.isfinite(ma60) and close > ma60:
            score += 5
            contributions.append(
                _contribution(
                    "ma60", "站上MA60", 5, close / ma60 - 1 if ma60 else None,
                    "ratio", f"收盘 {close:.2f} / MA60 {ma60:.2f}",
                )
            )
            reasons.append("站上MA60")
        if math.isfinite(ma120) and ma20 > ma120:
            score += 4
            contributions.append(
                _contribution(
                    "ma20_ma120", "MA20>MA120", 4, (ma20 - ma120) / ma120 if ma120 else None,
                    "ratio", f"MA20 {ma20:.2f} / MA120 {ma120:.2f}",
                )
            )
            reasons.append("MA20>MA120")
    if math.isfinite(hist):
        if hist > 0:
            score += 7
            contributions.append(
                _contribution("macd_hist", "MACD柱为正", 7, hist, "raw", f"MACD柱 {hist:+.4f}")
            )
            reasons.append("MACD柱为正")
        if math.isfinite(dif) and math.isfinite(dea) and dif > dea:
            score += 3
            contributions.append(
                _contribution("dif_dea", "DIF>DEA", 3, dif - dea, "raw", f"DIF {dif:.4f} / DEA {dea:.4f}")
            )
    if math.isfinite(rsi):
        if 45 <= rsi <= 70:
            score += 5
            contributions.append(
                _contribution("rsi", "RSI健康", 5, rsi, "raw", f"RSI {rsi:.1f} 位于 45–70")
            )
            reasons.append("RSI健康")
        elif rsi > 80:
            score -= 8
            contributions.append(
                _contribution("rsi", "RSI超买", -8, rsi, "raw", f"RSI {rsi:.1f} > 80")
            )
            reasons.append("RSI超买")
        elif rsi < 30:
            score -= 3
            contributions.append(
                _contribution("rsi", "RSI超卖", -3, rsi, "raw", f"RSI {rsi:.1f} < 30")
            )
            reasons.append("RSI超卖")
    if math.isfinite(vol_ratio):
        if 1.1 <= vol_ratio <= 2.5:
            score += 4
            contributions.append(
                _contribution("vol_ratio", "温和放量", 4, vol_ratio, "raw", f"量比 {vol_ratio:.2f}")
            )
            reasons.append("温和放量")
        elif vol_ratio > 3:
            score -= 5
            contributions.append(
                _contribution("vol_ratio", "量能过热", -5, vol_ratio, "raw", f"量比 {vol_ratio:.2f} > 3")
            )
            reasons.append("量能过热")
    if math.isfinite(ret20):
        delta = 5 if ret20 > 0 else -3
        score += delta
        if ret20 > 0:
            label = "20日动量为正"
        elif ret20 < 0:
            label = "20日动量为负"
        else:
            label = "20日动量持平（按 −3 计）"
        contributions.append(
            _contribution("ret20", label, delta, ret20, "pct", f"20日收益 {ret20:+.2%}")
        )
    if math.isfinite(ret60) and ret60 > 0:
        score += 3
        contributions.append(
            _contribution("ret60", "60日动量为正", 3, ret60, "pct", f"60日收益 {ret60:+.2%}")
        )
    if math.isfinite(atr_pct) and atr_pct > 0.07:
        score -= 5
        contributions.append(
            _contribution("atr_pct", "波动偏高", -5, atr_pct, "pct", f"ATR/收盘 {atr_pct:.2%} > 7%")
        )
        reasons.append("波动偏高")
    if math.isfinite(drawdown) and drawdown < -0.25:
        score -= 5
        contributions.append(
            _contribution("drawdown", "距高点回撤大", -5, drawdown, "pct", f"距250日高点 {drawdown:.2%}")
        )
        reasons.append("距高点回撤大")

    raw_score = score
    score = max(0.0, min(100.0, score))
    if score != raw_score:
        contributions.append(
            _contribution(
                "clip", "0–100 截断", score - raw_score, raw_score, "raw",
                f"截断前 {raw_score:.1f}",
            )
        )
    if math.isfinite(ma20) and math.isfinite(ma60):
        if close > ma20 > ma60:
            trend = "多头"
        elif close < ma20 < ma60:
            trend = "空头"
        elif close > ma20 and close > ma60:
            trend = "震荡偏多"
        elif close < ma20 and close < ma60:
            trend = "震荡偏空"
        else:
            trend = "震荡"
    else:
        trend = "震荡"
    return {
        "technical_score": round(score, 1),
        "score_before_clip": round(raw_score, 1),
        "contributions": contributions,
        "close": round(close, 2),
        "ret20": ret20,
        "ret60": ret60,
        "rsi": rsi,
        "vol_ratio": vol_ratio,
        "atr_pct": atr_pct,
        "drawdown": drawdown,
        "trend": trend,
        "reasons": reasons,
    }


def _action(score: float) -> str:
    """研究语义：只描述技术面强弱，不做买卖动作。"""
    if score >= 68:
        return "技术面强"
    if score >= 52:
        return "技术面中性"
    return "技术面弱"


def percentile_basis(n: int) -> str:
    """分位口径文案：分位一律在候选池内计算，展示时必须带上 N。"""
    return f"候选池分位（N={int(n)}）" if n else "候选池分位"


def assign_percentiles(rows: list[dict], candidate_n: int) -> list[dict]:
    """按综合分取候选池并写入分位，返回候选池。

    live 与 backfill 共用同一基准：候选池（N=min(candidate_n, len(rows))）内
    rank(pct)，因此任意批次的分位都能按同一口径复算。
    """
    pool = sorted(rows, key=lambda r: r["composite"], reverse=True)[: max(1, int(candidate_n))]
    for row, pct in zip(pool, pool_percentile([r["composite"] for r in pool])):
        row["percentile"] = pct
    return pool


def pool_percentile(scores) -> list[float | None]:
    """候选池内综合分分位（0-100，越大越靠前）；非有限值给 None。"""
    values = [float(s) for s in scores]
    ranked = pd.Series(values, dtype=float).rank(pct=True) * 100.0
    return [
        round(float(x), 1) if math.isfinite(v) and math.isfinite(x) else None
        for v, x in zip(values, ranked)
    ]


def _display_percentile(value):
    """池内分位展示值：数值原样保留，缺失/非有限值显示占位符。"""
    if isinstance(value, bool) or value is None:
        return MISSING_PERCENTILE
    try:
        return float(value) if math.isfinite(float(value)) else MISSING_PERCENTILE
    except (TypeError, ValueError):
        return MISSING_PERCENTILE


def normalize_table(table) -> pd.DataFrame:
    """统一推荐表列：旧批次的「建议」列映射为「技术面」，并补齐「技术分位」。"""
    df = table.copy() if isinstance(table, pd.DataFrame) else pd.DataFrame(list(table or []))
    if "建议" in df.columns:
        df = df.rename(columns={"建议": "技术面"}) if "技术面" not in df.columns else df.drop(columns=["建议"])
    if "技术面" not in df.columns:
        if "综合分" in df.columns:
            df["技术面"] = df["综合分"].map(
                lambda s: _action(float(s)) if pd.notna(s) else ""
            )
        else:
            df["技术面"] = ""
    df["技术面"] = df["技术面"].map(normalize_action)
    if "技术分位" not in df.columns:
        df["技术分位"] = MISSING_PERCENTILE
    else:
        df["技术分位"] = df["技术分位"].map(_display_percentile)
    preferred = [
        "排名", "代码", "名称", "行业", "综合分", "技术分位", "技术分",
        "事件分", "收盘", "20日%", "RSI", "技术面", "理由",
    ]
    if set(preferred) & set(df.columns):
        df = df[[c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]]
    return df


def _fallback_reason(row: dict, evidence: list[dict]) -> tuple[str, list[str], list[str], str, float]:
    parts = [row.get("trend", "震荡")]
    if row.get("reasons"):
        parts.append("、".join(row["reasons"][:3]))
    ret20 = row.get("ret20")
    if ret20 is not None and math.isfinite(ret20):
        parts.append(f"20日{ret20:+.1%}")
    if math.isfinite(row.get("rsi", np.nan)):
        parts.append(f"RSI {row['rsi']:.1f}")
    if evidence:
        parts.append(f"近期{evidence[0]['kind']}：{evidence[0]['title'][:40]}")
    reason = "；".join(parts)

    catalysts = [f"{e['kind']}：{e['title'][:50]}" for e in evidence[:2]]
    risks = []
    if math.isfinite(row.get("rsi", np.nan)) and row["rsi"] > 75:
        risks.append("短期超买，追高风险")
    if any(e["kind"] == "公告" and sentiment(e["title"])[1] > sentiment(e["title"])[0] for e in evidence):
        risks.append("存在负面公告")
    if not risks:
        risks.append("市场系统性风险；单票集中风险")
    return reason, catalysts, risks, _action(row["composite"]), min(0.85, max(0.35, row["composite"] / 100))


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):  # LLM 输出非 JSON 时退回空结果
        return {}


def _llm_rank(rows: list[dict], evidence_map: dict[str, list[dict]], market: str) -> dict:
    lines = [
        "你是A股研究员。只能使用下面提供的数据，不得编造新闻、价格或业绩。",
        ("为每只候选给出 reason、catalysts、risks、evidence（引用的 E 编号）；"
        "不要给出买卖建议或动作词——技术面强弱由规则按综合分给出（池内分位仅作横截面展示），"
        "你只负责解释证据。"),
        "输出严格 JSON：{\"market\":\"一句话市场判断\",\"items\":[{\"code\":\"600519.SH\",\"reason\":\"...\",\"catalysts\":[\"...\"],\"risks\":[\"...\"],\"evidence\":[\"E1\"]}],\"risk_note\":\"...\"}",
        "",
        f"市场背景：{market}",
        "",
        "候选：",
    ]
    eid = 0
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}) {row['code']} {row['name']} 行业={row['industry']} "
            f"综合={row['composite']:.1f} 技术={row['technical_score']:.1f} 事件={row['event_score']:.1f} "
            f"收盘={row['close']} 20日={row['ret20']*100 if math.isfinite(row.get('ret20', np.nan)) else float('nan'):.1f}% "
            f"RSI={row['rsi']:.1f} 趋势={row['trend']}"
        )
        for ev in evidence_map.get(row["code"], [])[:3]:
            eid += 1
            date = pd.Timestamp(ev["pub_time"]).strftime("%Y-%m-%d")
            lines.append(f"E{eid} [{date} {ev['kind']}] {ev['title'][:70]}（{ev.get('source','')}）")
    lines.append("")
    lines.append("只输出 JSON，不要输出其他文字。")

    try:
        raw = llm.chat(
            [
                {"role": "system", "content": "你是严谨的A股研究员，只基于给定证据写结论。"},
                {"role": "user", "content": "\n".join(lines)},
            ],
            max_tokens=1600,
            temperature=0.2,
        )
        return _parse_json(raw)
    except Exception:  # noqa: BLE001 - LLM/网关异常类型不一，失败退回规则理由（ADR-009）
        return {}


def _market_snapshot(
    daily_map: dict[str, pd.DataFrame],
    major: pd.DataFrame | None,
    policy: pd.DataFrame | None,
) -> str:
    try:
        end = pd.Timestamp.today().strftime("%Y%m%d")
        start = (pd.Timestamp.today() - pd.Timedelta(days=365)).strftime("%Y%m%d")
        idx = load_index_daily("000300.SH", start=start, end=end)
        idx_e = enrich(idx)
        last = idx_e.iloc[-1]
        idx_ret20 = last["close"] / idx_e["close"].iloc[-21] - 1 if len(idx_e) > 21 else np.nan
        idx_trend = "站上MA20" if last["close"] > last["ma20"] else "低于MA20"
        idx_text = f"沪深300 近20日 {idx_ret20:+.1%}，{idx_trend}" if math.isfinite(idx_ret20) else "沪深300 数据不足"
    except ContractError:
        # 指数 schema 漂移与其他入口一致：显式失败，不降级成"数据不可用"。
        raise
    except Exception:  # noqa: BLE001 - ContractError 已单独 raise；其余异常只降级指数文案
        idx_text = "沪深300 数据不可用"

    valid = [df for df in daily_map.values() if len(df) >= 30]
    breadth = np.nan
    avg20 = np.nan
    if valid:
        above = [df.iloc[-1]["close"] > df.iloc[-1]["ma20"] for df in valid if pd.notna(df.iloc[-1].get("ma20"))]
        breadth = sum(above) / len(above) if above else np.nan
        rets = [df["close"].iloc[-1] / df["close"].iloc[-21] - 1 for df in valid if len(df) > 21]
        avg20 = float(np.mean(rets)) if rets else np.nan

    parts = [idx_text]
    if math.isfinite(breadth):
        parts.append(f"股票池 {breadth:.0%} 站上MA20")
    if math.isfinite(avg20):
        parts.append(f"池内平均20日 {avg20:+.1%}")
    if major is not None and not major.empty:
        parts.append("要闻：" + "；".join(major["title"].head(3).tolist()))
    if policy is not None and not policy.empty:
        parts.append("政策：" + "；".join(policy["title"].head(2).tolist()))
    return "。".join(parts) + "。"


def _report_markdown(
    result_rows: list[dict],
    market: str,
    ai: dict,
    as_of: str,
    tradable_from: str = "",
    candidate_n: int = 0,
) -> str:
    ai_items = {str(x.get("code")): x for x in (ai.get("items") or []) if isinstance(x, dict)}
    lines = [
        f"# 每日推荐 · {as_of}",
        "",
        (f"**数据截止**：{as_of} 收盘；**可成交起点**：{tradable_from or '下一交易日'} 开盘；"
        f"生成时间 {datetime.now():%Y-%m-%d %H:%M}"),
        "",
        f"**市场背景**：{market}",
        "",
        "## 今日候选",
    ]
    for i, row in enumerate(result_rows, 1):
        ai_row = ai_items.get(row["code"], {})
        action = normalize_action(row.get("action")) or _action(row["composite"])
        pct = row.get("percentile")
        pct_label = percentile_basis(candidate_n or len(result_rows))
        pct_text = (
            f"{pct_label}：{pct:.0f}%"
            if isinstance(pct, (int, float)) and math.isfinite(pct)
            else f"{pct_label}：—"
        )
        reason = ai_row.get("reason") or row.get("reason", "")
        lines.append(
            f"### {i}. {row['code']} {row['name']} · 综合 {row['composite']:.1f}"
        )
        lines.append(
            f"- **技术面**：{action_short(action)}；{pct_text}；技术 {row['technical_score']:.1f} / 事件 {row['event_score']:.1f}；"
            f"收盘 {row['close']}，20日 {row['ret20']*100:+.1f}%，RSI {row['rsi']:.1f}"
        )
        lines.append(f"- **逻辑**：{reason}")
        catalysts = ai_row.get("catalysts") or row.get("catalysts") or []
        risks = ai_row.get("risks") or row.get("risks") or []
        if catalysts:
            lines.append("- **催化**：" + "；".join(str(x) for x in catalysts[:3]))
        if risks:
            lines.append("- **风险**：" + "；".join(str(x) for x in risks[:3]))
        lines.append("")

    risk_note = ai.get("risk_note") or "以上为量化+文本证据的初筛结果，不构成投资建议；需结合仓位、流动性和自身风险承受能力复核。"
    lines.extend(
        [
            "## 风险提示",
            risk_note,
            "",
            ("> 数据来源：Tushare 日线/公告/研报/新闻/政策库；AI 只做归因和表述，不改变量化排序。"
            "本页仅供研究，不构成投资建议。"),
        ]
    )
    return "\n".join(lines)


def _evidence_markdown(result_rows: list[dict], evidence_map: dict[str, list[dict]]) -> str:
    lines = ["### 证据链", ""]
    eid = 0
    for row in result_rows:
        lines.append(f"**{row['code']} {row['name']}**")
        items = evidence_map.get(row["code"], [])
        if not items:
            lines.append("- 暂无可匹配的公告/研报/新闻/政策。")
        for ev in items[:4]:
            eid += 1
            date = pd.Timestamp(ev["pub_time"]).strftime("%Y-%m-%d")
            title = str(ev["title"])
            if ev.get("url"):
                title = f"[{title}]({ev['url']})"
            lines.append(f"- [E{eid}] {date} · {ev['kind']} · {title}（{ev.get('source', '')}）")
        lines.append("")
    return "\n".join(lines)


def _clean_json(obj):
    """Recursively remove NaN/Inf/NaT so json.dumps(allow_nan=False) is safe."""
    if obj is None or obj is pd.NaT:
        return None
    if isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if isinstance(obj, np.ndarray):
        return [_clean_json(x) for x in obj.tolist()]
    if isinstance(obj, dict):
        return {str(k): _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_clean_json(x) for x in obj]
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return str(obj)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save(
    result_rows: list[dict],
    market: str,
    ai: dict,
    evidence_map: dict[str, list[dict]],
    data_as_of: str,
    tradable_from: str,
    run_id: str,
    generated_at: str,
    source: str = "live",
    params: dict | None = None,
    diagnostics: dict | None = None,
    report_md: str = "",
    status_line: str = "",
) -> tuple[str, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    table = pd.DataFrame(
        [
            {
                "排名": i,
                "代码": row["code"],
                "名称": row["name"],
                "行业": row["industry"],
                "综合分": round(row["composite"], 1),
                "技术分位": row.get("percentile"),
                "技术分": round(row["technical_score"], 1),
                "事件分": round(row["event_score"], 1),
                "收盘": row["close"],
                "20日%": round(row["ret20"] * 100, 2) if math.isfinite(row.get("ret20", np.nan)) else None,
                "RSI": round(row["rsi"], 1) if math.isfinite(row.get("rsi", np.nan)) else None,
                "技术面": normalize_action(row.get("action")) or _action(row["composite"]),
                "理由": row.get("reason", ""),
            }
            for i, row in enumerate(result_rows, 1)
        ]
    )
    suffix = run_id[-8:] if run_id else uuid.uuid4().hex[:8]
    csv_path = REC_DIR / f"recommend_{stamp}_{suffix}.csv"

    weights = dict(
        (params or {}).get("composite_weights")
        or {"technical": 0.65, "event": 0.35}
    )
    # 证据链落盘：单条推荐的因子贡献、信号快照与 evidence_id 必须能脱离
    # 页面从 JSON/台账复算；旧批次没有该字段时读取侧降级，不伪造贡献。
    items_detail = {}
    for i, row in enumerate(result_rows, 1):
        code = str(row["code"])
        evs = evidence_map.get(code) or []
        items_detail[code] = {
            "rank": i,
            "code": code,
            "name": row.get("name", ""),
            "industry": row.get("industry", ""),
            "run_id": run_id,
            "data_as_of": data_as_of,
            "tradable_from": tradable_from,
            "generated_at": generated_at,
            "source": source,
            "scores": {
                "composite": round(row["composite"], 1),
                "technical": round(row["technical_score"], 1),
                "technical_raw": round(row.get("technical_raw", row["technical_score"]), 1),
                "technical_rank": row.get("technical_rank"),
                "score_before_clip": row.get("score_before_clip"),
                "event": round(row["event_score"], 1),
                "percentile": row.get("percentile"),
            },
            "weights": weights,
            "technical_blend": TECHNICAL_BLEND,
            "factor_contributions": row.get("contributions") or [],
            "signals": {
                key: row.get(key)
                for key in (
                    "close", "ret20", "ret60", "rsi", "vol_ratio",
                    "atr_pct", "drawdown", "trend",
                )
            },
            "event_stats": row.get("stats") or {},
            "evidence_ids": [make_evidence_id(run_id, code, ev) for ev in evs],
            "reason": row.get("reason", ""),
            "catalysts": row.get("catalysts") or [],
            "risks": row.get("risks") or [],
            "confidence": row.get("confidence"),
        }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "as_of": data_as_of,
        "data_as_of": data_as_of,
        "tradable_from": tradable_from,
        "source": source,
        "params": params or {},
        "diagnostics": diagnostics or {},
        "market": market,
        "ai": ai,
        "report_md": report_md,
        "status_line": status_line,
        "table": table.to_dict("records"),
        "items_detail": items_detail,
        "evidence": {
            code: [
                {
                    "evidence_id": make_evidence_id(run_id, code, ev),
                    "kind": ev.get("kind", ""),
                    "pub_time": normalize_pub_time(ev.get("pub_time")),
                    "title": ev.get("title", ""),
                    "source": ev.get("source", ""),
                    "url": ev.get("url", ""),
                    "content": ev.get("content", ""),
                }
                for ev in items
            ]
            for code, items in evidence_map.items()
        },
    }
    payload = _clean_json(payload)
    # 写盘前过 RecommendationRun 契约（DATA-01：schema_version=2、必填字段、
    # 证据字段、无 NaN）；失败抛 ContractError，不再静默产出残缺 JSON 或空结果。
    payload = validate_recommendation_run(payload, source=f"recommend._save({run_id})")
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    tmp_csv = csv_path.with_suffix(".csv.tmp")
    table.to_csv(tmp_csv, index=False, encoding="utf-8-sig")
    os.replace(tmp_csv, csv_path)
    json_path = REC_DIR / f"recommend_{stamp}_{suffix}.json"
    _atomic_write_text(json_path, text)
    _atomic_write_text(REC_DIR / "latest.json", text)
    try:
        from .ledger import save_run

        save_run(payload, json_path=str(json_path), csv_path=str(csv_path))
    except Exception:  # noqa: BLE001, S110 - JSON 仍是主产物；ledger 故障不应阻断推荐生成
        pass
    return str(csv_path), str(json_path)


def _pick_diverse(rows: list[dict], n: int, max_per_industry: int = 2) -> list[dict]:
    """按综合分排序，但同一行业最多取 N 只；池内行业不足时宁缺毋滥。"""
    picked: list[dict] = []
    counts: dict[str, int] = {}
    for row in rows:
        industry = row.get("industry", "") or "其他"
        if counts.get(industry, 0) >= max_per_industry:
            continue
        picked.append(row)
        counts[industry] = counts.get(industry, 0) + 1
        if len(picked) >= n:
            return picked
    return picked


def generate_recommendation(
    universe_text: str = "",
    top_n: int = 5,
    candidate_n: int = 15,
    news_days: int = 7,
    use_llm: bool = True,
    progress=None,
) -> Recommendation:
    started = time.time()
    top_n = max(1, min(10, int(top_n)))
    candidate_n = max(top_n, min(30, int(candidate_n)))
    news_days = max(1, min(30, int(news_days)))
    now = datetime.now()
    generated_at = now.isoformat(timespec="seconds")
    run_id = f"rec_{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"

    def step(value: float, text: str) -> None:
        if progress is not None:
            progress(value, desc=text)

    step(0.02, "读取股票池")
    codes = parse_universe(universe_text)
    if not codes:
        raise ValueError("股票池为空：请在 stocks.csv 中加入代码，或手动输入 6 位代码")

    basic = stock_basic()
    meta = basic.set_index("ts_code")
    failed: list[dict] = []
    step(0.05, f"加载 {len(codes)} 只股票日线")
    daily_map = load_many(codes, progress, diagnostics=failed)

    # 停牌/长期无新 bar 的股票不参与打分，也不再拉高全池 as_of
    last_dates = {
        code: pd.Timestamp(df["trade_date"].max())
        for code, df in daily_map.items()
        if df is not None and not df.empty
    }
    if not last_dates:
        detail = "；".join(f"{x['code']}:{x['error']}" for x in failed[:8]) or "全部为空"
        raise ValueError(f"没有可计算技术分的股票；加载失败明细：{detail}")
    market_as_of = max(last_dates.values())
    expected = last_completed_trade_date(now)
    if expected is not None and pd.Timestamp(market_as_of) < pd.Timestamp(expected) and now.hour >= 15:
        raise StaleDataError(
            f"日线数据未更新：data_as_of={market_as_of:%Y-%m-%d} < expected={expected:%Y-%m-%d}；"
            "本次不生成推荐，等待数据源更新后重试。"
        )
    trade_cal = load_trade_cal()
    open_days = trade_cal.loc[trade_cal["is_open"] == 1, "cal_date"]

    def _gap(last, market) -> int:
        return int(
            ((open_days > pd.Timestamp(last)) & (open_days <= pd.Timestamp(market))).sum()
        )

    stale_codes = [
        code for code, last in last_dates.items()
        if _gap(last, market_as_of) > 5
    ]
    for code in stale_codes:
        daily_map.pop(code, None)
        failed.append({"code": code, "error": f"stale last_bar={last_dates[code]:%Y-%m-%d}"})
    if not daily_map:
        raise ValueError("全部股票都停牌/数据过期，无法生成推荐")

    step(0.36, "计算技术分")
    rows = []
    for code, df in daily_map.items():
        ts = technical_score(df)
        if not ts:
            failed.append({"code": code, "error": "insufficient history for technical score"})
            continue
        info = meta.loc[code] if code in meta.index else None
        rows.append(
            {
                "code": code,
                "name": str(info["name"]) if info is not None else code,
                "industry": str(info["industry"]) if info is not None else "",
                "last_bar_date": pd.Timestamp(df["trade_date"].max()).strftime("%Y-%m-%d"),
                **ts,
            }
        )
    if not rows:
        raise ValueError("没有可计算技术分的股票（日线不足 60 根）")
    rows.sort(key=lambda x: x["technical_score"], reverse=True)

    # 绝对分容易在 100 附近饱和；混入横截面排名，避免所有候选都是 100 分
    raw_scores = pd.Series([r["technical_score"] for r in rows], dtype=float)
    ranks = raw_scores.rank(pct=True) * 100.0
    for row, rank in zip(rows, ranks):
        row["technical_raw"] = row["technical_score"]
        row["technical_rank"] = float(rank)
        row["technical_score"] = round(
            TECHNICAL_BLEND["absolute"] * row["technical_raw"]
            + TECHNICAL_BLEND["rank"] * row["technical_rank"],
            1,
        )
    rows.sort(key=lambda x: x["technical_score"], reverse=True)
    candidates = rows[:candidate_n]

    step(0.46, "抓取公告/研报/新闻/政策")
    cand_codes = [r["code"] for r in candidates]
    announcements = fetch_announcements(cand_codes, days=30)
    reports = fetch_reports(days=7)
    news = fetch_news(days=min(3, news_days))
    major = fetch_major_news(days=news_days)
    policy = fetch_policy(days=news_days)

    step(0.64, "聚合事件分")
    evidence_map: dict[str, list[dict]] = {}
    evidence_dropped = 0
    generated_ts = pd.Timestamp(generated_at)
    for row in candidates:
        evidence = evidence_for_stock(
            row["code"],
            row["name"],
            row["industry"],
            announcements,
            reports,
            news,
            major,
            policy,
            limit=8,
        )
        kept = []
        for ev in evidence:
            try:
                pub = pd.Timestamp(ev.get("pub_time"))
                if pub > generated_ts:
                    evidence_dropped += 1
                    continue
            except (TypeError, ValueError):  # 发布时间非法时丢弃该条证据
                evidence_dropped += 1
                continue
            kept.append(ev)
        evidence = kept
        event_score, stats = score_events(evidence)
        evidence_map[row["code"]] = evidence
        row["evidence"] = evidence
        row["event_score"] = event_score
        row["stats"] = stats
        row["composite"] = 0.65 * row["technical_score"] + 0.35 * event_score
        row["action"] = _action(row["composite"])
        reason, catalysts, risks, action, confidence = _fallback_reason(row, evidence)
        row["reason"] = reason
        row["catalysts"] = catalysts
        row["risks"] = risks
        row["action"] = action
        row["confidence"] = confidence

    candidates.sort(key=lambda x: x["composite"], reverse=True)
    # ADR-010 C1：分位基准 = 候选池（N=candidate_n），与 backfill 共用 assign_percentiles。
    assign_percentiles(candidates, len(candidates))
    ai_rows = candidates[: min(8, len(candidates))]

    step(0.78, "生成市场背景与 AI 归因")
    market = _market_snapshot(daily_map, major, policy)
    ai = _llm_rank(ai_rows, evidence_map, market) if use_llm else {}
    ai_items = {str(x.get("code")): x for x in (ai.get("items") or []) if isinstance(x, dict)}
    for row in candidates:
        ai_row = ai_items.get(row["code"])
        if ai_row:
            # AI-01：action 由规则决定，LLM 只能补充理由/催化/风险。
            row["reason"] = ai_row.get("reason") or row["reason"]
            if ai_row.get("catalysts"):
                row["catalysts"] = ai_row["catalysts"]
            if ai_row.get("risks"):
                row["risks"] = ai_row["risks"]

    final_rows = _pick_diverse(candidates, top_n, max_per_industry=2)
    data_as_of_ts = market_as_of
    data_as_of = pd.Timestamp(data_as_of_ts).strftime("%Y-%m-%d")
    expected = last_completed_trade_date(now)
    stale_data = expected is not None and pd.Timestamp(data_as_of) < pd.Timestamp(expected)
    next_open = next_open_date(data_as_of_ts)
    # 若下一个开市日就是今天且已经开盘，今天开盘价不可再获得，顺延到再下一个开市日
    if (
        next_open is not None
        and pd.Timestamp(next_open).normalize() == pd.Timestamp(now.date())
        and (now.hour, now.minute) >= (9, 30)
    ):
        next_open = next_open_date(next_open)
    tradable_from = (
        pd.Timestamp(next_open).strftime("%Y-%m-%d")
        if next_open is not None
        else (pd.Timestamp(data_as_of_ts) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    )
    diagnostics = {
        "failed_codes": failed,
        "stale_codes": stale_codes,
        "evidence_dropped": evidence_dropped,
        "generated_at": generated_at,
        "expected_trade_date": expected.strftime("%Y-%m-%d") if expected is not None else None,
        "data_as_of": data_as_of,
        "tradable_from": tradable_from,
        "stale_data": bool(stale_data),
        "industry_relaxed": len(final_rows) < top_n,
        # 审计链：分位在候选池内计算，落盘只保留 final_rows，因此把池快照一并写入，
        # 供「读回 JSON → 对 candidate_pool 复算 pool_percentile → 与 table 逐行相等」复现。
        "candidate_n": len(candidates),
        "candidate_pool": [
            {"code": r["code"], "composite": float(r["composite"])} for r in candidates
        ],
        "percentile_basis": "candidate_pool",
    }
    report = _report_markdown(
        final_rows, market, ai, data_as_of, tradable_from, candidate_n=len(candidates)
    )
    evidence_md = _evidence_markdown(final_rows, evidence_map)

    elapsed = time.time() - started
    ai_ok = bool((ai or {}).get("items"))
    industry_note = ""
    if len(final_rows) < top_n:
        industry_note = f" · 行业上限导致输出少于 {top_n} 只"
    status = (
        f"股票池 {len(codes)} 只 · 有效日线 {len(rows)} 只 · 技术预筛 {len(candidates)} · "
        f"输出 {len(final_rows)} 只（同行业最多 2 只）{industry_note} · "
        f"新闻 {min(3, news_days)} 天 / 公告 30 天 / 研报 7 天 / 政策 {news_days} 天 · "
        f"AI归因 {'已生成' if ai_ok else '未生成（使用规则理由）'} · 用时 {elapsed:.1f}s · "
        f"data_as_of {data_as_of} · tradable_from {tradable_from} · run_id {run_id}"
    )
    if failed:
        status += f" · 失败/剔除 {len(failed)} 只（详见 JSON diagnostics）"
    step(0.92, "保存结果")
    params = {
        "top_n": top_n,
        "candidate_n": candidate_n,
        "news_days": news_days,
        "use_llm": bool(use_llm),
        "composite_weights": {"technical": 0.65, "event": 0.35},
    }
    csv_path, json_path = _save(
        final_rows,
        market,
        ai,
        {r["code"]: evidence_map[r["code"]] for r in final_rows},
        data_as_of,
        tradable_from,
        run_id,
        generated_at,
        source="live",
        params=params,
        diagnostics=diagnostics,
        report_md=report,
        status_line=status,
    )
    return Recommendation(
        table=pd.DataFrame(
            [
                {
                    "排名": i,
                    "代码": r["code"],
                    "名称": r["name"],
                    "行业": r["industry"],
                    "综合分": round(r["composite"], 1),
                    "技术分位": r.get("percentile"),
                    "技术分": round(r["technical_score"], 1),
                    "事件分": round(r["event_score"], 1),
                    "收盘": r["close"],
                    "20日%": round(r["ret20"] * 100, 2) if math.isfinite(r.get("ret20", np.nan)) else None,
                    "RSI": round(r["rsi"], 1) if math.isfinite(r.get("rsi", np.nan)) else None,
                    "技术面": normalize_action(r.get("action")) or _action(r["composite"]),
                    "理由": r["reason"],
                }
                for i, r in enumerate(final_rows, 1)
            ]
        ),
        report=report,
        evidence_md=evidence_md,
        status=status,
        csv_path=csv_path,
        json_path=json_path,
        items=final_rows,
        market=market,
        evidence={r["code"]: evidence_map[r["code"]] for r in final_rows},
        as_of=data_as_of,
        data_as_of=data_as_of,
        tradable_from=tradable_from,
        run_id=run_id,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# 证据链：单条详情 / 批次复盘 / 单批简报
# 所有函数只读推荐结果（JSON payload 或 ledger.get_run 的 dict），不重算推荐
# 排序、不改 alpha 声明；因此页面和导出共用同一套可追溯口径。
# ---------------------------------------------------------------------------


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_pct(value) -> str:
    number = _num(value)
    return "—" if number is None else f"{number * 100:+.2f}%"


def _fmt_score(value) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:.1f}"


def _fmt_contribution_value(item: dict) -> str:
    value = _num(item.get("value"))
    if value is None:
        return "—"
    unit = item.get("unit") or ""
    if unit == "pct":
        return f"{value * 100:+.2f}%"
    if unit == "ratio":
        return f"{value:+.4f}"
    if abs(value) >= 10:
        return f"{value:.2f}"
    return f"{value:+.4f}"


def recommendation_detail(run: dict | None, code: str) -> dict | None:
    """Assemble one recommendation's auditable detail from a persisted run."""
    if not run:
        return None
    code = str(code or "").strip()
    items = list(run.get("items") or [])
    item = next((x for x in items if str(x.get("code")) == code), None)
    if item is None:
        return None

    run_id = str(run.get("run_id") or "")
    details = run.get("items_detail")
    stored = details.get(code) if isinstance(details, dict) else None
    stored = stored or {}

    scores: list[float] = []
    for row in items:
        number = _num(row.get("composite"))
        if number is not None:
            scores.append(number)
    batch_size = len(items)
    mean = sum(scores) / len(scores) if scores else None
    median = float(statistics.median(scores)) if scores else None
    top_score = max(scores) if scores else None

    industry = item.get("industry")
    same = [x for x in items if x.get("industry") == industry]
    same_sorted = sorted(
        same, key=lambda x: (x.get("rank") is None, x.get("rank") or 0)
    )
    same_rank = next(
        (i for i, x in enumerate(same_sorted, 1) if str(x.get("code")) == code), None
    )
    ranked = sorted(items, key=lambda x: (x.get("rank") is None, x.get("rank") or 0))

    evidence = []
    for ev in run.get("evidence") or []:
        if str(ev.get("code")) != code:
            continue
        entry = dict(ev)
        entry["evidence_id"] = entry.get("evidence_id") or make_evidence_id(
            run_id, code, entry
        )
        evidence.append(entry)
    evidence.sort(key=lambda x: str(x.get("pub_time") or ""), reverse=True)

    tracking = {}
    for row in run.get("tracking") or []:
        if str(row.get("code")) != code:
            continue
        horizon = int(row.get("horizon") or 0)
        tracking[str(horizon)] = {
            "horizon": horizon,
            "entry_date": row.get("entry_date"),
            "exit_date": row.get("exit_date"),
            "ret": _num(row.get("ret")),
            "excess": _num(row.get("excess")),
        }

    contributions = list(stored.get("factor_contributions") or [])
    notes = []
    if not stored:
        notes.append("该批次未落盘因子贡献（旧批次），仅保留技术分/事件分与规则理由。")
    elif not contributions:
        notes.append("该批次未记录逐条因子贡献。")

    stored_scores = stored.get("scores") or {}
    return {
        "run_id": run_id,
        "data_as_of": run.get("data_as_of") or run.get("as_of") or "",
        "tradable_from": run.get("tradable_from") or "",
        "generated_at": run.get("generated_at") or "",
        "source": run.get("source") or "",
        "item": {
            key: item.get(key)
            for key in (
                "rank", "code", "name", "industry", "composite", "technical",
                "event", "percentile", "close", "ret20", "rsi", "action", "reason",
            )
        },
        "rank": item.get("rank"),
        "batch_size": batch_size,
        "peer": {
            "rank": item.get("rank"),
            "n": batch_size,
            "mean": mean,
            "median": median,
            "max": top_score,
            "percentile": _num(item.get("percentile")),
            "same_industry": industry,
            "same_industry_count": len(same),
            "same_industry_rank": same_rank,
            "top": [
                {
                    "rank": x.get("rank"),
                    "code": x.get("code"),
                    "name": x.get("name"),
                    "composite": _num(x.get("composite")),
                    "technical": _num(x.get("technical")),
                    "event": _num(x.get("event")),
                    "percentile": _num(x.get("percentile")),
                }
                for x in ranked[:10]
            ],
        },
        "scores": {
            "composite": _num(item.get("composite")),
            "technical": _num(item.get("technical")),
            "technical_raw": _num(stored_scores.get("technical_raw")),
            "technical_rank": _num(stored_scores.get("technical_rank")),
            "score_before_clip": _num(stored_scores.get("score_before_clip")),
            "event": _num(item.get("event")),
            "percentile": _num(item.get("percentile")),
        },
        "weights": stored.get("weights") or {"technical": 0.65, "event": 0.35},
        "technical_blend": stored.get("technical_blend") or TECHNICAL_BLEND,
        "factor_contributions": contributions,
        "signals": stored.get("signals") or {},
        "event_stats": stored.get("event_stats") or {},
        "evidence": evidence,
        "evidence_ids": [e["evidence_id"] for e in evidence],
        "tracking": tracking,
        "notes": notes,
    }


def _scoring_bridge(detail: dict) -> list[str]:
    """Two-step bridge: factor sum -> raw technical -> displayed technical.

    The displayed technical score is a fixed blend of the clipped absolute
    score and the batch rank; the detail page must show both steps, otherwise
    the contribution table cannot reproduce the number on screen.
    """
    scores = detail.get("scores") or {}
    raw = _num(scores.get("technical_raw"))
    rank = _num(scores.get("technical_rank"))
    before = _num(scores.get("score_before_clip"))
    tech = _num(scores.get("technical"))
    event = _num(scores.get("event"))
    composite = _num(scores.get("composite"))
    weights = detail.get("weights") or {"technical": 0.65, "event": 0.35}
    blend = detail.get("technical_blend") or TECHNICAL_BLEND
    lines = []
    if raw is not None:
        lines.append(f"- Σ因子贡献 = 技术原始分 {_fmt_score(raw)}（截断后）")
    if before is not None and raw is not None:
        lines.append(
            f"- 截断：{_fmt_score(before)} → {_fmt_score(raw)}（clip {raw - before:+.1f}）"
        )
    if raw is not None and rank is not None and tech is not None:
        lines.append(
            f"- 技术分构成：展示技术 {_fmt_score(tech)} = "
            f"{blend['absolute']:.2f}×技术原始分 {_fmt_score(raw)} + "
            f"{blend['rank']:.2f}×批内排名分 {_fmt_score(rank)}（候选池百分位×100）"
        )
    if tech is not None and event is not None:
        lines.append(
            f"- 综合 {_fmt_score(composite)} = "
            f"{weights.get('technical', 0.65):.2f}×技术 {_fmt_score(tech)} + "
            f"{weights.get('event', 0.35):.2f}×事件 {_fmt_score(event)}"
        )
    return lines


def recommendation_detail_markdown(run: dict | None, code: str) -> str:
    detail = recommendation_detail(run, code)
    if detail is None:
        return f"⚠️ 未找到 {code or '该代码'} 的推荐详情；请确认批次与代码是否匹配。"

    item = detail["item"]
    peer = detail["peer"]
    lines = [
        f"### 推荐详情：{item.get('name') or item.get('code')}（`{item.get('code')}`）",
        "",
        "> 研究候选，不是交易指令。以下每个数字都能回到 run_id / evidence_id。",
        "",
        (
            f"- **批次**：`{detail['run_id']}` ｜ data_as_of {detail['data_as_of'] or '—'} ｜ "
            f"tradable_from {detail['tradable_from'] or '—'} ｜ generated_at {detail['generated_at'] or '—'} ｜ "
            f"source {detail['source'] or '—'}"
        ),
        (
            f"- **技术面**：{item.get('action') or '—'} ｜ 收盘 {_fmt_score(item.get('close'))} ｜ "
            f"20日 {_fmt_pct(item.get('ret20'))} ｜ RSI {_fmt_score(item.get('rsi'))}"
        ),
    ]

    same_text = ""
    if peer.get("same_industry_count"):
        same_text = (
            f"；同业 {peer['same_industry']} {peer['same_industry_count']} 只中第 "
            f"{peer['same_industry_rank'] or '—'}"
        )
    lines.append(
        f"- **同批对比**：第 {peer['rank'] or '—'}/{peer['n']} 名；综合分 "
        f"{_fmt_score(detail['scores']['composite'])}（均值 {_fmt_score(peer['mean'])} / "
        f"中位 {_fmt_score(peer['median'])} / 最高 {_fmt_score(peer['max'])}）；"
        f"技术分位 {_fmt_score(peer['percentile'])}%{same_text}"
    )

    lines += ["", "#### 打分拆解", ""]
    lines += _scoring_bridge(detail)

    lines += ["", "#### 因子贡献（技术分规则逐条）", ""]
    if detail["factor_contributions"]:
        lines += ["| 因子 | 贡献 | 指标值 | 说明 |", "|---|---:|---|---|"]
        for contribution in detail["factor_contributions"]:
            lines.append(
                f"| {contribution.get('label') or contribution.get('factor')} | "
                f"{float(contribution.get('delta') or 0):+.1f} | "
                f"{_fmt_contribution_value(contribution)} | "
                f"{contribution.get('detail') or '—'} |"
            )
        raw = _num(detail["scores"].get("technical_raw"))
        before_clip = _num(detail["scores"].get("score_before_clip"))
        total = round(
            sum(float(c.get("delta") or 0) for c in detail["factor_contributions"]),
            1,
        )
        lines.append(
            f"| **Σ贡献（截断后）** | **{total:+.1f}** | — | "
            f"技术原始分 {_fmt_score(raw)} |"
        )
        if before_clip is not None and raw is not None:
            lines.append(
                f"| **截断** | **{raw - before_clip:+.1f}** | — | "
                f"{_fmt_score(before_clip)} → {_fmt_score(raw)} |"
            )
    else:
        lines.append("- 该批次未记录逐条因子贡献（旧批次）；技术分/事件分仍可追溯。")

    lines += ["", f"#### 事件证据（{len(detail['evidence'])} 条，pub_time ≤ generated_at）", ""]
    if detail["evidence"]:
        lines += ["| evidence_id | 发布时间 | 类型 | 标题 | 来源 |", "|---|---|---|---|---|"]
        for ev in detail["evidence"]:
            title = str(ev.get("title") or "").replace("|", "\\|")
            lines.append(
                f"| `{ev['evidence_id']}` | {str(ev.get('pub_time') or '')[:19]} | "
                f"{ev.get('kind') or '—'} | {title} | {ev.get('source') or '—'} |"
            )
    else:
        lines.append("- 暂无匹配的公告/研报/新闻/政策证据。")

    lines += ["", "#### 同批入选对比", ""]
    lines += ["| 排名 | 代码 | 名称 | 综合分 | 技术分 | 事件分 | 分位 |", "|---:|---|---|---:|---:|---:|---:|"]
    for row in peer["top"]:
        marker = "→ " if str(row.get("code")) == item.get("code") else ""
        lines.append(
            f"| {row.get('rank') or '—'} | {marker}`{row.get('code')}` | {row.get('name') or ''} | "
            f"{_fmt_score(row.get('composite'))} | {_fmt_score(row.get('technical'))} | "
            f"{_fmt_score(row.get('event'))} | {_fmt_score(row.get('percentile'))}% |"
        )

    lines += ["", "#### 后续表现（ledger.tracking，已结算记录）", ""]
    if detail["tracking"]:
        lines += ["| 期限 | 收益 | 超额 | 入场日 | 结算日 |", "|---|---:|---:|---|---|"]
        for horizon in (1, 5, 20):
            row = detail["tracking"].get(str(horizon))
            if not row:
                lines.append(f"| T+{horizon} | 待结算 | 待结算 | — | — |")
                continue
            lines.append(
                f"| T+{horizon} | {_fmt_pct(row.get('ret'))} | {_fmt_pct(row.get('excess'))} | "
                f"{row.get('entry_date') or '—'} | {row.get('exit_date') or '—'} |"
            )
    else:
        lines.append("- 该批次尚无 T+N 跟踪记录（尚未结算）。")

    lines += ["", "#### 追溯", ""]
    lines.append(f"- run_id：`{detail['run_id']}`")
    lines.append(f"- data_as_of：{detail['data_as_of'] or '—'}；tradable_from：{detail['tradable_from'] or '—'}")
    lines.append(
        "- evidence_id：" + ("、".join(f"`{x}`" for x in detail["evidence_ids"]) or "—")
    )
    for note in detail["notes"]:
        lines.append(f"- 口径提示：{note}")
    return "\n".join(lines)


def run_detail_markdown(run_id: str, code: str) -> str:
    """UI 只传 run_id/code；读取台账与渲染口径留在本模块。"""
    from .ledger import get_run

    run = get_run(str(run_id or "").strip())
    if not run:
        return f"⚠️ 未找到批次 `{run_id}`。"
    return recommendation_detail_markdown(run, code)


DRAWDOWN_HORIZON = 20


def _review_window(entry: pd.Timestamp) -> pd.Timestamp:
    """Last settled day of the fixed T+20 review window.

    The window is anchored at ``tradable_from`` and never extends to
    ``now``: a batch's drawdown must not change every day.  Business days are
    used as the trading-day proxy so the bound is deterministic offline.
    """
    cutoff = pd.bdate_range(entry, periods=DRAWDOWN_HORIZON + 1)[-1]
    return min(cutoff, pd.Timestamp.now().normalize())


def _review_price_frames(
    codes: list[str], entry: pd.Timestamp, window_end: pd.Timestamp
) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    start = (entry - pd.Timedelta(days=10)).strftime("%Y%m%d")
    end = window_end.strftime("%Y%m%d")
    frames: dict[str, pd.DataFrame] = {}
    failed: list[dict] = []
    for code in codes:
        try:
            frame = load_daily(code, start=start, end=end, adj="qfq")
        except Exception as exc:  # noqa: BLE001 - 单票失败记入 failed_codes，不静默换口径
            failed.append({"code": code, "error": str(exc)[:160]})
            continue
        if frame is None or frame.empty:
            failed.append({"code": code, "error": "empty daily frame"})
            continue
        frames[code] = frame
    return frames, failed


def _drawdown_from_prices(
    frames: dict[str, pd.DataFrame],
    entry: pd.Timestamp,
    window_end: pd.Timestamp,
    failed_codes: list[dict] | None = None,
) -> dict | None:
    closes = {}
    entry_price = {}
    for code, frame in (frames or {}).items():
        if frame is None or frame.empty or not {"trade_date", "open", "close"} <= set(frame.columns):
            continue
        daily = frame.copy()
        daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
        daily = (
            daily.dropna(subset=["trade_date"])
            .sort_values("trade_date")
            .drop_duplicates("trade_date", keep="last")
            .set_index("trade_date")
        )
        if entry not in daily.index:
            continue
        base = _num(daily.loc[entry, "open"])
        if base is None or base <= 0:
            continue
        close = pd.to_numeric(daily["close"], errors="coerce")
        close = close[(close > 0) & (close.index >= entry) & (close.index <= window_end)]
        if close.empty:
            continue
        closes[code] = close
        entry_price[code] = base
    if not closes:
        return None

    prices = pd.DataFrame(closes).sort_index()
    prices = prices.ffill()
    nav = (prices / pd.Series(entry_price, dtype=float)).mean(axis=1).dropna()
    if len(nav) < 2:
        return None
    drawdown = float((nav / nav.cummax() - 1.0).min())
    return {
        "value": round(drawdown, 4),
        "caliber": "daily_nav",
        "start": nav.index[0].date().isoformat(),
        "end": nav.index[-1].date().isoformat(),
        "as_of": nav.index[-1].date().isoformat(),
        "window": {
            "start": entry.date().isoformat(),
            "end": window_end.date().isoformat(),
        },
        "observations": len(nav),
        "n_codes": len(closes),
        "codes": sorted(closes),
        "failed_codes": list(failed_codes or []),
    }


def _drawdown_from_horizons(
    horizons: list[dict],
    failed_codes: list[dict] | None = None,
    reason: str = "daily_frames_unavailable",
) -> dict:
    points = [
        (int(row["horizon"]), 1.0 + float(row["mean_ret"]))
        for row in horizons
        if row.get("mean_ret") is not None
    ]
    if not points:
        return {
            "value": None,
            "caliber": "unavailable",
            "observations": 0,
            "horizons": [],
            "reason": reason,
            "failed_codes": list(failed_codes or []),
        }
    nav = pd.Series([value for _, value in points], index=[h for h, _ in points], dtype=float)
    nav = nav.sort_index()
    drawdown = float((nav / nav.cummax() - 1.0).min())
    return {
        "value": round(drawdown, 4),
        "caliber": "settled_horizons",
        "start": f"T+{nav.index[0]}",
        "end": f"T+{nav.index[-1]}",
        "observations": len(nav),
        "horizons": [int(x) for x in nav.index],
        "reason": reason,
        "failed_codes": list(failed_codes or []),
    }


def batch_review(
    run: dict | None,
    tracking: list[dict] | None = None,
    top_n: int = 5,
    price_frames: dict[str, pd.DataFrame] | None = None,
) -> dict:
    """Aggregate a batch's TopN forward returns from ledger tracking rows.

    ``price_frames`` is for tests/offline callers: ``None`` loads qfq daily
    bars for the daily-NAV drawdown, ``{}`` forces the settled-horizon fallback
    (no I/O).  No new empirical run and no re-ranking happens here.
    """
    if not run:
        return {
            "run_id": "", "data_as_of": "", "tradable_from": "", "source": "",
            "top_n": top_n, "batch_size": 0, "topn_size": 0,
            "horizons": [], "drawdown": {"value": None, "caliber": "unavailable"},
            "settled_total": 0,
        }

    items = list(run.get("items") or [])
    tracked = list(tracking) if tracking is not None else list(run.get("tracking") or [])
    top_items = [
        x for x in items
        if x.get("rank") is not None and int(x["rank"]) <= int(top_n)
    ]
    expected = len(top_items)

    horizons = []
    for horizon in (1, 5, 20):
        rows = []
        for row in tracked:
            if int(row.get("horizon") or 0) != horizon:
                continue
            rank = row.get("rank")
            if rank is None or int(rank) > int(top_n):
                continue
            rows.append(row)
        settled = []
        rets: list[float] = []
        excess: list[float] = []
        for row in rows:
            ret = _num(row.get("ret"))
            exc = _num(row.get("excess"))
            if ret is None or exc is None:
                continue
            settled.append(row)
            rets.append(ret)
            excess.append(exc)
        count = len(settled)
        horizons.append(
            {
                "horizon": horizon,
                "samples": count,
                "expected": expected,
                "coverage": round(count / expected, 4) if expected else None,
                "mean_ret": round(sum(rets) / count, 6) if count else None,
                "mean_excess": round(sum(excess) / count, 6) if count else None,
                "hit_rate": round(sum(1 for x in excess if x > 0) / count, 4) if count else None,
                "win_rate": round(sum(1 for x in rets if x > 0) / count, 4) if count else None,
                "entry_date": min((str(row.get("entry_date")) for row in settled), default=None),
                "exit_date": max((str(row.get("exit_date")) for row in settled), default=None),
            }
        )

    drawdown = None
    failed_codes: list[dict] = []
    entry_raw = run.get("tradable_from") or run.get("data_as_of")
    try:
        entry = pd.Timestamp(entry_raw).normalize() if entry_raw else None
    except (TypeError, ValueError):
        entry = None
    window_end = _review_window(entry) if entry is not None else None
    if price_frames is None and window_end is not None and top_items:
        price_frames, failed_codes = _review_price_frames(
            [str(x.get("code")) for x in top_items], entry, window_end
        )
    if price_frames and window_end is not None:
        drawdown = _drawdown_from_prices(
            price_frames, entry, window_end, failed_codes
        )
    if drawdown is None:
        if entry is None:
            reason = "no_tradable_from"
        elif not price_frames:
            reason = "no_daily_frames"
        else:
            reason = "insufficient_daily_data"
        drawdown = _drawdown_from_horizons(horizons, failed_codes, reason)

    return {
        "run_id": str(run.get("run_id") or ""),
        "data_as_of": run.get("data_as_of") or run.get("as_of") or "",
        "tradable_from": run.get("tradable_from") or "",
        "generated_at": run.get("generated_at") or "",
        "source": run.get("source") or "",
        "top_n": int(top_n),
        "batch_size": len(items),
        "topn_size": expected,
        "horizons": horizons,
        "primary_horizon": 5,
        "drawdown": drawdown,
        "failed_codes": failed_codes,
        "settled_total": sum(row["samples"] for row in horizons),
        "caveat": "样本为已结算的 T+N 跟踪记录；回撤窗口固定为 tradable_from 起 20 个交易日，口径见 drawdown.caliber；仅作研究记录，不构成 alpha 声明。",
    }


def batch_review_markdown(run: dict | None, review: dict | None = None) -> str:
    review = review or batch_review(run)
    lines = [
        f"### 批次复盘 · `{review.get('run_id') or '—'}`",
        "",
        (
            f"- data_as_of {review.get('data_as_of') or '—'} ｜ tradable_from "
            f"{review.get('tradable_from') or '—'} ｜ source {review.get('source') or '—'}"
        ),
        (
            f"- 批内 {review.get('batch_size', 0)} 只，TopN {review.get('topn_size', 0)} 只；"
            f"已结算跟踪记录 {review.get('settled_total', 0)} 条"
        ),
        "",
        "| 期限 | 样本数 | TopN 平均收益 | 平均超额 | 命中率（超额>0） | 胜率（收益>0） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in review.get("horizons") or []:
        lines.append(
            f"| T+{row['horizon']} | {row['samples']}/{row['expected']} | "
            f"{_fmt_pct(row.get('mean_ret'))} | {_fmt_pct(row.get('mean_excess'))} | "
            f"{_fmt_score(row.get('hit_rate') * 100 if row.get('hit_rate') is not None else None)}% | "
            f"{_fmt_score(row.get('win_rate') * 100 if row.get('win_rate') is not None else None)}% |"
        )
    if not review.get("settled_total"):
        lines.append("")
        lines.append("- 暂无已结算的 T+N 记录；请先在跟踪页刷新结算后再复盘。")

    drawdown = review.get("drawdown") or {}
    value = _num(drawdown.get("value"))
    failed = drawdown.get("failed_codes") or []
    failed_text = (
        "；日线失败：" + "、".join(str(x.get("code")) for x in failed)
        if failed
        else ""
    )
    lines += ["", "#### 回撤", ""]
    if value is None:
        lines.append(
            f"- 回撤：暂不可算（原因 {drawdown.get('reason') or '无已结算期限'}{failed_text}）。"
        )
    elif drawdown.get("caliber") == "daily_nav":
        lines.append(
            f"- 回撤：**{value * 100:.2f}%**（口径：TopN 等权日净值，"
            f"窗口 {drawdown.get('window', {}).get('start')} 至 "
            f"{drawdown.get('window', {}).get('end')}，as_of {drawdown.get('as_of')}，"
            f"{drawdown.get('observations')} 个交易日，{drawdown.get('n_codes')} 只成分"
            f"{failed_text}）"
        )
    else:
        lines.append(
            f"- 回撤：**{value * 100:.2f}%**（口径：已结算 T+N 等权净值路径，"
            f"{drawdown.get('observations')} 个观测点；原因 {drawdown.get('reason') or '日线不可得'}"
            f"{failed_text}；仅作期限结构参考）"
        )

    lines += ["", f"> {review.get('caveat') or '仅作研究记录，不构成 alpha 声明。'}"]
    return "\n".join(lines)


def run_review_markdown(run_id: str) -> str:
    from .ledger import get_run

    run = get_run(str(run_id or "").strip())
    if not run:
        return f"⚠️ 未找到批次 `{run_id}`。"
    return batch_review_markdown(run)


def _tracking_lookup(run: dict) -> dict[tuple[str, int], dict]:
    lookup = {}
    for row in run.get("tracking") or []:
        try:
            horizon = int(row.get("horizon") or 0)
        except (TypeError, ValueError):
            continue
        lookup[(str(row.get("code")), horizon)] = row
    return lookup


def export_batch_brief(
    run: dict,
    review: dict | None = None,
    out_dir: Path | str | None = None,
) -> str:
    """Write a single-batch Markdown brief and return its path.

    The brief embeds the same run_id/data_as_of/evidence_id values as the UI so
    a PDF/email copy can still be traced back to the ledger.
    """
    if not run:
        raise ValueError("run is required")
    review = review or batch_review(run)
    run_id = str(run.get("run_id") or "latest")
    tracking = _tracking_lookup(run)
    ranked = sorted(
        (x for x in run.get("items") or [] if x.get("rank") is not None),
        key=lambda x: int(x["rank"]),
    )
    top_items = [x for x in ranked if int(x["rank"]) <= int(review.get("top_n") or 5)]
    details = [recommendation_detail(run, str(x.get("code"))) for x in top_items]

    lines = [
        f"# 单批简报 · `{run_id}`",
        "",
        "> 研究候选，不是交易指令。本简报中的每个数字都能回到 run_id / evidence_id。",
        "",
        "## 批次信息",
        "",
        f"- data_as_of：{review.get('data_as_of') or '—'}",
        f"- tradable_from：{review.get('tradable_from') or '—'}",
        f"- generated_at：{review.get('generated_at') or '—'}",
        f"- source：{review.get('source') or '—'}",
        f"- 批内 {review.get('batch_size', 0)} 只，TopN {review.get('topn_size', 0)} 只",
        f"- JSON 归档：`{run.get('json_path') or '—'}`",
        "",
        batch_review_markdown(run, review),
        "",
        "## TopN 明细与后续",
        "",
        "| 排名 | 代码 | 名称 | 综合分 | 技术分 | 事件分 | 技术分位 | T+5 | 超额T+5 | 证据数 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in top_items:
        code = str(item.get("code"))
        row5 = tracking.get((code, 5)) or {}
        row_detail = recommendation_detail(run, code) or {}
        lines.append(
            f"| {item.get('rank')} | `{code}` | {item.get('name') or ''} | "
            f"{_fmt_score(item.get('composite'))} | {_fmt_score(item.get('technical'))} | "
            f"{_fmt_score(item.get('event'))} | {_fmt_score(item.get('percentile'))}% | "
            f"{_fmt_pct(row5.get('ret'))} | {_fmt_pct(row5.get('excess'))} | "
            f"{len(row_detail.get('evidence') or [])} |"
        )

    lines += ["", "## 因子贡献与证据", ""]
    for index, detail in enumerate(details, 1):
        if not detail:
            continue
        item = detail["item"]
        lines.append(f"### {index}. {item.get('code')} {item.get('name') or ''}")
        lines.append("")
        lines.append("- 打分拆解：")
        lines.extend(f"  {line}" for line in _scoring_bridge(detail))
        contributions = detail.get("factor_contributions") or []
        if contributions:
            parts = [
                f"{c.get('label')} {float(c.get('delta') or 0):+.1f}"
                for c in contributions
            ]
            lines.append("- 因子贡献：" + "；".join(parts))
        else:
            lines.append("- 因子贡献：未记录（旧批次）")
        evidence = detail.get("evidence") or []
        if evidence:
            lines.append(f"- 事件证据（{len(evidence)} 条）：")
            for ev in evidence:
                lines.append(
                    f"  - `{ev['evidence_id']}` {str(ev.get('pub_time') or '')[:19]} "
                    f"{ev.get('kind') or '—'} · {ev.get('title') or ''}"
                    f"（{ev.get('source') or '—'}）"
                )
        else:
            lines.append("- 事件证据：无")
        lines.append("")

    evidence_ids = sorted(
        {
            e.get("evidence_id")
            for d in details
            if d
            for e in d.get("evidence") or []
            if e.get("evidence_id")
        }
    )
    lines += [
        "## 追溯口径",
        "",
        f"- run_id：`{run_id}`",
        (
            f"- data_as_of / tradable_from：{review.get('data_as_of') or '—'} / "
            f"{review.get('tradable_from') or '—'}"
        ),
        f"- evidence_id：{len(evidence_ids)} 个"
        + ("：" + "、".join(f"`{x}`" for x in evidence_ids) if evidence_ids else ""),
        f"- 回撤口径：{review.get('drawdown', {}).get('caliber') or '—'}",
        (
            "- 研究语义：复用现有推荐结果与 ledger.tracking，不另跑实证；"
            "不改变推荐排序，不声明 alpha。"
        ),
        "",
    ]

    folder = Path(out_dir or REC_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id) or "latest"
    path = folder / f"brief_{safe}.md"
    _atomic_write_text(path, "\n".join(lines))
    return str(path)


def export_run_brief(run_id: str) -> str:
    from .ledger import get_run

    run = get_run(str(run_id or "").strip())
    if not run:
        raise ValueError(f"未找到批次 `{run_id}`")
    return export_batch_brief(run)
