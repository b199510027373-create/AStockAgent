"""图表图注必须如实标注复权口径（ADR-008 D1 / plan-20261114-003 验收⑥）。

旧实现用 ``'前复权' if adj == 'qfq' else '不复权'``，会把 hfq 说成"不复权"，
也不展示 adj_base —— 图上看不出后复权的基准日与基准因子。
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from astock.charts import _adj_caption, summary_markdown


def _daily(n: int = 3, attrs: dict | None = None) -> pd.DataFrame:
    close = [10.0 + i for i in range(n)]
    frame = pd.DataFrame(
        {
            "trade_date": pd.date_range("2025-01-02", periods=n, freq="D"),
            "open": close,
            "high": [c + 0.5 for c in close],
            "low": [c - 0.5 for c in close],
            "close": close,
            "vol": [1000.0] * n,
            "vol_ma20": [1000.0] * n,
            "ma20": close,
            "ma60": close,
            "ma250": close,
            "rsi14": [50.0] * n,
            "macd_hist": [0.1] * n,
        }
    )
    if attrs is not None:
        frame.attrs["adjustment"] = attrs
    return frame


class AdjCaptionTests(unittest.TestCase):
    def test_hfq_is_not_labeled_unadjusted(self):
        daily = _daily(
            attrs={
                "adj": "hfq",
                "adj_base": "2001-08-27",
                "base_factor": 1.0,
                "adj_source": "daily",
            }
        )
        caption = _adj_caption("hfq", daily)
        self.assertIn("后复权（hfq）", caption)
        self.assertIn("基准 2001-08-27 起 1.0", caption)
        self.assertIn("因子来源 daily", caption)
        self.assertNotIn("不复权", caption)

    def test_qfq_shows_base_date_and_factor(self):
        daily = _daily(
            attrs={
                "adj": "qfq",
                "adj_base": "2025-01-06",
                "base_factor": 8.0,
                "adj_source": "daily",
            }
        )
        caption = _adj_caption("qfq", daily)
        self.assertIn("前复权（qfq）", caption)
        self.assertIn("基准 2025-01-06", caption)
        self.assertIn("因子 8.0000", caption)

    def test_none_hides_base(self):
        daily = _daily(
            attrs={
                "adj": "none",
                "adj_base": None,
                "base_factor": None,
                "adj_source": "daily",
            }
        )
        caption = _adj_caption("none", daily)
        self.assertIn("不复权（原始成交价）", caption)
        self.assertNotIn("基准", caption)

    def test_falls_back_to_argument_without_attrs(self):
        caption = _adj_caption("hfq", _daily())
        self.assertIn("后复权（hfq）", caption)
        self.assertNotIn("基准", caption)

    def test_unknown_caliber_is_flagged(self):
        caption = _adj_caption("raw_v2", _daily(attrs={"adj": "raw_v2"}))
        self.assertIn("口径未知（raw_v2）", caption)

    def test_summary_markdown_uses_caliber_caption(self):
        daily = _daily(
            n=4,
            attrs={
                "adj": "hfq",
                "adj_base": "2001-08-27",
                "base_factor": 1.0,
                "adj_source": "daily",
            },
        )
        text = summary_markdown(
            "600519.SH",
            daily,
            daily,
            daily,
            "20250102",
            "20250105",
            "hfq",
        )
        self.assertIn("后复权（hfq）", text)
        self.assertIn("基准 2001-08-27 起 1.0", text)
        self.assertIn("共 4 根日线", text)
        self.assertTrue(np.isfinite(daily["close"].iloc[-1]))


if __name__ == "__main__":
    unittest.main()
