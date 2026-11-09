from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

import astock.recommend_ui as recommend_ui
import astock.use_cases as use_cases
from astock.contracts import ContractError
from astock.recommend import Recommendation, StaleDataError
from astock.use_cases import RunResult


def _history_run(**overrides):
    run = {
        "run_id": "rec_hist",
        "data_as_of": "2026-11-06",
        "tradable_from": "2026-11-09",
        "generated_at": "2026-11-06T18:00:00",
        "source": "live",
        "items": [
            {
                "rank": 1,
                "code": "600519.SH",
                "name": "贵州茅台",
                "industry": "白酒",
                "composite": 82.0,
                "technical": 80,
                "event": 86,
                "action": "技术面强",
                "reason": "测试",
            }
        ],
        "evidence": [
            {
                "code": "600519.SH",
                "kind": "新闻",
                "pub_time": "2026-11-06T09:00:00",
                "title": "测试标题",
                "url": "http://example.com/a",
                "source": "测试源",
            }
        ],
        "csv_path": "/tmp/x.csv",
        "json_path": "/tmp/x.json",
    }
    run.update(overrides)
    return run


def _recommendation(**overrides) -> Recommendation:
    values = {
        "table": pd.DataFrame(
            [
                {
                    "排名": 1,
                    "代码": "600519.SH",
                    "名称": "贵州茅台",
                    "行业": "白酒",
                    "综合分": 82.0,
                    "技术分": 80,
                    "事件分": 86,
                    "建议": "技术面强",
                    "理由": "测试",
                }
            ]
        ),
        "report": "### 报告",
        "evidence_md": "### 证据链",
        "status": "✅ 生成完成；data_as_of=2026-11-06；run_id=rec_gen",
        "csv_path": "/tmp/gen.csv",
        "json_path": "/tmp/gen.json",
        "run_id": "rec_gen",
        "data_as_of": "2026-11-06",
        "tradable_from": "2026-11-09",
        "diagnostics": {
            "generated_at": "2026-11-06T18:00:00",
            "expected_trade_date": "2026-11-06",
            "stale_data": False,
        },
    }
    values.update(overrides)
    return Recommendation(**values)


class RunResultTests(unittest.TestCase):
    def test_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            RunResult("r", "ok", {}, {})

    def test_stale_and_failed_require_message(self):
        for status in ("stale", "failed"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    RunResult("r", status, {}, {"reason": "x"})
                with self.assertRaises(ValueError):
                    RunResult("r", status, {}, {"message": "   "})

    def test_success_allows_empty_message(self):
        result = RunResult("r", "success", {"a": 1}, {})
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "")


class GenerateRecommendationTests(unittest.TestCase):
    def test_success_wraps_payload_and_diagnostics(self):
        rec = _recommendation()
        with patch.object(use_cases, "generate_recommendation", return_value=rec) as fn:
            result = use_cases.run_recommendation(
                mode="generate",
                universe_text="600519.SH",
                top_n=3,
                candidate_n=8,
                news_days=7,
                use_llm=False,
            )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.run_id, "rec_gen")
        self.assertEqual(result.payload["report"], "### 报告")
        self.assertEqual(result.payload["evidence_md"], "### 证据链")
        self.assertEqual(result.payload["files"], ["/tmp/gen.csv", "/tmp/gen.json"])
        self.assertIn("data_as_of=2026-11-06", result.payload["status_text"])
        self.assertEqual(result.diagnostics["reason"], "generated")
        fn.assert_called_once()
        self.assertEqual(fn.call_args.kwargs["top_n"], 3)

    def test_stale_data_is_reported_as_stale(self):
        rec = _recommendation(
            diagnostics={
                "generated_at": "2026-11-06T10:00:00",
                "expected_trade_date": "2026-11-05",
                "stale_data": True,
            }
        )
        with patch.object(use_cases, "generate_recommendation", return_value=rec):
            result = use_cases.run_recommendation(mode="generate")
        self.assertEqual(result.status, "stale")
        self.assertIn("数据未更新", result.message)
        self.assertIn("2026-11-05", result.message)
        self.assertTrue(result.payload["status_text"].startswith("⚠️"))

    def test_stale_exception_returns_stale_without_payload(self):
        with patch.object(
            use_cases,
            "generate_recommendation",
            side_effect=StaleDataError("日线数据未更新：data_as_of=2026-11-05"),
        ):
            result = use_cases.run_recommendation(mode="generate")
        self.assertEqual(result.status, "stale")
        self.assertEqual(result.run_id, "")
        self.assertEqual(result.payload["table"].empty, True)
        self.assertIn("日线数据未更新", result.message)
        self.assertIn("⚠️", result.payload["status_text"])

    def test_contract_error_is_failed(self):
        with patch.object(
            use_cases,
            "generate_recommendation",
            side_effect=ContractError("RecommendationRun", "table 缺列"),
        ):
            result = use_cases.run_recommendation(mode="generate")
        self.assertEqual(result.status, "failed")
        self.assertIn("契约校验失败", result.message)
        self.assertIn("❌", result.payload["status_text"])

    def test_unexpected_error_is_failed_with_type(self):
        with patch.object(
            use_cases, "generate_recommendation", side_effect=RuntimeError("boom")
        ):
            result = use_cases.run_recommendation(mode="generate")
        self.assertEqual(result.status, "failed")
        self.assertIn("生成失败：RuntimeError: boom", result.message)


class HistoryRecommendationTests(unittest.TestCase):
    def test_latest_success_maps_ledger_rows(self):
        with (
            patch.object(use_cases.ledger, "latest_run", return_value=_history_run()),
            patch.object(
                use_cases,
                "last_completed_trade_date",
                return_value=pd.Timestamp("2026-11-06"),
            ),
        ):
            result = use_cases.run_recommendation(
                mode="latest", now=datetime(2026, 11, 6, 20, 0)
            )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.run_id, "rec_hist")
        table = result.payload["table"]
        self.assertEqual(table.iloc[0]["代码"], "600519.SH")
        self.assertEqual(table.iloc[0]["建议"], "技术面强")
        self.assertIn("rec_hist", result.payload["report"])
        self.assertIn("测试标题", result.payload["evidence_md"])
        self.assertIn("http://example.com/a", result.payload["evidence_md"])
        self.assertEqual(result.payload["files"], ["/tmp/x.csv", "/tmp/x.json"])
        self.assertIn("已从 SQLite 读取 rec_hist", result.payload["status_text"])

    def test_run_mode_uses_requested_run_id(self):
        with (
            patch.object(use_cases.ledger, "get_run", return_value=_history_run()),
            patch.object(
                use_cases,
                "last_completed_trade_date",
                return_value=pd.Timestamp("2026-11-06"),
            ),
        ):
            result = use_cases.run_recommendation(
                mode="run", run_id="rec_hist", now=datetime(2026, 11, 6, 20, 0)
            )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.diagnostics["reason"], "ledger_run")

    def test_history_older_than_expected_is_stale(self):
        with (
            patch.object(
                use_cases.ledger,
                "latest_run",
                return_value=_history_run(data_as_of="2026-11-05"),
            ),
            patch.object(
                use_cases,
                "last_completed_trade_date",
                return_value=pd.Timestamp("2026-11-06"),
            ),
        ):
            result = use_cases.run_recommendation(
                mode="latest", now=datetime(2026, 11, 6, 20, 0)
            )
        self.assertEqual(result.status, "stale")
        self.assertIn("早于最近交易日", result.message)
        self.assertTrue(result.payload["status_text"].startswith("⚠️"))
        self.assertIn("已从 SQLite 读取", result.payload["status_text"])

    def test_history_missing_data_as_of_is_stale(self):
        with (
            patch.object(
                use_cases.ledger,
                "latest_run",
                return_value=_history_run(data_as_of=""),
            ),
            patch.object(
                use_cases,
                "last_completed_trade_date",
                return_value=pd.Timestamp("2026-11-06"),
            ),
        ):
            result = use_cases.run_recommendation(mode="latest")
        self.assertEqual(result.status, "stale")
        self.assertIn("缺少 data_as_of", result.message)

    def test_empty_ledger_is_failed(self):
        with patch.object(use_cases.ledger, "latest_run", return_value=None):
            result = use_cases.run_recommendation(mode="latest")
        self.assertEqual(result.status, "failed")
        self.assertIn("台账里还没有推荐记录", result.message)
        self.assertEqual(result.payload["table"].empty, True)

    def test_ledger_read_error_is_failed(self):
        with patch.object(
            use_cases.ledger, "latest_run", side_effect=RuntimeError("db locked")
        ):
            result = use_cases.run_recommendation(mode="latest")
        self.assertEqual(result.status, "failed")
        self.assertIn("读取台账失败：RuntimeError: db locked", result.message)
        self.assertIn("❌", result.payload["status_text"])

    def test_unknown_run_id_is_failed(self):
        with patch.object(use_cases.ledger, "get_run", return_value=None):
            result = use_cases.run_recommendation(mode="run", run_id="missing")
        self.assertEqual(result.status, "failed")
        self.assertIn("missing", result.message)

    def test_run_mode_requires_run_id(self):
        with self.assertRaises(ValueError):
            use_cases.run_recommendation(mode="run")

    def test_generate_and_history_share_one_renderer(self):
        with (
            patch.object(use_cases, "generate_recommendation", return_value=_recommendation()),
            patch.object(use_cases.ledger, "latest_run", return_value=_history_run()),
            patch.object(
                use_cases,
                "last_completed_trade_date",
                return_value=pd.Timestamp("2026-11-06"),
            ),
            patch.object(
                use_cases,
                "_render_recommendation",
                wraps=use_cases._render_recommendation,
            ) as render,
        ):
            use_cases.run_recommendation(mode="generate")
            use_cases.run_recommendation(
                mode="latest", now=datetime(2026, 11, 6, 20, 0)
            )
        self.assertEqual(render.call_count, 2)


class TrackingTests(unittest.TestCase):
    def _detail(self, excess=0.5):
        return pd.DataFrame(
            [
                {
                    "排名": 1,
                    "代码": "600519.SH",
                    "名称": "贵州茅台",
                    "综合分": 82.0,
                    "建议": "技术面强",
                    "T+1": 1.2,
                    "超额T+1": excess,
                    "推荐日": pd.Timestamp("2026-11-05"),
                    "入场日": pd.Timestamp("2026-11-06"),
                }
            ]
        )

    def _summary(self):
        return pd.DataFrame([{"分组": "Top2", "样本数": 1}])

    def _nav(self):
        return pd.DataFrame(
            [
                {
                    "交易日": pd.Timestamp("2026-11-06"),
                    "组合净值": 1.01,
                    "沪深300净值": 1.0,
                }
            ]
        )

    def test_success_returns_all_payload_fields(self):
        detail = self._detail()
        summary = self._summary()
        with (
            patch.object(
                use_cases, "compute_tracking", return_value=(detail, summary, "跟踪状态")
            ),
            patch.object(
                use_cases,
                "portfolio_nav",
                return_value=(self._nav(), pd.DataFrame()),
            ),
            patch.object(use_cases, "nav_status_text", return_value="净值状态"),
            patch.object(use_cases, "nav_figure", return_value="NAV"),
            patch.object(use_cases, "significance_table", return_value="SIG"),
            patch.object(use_cases, "track_figure", return_value="EXCESS"),
        ):
            result = use_cases.run_tracking()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.payload["nav_figure"], "NAV")
        self.assertEqual(result.payload["excess_figure"], "EXCESS")
        self.assertEqual(result.payload["significance"], "SIG")
        self.assertEqual(result.payload["summary"].iloc[0]["分组"], "Top2")
        self.assertEqual(result.payload["detail"].iloc[0]["推荐日"], "2026-11-05")
        self.assertIn("净值状态", result.payload["status_text"])
        self.assertIn("跟踪状态", result.payload["status_text"])
        self.assertEqual(result.diagnostics["usable_t1"], 1)

    def test_no_forward_return_is_stale(self):
        with (
            patch.object(
                use_cases,
                "compute_tracking",
                return_value=(self._detail(excess=float("nan")), self._summary(), "状态"),
            ),
            patch.object(
                use_cases, "portfolio_nav", return_value=(self._nav(), pd.DataFrame())
            ),
            patch.object(use_cases, "nav_status_text", return_value="净值状态"),
            patch.object(use_cases, "nav_figure", return_value="NAV"),
            patch.object(use_cases, "significance_table", return_value=pd.DataFrame()),
            patch.object(use_cases, "track_figure", return_value=None),
        ):
            result = use_cases.run_tracking()
        self.assertEqual(result.status, "stale")
        self.assertIn("T+1 收盘数据", result.message)
        self.assertIn("⚠️", result.payload["status_text"])

    def test_compute_error_is_failed(self):
        with patch.object(
            use_cases, "compute_tracking", side_effect=RuntimeError("boom")
        ):
            result = use_cases.run_tracking()
        self.assertEqual(result.status, "failed")
        self.assertIn("跟踪计算失败：RuntimeError: boom", result.message)
        self.assertIn("❌", result.payload["status_text"])

    def test_empty_tracking_is_failed(self):
        with patch.object(
            use_cases,
            "compute_tracking",
            return_value=(pd.DataFrame(), pd.DataFrame(), "暂无历史推荐。"),
        ):
            result = use_cases.run_tracking()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.diagnostics["reason"], "no_tracking_data")
        self.assertIn("暂无历史推荐", result.message)

    def test_nav_failure_keeps_summary_and_is_diagnosed(self):
        with (
            patch.object(
                use_cases,
                "compute_tracking",
                return_value=(self._detail(), self._summary(), "跟踪状态"),
            ),
            patch.object(
                use_cases, "portfolio_nav", side_effect=RuntimeError("boom")
            ),
            patch.object(use_cases, "nav_figure", return_value="EMPTY"),
            patch.object(use_cases, "significance_table", return_value=pd.DataFrame()),
            patch.object(use_cases, "track_figure", return_value=None),
        ):
            result = use_cases.run_tracking()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.payload["summary"].iloc[0]["分组"], "Top2")
        self.assertIn("净值计算失败", result.payload["status_text"])
        self.assertIn("boom", result.payload["status_text"])
        self.assertIn("nav_error", result.diagnostics)

    def test_significance_failure_keeps_detail(self):
        with (
            patch.object(
                use_cases,
                "compute_tracking",
                return_value=(self._detail(), self._summary(), "跟踪状态"),
            ),
            patch.object(
                use_cases, "portfolio_nav", return_value=(self._nav(), pd.DataFrame())
            ),
            patch.object(use_cases, "nav_status_text", return_value="净值状态"),
            patch.object(use_cases, "nav_figure", return_value="NAV"),
            patch.object(
                use_cases, "significance_table", side_effect=RuntimeError("stat boom")
            ),
            patch.object(use_cases, "track_figure", return_value=None),
        ):
            result = use_cases.run_tracking()
        self.assertEqual(result.status, "success")
        self.assertFalse(result.payload["detail"].empty)
        self.assertIn("significance_error", result.diagnostics)


class UIConsumptionTests(unittest.TestCase):
    def test_recommend_ui_generate_maps_run_result(self):
        table = pd.DataFrame([{"排名": 1, "代码": "600519.SH"}])
        result = RunResult(
            "rec_ui",
            "success",
            {
                "status_text": "✅ UI",
                "table": table,
                "report": "报告",
                "evidence_md": "证据",
                "files": ["a.csv", "a.json"],
            },
            {"message": ""},
        )
        with patch.object(
            recommend_ui, "run_recommendation", return_value=result
        ) as fn:
            outputs = recommend_ui.run_recommend(
                "600519.SH", 3, 8, "3日", False
            )
        self.assertEqual(outputs, ("✅ UI", table, "报告", "证据", ["a.csv", "a.json"]))
        self.assertEqual(fn.call_args.kwargs["mode"], "generate")
        self.assertEqual(fn.call_args.kwargs["news_days"], 3)

    def test_recommend_ui_latest_maps_run_result(self):
        result = RunResult(
            "rec_ui",
            "stale",
            {"status_text": "⚠️ 旧数据", "table": pd.DataFrame()},
            {"message": "旧数据"},
        )
        with patch.object(
            recommend_ui, "run_recommendation", return_value=result
        ) as fn:
            outputs = recommend_ui.load_latest_ledger()
        self.assertEqual(outputs[0], "⚠️ 旧数据")
        self.assertEqual(fn.call_args.kwargs["mode"], "latest")


if __name__ == "__main__":
    unittest.main()
