from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import astock.ledger as ledger
import astock.recommend as recommend
import astock.track as track
import astock.use_cases as use_cases
from astock.data import enrich


def synthetic_daily(n: int = 140, start: str = "2025-01-02") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=n)
    rng = np.random.default_rng(7)
    close = pd.Series(np.linspace(10.0, 15.0, n) + rng.normal(0, 0.05, n))
    df = pd.DataFrame(
        {
            "trade_date": dates,
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "pre_close": close.shift(1).fillna(close.iloc[0]),
            "vol": rng.integers(1000, 5000, n),
            "amount": rng.integers(10000, 50000, n),
        }
    )
    return enrich(df)


class RecommendationCorePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rec_dir = self.root / "recommendations"
        self.rec_dir.mkdir()
        self.patches = [
            patch.object(recommend, "REC_DIR", self.rec_dir),
            patch.object(ledger, "DB_PATH", self.root / "ledger.db"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_offline_recommendation_writes_json_and_ledger(self):
        daily = synthetic_daily(140, "2025-01-02")
        last = daily["trade_date"].max()
        basic = pd.DataFrame(
            [{"ts_code": "600519.SH", "name": "贵州茅台", "industry": "白酒", "aliases": "茅台"}]
        )
        cal = pd.DataFrame(
            {
                "cal_date": pd.bdate_range("2025-01-01", "2025-12-31"),
                "is_open": 1,
            }
        )
        with (
            patch.object(recommend, "parse_universe", return_value=["600519.SH"]),
            patch.object(recommend, "stock_basic", return_value=basic),
            patch.object(recommend, "load_many", return_value={"600519.SH": daily}),
            patch.object(recommend, "load_index_daily", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_announcements", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_reports", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_news", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_major_news", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_policy", return_value=pd.DataFrame()),
            patch.object(recommend, "evidence_for_stock", return_value=[]),
            patch.object(recommend, "last_completed_trade_date", return_value=last),
            patch.object(recommend, "next_open_date", return_value=pd.Timestamp("2025-07-11")),
            patch.object(recommend, "load_trade_cal", return_value=cal),
        ):
            result = use_cases.run_recommendation(
                mode="generate",
                universe_text="600519.SH",
                top_n=1,
                candidate_n=1,
                use_llm=False,
            )
        self.assertEqual(result.status, "success")
        table = result.payload["table"]
        self.assertEqual(len(table), 1)
        self.assertTrue(result.run_id.startswith("rec_"))
        self.assertEqual(result.payload["data_as_of"], last.strftime("%Y-%m-%d"))
        payload = json.loads(
            Path(result.payload["json_path"]).read_text(encoding="utf-8"),
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
        self.assertEqual(payload["run_id"], result.run_id)
        self.assertEqual(payload["schema_version"], 2)
        saved = ledger.get_run(result.run_id)
        self.assertIsNotNone(saved)
        self.assertEqual(len(saved["items"]), 1)

    def test_ledger_batches_shape(self):
        payload = {
            "run_id": "r1",
            "data_as_of": "2026-10-30",
            "tradable_from": "2026-11-02",
            "generated_at": "2026-10-30T18:00:00",
            "source": "live",
            "table": [{"排名": 1, "代码": "600519.SH", "名称": "贵州茅台", "综合分": 80}],
            "evidence": {},
        }
        ledger.save_run(payload)
        batches = ledger.recommendation_batches()
        self.assertEqual(batches[0]["run_id"], "r1")
        self.assertEqual(batches[0]["rows"][0]["代码"], "600519.SH")

    def test_tracking_rows_joins_rank_and_keeps_live_only(self):
        payload = {
            "run_id": "r1",
            "data_as_of": "2026-10-30",
            "tradable_from": "2026-11-02",
            "generated_at": "2026-10-30T18:00:00",
            "source": "live",
            "table": [
                {"排名": 1, "代码": "600519.SH", "名称": "贵州茅台", "综合分": 80},
                {"排名": 2, "代码": "000001.SZ", "名称": "平安银行", "综合分": 70},
            ],
            "evidence": {},
        }
        ledger.save_run(payload)
        ledger.save_tracking(
            "r1",
            [
                {
                    "code": "600519.SH",
                    "horizon": 5,
                    "entry_date": "2026-11-02",
                    "exit_date": "2026-11-06",
                    "ret": 0.03,
                    "excess": 0.01,
                },
                {
                    "code": "000001.SZ",
                    "horizon": 5,
                    "entry_date": "2026-11-02",
                    "exit_date": "2026-11-06",
                    "ret": -0.01,
                    "excess": -0.03,
                },
                {
                    "code": "600519.SH",
                    "horizon": 20,
                    "entry_date": "2026-11-02",
                    "exit_date": "2026-11-27",
                    "ret": 0.05,
                    "excess": 0.02,
                },
            ],
        )
        backtest = dict(payload, run_id="r2", source="backtest", generated_at="2026-10-31T18:00:00")
        ledger.save_run(backtest)
        ledger.save_tracking(
            "r2",
            [
                {
                    "code": "600519.SH",
                    "horizon": 5,
                    "entry_date": "2026-11-02",
                    "exit_date": "2026-11-06",
                    "ret": 0.9,
                    "excess": 0.8,
                }
            ],
        )

        rows = ledger.tracking_rows(5)

        self.assertEqual([row["run_id"] for row in rows], ["r1", "r1"])
        self.assertEqual({row["rank"] for row in rows}, {1, 2})
        self.assertTrue(all(row["data_as_of"] == "2026-10-30" for row in rows))
        self.assertNotIn(0.8, [row["excess"] for row in rows])


class TrackerCorePathTests(unittest.TestCase):
    def test_tracker_end_to_end(self):
        daily = synthetic_daily(40, "2025-01-02")
        batch = {
            "path": None,
            "run_id": "r1",
            "as_of": pd.Timestamp("2025-01-10"),
            "tradable_from": pd.Timestamp("2025-01-13"),
            "generated_at": "2025-01-10T18:00:00",
            "source": "live",
            "rows": [
                {"排名": 1, "代码": "600519.SH", "名称": "贵州茅台", "综合分": 80, "建议": "技术面强"},
                {"排名": 2, "代码": "000001.SZ", "名称": "平安银行", "综合分": 70, "建议": "技术面中性"},
            ],
        }
        with (
            patch.object(track, "_load_batches", return_value=[batch]),
            patch.object(track, "load_daily", return_value=daily),
            patch.object(track, "load_index_daily", return_value=daily),
            patch.object(track.ledger, "save_tracking") if hasattr(track, "ledger") else patch("astock.ledger.save_tracking"),
        ):
            detail, summary, status = track.compute_tracking()
        self.assertEqual(len(detail), 2)
        self.assertIn("推荐日", detail.columns)
        self.assertIn("T+1", detail.columns)
        self.assertIn("全部", set(summary["分组"]))
        self.assertIn("跟踪口径", status)


if __name__ == "__main__":
    unittest.main()
