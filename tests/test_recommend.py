from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from astock import events
from astock.indicators import add_all
from astock.recommend import _parse_json, _pick_diverse, score_events, technical_score


def make_df(up: bool = True, n: int = 120) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=n)
    close = np.linspace(10, 20, n) if up else np.linspace(20, 10, n)
    close = close + np.sin(np.arange(n) / 5) * 0.1
    df = pd.DataFrame(
        {
            "trade_date": dates,
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "vol": np.linspace(1000, 2000, n),
        }
    )
    return add_all(df)


class RecommendTests(unittest.TestCase):
    def test_uptrend_scores_higher_than_downtrend(self):
        up = technical_score(make_df(True))
        down = technical_score(make_df(False))
        self.assertIsNotNone(up)
        self.assertIsNotNone(down)
        self.assertGreater(up["technical_score"], down["technical_score"])

    def test_event_sentiment_positive_vs_negative(self):
        pos = [
            {"kind": "公告", "title": "公司回购增持，业绩预增", "date": pd.Timestamp("2026-10-01")},
            {"kind": "研报", "title": "买入评级，目标价上调", "date": pd.Timestamp("2026-10-02"), "rating": "买入"},
        ]
        neg = [
            {"kind": "公告", "title": "股东减持，涉及诉讼", "date": pd.Timestamp("2026-10-01")},
            {"kind": "公告", "title": "业绩预减，收到问询函", "date": pd.Timestamp("2026-10-02")},
        ]
        pos_score, _ = score_events(pos)
        neg_score, _ = score_events(neg)
        self.assertGreater(pos_score, neg_score)

    def test_aliases_do_not_use_group_prefix(self):
        self.assertEqual(events._aliases("招商银行"), "招商银行")
        self.assertIn("茅台", events._aliases("贵州茅台"))
        self.assertEqual(events._aliases("宁德时代"), "宁德时代")

    def test_parse_json_with_fence(self):
        raw = '```json\n{"market":"x","items":[{"code":"600519.SH"}]}\n```'
        data = _parse_json(raw)
        self.assertEqual(data["items"][0]["code"], "600519.SH")

    def test_industry_diversification(self):
        rows = [
            {"code": "1", "industry": "银行", "score": 5},
            {"code": "2", "industry": "银行", "score": 4},
            {"code": "3", "industry": "银行", "score": 3},
            {"code": "4", "industry": "半导体", "score": 2},
        ]
        picked = _pick_diverse(rows, 3, max_per_industry=2)
        self.assertEqual(len(picked), 3)
        self.assertEqual(sum(1 for r in picked if r["industry"] == "银行"), 2)
        self.assertIn("4", [r["code"] for r in picked])


if __name__ == "__main__":
    unittest.main()
