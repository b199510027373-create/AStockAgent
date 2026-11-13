from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import astock.data as data
import astock.events as events
import astock.pit as pit


class FakeTushare:
    def __init__(self, daily: pd.DataFrame, factors: pd.DataFrame | None = None):
        self.daily = daily
        self.factors = factors
        self.calls: list[str] = []

    def query(self, api_name, **kwargs):
        self.calls.append(api_name)
        if api_name == "daily":
            return self.daily.copy()
        if api_name == "adj_factor" and self.factors is not None:
            return self.factors.copy()
        raise AssertionError(f"unexpected api: {api_name}")


class FakeMinuteClient:
    def __init__(self, minute: pd.DataFrame, factors: pd.DataFrame | None = None):
        self.minute = minute
        self.factors = factors
        self.calls: list[str] = []

    def query(self, api_name, **kwargs):
        self.calls.append(api_name)
        if api_name == "rt_min":
            return self.minute.copy()
        if api_name == "adj_factor" and self.factors is not None:
            return self.factors.copy()
        raise AssertionError(f"unexpected api: {api_name}")


class DataCacheAndQfqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = pd.DataFrame(
            {
                "ts_code": ["600519.SH"] * 3,
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
                "open": [10.0, 11.0, 12.0],
                "high": [10.5, 11.5, 12.5],
                "low": [9.8, 10.8, 11.8],
                "close": [10.2, 11.2, 12.2],
                "pre_close": [10.0, 10.2, 11.2],
                "vol": [1000, 1100, 1200],
                "amount": [10000, 11000, 12000],
            }
        )
        self.factors = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
                "adj_factor": [1.0, 2.0, 4.0],
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_daily_qfq_keeps_raw_prices(self):
        fake = FakeTushare(self.raw, self.factors)
        out = data._fetch_daily(fake, "600519.SH", "20250102", "20250106", "qfq")
        self.assertIn("raw_close", out.columns)
        self.assertAlmostEqual(float(out["close"].iloc[-1]), 12.2)
        self.assertAlmostEqual(float(out["raw_close"].iloc[0]), 10.2)
        self.assertAlmostEqual(float(out["raw_close"].iloc[-1]), 12.2)
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.2 * 0.25)
        self.assertIn("daily", fake.calls)
        self.assertIn("adj_factor", fake.calls)

    def test_load_daily_disk_cache_avoids_second_fetch(self):
        fake = FakeTushare(self.raw, self.factors)
        with (
            patch.object(data, "DAILY_DIR", self.root),
            patch.object(data, "_DAILY_MEM", {}),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")),
            patch.object(data, "last_open_date", side_effect=lambda x: pd.Timestamp(x)),
        ):
            first = data.load_daily(
                "600519.SH", start="2025-01-02", end="2025-01-06", adj="qfq", client=fake
            )
            self.assertEqual(len(first), 3)
            data._DAILY_MEM.clear()
            fake2 = FakeTushare(self.raw, self.factors)
            second = data.load_daily(
                "600519.SH", start="2025-01-02", end="2025-01-06", adj="qfq", client=fake2
            )
            self.assertEqual(len(second), 3)
            self.assertEqual(fake2.calls, [])

    def test_normalize_minute_units_and_missing_time(self):
        raw = pd.DataFrame(
            {
                "trade_time": ["2025-01-02 09:35:00"],
                "open": ["10"],
                "high": ["10.5"],
                "low": ["9.8"],
                "close": ["10.2"],
                "vol": ["10000"],
                "amount": ["500000"],
            }
        )
        out = data._normalize_minute(raw)
        self.assertEqual(float(out["vol"].iloc[0]), 100.0)
        self.assertEqual(float(out["amount"].iloc[0]), 500.0)
        self.assertEqual(out["trade_date"].iloc[0], pd.Timestamp("2025-01-02"))
        with self.assertRaises(data.TushareError):
            data._normalize_minute(pd.DataFrame({"close": [1.0]}))

    def test_load_adj_factors_cache_and_empty_error(self):
        fake = FakeTushare(self.raw, self.factors)
        with (
            patch.object(data, "_ADJ_MEM", {}),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")),
        ):
            first = data.load_adj_factors("600519.SH", client=fake)
            self.assertEqual(len(first), 3)
            second = data.load_adj_factors("600519.SH", client=fake)
            self.assertEqual(len(second), 3)
        empty = FakeTushare(self.raw, pd.DataFrame())
        with (
            patch.object(data, "_ADJ_MEM", {}),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")),
            self.assertRaises(data.TushareError),
        ):
            data.load_adj_factors("600519.SH", client=empty)

    def test_load_minute_rt_qfq_cache(self):
        minute = pd.DataFrame(
            {
                "time": ["2025-01-02 09:35:00", "2025-01-02 09:40:00"],
                "open": [10.0, 10.2],
                "high": [10.1, 10.3],
                "low": [9.9, 10.1],
                "close": [10.0, 10.2],
                "vol": [1000, 2000],
                "amount": [10000, 20000],
            }
        )
        fake = FakeMinuteClient(minute, self.factors)
        with (
            patch.object(data, "_MINUTE_MEM", {}),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-06")),
        ):
            out = data.load_minute("600519.SH", freq="5min", days=1, adj="qfq", client=fake)
            self.assertEqual(len(out), 2)
            self.assertEqual(float(out["vol"].iloc[0]), 10.0)
            self.assertAlmostEqual(float(out["close"].iloc[-1]), 10.2 * 0.25)
            fake2 = FakeMinuteClient(minute, self.factors)
            out2 = data.load_minute("600519.SH", freq="5min", days=1, adj="qfq", client=fake2)
            self.assertEqual(len(out2), 2)
            self.assertEqual(fake2.calls, [])

    def test_load_minute_history_uses_calendar(self):
        minute = pd.DataFrame(
            {
                "time": ["2025-01-02 09:35:00", "2025-01-03 09:35:00"],
                "open": [10.0, 11.0],
                "high": [10.1, 11.1],
                "low": [9.9, 10.9],
                "close": [10.0, 11.0],
                "vol": [1000, 2000],
                "amount": [10000, 20000],
            }
        )
        cal = pd.DataFrame(
            {
                "cal_date": pd.to_datetime(["2025-01-02", "2025-01-03"]),
                "is_open": [1, 1],
            }
        )

        class HistClient(FakeTushare):
            def query(self, api_name, **kwargs):
                if api_name == "stk_mins":
                    return minute.copy()
                if api_name == "rt_min":
                    return pd.DataFrame()
                return super().query(api_name, **kwargs)

        fake = HistClient(minute, self.factors)
        with (
            patch.object(data, "_MINUTE_MEM", {}),
            patch.object(data, "load_trade_cal", return_value=cal),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-03")),
        ):
            out = data.load_minute("600519.SH", freq="5min", days=2, adj=None, client=fake)
        self.assertEqual(len(out), 2)
        self.assertEqual(set(pd.to_datetime(out["trade_date"]).dt.date), {pd.Timestamp("2025-01-02").date(), pd.Timestamp("2025-01-03").date()})


class PitSnapshotBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.master = pd.DataFrame(
            {
                "code": ["600519.SH", "000001.SZ", "999999.SZ"],
                "name": ["贵州茅台", "平安银行", "未来上市"],
                "industry": ["白酒", "银行", "其他"],
                "list_date": pd.to_datetime(["2001-01-01", "1991-01-01", "2024-01-01"]),
                "delist_date": pd.to_datetime([None, None, None]),
            }
        )
        self.members = pd.DataFrame(
            {
                "date": pd.to_datetime(["2022-01-10", "2022-01-10", "2022-01-10"]),
                "code": ["600519.SH", "000001.SZ", "999999.SZ"],
                "weight": [1.0, 2.0, 3.0],
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_filters_future_listing_and_fills_industry(self):
        with (
            patch.object(pit, "PIT_DIR", self.root),
            patch.object(pit, "index_members", return_value=self.members),
            patch.object(pit, "security_master", return_value=self.master),
            patch.object(pit, "_caps_on", return_value={"600519.SH": 100.0, "000001.SZ": 200.0}),
            patch.object(pit, "sw_industry_on", return_value={"600519.SH": "白酒", "000001.SZ": "银行"}),
            patch.object(
                pit,
                "_bak_industry_on",
                return_value={"600519.SH": ("贵州茅台", "旧行业"), "000001.SZ": ("平安银行", "旧行业")},
            ),
        ):
            snap = pit.build_index_snapshot("000300.SH", start="2022-01-01", end="2022-01-31")
        self.assertEqual(len(snap), 2)
        self.assertEqual(set(snap["code"]), {"600519.SH", "000001.SZ"})
        self.assertEqual(snap["industry_version"].iloc[0], "SW2021")
        self.assertIn("白酒", set(snap["industry"]))
        self.assertEqual(float(snap["total_mv"].max()), 200.0)

    def test_snapshot_inserts_switch_date(self):
        members = pd.DataFrame(
            {
                "date": pd.to_datetime(["2021-12-01", "2021-12-31"]),
                "code": ["600519.SH", "600519.SH"],
                "weight": [1.0, 1.0],
            }
        )
        master = self.master[self.master["code"] == "600519.SH"].copy()
        with (
            patch.object(pit, "PIT_DIR", self.root),
            patch.object(pit, "index_members", return_value=members),
            patch.object(pit, "security_master", return_value=master),
            patch.object(pit, "_caps_on", return_value={"600519.SH": 100.0}),
            patch.object(pit, "sw_industry_on", return_value={"600519.SH": "白酒"}),
            patch.object(pit, "_bak_industry_on", return_value={"600519.SH": ("贵州茅台", "旧行业")}),
        ):
            snap = pit.build_index_snapshot("000300.SH", start="2021-12-01", end="2022-01-31")
        dates = set(pd.to_datetime(snap["date"]).dt.normalize())
        self.assertIn(pd.Timestamp("2021-12-13"), dates)
        switch = snap[pd.to_datetime(snap["date"]).dt.normalize() == pd.Timestamp("2021-12-13")]
        self.assertEqual(switch["industry_version"].iloc[0], "SW2021")
        self.assertEqual(float(switch["weight"].iloc[0]), 1.0)


class FakeEventClient:
    def query(self, api_name, **kwargs):
        if api_name == "news":
            return pd.DataFrame(
                {
                    "datetime": ["2026-11-01 10:00:00"],
                    "title": [f"<b>公司中标</b> 大单 {kwargs.get('src', 'sina')}"],
                    "content": ["内容"],
                    "src": [kwargs.get("src", "sina")],
                }
            )
        if api_name == "anns_d":
            return pd.DataFrame(
                {
                    "ann_date": ["20261101"],
                    "title": ["<p>回购公告</p>"],
                    "url": ["http://example.com/a"],
                    "name": ["贵州茅台"],
                }
            )
        if api_name == "npr":
            return pd.DataFrame(
                {
                    "pubtime": ["2026-11-01"],
                    "title": ["政策支持消费"],
                    "pcode": ["001"],
                    "puborg": ["国务院"],
                    "ptype": ["消费"],
                }
            )
        raise AssertionError(f"unexpected api: {api_name}")

    def query_all(self, api_name, **kwargs):
        if api_name == "report_rc":
            return pd.DataFrame(
                {
                    "report_date": ["20261101"],
                    "report_title": ["买入评级"],
                    "ts_code": ["600519.SH"],
                    "name": ["贵州茅台"],
                    "org_name": ["某券商"],
                    "rating": ["买入"],
                }
            )
        if api_name == "research_report":
            return pd.DataFrame(
                {
                    "trade_date": ["20261101"],
                    "title": ["深度报告"],
                    "ts_code": ["600519.SH"],
                    "name": ["贵州茅台"],
                    "inst_csname": ["某研究所"],
                    "ind_name": ["白酒"],
                    "url": ["http://example.com/r"],
                }
            )
        raise AssertionError(f"unexpected api: {api_name}")


class EventFetchParsingTests(unittest.TestCase):
    def test_fetch_news_parses_and_dedupes(self):
        fake = FakeEventClient()
        with (
            patch.object(events, "get_client", return_value=fake),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, df: df),
        ):
            out = events.fetch_news(days=2, refresh=True)
        self.assertEqual(len(out), 3)
        self.assertTrue(out["title"].str.contains("公司中标").all())
        self.assertTrue((out["kind"] == "news").all())
        self.assertEqual(out["source"].nunique(), 3)

    def test_fetch_announcements_parses_date_and_kind(self):
        fake = FakeEventClient()
        with (
            patch.object(events, "get_client", return_value=fake),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, df: df),
        ):
            out = events.fetch_announcements(["600519.SH"], days=30)
        self.assertEqual(len(out), 1)
        self.assertEqual(out["kind"].iloc[0], "announcement")
        self.assertEqual(out["name"].iloc[0], "贵州茅台")
        self.assertIn("回购公告", out["title"].iloc[0])

    def test_fetch_reports_parses_both_sources(self):
        fake = FakeEventClient()
        with (
            patch.object(events, "get_client", return_value=fake),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, df: df),
        ):
            out = events.fetch_reports(days=7)
        self.assertEqual(len(out), 2)
        self.assertEqual(set(out["kind"]), {"rating", "research"})
        self.assertTrue(out["rating"].astype(str).str.contains("买入").any())

    def test_fetch_policy_parses_source_and_kind(self):
        fake = FakeEventClient()
        with (
            patch.object(events, "get_client", return_value=fake),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, df: df),
        ):
            out = events.fetch_policy(days=7)
        self.assertEqual(len(out), 1)
        self.assertEqual(out["kind"].iloc[0], "policy")
        self.assertEqual(out["source"].iloc[0], "国务院")


class AdjCaliberTests(unittest.TestCase):
    """ADR-008 口径：qfq/hfq/none 必须是三种不同算法（评审 P0 回归）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = pd.DataFrame(
            {
                "ts_code": ["600519.SH"] * 3,
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
                "open": [10.0, 11.0, 12.0],
                "high": [10.5, 11.5, 12.5],
                "low": [9.8, 10.8, 11.8],
                "close": [10.2, 11.2, 12.2],
                "pre_close": [10.0, 10.2, 11.2],
                "vol": [1000.0, 1100.0, 1200.0],
                "amount": [10000.0, 11000.0, 12000.0],
            }
        )
        self.factors = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
                "adj_factor": [1.0, 2.0, 4.0],
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_fetch_daily_none_keeps_raw_prices(self):
        fake = FakeTushare(self.raw, self.factors)
        out = data._fetch_daily(fake, "600519.SH", "20250102", "20250106", None)
        self.assertNotIn("raw_close", out.columns)
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.2)
        self.assertAlmostEqual(float(out["close"].iloc[-1]), 12.2)
        # none 口径不需要复权因子
        self.assertEqual(fake.calls, ["daily"])

    def test_fetch_daily_hfq_scales_against_original_factor(self):
        fake = FakeTushare(self.raw, self.factors)
        out = data._fetch_daily(fake, "600519.SH", "20250102", "20250106", "hfq")
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.2 * 1.0)
        self.assertAlmostEqual(float(out["close"].iloc[-1]), 12.2 * 4.0)
        self.assertAlmostEqual(float(out["raw_close"].iloc[-1]), 12.2)

    def test_load_daily_rejects_unknown_adj(self):
        fake = FakeTushare(self.raw, self.factors)
        with self.assertRaises(ValueError):
            data.load_daily("600519.SH", adj="raw", client=fake)

    def test_apply_minute_adj_none_is_identity(self):
        minute = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2025-01-06"]),
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "pre_close": [10.0],
            }
        )
        out = data._apply_minute_adj(minute, self.factors, None)
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.2)
        hfq = data._apply_minute_adj(minute, self.factors, "hfq")
        self.assertAlmostEqual(float(hfq["close"].iloc[0]), 10.2 * 4.0)


class LatestBarTimeTests(unittest.TestCase):
    """§6.2 唯一时点生产者：取时间最大值、两段交易时段、不依赖行序（评审 P2）。"""

    def test_daily_uses_max_date_not_row_order(self):
        df = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2024-01-04", "2024-01-02", "2024-01-03"]),
                "close": [1.0, 2.0, 3.0],
            }
        )
        as_of, bar_time = data.latest_bar_time(df)
        self.assertEqual(as_of, "2024-01-04")
        self.assertEqual(bar_time, "2024-01-04 15:00")

    def test_minute_prefers_trade_time_and_accepts_session_edges(self):
        for stamp in ("2024-01-02 09:30", "2024-01-02 11:30", "2024-01-02 13:00", "2024-01-02 15:00"):
            df = pd.DataFrame({"trade_time": pd.to_datetime([stamp])})
            as_of, bar_time = data.latest_bar_time(df, freq="5min")
            self.assertEqual(as_of, "2024-01-02")
            self.assertEqual(bar_time, stamp)

    def test_minute_rejects_lunch_break_and_off_hours_in_strict_mode(self):
        for stamp in ("2024-01-02 11:31", "2024-01-02 12:30", "2024-01-02 09:29", "2024-01-02 15:01"):
            df = pd.DataFrame({"trade_time": pd.to_datetime([stamp])})
            with self.assertRaises(ValueError):
                data.latest_bar_time(df, freq="5min")
            with self.assertRaises(ValueError):
                data.latest_bar_time(df, freq="5min", strict=True)

    def test_minute_skips_out_of_session_bars_by_default(self):
        # 集合竞价 09:25 bar 真实存在；默认跳过，取时段内最后一根（决策记录 Q7）。
        df = pd.DataFrame(
            {
                "trade_time": pd.to_datetime(
                    ["2024-01-02 09:25", "2024-01-02 09:35", "2024-01-02 15:01"]
                )
            }
        )
        notes: list[str] = []
        as_of, bar_time = data.latest_bar_time(df, freq="5min", notes=notes)
        self.assertEqual(as_of, "2024-01-02")
        self.assertEqual(bar_time, "2024-01-02 09:35")
        self.assertEqual(len(notes), 1)
        self.assertIn("2 根", notes[0])

        with self.assertRaises(ValueError):
            data.latest_bar_time(df, freq="5min", strict=True)

    def test_minute_raises_when_every_bar_is_out_of_session(self):
        df = pd.DataFrame({"trade_time": pd.to_datetime(["2024-01-02 09:25"])})
        with self.assertRaises(ValueError):
            data.latest_bar_time(df, freq="5min")

    def test_empty_frame_raises(self):
        with self.assertRaises(ValueError):
            data.latest_bar_time(pd.DataFrame())

    def test_unknown_freq_raises(self):
        df = pd.DataFrame({"trade_date": pd.to_datetime(["2024-01-02"])})
        with self.assertRaises(ValueError):
            data.latest_bar_time(df, freq="1m")


if __name__ == "__main__":
    unittest.main()
