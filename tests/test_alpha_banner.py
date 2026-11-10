"""「无显著 alpha」横幅：ledger 聚合 + stats 门控。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from astock.track import (
    AlphaBanner,
    alpha_banner,
    alpha_banner_markdown,
    alpha_banner_snapshot,
)


def _rows(diffs, *, start: date = date(2026, 1, 1)) -> list[dict]:
    """Each day: rank 1 carries ``diff`` excess, rank 3 carries 0."""
    rows = []
    for i, diff in enumerate(diffs):
        day = (start + timedelta(days=i)).isoformat()
        rows.append(
            {"run_id": f"r{i}", "data_as_of": day, "rank": 1, "excess": float(diff)}
        )
        rows.append(
            {"run_id": f"r{i}", "data_as_of": day, "rank": 3, "excess": 0.0}
        )
    return rows


class AlphaBannerStatsTests(unittest.TestCase):
    def test_insufficient_sample_hides_p(self):
        banner = alpha_banner(_rows([2.0] * 4))

        self.assertEqual(banner.state, "insufficient")
        self.assertEqual(banner.live_days, 4)
        self.assertEqual(banner.independent_windows, 1)
        self.assertLess(banner.effective_n, 10)
        self.assertIsNone(banner.p_value)
        self.assertFalse(banner.trustworthy)

        md = alpha_banner_markdown(banner)
        self.assertIn("实盘样本 **4** 个交易日", md)
        self.assertIn("独立窗口≈**1**", md)
        self.assertIn("样本不足，不显示显著性", md)
        # 无可信统计时不得出现任何 p 值
        self.assertNotIn("p=", md)
        self.assertIn("#f1f3f5", md)

    def test_positive_when_effective_sample_reaches_threshold(self):
        diffs = [2.0 if i % 2 == 0 else 2.5 for i in range(60)]
        banner = alpha_banner(_rows(diffs))

        self.assertEqual(banner.state, "positive")
        self.assertEqual(banner.live_days, 60)
        self.assertEqual(banner.independent_windows, 12)
        self.assertGreaterEqual(banner.effective_n, 10)
        self.assertIsNotNone(banner.p_value)
        self.assertLess(banner.p_value, 0.05)
        self.assertTrue(banner.trustworthy)
        self.assertAlmostEqual(banner.top2_mean, 2.25, places=6)
        self.assertAlmostEqual(banner.rest_mean, 0.0, places=6)

        md = alpha_banner_markdown(banner)
        self.assertIn("p=**", md)
        self.assertIn("暂优于其余", md)
        self.assertIn("#ebfbee", md)

    def test_no_alpha_keeps_numbers_and_says_not_significant(self):
        diffs = [0.2 if i % 2 == 0 else -0.2 for i in range(60)]
        banner = alpha_banner(_rows(diffs))

        self.assertEqual(banner.state, "no_alpha")
        self.assertIsNotNone(banner.p_value)
        self.assertGreaterEqual(banner.p_value, 0.05)
        self.assertTrue(banner.trustworthy)

        md = alpha_banner_markdown(banner)
        self.assertIn("未发现显著 alpha", md)
        self.assertIn("p=**1.0000", md)
        self.assertIn("不显著", md)

    def test_negative_result_is_reported_as_negative_not_alpha(self):
        diffs = [-2.0 if i % 2 == 0 else -2.5 for i in range(60)]
        banner = alpha_banner(_rows(diffs))

        self.assertEqual(banner.state, "negative")
        self.assertLess(banner.p_value, 0.05)
        md = alpha_banner_markdown(banner)
        self.assertIn("显著弱于其余", md)
        self.assertIn("#fff5f5", md)
        self.assertNotIn("暂优于", md)

    def test_zero_variance_is_unavailable_without_p(self):
        banner = alpha_banner(_rows([2.0] * 60))

        self.assertEqual(banner.state, "unavailable")
        self.assertGreaterEqual(banner.effective_n, 10)
        self.assertIsNone(banner.p_value)
        self.assertFalse(banner.trustworthy)

        md = alpha_banner_markdown(banner)
        self.assertIn("不显示显著性结论", md)
        self.assertNotIn("p=", md)

    def test_rows_without_rank_count_days_but_not_windows(self):
        rows = [
            {"data_as_of": f"2026-02-0{i}", "rank": None, "excess": 0.1}
            for i in range(1, 5)
        ]
        banner = alpha_banner(rows)

        self.assertEqual(banner.state, "insufficient")
        self.assertEqual(banner.live_days, 4)
        self.assertEqual(banner.independent_windows, 0)
        self.assertIsNone(banner.p_value)

    def test_banner_reads_live_ledger_rows_each_time(self):
        with patch(
            "astock.ledger.tracking_rows", return_value=_rows([2.0] * 4)
        ) as fn:
            first = alpha_banner_snapshot()
        self.assertEqual(first.live_days, 4)
        fn.assert_called_once_with(5)

        with patch(
            "astock.ledger.tracking_rows", return_value=_rows([2.0] * 60)
        ):
            second = alpha_banner_snapshot()
        self.assertEqual(second.live_days, 60)
        self.assertNotEqual(first.live_days, second.live_days)

    def test_ledger_failure_falls_back_to_insufficient(self):
        with patch("astock.ledger.tracking_rows", side_effect=RuntimeError("db locked")):
            banner = alpha_banner_snapshot()

        self.assertEqual(banner.state, "insufficient")
        self.assertEqual(banner.live_days, 0)
        self.assertIsNone(banner.p_value)


class RecommendTabBannerTests(unittest.TestCase):
    def test_tab_places_banner_above_static_warning(self):
        import gradio as gr

        from astock import recommend_ui

        demo = gr.Blocks()
        with demo, patch.object(
            recommend_ui, "banner_markdown", return_value="LIVE BANNER"
        ):
            recommend_ui.build_recommend_tab()

        blocks = list(demo.blocks.values())
        banner_idx = next(
            i
            for i, block in enumerate(blocks)
            if isinstance(block, gr.Markdown) and block.value == "LIVE BANNER"
        )
        warning_idx = next(
            i
            for i, block in enumerate(blocks)
            if isinstance(block, gr.Markdown) and "研究候选" in str(block.value)
        )
        self.assertLess(banner_idx, warning_idx)


class AlphaBannerDataclassTests(unittest.TestCase):
    def test_trustworthy_requires_p(self):
        banner = AlphaBanner(
            state="no_alpha",
            horizon=5,
            live_days=60,
            independent_windows=12,
            effective_n=12.0,
            p_value=None,
            top2_mean=0.1,
            rest_mean=0.0,
        )
        self.assertFalse(banner.trustworthy)


if __name__ == "__main__":
    unittest.main()
