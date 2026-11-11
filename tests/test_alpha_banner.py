"""「无显著 alpha」横幅：ledger 聚合 + stats 门控 + 刷新/结算路径。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import astock.ledger as ledger
import astock.track as track
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


def _load_output_element_ids(demo) -> set[str]:
    """elem_ids written by every event whose trigger is ``load`` (Gradio 6)."""
    out: set[str] = set()
    for fn in demo.fns.values():
        triggers = {name for _id, name in getattr(fn, "targets", []) or []}
        if "load" in triggers:
            out.update(
                str(getattr(block, "elem_id", "") or "") for block in fn.outputs or []
            )
    return out


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
        self.assertIn("有效独立样本 < 10，暂不显示显著性结论", md)
        self.assertEqual(md.count("样本不足"), 1)
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

    def test_pending_runs_are_disclosed_not_hidden_as_empty_sample(self):
        banner = alpha_banner(
            _rows([2.0] * 4),
            latest_live_date="2026-01-10",
            tracking_date="2026-01-04",
            pending_days=6,
        )

        self.assertEqual(banner.pending_days, 6)
        self.assertEqual(banner.tracking_date, "2026-01-04")
        md = alpha_banner_markdown(banner)
        self.assertIn("跟踪数据截至 **2026-01-04**", md)
        self.assertIn("最新推荐日 **2026-01-10**", md)
        self.assertIn("6 个推荐日尚无 T+5 结果", md)
        self.assertIn("样本不足", md)
        self.assertNotIn("p=", md)

    def test_banner_reads_live_ledger_rows_each_time(self):
        with (
            patch("astock.ledger.tracking_rows", return_value=_rows([2.0] * 4)) as fn,
            patch("astock.ledger.live_run_dates", return_value=[]),
        ):
            first = alpha_banner_snapshot()
        self.assertEqual(first.live_days, 4)
        fn.assert_called_once_with(5)

        with (
            patch("astock.ledger.tracking_rows", return_value=_rows([2.0] * 60)),
            patch("astock.ledger.live_run_dates", return_value=[]),
        ):
            second = alpha_banner_snapshot()
        self.assertEqual(second.live_days, 60)
        self.assertNotEqual(first.live_days, second.live_days)

    def test_snapshot_reports_tracking_lag(self):
        rows = _rows([2.0] * 4, start=date(2026, 1, 1))  # data_as_of 2026-01-01..04
        with (
            patch("astock.ledger.tracking_rows", return_value=rows),
            patch(
                "astock.ledger.live_run_dates",
                return_value=["2026-01-10", "2026-01-09", "2026-01-04"],
            ),
        ):
            banner = alpha_banner_snapshot()

        self.assertEqual(banner.tracking_date, "2026-01-04")
        self.assertEqual(banner.latest_live_date, "2026-01-10")
        self.assertEqual(banner.pending_days, 2)

    def test_database_error_propagates_instead_of_looking_like_no_sample(self):
        with patch(
            "astock.ledger.tracking_rows",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                alpha_banner_snapshot()


class BannerCaveatDedupTests(unittest.TestCase):
    """ADR-010 C2：横幅五状态各一句说明，静态提示不再复述「样本不足/未验证」。"""

    DETAILS = {
        "insufficient": "有效独立样本 < 10，暂不显示显著性结论",
        "unavailable": "p 值暂不可计算",
        "no_alpha": "差异不显著",
        "positive": "历史样本内暂优于其余",
        "negative": "显著弱于其余",
    }

    def _banner(self, state: str) -> AlphaBanner:
        p_value = None if state in {"insufficient", "unavailable"} else 0.01
        return AlphaBanner(
            state=state,
            horizon=5,
            live_days=20,
            independent_windows=6,
            effective_n=12.0,
            p_value=p_value,
            top2_mean=1.2,
            rest_mean=0.3,
        )

    def test_static_note_does_not_repeat_the_banner_caveat(self):
        from astock import recommend_ui

        for word in ("样本不足", "未验证"):
            self.assertNotIn(word, recommend_ui.RESEARCH_NOTE)

    def test_each_state_has_one_explanation_on_screen(self):
        from astock import recommend_ui

        for state, detail in self.DETAILS.items():
            md = alpha_banner_markdown(self._banner(state))
            self.assertIn(detail, md)
            screen = recommend_ui.RESEARCH_NOTE + md
            # 同一屏幕最多一处「样本不足」、一处「未验证」。
            self.assertLessEqual(screen.count("样本不足"), 1)
            self.assertLessEqual(screen.count("未验证"), 1)


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

    def test_builder_returns_banner_that_is_refreshed_on_load(self):
        import gradio as gr

        from astock import recommend_ui

        demo = gr.Blocks()
        with demo, patch.object(
            recommend_ui, "banner_markdown", return_value="LIVE BANNER"
        ):
            banner = recommend_ui.build_recommend_tab()
            recommend_ui.bind_banner_refresh(demo, banner)

        self.assertIsInstance(banner, gr.Markdown)
        self.assertEqual(banner.elem_id, "alpha-banner")
        self.assertIn("alpha-banner", _load_output_element_ids(demo))


class AppBannerWiringTests(unittest.TestCase):
    """The real app must refresh the banner when the page is (re)opened."""

    @classmethod
    def setUpClass(cls):
        try:
            import app as app_module
        except Exception as exc:  # pragma: no cover - optional heavy import
            raise unittest.SkipTest(f"app import failed: {exc}") from exc
        cls.app_module = app_module

    def test_app_registers_load_refresh_for_banner(self):
        self.assertIn("alpha-banner", _load_output_element_ids(self.app_module.app))


class TrackingSettlementTests(unittest.TestCase):
    """Scheduler-side settlement: the banner grows without opening the tab."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rec_dir = self.root / "recommendations"
        self.rec_dir.mkdir()
        # a weekday inside refresh_tracking's lookback window
        self.as_of = pd.bdate_range(
            end=pd.Timestamp.today().normalize() - pd.Timedelta(days=7),
            periods=1,
        )[0]
        self.daily = pd.DataFrame(
            {
                "trade_date": pd.bdate_range(self.as_of, periods=40),
                "open": 10.0,
                "close": [10.0 + 0.1 * i for i in range(40)],
            }
        )
        self.batch = {
            "path": None,
            "as_of": self.as_of,
            "tradable_from": self.as_of,
            "generated_at": "",
            "source": "live",
            "run_id": "run-1",
            "rows": [{"代码": "600519.SH", "排名": 1}],
        }
        self.patches = [
            patch.object(ledger, "DB_PATH", self.root / "ledger.db"),
            patch.object(track, "REC_DIR", self.rec_dir),
            patch.object(track, "_load_batches", return_value=[self.batch]),
            patch.object(track, "load_daily", return_value=self.daily),
            patch.object(track, "load_index_daily", return_value=self.daily),
        ]
        for p in self.patches:
            p.start()
        ledger.save_run(
            {
                "run_id": "run-1",
                "data_as_of": self.as_of.strftime("%Y-%m-%d"),
                "tradable_from": self.as_of.strftime("%Y-%m-%d"),
                "generated_at": f"{self.as_of.strftime('%Y-%m-%d')}T16:40:00",
                "source": "live",
                "table": [{"排名": 1, "代码": "600519.SH"}],
            }
        )

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_refresh_tracking_settles_pending_runs_once(self):
        saved, message = track.refresh_tracking()
        self.assertEqual(saved, 1)
        self.assertIn("已结算 1 个批次", message)
        self.assertEqual(ledger.tracking_run_ids(5, complete_only=True), {"run-1"})

        again, message = track.refresh_tracking()
        self.assertEqual(again, 0)
        self.assertEqual(message, "无待结算批次")


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
