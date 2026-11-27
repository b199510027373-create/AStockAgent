"""事件分单点配置、人工标注校准与排序对比。

事件分（evidence -> 0..100）的唯一实现位于本模块。``astock.events`` 仅做
re-export，``astock.recommend`` 通过 :func:`event_scoring_params` 把权重快照写进
``run.params``，保证历史推荐可审计。

校准只使用人工标注的 (kind, title, rating) -> label，不调用任何 LLM，也不修改
推荐动作/置信度（ADR-009）。
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

EVENT_SCORING_VERSION = "event-score-v2-20261128"

# 单点权重：所有事件分计算只读取这里。修改后必须重跑
# ``python -m astock.scoring`` 并更新 docs/reports 下的校准报告。
EVENT_TYPE_WEIGHTS = {"公告": 1.2, "研报": 0.8, "新闻": 0.4, "政策": 0.6}
EVENT_RATING_WEIGHT = 0.35
EVENT_POLICY_BONUS = 0.0
EVENT_TIME_DECAY_DAYS = 45.0
EVENT_NEUTRAL_SCORE = 50.0
EVENT_DEFAULT_TYPE_WEIGHT = 0.8
EVENT_POSITIVE_THRESHOLD = 50.0
EVENT_NEGATIVE_THRESHOLD = 50.0

LEGACY_EVENT_TYPE_WEIGHTS = {"公告": 1.0, "研报": 1.2, "新闻": 0.8, "政策": 0.7}
LEGACY_EVENT_RATING_WEIGHT = 0.35
LEGACY_EVENT_POLICY_BONUS = 0.05

CALIBRATION_LABELS_FILE = "docs/calibration/event_labels_v1.csv"
CALIBRATION_DATE = "2026-11-28"
CALIBRATION_SAMPLE_COUNT = 153
CALIBRATION_CASE_COUNT = 51

POSITIVE_WORDS = list(
    dict.fromkeys(
        [
            "预增", "扭亏", "增长", "回购", "增持", "中标", "签约", "签订", "合同",
            "获批", "投产", "涨价", "提价", "突破", "超预期", "分红", "订单", "合作",
            "买入", "增持", "强烈推荐", "推荐", "上调", "创新高", "放量", "利好",
            "上涨", "大涨", "小涨", "走强", "拉升", "反弹", "涨停",
        ]
    )
)
NEGATIVE_WORDS = [
    "预减", "亏损", "减持", "质押", "诉讼", "仲裁", "处罚", "立案", "问询",
    "终止", "下修", "下调", "风险提示", "退市", "停牌", "冻结", "担保",
    "逾期", "违规", "跌停", "下滑", "不及预期", "卖出", "利空", "解禁",
    "下跌", "大跌", "走弱", "回落", "承压",
]
RATING_WORDS = {
    "强烈推荐": 2.0,
    "买入": 2.0,
    "增持": 1.5,
    "推荐": 1.0,
    "优于大市": 1.0,
    "跑赢行业": 1.0,
    "中性": 0.0,
    "持有": 0.0,
    "减持": -1.5,
    "卖出": -2.0,
}

# 校准网格。时间衰减在样本上没有可识别信号，固定为 45 天。
_TYPE_WEIGHT_GRID = {
    "公告": (0.8, 1.0, 1.2),
    "研报": (0.8, 1.0, 1.2),
    "新闻": (0.4, 0.6, 0.8, 1.0),
    "政策": (0.4, 0.6, 0.8),
}
_RATING_WEIGHT_GRID = (0.0, 0.35, 0.7)
_POLICY_BONUS_GRID = (0.0, 0.05)


def sentiment(text: str) -> tuple[int, int]:
    """返回 (正面词命中数, 负面词命中数)。"""
    text = str(text or "")
    pos = sum(text.count(word) for word in POSITIVE_WORDS)
    neg = sum(text.count(word) for word in NEGATIVE_WORDS)
    return pos, neg


def event_scoring_config() -> dict[str, Any]:
    """当前生效的事件分配置（深拷贝，调用方可安全修改）。"""
    return {
        "version": EVENT_SCORING_VERSION,
        "type_weights": dict(EVENT_TYPE_WEIGHTS),
        "rating_weight": EVENT_RATING_WEIGHT,
        "policy_bonus": EVENT_POLICY_BONUS,
        "time_decay_days": EVENT_TIME_DECAY_DAYS,
        "neutral_score": EVENT_NEUTRAL_SCORE,
        "default_type_weight": EVENT_DEFAULT_TYPE_WEIGHT,
        "positive_threshold": EVENT_POSITIVE_THRESHOLD,
        "negative_threshold": EVENT_NEGATIVE_THRESHOLD,
    }


def legacy_event_config() -> dict[str, Any]:
    """校准前的配置，仅用于前后对比。"""
    return {
        "version": "event-score-v1-legacy",
        "type_weights": dict(LEGACY_EVENT_TYPE_WEIGHTS),
        "rating_weight": LEGACY_EVENT_RATING_WEIGHT,
        "policy_bonus": LEGACY_EVENT_POLICY_BONUS,
        "time_decay_days": EVENT_TIME_DECAY_DAYS,
        "neutral_score": EVENT_NEUTRAL_SCORE,
        "default_type_weight": EVENT_DEFAULT_TYPE_WEIGHT,
        "positive_threshold": EVENT_POSITIVE_THRESHOLD,
        "negative_threshold": EVENT_NEGATIVE_THRESHOLD,
    }


def event_scoring_params() -> dict[str, Any]:
    """写入 run.params 的可审计快照（JSON 可序列化）。"""
    cfg = event_scoring_config()
    return {
        "version": cfg["version"],
        "type_weights": dict(cfg["type_weights"]),
        "rating_weight": cfg["rating_weight"],
        "policy_bonus": cfg["policy_bonus"],
        "time_decay_days": cfg["time_decay_days"],
        "neutral_score": cfg["neutral_score"],
        "default_type_weight": cfg["default_type_weight"],
        "positive_threshold": cfg["positive_threshold"],
        "negative_threshold": cfg["negative_threshold"],
        "calibration": {
            "labels_file": CALIBRATION_LABELS_FILE,
            "samples": CALIBRATION_SAMPLE_COUNT,
            "cases": CALIBRATION_CASE_COUNT,
            "method": "human_labels_grid_search",
            "calibrated_at": CALIBRATION_DATE,
            "llm_used": False,
        },
    }


def _normalize_config(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    base = event_scoring_config()
    if config is None:
        return base
    out = dict(base)
    if "type_weights" in config:
        weights = dict(base["type_weights"])
        weights.update({str(k): float(v) for k, v in dict(config["type_weights"]).items()})
        out["type_weights"] = weights
    for key in (
        "version",
        "rating_weight",
        "policy_bonus",
        "time_decay_days",
        "neutral_score",
        "default_type_weight",
        "positive_threshold",
        "negative_threshold",
    ):
        if key in config:
            out[key] = config[key]
    return out


def _rating_value(rating: Any) -> float:
    text = str(rating or "")
    for word, value in RATING_WORDS.items():
        if word in text:
            return value
    return 0.0


def _age_days(raw_time: Any, now: pd.Timestamp) -> int | None:
    if raw_time is None or raw_time == "":
        return None
    try:
        published = pd.Timestamp(raw_time)
        return max(0, int((now.normalize() - published.normalize()).days))
    except (TypeError, ValueError):
        return None


def score_events(
    evidence: Sequence[Mapping[str, Any]] | None,
    *,
    config: Mapping[str, Any] | None = None,
    now: Any = None,
) -> tuple[float, dict[str, int]]:
    """把证据列表打成 0..100 的事件分，并返回计数诊断。"""
    cfg = _normalize_config(config)
    neutral = float(cfg["neutral_score"])
    stats = {"pos": 0, "neg": 0, "news": 0, "anns": 0, "reports": 0, "policy": 0, "n": 0}
    if not evidence:
        return neutral, stats

    current = pd.Timestamp(now) if now is not None else pd.Timestamp.now()
    weighted_sum = 0.0
    weight_sum = 0.0
    for item in evidence:
        kind = str(item.get("kind") or "")
        stats["n"] += 1
        stats["anns"] += kind == "公告"
        stats["reports"] += kind == "研报"
        stats["news"] += kind == "新闻"
        stats["policy"] += kind == "政策"

        pos, neg = sentiment(str(item.get("title") or ""))
        stats["pos"] += pos
        stats["neg"] += neg
        polarity = (pos - neg) / max(1, pos + neg)
        if kind == "研报":
            polarity += float(cfg["rating_weight"]) * (_rating_value(item.get("rating")) / 2.0)
        elif kind == "政策":
            polarity += float(cfg["policy_bonus"])
        polarity = min(1.0, max(-1.0, polarity))

        age = _age_days(item.get("pub_time") or item.get("date"), current)
        decay = 1.0 if age is None else 1.0 / (1.0 + age / float(cfg["time_decay_days"]))
        type_weight = float(cfg["type_weights"].get(kind, cfg["default_type_weight"]))
        weight = type_weight * decay
        weighted_sum += weight * (neutral + neutral * polarity)
        weight_sum += weight

    score = weighted_sum / weight_sum if weight_sum else neutral
    return round(max(0.0, min(100.0, score)), 1), stats


def _label_for_score(score: float, cfg: Mapping[str, Any]) -> int:
    if score > float(cfg["positive_threshold"]):
        return 1
    if score < float(cfg["negative_threshold"]):
        return -1
    return 0


def _resolve_labels_path(path: str | Path | None) -> Path:
    target = Path(path or CALIBRATION_LABELS_FILE)
    if target.is_absolute():
        return target
    root_candidate = Path(__file__).resolve().parents[1] / target
    return root_candidate if root_candidate.exists() or not target.exists() else target


def load_labeled_samples(path: str | Path | None = None) -> list[dict[str, Any]]:
    """读取人工标注 CSV，返回逐条样本。"""
    target = _resolve_labels_path(path)
    samples: list[dict[str, Any]] = []
    with target.open(encoding="utf-8-sig", newline="") as handle:
        for lineno, row in enumerate(csv.DictReader(handle), start=2):
            try:
                sample: dict[str, Any] = {
                    "case_id": str(row["case_id"]),
                    "label": int(row["label"]),
                    "case_strength": float(row["case_strength"]),
                    "item_label": int(row["item_label"]),
                    "kind": str(row["kind"]),
                    "title": str(row["title"]),
                    "rating": str(row.get("rating") or ""),
                    "strength": float(row["strength"]),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{target}:{lineno}: invalid label row: {exc}") from exc
            if sample["label"] not in (-1, 0, 1) or sample["item_label"] not in (-1, 0, 1):
                raise ValueError(f"{target}:{lineno}: label must be -1, 0 or 1")
            if sample["strength"] < 0 or sample["strength"] > 3:
                raise ValueError(f"{target}:{lineno}: strength must be within 0..3")
            samples.append(sample)
    if not samples:
        raise ValueError(f"{target}: no labeled samples")
    return samples


def _macro_f1(actual: Sequence[int], predicted: Sequence[int]) -> float:
    scores = []
    for label in (-1, 0, 1):
        tp = sum(1 for a, p in zip(actual, predicted, strict=True) if a == label and p == label)
        fp = sum(1 for a, p in zip(actual, predicted, strict=True) if a != label and p == label)
        fn = sum(1 for a, p in zip(actual, predicted, strict=True) if a == label and p != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return round(sum(scores) / 3.0, 4)


def _rank_corr(left: Sequence[float], right: Sequence[float]) -> float | None:
    x = pd.Series(list(left), dtype=float)
    y = pd.Series(list(right), dtype=float)
    mask = x.notna() & y.notna()
    if int(mask.sum()) < 3:
        return None
    ranked_x = x[mask].rank(method="average")
    ranked_y = y[mask].rank(method="average")
    if ranked_x.std(ddof=0) == 0 or ranked_y.std(ddof=0) == 0:
        return None
    value = ranked_x.corr(ranked_y)
    return None if pd.isna(value) else round(float(value), 4)


def evaluate_event_config(
    samples: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any] | None = None,
    *,
    now: Any = None,
) -> dict[str, Any]:
    """在人工标注样本上计算逐条准确率、案例准确率与 Rank IC。"""
    cfg = _normalize_config(config)
    current = pd.Timestamp(now) if now is not None else pd.Timestamp.now()
    item_rows: list[dict[str, Any]] = []
    case_items: dict[str, list[dict[str, Any]]] = {}
    case_meta: dict[str, Mapping[str, Any]] = {}

    for sample in samples:
        evidence = {
            "kind": sample["kind"],
            "title": sample["title"],
            "rating": sample.get("rating") or "",
            "pub_time": current,
        }
        score, _ = score_events([evidence], config=cfg, now=current)
        item_label = int(sample["item_label"])
        item_rows.append(
            {
                "case_id": sample["case_id"],
                "score": score,
                "pred": _label_for_score(score, cfg),
                "label": item_label,
                "target": item_label * float(sample["strength"]) / 3.0,
            }
        )
        case_items.setdefault(str(sample["case_id"]), []).append(evidence)
        case_meta[str(sample["case_id"])] = sample

    case_rows: list[dict[str, Any]] = []
    for case_id, case_evidence in case_items.items():
        score, _ = score_events(case_evidence, config=cfg, now=current)
        target = float(case_meta[case_id]["case_strength"])
        case_rows.append(
            {
                "case_id": case_id,
                "score": score,
                "pred": _label_for_score(score, cfg),
                "label": int(case_meta[case_id]["label"]),
                "target": target,
            }
        )

    item_actual = [int(r["label"]) for r in item_rows]
    item_pred = [int(r["pred"]) for r in item_rows]
    case_actual = [int(r["label"]) for r in case_rows]
    case_pred = [int(r["pred"]) for r in case_rows]
    item_accuracy = sum(a == p for a, p in zip(item_actual, item_pred, strict=True)) / len(item_rows)
    case_accuracy = sum(a == p for a, p in zip(case_actual, case_pred, strict=True)) / len(case_rows)
    return {
        "version": cfg["version"],
        "type_weights": dict(cfg["type_weights"]),
        "rating_weight": cfg["rating_weight"],
        "policy_bonus": cfg["policy_bonus"],
        "item_accuracy": round(item_accuracy, 4),
        "case_accuracy": round(case_accuracy, 4),
        "macro_f1": _macro_f1(case_actual, case_pred),
        "ic": _rank_corr([float(r["score"]) for r in case_rows], [float(r["target"]) for r in case_rows]),
        "item_ic": _rank_corr([float(r["score"]) for r in item_rows], [float(r["target"]) for r in item_rows]),
        "n_items": len(item_rows),
        "n_cases": len(case_rows),
        "confusion": {
            "item": {f"true_{a}_pred_{p}": sum(1 for x, y in zip(item_actual, item_pred, strict=True) if x == a and y == p) for a in (-1, 0, 1) for p in (-1, 0, 1)},
            "case": {f"true_{a}_pred_{p}": sum(1 for x, y in zip(case_actual, case_pred, strict=True) if x == a and y == p) for a in (-1, 0, 1) for p in (-1, 0, 1)},
        },
    }


def _config_deviation(config: Mapping[str, Any]) -> float:
    weights = dict(config["type_weights"])
    return sum(abs(float(weights.get(kind, 1.0)) - 1.0) for kind in ("公告", "研报", "新闻", "政策"))


def calibrate_event_weights(
    samples: Sequence[Mapping[str, Any]],
    *,
    now: Any = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """在人工标注上网格搜索权重；准确率优先，其次 IC，最后偏好等权。"""
    current = pd.Timestamp(now) if now is not None else pd.Timestamp("2026-11-28")
    prior = event_scoring_config()
    results: list[dict[str, Any]] = []
    for ann in _TYPE_WEIGHT_GRID["公告"]:
        for report in _TYPE_WEIGHT_GRID["研报"]:
            for news in _TYPE_WEIGHT_GRID["新闻"]:
                for policy in _TYPE_WEIGHT_GRID["政策"]:
                    for rating in _RATING_WEIGHT_GRID:
                        for bonus in _POLICY_BONUS_GRID:
                            cfg = event_scoring_config()
                            cfg["type_weights"] = {"公告": ann, "研报": report, "新闻": news, "政策": policy}
                            cfg["rating_weight"] = rating
                            cfg["policy_bonus"] = bonus
                            metrics = evaluate_event_config(samples, cfg, now=current)
                            deviation = _config_deviation(cfg)
                            prior_deviation = abs(rating - float(prior["rating_weight"])) + abs(
                                bonus - float(prior["policy_bonus"])
                            )
                            prior_deviation += sum(
                                abs(float(cfg["type_weights"][kind]) - float(prior["type_weights"][kind]))
                                for kind in prior["type_weights"]
                            )
                            objective = (
                                100.0 * metrics["item_accuracy"]
                                + 20.0 * metrics["case_accuracy"]
                                + (metrics["ic"] or -1.0)
                                - 0.002 * deviation
                            )
                            results.append(
                                {
                                    "config": cfg,
                                    "metrics": metrics,
                                    "objective": round(objective, 6),
                                    "deviation": round(deviation, 4),
                                    "prior_deviation": round(prior_deviation, 4),
                                }
                            )
    results.sort(key=lambda row: (row["objective"], -row["prior_deviation"]), reverse=True)
    best = results[0]
    return {
        "config": best["config"],
        "metrics": best["metrics"],
        "objective": best["objective"],
        "grid_points": len(results),
        "top": [
            {
                "type_weights": row["config"]["type_weights"],
                "rating_weight": row["config"]["rating_weight"],
                "policy_bonus": row["config"]["policy_bonus"],
                "item_accuracy": row["metrics"]["item_accuracy"],
                "case_accuracy": row["metrics"]["case_accuracy"],
                "ic": row["metrics"]["ic"],
                "objective": row["objective"],
            }
            for row in results[:top_k]
        ],
    }


def compare_event_rankings(
    candidates: Sequence[Mapping[str, Any]],
    *,
    old_config: Mapping[str, Any] | None = None,
    new_config: Mapping[str, Any] | None = None,
    now: Any = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """按旧/新事件权重重算复合分，返回排序变化。"""
    old_cfg = _normalize_config(old_config or legacy_event_config())
    new_cfg = _normalize_config(new_config or event_scoring_config())
    current = pd.Timestamp(now) if now is not None else pd.Timestamp("2026-11-28")
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        evidence = candidate.get("evidence") or []
        technical = float(candidate.get("technical_score") or 0.0)
        old_event, _ = score_events(evidence, config=old_cfg, now=current)
        new_event, _ = score_events(evidence, config=new_cfg, now=current)
        rows.append(
            {
                "code": str(candidate.get("code") or ""),
                "name": str(candidate.get("name") or ""),
                "technical_score": technical,
                "event_score_old": old_event,
                "event_score_new": new_event,
                "composite_old": round(0.65 * technical + 0.35 * old_event, 1),
                "composite_new": round(0.65 * technical + 0.35 * new_event, 1),
            }
        )
    old_order = sorted(rows, key=lambda row: row["composite_old"], reverse=True)
    new_order = sorted(rows, key=lambda row: row["composite_new"], reverse=True)
    old_rank = {row["code"]: idx + 1 for idx, row in enumerate(old_order)}
    for idx, row in enumerate(new_order):
        row["rank_old"] = old_rank[row["code"]]
        row["rank_new"] = idx + 1
        row["rank_delta"] = row["rank_old"] - row["rank_new"]
    return {
        "rows": new_order,
        "rank_correlation": _rank_corr([r["composite_old"] for r in rows], [r["composite_new"] for r in rows]),
        "top_n": top_n,
        "top_n_overlap": len({r["code"] for r in old_order[:top_n]} & {r["code"] for r in new_order[:top_n]}),
        "old_config": old_cfg,
        "new_config": new_cfg,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="事件分权重校准")
    parser.add_argument("--labels", default=CALIBRATION_LABELS_FILE)
    parser.add_argument("--now", default=CALIBRATION_DATE)
    args = parser.parse_args(argv)
    samples = load_labeled_samples(args.labels)
    legacy = evaluate_event_config(samples, legacy_event_config(), now=args.now)
    calibrated = calibrate_event_weights(samples, now=args.now)
    payload = {
        "labels_file": str(_resolve_labels_path(args.labels)),
        "samples": len(samples),
        "legacy": legacy,
        "calibrated": calibrated["metrics"],
        "selected": {
            "type_weights": calibrated["config"]["type_weights"],
            "rating_weight": calibrated["config"]["rating_weight"],
            "policy_bonus": calibrated["config"]["policy_bonus"],
        },
        "grid_points": calibrated["grid_points"],
        "top": calibrated["top"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
