from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import astock.ledger as ledger
import astock.pit as pit
import astock.scheduler as scheduler
from astock.pit import snapshot_maps
from astock.backtest import _one_price_limit, limit_pct, run_backtest
from astock.events import _col, score_events
from astock.recommend import _clean_json, _pick_diverse
from astock.stats import benjamini_hochberg, effective_n, newey_west, student_t_p
from astock.track import _forward_return
from astock.use_cases import RunResult


def make_result(
    data_as_of: str = "",
    *,
    status: str = "success",
    message: str = "",
    run_id: str = "run_test",
    csv_path: str = "/tmp/a.csv",
    json_path: str = "/tmp/a.json",
) -> RunResult:
    payload = {
        "data_as_of": data_as_of,
        "run_id": run_id,
        "csv_path": csv_path,
        "json_path": json_path,
    }
    return RunResult(run_id, status, payload, {"message": message})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(ledger, "DB_PATH", Path(self.tmp.name) / "test.db")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_run_and_job_roundtrip(self):
        payload = {
            "run_id": "rec_test",
            "schema_version": 2,
            "data_as_of": "2026-10-30",
            "tradable_from": "2026-11-03",
            "generated_at": "2026-11-02T09:00:00",
            "source": "live",
            "table": [
                {"排名": 1, "代码": "600519.SH", "名称": "贵州茅台", "行业": "白酒",
                 "综合分": 82.4, "技术分位": 91.0, "技术分": 80, "事件分": 86,
                 "技术面": "技术面强", "理由": "test"}
            ],
            "evidence": {"600519.SH": [{"kind": "新闻", "date": "2026-10-30", "title": "t", "url": ""}]},
            "params": {"top_n": 5},
            "diagnostics": {"failed_codes": []},
        }
        ledger.save_run(payload, json_path="/tmp/x.json")
        got = ledger.get_run("rec_test")
        self.assertIsNotNone(got)
        self.assertEqual(got["data_as_of"], "2026-10-30")
        self.assertEqual(len(got["items"]), 1)
        self.assertEqual(got["items"][0]["action"], "技术面强")
        self.assertEqual(got["items"][0]["percentile"], 91.0)
        self.assertEqual(len(got["evidence"]), 1)
        self.assertEqual(ledger.stats()["runs"], 1)

        ledger.save_job("2026-11-02:close", trade_date="2026-11-02", slot="close",
                        status="success", run_id="rec_test")
        job = ledger.get_job("2026-11-02:close")
        self.assertEqual(job["status"], "success")
        self.assertEqual(job["run_id"], "rec_test")


class SchedulerIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.refresh_mock = MagicMock(return_value=(0, "skip"))
        self.patches = [
            patch.object(scheduler, "STATE_PATH", root / "state.json"),
            patch.object(scheduler, "LOG_PATH", root / "scheduler.log"),
            patch.object(scheduler, "LOCK_PATH", root / "scheduler.lock"),
            patch.object(scheduler, "refresh_tracking", new=self.refresh_mock),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_premarket_and_close_have_distinct_keys(self):
        fake = make_result("2026-10-30", run_id="pre")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-10-30")):
            with patch.object(scheduler, "run_recommendation", return_value=fake) as fn:
                scheduler.run_once(slot="premarket", now=datetime(2026, 11, 2, 8, 31))
                self.assertEqual(fn.call_count, 1)
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["jobs"]["2026-10-30:premarket"]["status"], "success")

        fake2 = make_result("2026-11-02", run_id="close")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(scheduler, "run_recommendation", return_value=fake2) as fn:
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
                self.assertEqual(fn.call_count, 1)
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        self.assertIn("2026-11-02:close", state["jobs"])
        self.assertEqual(state["jobs"]["2026-11-02:close"]["status"], "success")
        self.assertEqual(state["last_run_date"], "2026-11-02")

    def test_success_settles_tracking_for_the_banner(self):
        fake = make_result("2026-11-02", run_id="close")
        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")
        ):
            with patch.object(scheduler, "run_recommendation", return_value=fake):
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
        # 调度成功后必须顺手结算 tracking，否则横幅只能等用户打开跟踪页
        self.refresh_mock.assert_called_once_with()

    def test_stale_result_does_not_settle_tracking(self):
        stale = make_result(
            "2026-10-30", status="stale", message="数据未更新"
        )
        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")
        ):
            with patch.object(scheduler, "run_recommendation", return_value=stale):
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
        self.refresh_mock.assert_not_called()

    def test_stale_as_of_does_not_mark_success(self):
        fake = make_result(
            "2026-10-30",
            status="stale",
            message="数据未更新：data_as_of=2026-10-30 < expected=2026-11-02",
        )
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(scheduler, "run_recommendation", return_value=fake):
                result = scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
        self.assertIsNone(result)
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["jobs"]["2026-11-02:close"]["status"], "stale")
        self.assertNotIn("last_run_date", state)

    def test_success_is_skipped_on_second_call(self):
        fake = make_result("2026-11-02")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(scheduler, "run_recommendation", return_value=fake) as fn:
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 45))
                self.assertEqual(fn.call_count, 1)

    def test_stale_result_marks_stale_without_last_run(self):
        stale = make_result(
            status="stale", message="StaleDataError: 数据未更新"
        )
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(
                scheduler,
                "run_recommendation",
                return_value=stale,
            ):
                result = scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
        self.assertIsNone(result)
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["jobs"]["2026-11-02:close"]["status"], "stale")
        self.assertIn("数据未更新", state["last_error"])
        self.assertNotIn("last_run_date", state)

    def test_failed_result_increments_attempts(self):
        failed = make_result(status="failed", message="boom")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(
                scheduler,
                "run_recommendation",
                return_value=failed,
            ):
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 45))
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        job = state["jobs"]["2026-11-02:close"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["attempts"], 2)
        self.assertIn("RuntimeError: boom", state["last_error"])
        self.assertNotIn("last_run_date", state)

    def test_force_reruns_success(self):
        fake = make_result("2026-11-02")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(scheduler, "run_recommendation", return_value=fake) as fn:
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 45), force=True)
                self.assertEqual(fn.call_count, 2)

    def test_no_trade_date_returns_none(self):
        with patch.object(scheduler, "last_completed_trade_date", return_value=None):
            with patch.object(scheduler, "run_recommendation") as fn:
                result = scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
        self.assertIsNone(result)
        self.assertEqual(fn.call_count, 0)

    def test_start_background_skips_when_lock_held(self):
        with (
            patch.object(scheduler, "_THREAD", None),
            patch.object(scheduler, "_acquire_lock", return_value=None),
        ):
            self.assertIsNone(scheduler.start_background())

    def test_loop_exits_when_stop_is_set(self):
        stop = threading.Event()
        stop.set()
        with patch.object(scheduler, "_log") as log:
            scheduler.loop(stop)
        self.assertTrue(log.called)


class PitSnapshotTests(unittest.TestCase):
    def test_snapshot_maps(self):
        snap = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-10-30", "2026-10-30"]),
                "code": ["600519.SH", "000001.SZ"],
                "weight": [1.0, 2.0],
                "name": ["贵州茅台", "平安银行"],
                "industry": ["白酒", "银行"],
                "total_mv": [1e7, 2e7],
                "is_st": [0, 0],
            }
        )
        membership, sizes, industries = snapshot_maps(snap)
        self.assertEqual(membership[pd.Timestamp("2026-10-30")], {"600519.SH", "000001.SZ"})
        self.assertEqual(industries[pd.Timestamp("2026-10-30")]["600519.SH"], "白酒")
        self.assertEqual(sizes[pd.Timestamp("2026-10-30")]["000001.SZ"], 2e7)

    def test_sw_industry_asof(self):
        members = pd.DataFrame(
            {
                "code": ["000001.SZ", "000001.SZ", "000002.SZ"],
                "industry": ["银行", "综合", "房地产"],
                "in_date": pd.to_datetime(["2014-02-21", "2000-01-01", "1991-01-29"]),
                "out_date": pd.to_datetime([None, "2014-02-20", None]),
            }
        )
        with patch.object(pit, "sw_l1_members", return_value=members):
            got = pit.sw_industry_on("2015-06-01")
        self.assertEqual(got["000001.SZ"], "银行")
        self.assertEqual(got["000002.SZ"], "房地产")


class EventScoreTests(unittest.TestCase):
    def test_event_score_is_neutral_for_neutral_evidence(self):
        evidence = [
            {"kind": "新闻", "title": "公司发布日常公告", "date": "2026-11-01"}
            for _ in range(8)
        ]
        score, stats = score_events(evidence)
        self.assertAlmostEqual(score, 50.0, delta=2.0)
        self.assertEqual(stats["n"], 8)

    def test_event_score_direction(self):
        pos = [{"kind": "公告", "title": "预增 回购 中标 订单", "date": "2026-11-01"}]
        neg = [{"kind": "公告", "title": "亏损 减持 诉讼 处罚", "date": "2026-11-01"}]
        self.assertGreater(score_events(pos)[0], 50)
        self.assertLess(score_events(neg)[0], 50)


class JsonContractTests(unittest.TestCase):
    def test_clean_json_is_strict(self):
        payload = _clean_json(
            {
                "nan": np.nan,
                "inf": [np.inf, -np.inf],
                "nat": pd.NaT,
                "ts": pd.Timestamp("2026-11-02 10:00:00"),
                "arr": np.array([1.0, np.nan]),
            }
        )
        raw = json.dumps(payload, allow_nan=False)
        parsed = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        self.assertIsNone(parsed["nan"])
        self.assertEqual(parsed["arr"], [1.0, None])


class QuantitativeRuleTests(unittest.TestCase):
    def test_st_limit_is_five_percent(self):
        self.assertEqual(limit_pct("600000.SH", "*ST某某"), 0.05)
        self.assertEqual(limit_pct("300001.SZ"), 0.20)

    def test_ipo_first_day_has_no_limit(self):
        row = pd.Series({"trade_date": pd.Timestamp("2026-10-30"), "pre_close": 10.0,
                         "high": 11.0, "low": 11.0, "raw_pre_close": 10.0,
                         "raw_high": 11.0, "raw_low": 11.0})
        self.assertTrue(_one_price_limit(row, 0.10, "buy"))
        self.assertFalse(_one_price_limit(row, 0.10, "buy", list_date="2026-10-30"))

    def test_walk_forward_state_carries_position(self):
        def bars(start, n):
            dates = pd.bdate_range(start, periods=n)
            return pd.DataFrame(
                {
                    "trade_date": dates,
                    "open": [10.0 + i * 0.1 for i in range(n)],
                    "high": [10.2 + i * 0.1 for i in range(n)],
                    "low": [9.9 + i * 0.1 for i in range(n)],
                    "close": [10.1 + i * 0.1 for i in range(n)],
                    "vol": [1000] * n,
                }
            )

        first = bars("2026-01-05", 3)
        r1 = run_backtest(first, pd.Series([1, 1, 1]), "600000.SH", prev_signal=1)
        self.assertGreater(r1.state["shares"], 0)
        second = bars("2026-01-08", 3)
        r2 = run_backtest(
            second, pd.Series([1, 1, 1]), "600000.SH",
            initial_state=r1.state, prev_signal=1,
        )
        self.assertEqual(r2.state["shares"], r1.state["shares"])
        self.assertEqual(r2.state["entry_price"], r1.state["entry_price"])

    def test_industry_cap_is_preserved(self):
        rows = [{"code": f"60000{i}.SH", "industry": "银行", "composite": 90 - i} for i in range(6)]
        picked = _pick_diverse(rows, 5, max_per_industry=2)
        self.assertEqual(len(picked), 2)
        self.assertTrue(all(r["industry"] == "银行" for r in picked))

    def test_missing_vendor_column_returns_series(self):
        df = pd.DataFrame({"title": ["a", "b"]})
        series = _col(df, "url")
        self.assertEqual(series.tolist(), ["", ""])
        self.assertEqual(_col(df, "title").tolist(), ["a", "b"])

    def test_forward_return_requires_exact_entry_bar(self):
        df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2026-10-29", "2026-10-30", "2026-11-02"]),
                "open": [10.0, 11.0, 12.0],
                "close": [10.5, 11.5, 12.5],
            }
        )
        self.assertIsNone(_forward_return(df, pd.Timestamp("2026-10-31"), 1, entry="open"))
        self.assertAlmostEqual(_forward_return(df, pd.Timestamp("2026-10-30"), 1, entry="open"), 12.5 / 11.0 - 1)


class StatsTests(unittest.TestCase):
    def test_newey_west_uses_horizon_lag(self):
        values = pd.Series(np.linspace(-0.01, 0.02, 40)).to_numpy()
        result = newey_west(values, horizon=20)
        self.assertEqual(result.lag, 19)
        self.assertEqual(result.effective_n, 2.0)
        self.assertFalse(result.usable)

    def test_bh_and_effective_n(self):
        self.assertEqual(effective_n(40, 20), 2.0)
        adjusted = benjamini_hochberg([0.01, 0.04, None])
        self.assertIsNone(adjusted[2])
        self.assertAlmostEqual(adjusted[1], 0.04)

    def test_t_distribution_p(self):
        self.assertIsNotNone(student_t_p(2.0, 10))
        self.assertLess(student_t_p(2.0, 10), 0.1)


if __name__ == "__main__":
    unittest.main()
