from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import astock.scheduler as scheduler
from astock.backtest import limit_pct
from astock.events import _col
from astock.recommend import _clean_json, _pick_diverse
from astock.stats import benjamini_hochberg, effective_n, newey_west, student_t_p
from astock.track import _forward_return


@dataclass
class FakeResult:
    data_as_of: str
    as_of: str
    status: str = "ok"
    csv_path: str = "/tmp/a.csv"
    json_path: str = "/tmp/a.json"
    run_id: str = "run_test"


class SchedulerIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(scheduler, "STATE_PATH", root / "state.json"),
            patch.object(scheduler, "LOG_PATH", root / "scheduler.log"),
            patch.object(scheduler, "LOCK_PATH", root / "scheduler.lock"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_premarket_and_close_have_distinct_keys(self):
        fake = FakeResult(data_as_of="2026-10-30", as_of="2026-10-30", run_id="pre")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-10-30")):
            with patch.object(scheduler, "run_daily_recommendation", return_value=fake) as fn:
                scheduler.run_once(slot="premarket", now=datetime(2026, 11, 2, 8, 31))
                self.assertEqual(fn.call_count, 1)
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["jobs"]["2026-10-30:premarket"]["status"], "success")

        fake2 = FakeResult(data_as_of="2026-11-02", as_of="2026-11-02", run_id="close")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(scheduler, "run_daily_recommendation", return_value=fake2) as fn:
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
                self.assertEqual(fn.call_count, 1)
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        self.assertIn("2026-11-02:close", state["jobs"])
        self.assertEqual(state["jobs"]["2026-11-02:close"]["status"], "success")
        self.assertEqual(state["last_run_date"], "2026-11-02")

    def test_stale_as_of_does_not_mark_success(self):
        fake = FakeResult(data_as_of="2026-10-30", as_of="2026-10-30")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(scheduler, "run_daily_recommendation", return_value=fake):
                result = scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
        self.assertIsNone(result)
        state = json.loads(scheduler.STATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["jobs"]["2026-11-02:close"]["status"], "stale")
        self.assertNotIn("last_run_date", state)

    def test_success_is_skipped_on_second_call(self):
        fake = FakeResult(data_as_of="2026-11-02", as_of="2026-11-02")
        with patch.object(scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-02")):
            with patch.object(scheduler, "run_daily_recommendation", return_value=fake) as fn:
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 40))
                scheduler.run_once(slot="close", now=datetime(2026, 11, 2, 16, 45))
                self.assertEqual(fn.call_count, 1)


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
