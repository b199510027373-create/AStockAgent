from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import BASE_DIR
from .events import stock_basic
from .recommend import (
    _action,
    _pick_diverse,
    assign_percentiles,
    load_many,
    parse_universe,
    technical_score,
)

REC_DIR = BASE_DIR / "data" / "recommendations"
REC_DIR.mkdir(parents=True, exist_ok=True)


def _existing_as_of() -> set[str]:
    out = set()
    for path in REC_DIR.glob("recommend_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("as_of"):
                out.add(str(data["as_of"])[:10])
        except Exception:
            continue
    return out


def backfill_technical(
    days: int = 30,
    top_n: int = 5,
    universe_text: str = "",
    progress=None,
    candidate_n: int = 15,
) -> tuple[int, str]:
    """用历史日线回填「技术分」推荐批次。

    事件分固定 50，因此这是技术分回填，不是完整实盘推荐；
    用于在等不到 20 个自然交易日时，先检验技术分排序的前向表现。
    """
    codes = parse_universe(universe_text)
    if not codes:
        raise ValueError("股票池为空")
    if progress is not None:
        progress(0.05, desc="加载日线")
    daily_map = load_many(codes, progress=None)
    if not daily_map:
        raise ValueError("日线加载失败")
    basic = stock_basic().set_index("ts_code")
    existing = _existing_as_of()

    ref = next(iter(daily_map.values()))
    all_dates = sorted(pd.to_datetime(ref["trade_date"]).dropna().unique())
    candidates = all_dates[-(int(days) + len(existing) + 5) :]
    created = 0
    skipped = 0
    for raw_date in candidates:
        if created >= int(days):
            break
        date = pd.Timestamp(raw_date).normalize()
        key = date.strftime("%Y-%m-%d")
        if key in existing:
            skipped += 1
            continue
        rows = []
        for code, df in daily_map.items():
            sub = df[df["trade_date"] <= date]
            if len(sub) < 60:
                continue
            ts = technical_score(sub)
            if not ts:
                continue
            info = basic.loc[code] if code in basic.index else None
            rows.append(
                {
                    "code": code,
                    "name": str(info["name"]) if info is not None else code,
                    "industry": str(info["industry"]) if info is not None else "",
                    **ts,
                }
            )
        if len(rows) < 2:
            continue
        raw = pd.Series([r["technical_score"] for r in rows], dtype=float)
        ranks = raw.rank(pct=True) * 100
        for row, rank in zip(rows, ranks):
            row["technical_raw"] = row["technical_score"]
            row["technical_rank"] = float(rank)
            row["technical_score"] = round(0.45 * row["technical_score"] + 0.55 * rank, 1)
            row["event_score"] = 50.0
            row["composite"] = 0.65 * row["technical_score"] + 0.35 * 50.0
            row["action"] = _action(row["composite"])
            row["reason"] = (
                f"技术分回填：{row.get('trend', '震荡')}；"
                f"20日 {row['ret20']*100:+.1f}%；RSI {row['rsi']:.1f}；事件分固定 50。"
            )
        # 与 live 同一基准：先截断到候选池（N=candidate_n），再在池内算分位。
        pool = assign_percentiles(rows, candidate_n)
        pool_n = len(pool)
        selected = _pick_diverse(pool, top_n, max_per_industry=2)
        table = []
        for i, r in enumerate(selected, 1):
            table.append(
                {
                    "排名": i,
                    "代码": r["code"],
                    "名称": r["name"],
                    "行业": r["industry"],
                    "综合分": round(r["composite"], 1),
                    "技术分位": r["percentile"],
                    "技术分": round(r["technical_score"], 1),
                    "事件分": 50.0,
                    "收盘": r["close"],
                    "20日%": round(r["ret20"] * 100, 2),
                    "RSI": round(r["rsi"], 1),
                    "技术面": r["action"],
                    "理由": r["reason"],
                }
            )
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "as_of": key,
            "source": "backfill_technical",
            "market": (
                "技术分回填（事件分固定 50），不是完整实盘推荐，仅用于检验技术分排序；"
                f"分位基准=候选池（N={pool_n}）。"
            ),
            "params": {"candidate_n": int(candidate_n), "pool_size": pool_n},
            "diagnostics": {
                "candidate_n": pool_n,
                "percentile_basis": "candidate_pool",
                "candidate_pool": [
                    {"code": r["code"], "composite": float(r["composite"])} for r in pool
                ],
            },
            "ai": {},
            "table": table,
            "evidence": {},
        }
        path = REC_DIR / f"recommend_{date:%Y%m%d}_backfill.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        created += 1
        existing.add(key)
        if progress is not None:
            progress(min(0.95, 0.1 + 0.8 * created / max(1, int(days))), desc=f"回填 {created}/{days}")

    status = (
        f"技术分回填完成：新增 {created} 个批次，跳过已存在的 {skipped} 个日期。"
        "事件分固定 50，结果只用于检验技术分排序，不等同于完整每日推荐。"
    )
    return created, status
