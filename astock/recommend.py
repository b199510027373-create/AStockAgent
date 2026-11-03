from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import llm
from .config import BASE_DIR
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
from .tushare_client import TushareClient
from .utils import is_code, normalize_code

REC_DIR = BASE_DIR / "data" / "recommendations"
REC_DIR.mkdir(parents=True, exist_ok=True)
SCHEMA_VERSION = 2
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
    except Exception as exc:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
                code, df, error = futures[future], None, f"{type(exc).__name__}: {exc}"
            if df is not None and not df.empty:
                out[code] = df
            elif diagnostics is not None:
                diagnostics.append({"code": code, "error": error or "empty dataframe"})
            if progress is not None:
                progress(min(0.30, 0.05 + 0.30 * i / total), desc=f"加载日线 {i}/{total}")
    return out


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
    reasons: list[str] = []

    if math.isfinite(ma20):
        if close > ma20:
            score += 8
            reasons.append("站上MA20")
        if math.isfinite(ma60) and ma20 > ma60:
            score += 8
            reasons.append("MA20>MA60")
        if math.isfinite(ma60) and close > ma60:
            score += 5
            reasons.append("站上MA60")
        if math.isfinite(ma120) and ma20 > ma120:
            score += 4
            reasons.append("MA20>MA120")
    if math.isfinite(hist):
        if hist > 0:
            score += 7
            reasons.append("MACD柱为正")
        if math.isfinite(dif) and math.isfinite(dea) and dif > dea:
            score += 3
    if math.isfinite(rsi):
        if 45 <= rsi <= 70:
            score += 5
            reasons.append("RSI健康")
        elif rsi > 80:
            score -= 8
            reasons.append("RSI超买")
        elif rsi < 30:
            score -= 3
            reasons.append("RSI超卖")
    if math.isfinite(vol_ratio):
        if 1.1 <= vol_ratio <= 2.5:
            score += 4
            reasons.append("温和放量")
        elif vol_ratio > 3:
            score -= 5
            reasons.append("量能过热")
    if math.isfinite(ret20):
        if ret20 > 0:
            score += 5
        else:
            score -= 3
    if math.isfinite(ret60) and ret60 > 0:
        score += 3
    if math.isfinite(atr_pct) and atr_pct > 0.07:
        score -= 5
        reasons.append("波动偏高")
    if math.isfinite(drawdown) and drawdown < -0.25:
        score -= 5
        reasons.append("距高点回撤大")

    score = max(0.0, min(100.0, score))
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
        m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if m:
            text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _llm_rank(rows: list[dict], evidence_map: dict[str, list[dict]], market: str) -> dict:
    lines = [
        "你是A股研究员。只能使用下面提供的数据，不得编造新闻、价格或业绩。",
        "为每只候选给出 action（技术面强/技术面中性/技术面弱）、confidence(0-1)、reason、catalysts、risks、evidence（引用的 E 编号）。",
        "输出严格 JSON：{\"market\":\"一句话市场判断\",\"items\":[{\"code\":\"600519.SH\",\"action\":\"技术面强\",\"confidence\":0.7,\"reason\":\"...\",\"catalysts\":[\"...\"],\"risks\":[\"...\"],\"evidence\":[\"E1\"]}],\"risk_note\":\"...\"}",
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
            date = pd.Timestamp(ev["date"]).strftime("%Y-%m-%d")
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
    except Exception:
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
    except Exception:
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
) -> str:
    ai_items = {str(x.get("code")): x for x in (ai.get("items") or []) if isinstance(x, dict)}
    lines = [
        f"# 每日推荐 · {as_of}",
        "",
        f"**数据截止**：{as_of} 收盘；**可成交起点**：{tradable_from or '下一交易日'} 开盘；"
        f"生成时间 {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"**市场背景**：{market}",
        "",
        "## 今日候选",
    ]
    for i, row in enumerate(result_rows, 1):
        ai_row = ai_items.get(row["code"], {})
        action = ai_row.get("action") or row.get("action") or _action(row["composite"])
        reason = ai_row.get("reason") or row.get("reason", "")
        conf = ai_row.get("confidence")
        conf_text = f"（置信度 {float(conf):.0%}）" if isinstance(conf, (int, float)) else ""
        lines.append(
            f"### {i}. {row['code']} {row['name']} · 综合 {row['composite']:.1f}"
        )
        lines.append(
            f"- **建议**：{action}{conf_text}；技术 {row['technical_score']:.1f} / 事件 {row['event_score']:.1f}；"
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
            "> 数据来源：Tushare 日线/公告/研报/新闻/政策库；AI 只做归因和表述，不改变量化排序。"
            "本页仅供研究，不构成投资建议。",
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
            date = pd.Timestamp(ev["date"]).strftime("%Y-%m-%d")
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
                "技术分": round(row["technical_score"], 1),
                "事件分": round(row["event_score"], 1),
                "收盘": row["close"],
                "20日%": round(row["ret20"] * 100, 2) if math.isfinite(row.get("ret20", np.nan)) else None,
                "RSI": round(row["rsi"], 1) if math.isfinite(row.get("rsi", np.nan)) else None,
                "建议": row.get("action", ""),
                "理由": row.get("reason", ""),
            }
            for i, row in enumerate(result_rows, 1)
        ]
    )
    suffix = run_id[-8:] if run_id else uuid.uuid4().hex[:8]
    csv_path = REC_DIR / f"recommend_{stamp}_{suffix}.csv"
    tmp_csv = csv_path.with_suffix(".csv.tmp")
    table.to_csv(tmp_csv, index=False, encoding="utf-8-sig")
    os.replace(tmp_csv, csv_path)

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
        "table": table.to_dict("records"),
        "evidence": {
            code: [
                {
                    "kind": ev.get("kind", ""),
                    "date": pd.Timestamp(ev["date"]).isoformat() if ev.get("date") is not None else "",
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
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    json_path = REC_DIR / f"recommend_{stamp}_{suffix}.json"
    _atomic_write_text(json_path, text)
    _atomic_write_text(REC_DIR / "latest.json", text)
    try:
        from .ledger import save_run

        save_run(payload, json_path=str(json_path), csv_path=str(csv_path))
    except Exception:
        # JSON 仍是主产物；ledger 故障不应阻断推荐生成
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


def run_daily_recommendation(
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
            0.45 * row["technical_raw"] + 0.55 * row["technical_rank"], 1
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
                pub = pd.Timestamp(ev.get("date"))
                if pub > generated_ts:
                    evidence_dropped += 1
                    continue
            except Exception:
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
    ai_rows = candidates[: min(8, len(candidates))]

    step(0.78, "生成市场背景与 AI 归因")
    market = _market_snapshot(daily_map, major, policy)
    ai = _llm_rank(ai_rows, evidence_map, market) if use_llm else {}
    ai_items = {str(x.get("code")): x for x in (ai.get("items") or []) if isinstance(x, dict)}
    for row in candidates:
        ai_row = ai_items.get(row["code"])
        if ai_row:
            ai_action = str(ai_row.get("action") or "")
            if ai_action:
                row["action"] = {
                    "关注": "技术面强",
                    "观察": "技术面中性",
                    "回避": "技术面弱",
                }.get(ai_action, ai_action)
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
    }
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
    )
    report = _report_markdown(final_rows, market, ai, data_as_of, tradable_from)
    evidence_md = _evidence_markdown(final_rows, evidence_map)

    elapsed = time.time() - started
    ai_ok = bool((ai or {}).get("items"))
    industry_note = ""
    if len(final_rows) < top_n:
        industry_note = f" · 行业上限导致输出少于 {top_n} 只"
    stale_note = " · ⚠️ 数据未更新" if stale_data else ""
    status = (
        f"✅ 股票池 {len(codes)} 只 · 有效日线 {len(rows)} 只 · 技术预筛 {len(candidates)} · "
        f"输出 {len(final_rows)} 只（同行业最多 2 只）{industry_note} · "
        f"新闻 {min(3, news_days)} 天 / 公告 30 天 / 研报 7 天 / 政策 {news_days} 天 · "
        f"AI归因 {'已生成' if ai_ok else '未生成（使用规则理由）'} · 用时 {elapsed:.1f}s · "
        f"data_as_of {data_as_of} · tradable_from {tradable_from}{stale_note} · run_id {run_id}"
    )
    if failed:
        status += f" · 失败/剔除 {len(failed)} 只（详见 JSON diagnostics）"
    return Recommendation(
        table=pd.DataFrame(
            [
                {
                    "排名": i,
                    "代码": r["code"],
                    "名称": r["name"],
                    "行业": r["industry"],
                    "综合分": round(r["composite"], 1),
                    "技术分": round(r["technical_score"], 1),
                    "事件分": round(r["event_score"], 1),
                    "收盘": r["close"],
                    "20日%": round(r["ret20"] * 100, 2) if math.isfinite(r.get("ret20", np.nan)) else None,
                    "RSI": round(r["rsi"], 1) if math.isfinite(r.get("rsi", np.nan)) else None,
                    "建议": r["action"],
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
