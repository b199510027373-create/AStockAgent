"""Top5 等权组合净值 vs 沪深300 的口径测试（ADR-010 §3 / DATA-03）。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from astock import track

DATES = pd.to_datetime(
    ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"]
)
CAL = pd.DataFrame({"cal_date": DATES, "is_open": 1})


def bars(dates, opens, closes) -> pd.DataFrame:
    dates = pd.to_datetime(dates)
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": list(opens),
            "high": list(closes),
            "low": list(opens),
            "close": list(closes),
            "vol": 1000.0,
        }
    )


def flat(dates=None, price: float = 10.0) -> pd.DataFrame:
    dates = DATES if dates is None else pd.to_datetime(dates)
    return bars(dates, [price] * len(dates), [price] * len(dates))


def detail_rows(batches: list[tuple[str, str, list[str]]]) -> pd.DataFrame:
    rows = []
    for as_of, entry, codes in batches:
        for rank, code in enumerate(codes, start=1):
            rows.append(
                {
                    "推荐日": pd.Timestamp(as_of),
                    "入场日": pd.Timestamp(entry),
                    "代码": code,
                    "名称": code,
                    "排名": rank,
                    "综合分": 90 - rank,
                }
            )
    return pd.DataFrame(rows)


def bench_frame() -> pd.DataFrame:
    return bars(DATES, [10.0] * 6, [10.5, 11.0, 11.0, 11.0, 11.1, 11.2])


class PortfolioNavTests(unittest.TestCase):
    def _run(self, detail, prices, bench=None, cal=None, **kwargs):
        with (
            patch.object(track, "load_trade_cal", return_value=CAL if cal is None else cal),
            patch.object(track, "load_daily", side_effect=lambda code, **kw: prices[code]),
        ):
            return track.portfolio_nav(
                detail, bench=bench_frame() if bench is None else bench, **kwargs
            )

    def test_batches_are_chained_by_trade_date(self):
        detail = detail_rows(
            [
                ("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)]),
                ("2026-01-07", "2026-01-08", [f"B{i}" for i in range(1, 6)]),
            ]
        )
        a_bars = bars(DATES, [10.0] * 6, [11.0, 12.0, 12.0, 12.0, 12.0, 12.0])
        prices = {f"A{i}": a_bars for i in range(1, 6)}
        prices.update({f"B{i}": flat() for i in range(1, 6)})

        nav, dropped = self._run(detail, prices)

        self.assertTrue(dropped.empty)
        self.assertEqual(len(nav), 6)
        self.assertEqual(list(nav.columns), list(track.NAV_COLUMNS))
        self.assertEqual(
            nav["组合净值"].round(6).tolist(), [1.1, 1.2, 1.2, 1.2, 1.2, 1.2]
        )
        self.assertEqual(
            nav["沪深300净值"].round(6).tolist(), [1.05, 1.1, 1.1, 1.1, 1.11, 1.12]
        )
        # 第二个批次在 2026-01-08 换仓，净值按上一批收盘拼接而不是重新归一到 1
        self.assertEqual(nav["入场日"].nunique(), 2)
        self.assertEqual(
            nav.loc[nav["换仓"], "交易日"].dt.strftime("%Y-%m-%d").tolist(),
            ["2026-01-05", "2026-01-08"],
        )

    def test_entry_uses_open_and_ignores_pre_entry_history(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        history = bars(
            ["2026-01-02"] + list(DATES), [1000.0] + [10.0] * 6, [1000.0, 11.0, 12.0, 12.0, 12.0, 12.0, 12.0]
        )
        prices = {f"A{i}": history for i in range(1, 6)}

        nav, _ = self._run(detail, prices)

        self.assertEqual(nav["交易日"].min(), pd.Timestamp("2026-01-05"))
        self.assertAlmostEqual(nav["组合净值"].iloc[0], 11.0 / 10.0)
        self.assertAlmostEqual(nav["组合净值"].iloc[1], 12.0 / 10.0)

    def test_equal_weight_and_suspension_carry_forward(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        prices = {f"A{i}": flat() for i in range(1, 5)}
        # 停牌：2026-01-06 缺 bar，2026-01-07 复牌涨到 12（基准是入场日开盘 10）
        prices["A5"] = bars(
            ["2026-01-05", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"],
            [10.0] * 5,
            [10.0, 12.0, 12.0, 12.0, 12.0],
        )

        nav, _ = self._run(detail, prices)

        # 5 只等权：只有 A5 上涨，1 + (12/10 - 1) / 5 = 1.04
        self.assertAlmostEqual(nav["组合净值"].iloc[2], 1.04)
        self.assertAlmostEqual(nav["组合净值"].iloc[1], 1.0)  # 停牌日按上一收盘持有

    def test_batch_missing_entry_bar_is_dropped_with_reason(self):
        detail = detail_rows(
            [
                ("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)]),
                ("2026-01-07", "2026-01-08", [f"B{i}" for i in range(1, 6)]),
            ]
        )
        a_bars = bars(DATES, [10.0] * 6, [11.0, 12.0, 12.0, 12.0, 12.0, 12.0])
        prices = {f"A{i}": a_bars for i in range(1, 6)}
        for i in range(1, 6):
            prices[f"B{i}"] = flat()
        # B5 在入场日停牌：整批剔除，A 的持仓延续到最后一个交易日
        prices["B5"] = bars(
            ["2026-01-05", "2026-01-06", "2026-01-09", "2026-01-12"],
            [10.0] * 4,
            [10.0] * 4,
        )

        nav, dropped = self._run(detail, prices)

        self.assertEqual(len(dropped), 1)
        self.assertIn("B5", dropped["原因"].iloc[0])
        self.assertIn("缺 bar", dropped["原因"].iloc[0])
        self.assertEqual(nav["入场日"].nunique(), 1)
        self.assertEqual(
            nav["组合净值"].round(6).tolist(), [1.1, 1.2, 1.2, 1.2, 1.2, 1.2]
        )

    def test_partial_batch_can_be_kept_when_not_strict(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        prices = {f"A{i}": flat() for i in range(1, 5)}
        prices["A5"] = bars(["2026-01-06", "2026-01-12"], [10.0, 10.0], [10.0, 10.0])

        nav, dropped = self._run(detail, prices, require_full_batch=False)

        self.assertTrue(dropped.empty)
        self.assertEqual(nav["入场日"].nunique(), 1)
        self.assertAlmostEqual(nav["组合净值"].iloc[0], 1.0)

    def test_batch_on_non_trading_day_is_dropped(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        prices = {f"A{i}": flat() for i in range(1, 6)}
        # 交易日历里没有 2026-01-05（例如休市）：这是“非交易日”，不是“指数缺 bar”
        cal = pd.DataFrame({"cal_date": DATES[1:], "is_open": 1})

        nav, dropped = self._run(detail, prices, cal=cal)

        self.assertTrue(nav.empty)
        self.assertEqual(len(dropped), 1)
        self.assertIn("不是交易日", dropped["原因"].iloc[0])

    def test_missing_bench_bar_on_entry_keeps_batch(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        prices = {f"A{i}": flat() for i in range(1, 6)}
        # 2026-01-05 是交易日，只是指数当日 bar 尚未生成
        bench = bars(DATES[1:], [10.0] * 5, [10.5] * 5)

        nav, dropped = self._run(detail, prices, bench=bench)

        self.assertTrue(dropped.empty)
        self.assertEqual(nav["交易日"].min(), DATES[0])
        self.assertTrue(pd.isna(nav["沪深300净值"].iloc[0]))
        text = track.nav_status_text(nav, dropped)
        self.assertNotIn("不是交易日", text)
        self.assertIn("沪深300", text)

    def test_nav_extends_when_benchmark_lags_tail(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        prices = {f"A{i}": flat() for i in range(1, 6)}
        bench = bars(DATES[:4], [10.0] * 4, [10.5, 11.0, 11.0, 11.0])

        nav, dropped = self._run(detail, prices, bench=bench)

        self.assertTrue(dropped.empty)
        self.assertEqual(nav["交易日"].iloc[-1], DATES[-1])
        self.assertTrue(pd.isna(nav["沪深300净值"].iloc[-1]))
        self.assertFalse(pd.isna(nav.loc[nav["交易日"] == DATES[3], "沪深300净值"]).any())
        self.assertTrue(pd.isna(nav["沪深300净值"].iloc[-2]))
        text = track.nav_status_text(nav, dropped)
        self.assertIn("数据截至 2026-01-08", text)

    def test_batch_with_fewer_than_top5_is_dropped(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", ["A1", "A2", "A3"])])
        prices = {f"A{i}": flat() for i in range(1, 4)}

        nav, dropped = self._run(detail, prices)

        self.assertTrue(nav.empty)
        self.assertEqual(len(dropped), 1)
        self.assertIn("3 只", dropped["原因"].iloc[0])

    def test_missing_rank_falls_back_to_score(self):
        rows = []
        for i, score in enumerate([100, 90, 80, 70, 60, 50], start=1):
            rows.append(
                {
                    "推荐日": pd.Timestamp("2026-01-02"),
                    "入场日": pd.Timestamp("2026-01-05"),
                    "代码": f"F{i}",
                    "排名": None,
                    "综合分": score,
                }
            )

        plans = track._batch_plans(pd.DataFrame(rows), 5)

        self.assertEqual(plans[0]["codes"], ["F1", "F2", "F3", "F4", "F5"])

    def test_nan_close_carries_previous_close_without_reweighting(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        prices = {f"A{i}": flat() for i in range(1, 5)}
        # A5 在 2026-01-06 行存在但 close 为 NaN：应按上一收盘持有，收益记 0
        prices["A5"] = bars(
            DATES, [10.0] * 6, [10.0, np.nan, 12.0, 12.0, 12.0, 12.0]
        )

        nav, _ = self._run(detail, prices)

        self.assertAlmostEqual(nav["组合净值"].iloc[1], 1.0)
        self.assertAlmostEqual(nav["组合净值"].iloc[2], 1.04)

    def test_duplicate_entry_date_keeps_latest_batch(self):
        detail = detail_rows(
            [
                ("2026-01-02", "2026-01-05", [f"OLD{i}" for i in range(1, 6)]),
                ("2026-01-04", "2026-01-05", [f"NEW{i}" for i in range(1, 6)]),
            ]
        )
        prices = {f"NEW{i}": flat() for i in range(1, 6)}

        nav, _ = self._run(detail, prices, bench=bench_frame())

        self.assertEqual(len(nav), 6)
        self.assertEqual(nav["推荐日"].iloc[0], pd.Timestamp("2026-01-04"))

    def test_missing_benchmark_keeps_portfolio_line(self):
        detail = detail_rows([("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)])])
        prices = {f"A{i}": flat() for i in range(1, 6)}

        nav, _ = self._run(detail, prices, bench=pd.DataFrame())

        self.assertEqual(len(nav), 6)
        self.assertTrue(nav["沪深300净值"].isna().all())

    def test_empty_detail_returns_empty_frames(self):
        nav, dropped = track.portfolio_nav(pd.DataFrame(), bench=bench_frame())
        self.assertTrue(nav.empty)
        self.assertTrue(dropped.empty)


class NavPresentationTests(unittest.TestCase):
    def _nav(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        detail = detail_rows(
            [
                ("2026-01-02", "2026-01-05", [f"A{i}" for i in range(1, 6)]),
                ("2026-01-07", "2026-01-08", [f"B{i}" for i in range(1, 6)]),
            ]
        )
        a_bars = bars(DATES, [10.0] * 6, [11.0, 12.0, 12.0, 12.0, 12.0, 12.0])
        prices = {f"A{i}": a_bars for i in range(1, 6)}
        prices.update({f"B{i}": flat() for i in range(1, 6)})
        with (
            patch.object(track, "load_trade_cal", return_value=CAL),
            patch.object(track, "load_daily", side_effect=lambda code, **kw: prices[code]),
        ):
            return track.portfolio_nav(detail, bench=bench_frame())

    def test_status_text_reports_nav_and_caveat(self):
        nav, dropped = self._nav()
        text = track.nav_status_text(nav, dropped)
        self.assertIn("组合净值", text)
        self.assertIn("Top5 等权累计", text)
        self.assertIn("沪深300", text)
        self.assertIn("口径", text)
        self.assertIn("不构成 alpha 证据", text)
        # 净值是小数比率，超额必须以百分点展示（1.2 - 1.12 = +8.00pp，而不是 +0.08pp）
        self.assertIn("+8.00pp", text)

    def test_status_text_mentions_dropped_batches(self):
        dropped = pd.DataFrame(
            [
                {
                    "推荐日": pd.Timestamp("2026-01-07"),
                    "入场日": pd.Timestamp("2026-01-08"),
                    "原因": "入场日 2026-01-08 缺 bar：B5",
                }
            ]
        )
        text = track.nav_status_text(pd.DataFrame(), dropped)
        self.assertIn("已剔除 1 个批次", text)
        self.assertIn("B5", text)

    def test_status_text_empty_nav_does_not_claim_carry_forward(self):
        dropped = pd.DataFrame(
            [
                {
                    "推荐日": pd.Timestamp("2026-01-07"),
                    "入场日": pd.Timestamp("2026-01-08"),
                    "原因": "入场日 2026-01-08 缺 bar：B5",
                }
            ]
        )

        text = track.nav_status_text(pd.DataFrame(), dropped)

        self.assertIn("暂无可拼接的批次", text)
        self.assertNotIn("延续", text)

    def test_status_text_non_empty_nav_with_dropped_mentions_carry_forward(self):
        nav, _ = self._nav()
        dropped = pd.DataFrame(
            [
                {
                    "推荐日": pd.Timestamp("2026-01-07"),
                    "入场日": pd.Timestamp("2026-01-08"),
                    "原因": "入场日 2026-01-08 缺 bar：B5",
                }
            ]
        )

        text = track.nav_status_text(nav, dropped)

        self.assertIn("已剔除 1 个批次", text)
        self.assertIn("延续到下一批", text)

    def test_nav_figure_has_portfolio_benchmark_and_rebalance_markers(self):
        nav, _ = self._nav()
        fig = track.nav_figure(nav)
        names = [trace.name for trace in fig.data]
        self.assertIn("Top5 等权组合", names)
        self.assertIn("沪深300", names)
        self.assertIn("批次换仓", names)

    def test_nav_figure_handles_empty_nav(self):
        fig = track.nav_figure(pd.DataFrame())
        self.assertEqual(len(fig.data), 0)


class TrackUiWiringTests(unittest.TestCase):
    def _fixtures(self):
        detail = pd.DataFrame(
            [
                {
                    "推荐日": pd.Timestamp("2026-01-02"),
                    "入场日": pd.Timestamp("2026-01-05"),
                    "代码": "600519.SH",
                    "名称": "贵州茅台",
                    "排名": 1,
                    "综合分": 90,
                }
            ]
        )
        summary = pd.DataFrame(
            [
                {
                    "分组": "Top2",
                    "样本数": 1,
                    "T+1平均超额%": 0.5,
                    "T+1胜率%": 100.0,
                    "T+1样本": 1,
                    "T+5平均超额%": None,
                    "T+5胜率%": None,
                    "T+5样本": 0,
                    "T+20平均超额%": None,
                    "T+20胜率%": None,
                    "T+20样本": 0,
                }
            ]
        )
        nav = pd.DataFrame(
            [
                {
                    "交易日": pd.Timestamp("2026-01-05"),
                    "组合净值": 1.01,
                    "沪深300净值": 1.0,
                    "推荐日": pd.Timestamp("2026-01-02"),
                    "入场日": pd.Timestamp("2026-01-05"),
                    "换仓": True,
                }
            ]
        )
        return detail, summary, nav

    def test_run_track_puts_nav_first_and_keeps_excess_table(self):
        from astock import track_ui

        detail, summary, nav = self._fixtures()
        with (
            patch.object(
                track_ui, "track_recommendations", return_value=(detail, summary, "状态")
            ),
            patch.object(track_ui, "portfolio_nav", return_value=(nav, pd.DataFrame())),
            patch.object(track_ui, "significance_table", return_value=pd.DataFrame()),
        ):
            outputs = track_ui.run_track()

        self.assertEqual(len(outputs), 6)
        self.assertIn("组合净值", outputs[0])
        self.assertIn("状态", outputs[0])
        self.assertIn("Top2", outputs[2]["分组"].tolist())

    def test_run_track_survives_nav_failure(self):
        from astock import track_ui

        detail, summary, _ = self._fixtures()
        with (
            patch.object(
                track_ui, "track_recommendations", return_value=(detail, summary, "状态")
            ),
            patch.object(track_ui, "portfolio_nav", side_effect=RuntimeError("boom")),
            patch.object(track_ui, "significance_table", return_value=pd.DataFrame()),
        ):
            outputs = track_ui.run_track()

        self.assertEqual(len(outputs), 6)
        self.assertIn("失败", outputs[0])
        self.assertIn("boom", outputs[0])
        # 净值段失败时，分组统计仍然返回
        self.assertIn("Top2", outputs[2]["分组"].tolist())

    def test_build_track_tab_folds_excess_tables_below_nav(self):
        import gradio as gr

        from astock import track_ui

        demo = gr.Blocks()
        with demo:
            track_ui.build_track_tab()
        blocks = list(demo.blocks.values())
        nav_idx = next(
            i
            for i, block in enumerate(blocks)
            if isinstance(block, gr.Plot)
            and block.label == "Top5 等权组合净值 vs 沪深300"
        )
        accordions = [block for block in blocks if isinstance(block, gr.Accordion)]

        self.assertEqual(len(accordions), 2)
        for accordion in accordions:
            self.assertFalse(accordion.open)
            self.assertGreater(blocks.index(accordion), nav_idx)
        folded_labels = {child.label for acc in accordions for child in acc.children}
        self.assertIn("分组统计（平均超额）", folded_labels)
        self.assertIn("显著性检验", folded_labels)
        self.assertIn("逐条记录", folded_labels)


if __name__ == "__main__":
    unittest.main()
