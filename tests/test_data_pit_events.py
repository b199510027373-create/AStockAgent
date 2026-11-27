from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from astock import data, events, pit
from astock.contracts import ContractError


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
            self.assertRaises(data.DataContractError),
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


class FakeReportClient:
    """report_rc / research_report 混合返回，可注入空 ts_code 或缺列。"""

    def __init__(self, codes: list[str | None], *, include_ts_code: bool = True):
        self.codes = codes
        self.include_ts_code = include_ts_code

    def query_all(self, api_name, **kwargs):
        n = len(self.codes)
        if api_name == "report_rc":
            data = {
                "report_date": ["20261101"] * n,
                "report_title": [f"个股评级{i}" for i in range(n)],
                "name": ["候选股"] * n,
                "org_name": ["券商"] * n,
                "rating": ["买入"] * n,
            }
            if self.include_ts_code:
                data["ts_code"] = self.codes
            return pd.DataFrame(data)
        if api_name == "research_report":
            return pd.DataFrame(
                {
                    "trade_date": ["20261101"] * n,
                    "title": [f"深度报告{i}" for i in range(n)],
                    "ts_code": self.codes,
                    "name": ["候选股"] * n,
                    "inst_csname": ["研究所"] * n,
                    "ind_name": ["行业"] * n,
                    "url": [""] * n,
                }
            )
        raise AssertionError(f"unexpected api: {api_name}")


class ReportEventContractTests(unittest.TestCase):
    def _fetch(self, client: FakeReportClient) -> pd.DataFrame:
        with (
            patch.object(events, "get_client", return_value=client),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, df: df),
        ):
            return events.fetch_reports(days=7)

    def test_fetch_reports_drops_blank_ts_code_rows(self):
        out = self._fetch(FakeReportClient(["600519.SH", ""]))
        self.assertEqual(len(out), 2)
        self.assertTrue(out["code"].str.strip().ne("").all())
        self.assertEqual(set(out["kind"]), {"rating", "research"})

    def test_fetch_reports_returns_empty_when_all_codes_blank(self):
        out = self._fetch(FakeReportClient(["", None]))
        self.assertTrue(out.empty)

    def test_fetch_reports_missing_ts_code_column_still_raises(self):
        with self.assertRaises(ContractError):
            self._fetch(FakeReportClient(["600519.SH"], include_ts_code=False))


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

    def test_fetch_daily_hfq_scales_against_earliest_factor(self):
        fake = FakeTushare(self.raw, self.factors)
        out = data._fetch_daily(fake, "600519.SH", "20250102", "20250106", "hfq")
        # 最早可用交易日（2025-01-02，因子 1.0）是基准，不是"原始因子 1.0"。
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.2 * 1.0)
        self.assertAlmostEqual(float(out["close"].iloc[-1]), 12.2 * 4.0)
        self.assertAlmostEqual(float(out["raw_close"].iloc[-1]), 12.2)
        self.assertEqual(out.attrs["adjustment"]["adj"], "hfq")
        self.assertEqual(out.attrs["adjustment"]["adj_base"], "2025-01-02")
        self.assertAlmostEqual(out.attrs["adjustment"]["base_factor"], 1.0)

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
    """§6.2 唯一时点生产者：取时间最大值、Q7 接受集合竞价/盘后 bar、不依赖行序。"""

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

    def test_minute_non_strict_accepts_lunch_break_and_off_hours(self):
        # Q7：渲染层不得因 bar 时段失败；越界只标注 session 并给 note。
        for stamp, expected in (
            ("2024-01-02 11:31", "closed"),
            ("2024-01-02 12:30", "closed"),
            ("2024-01-02 09:29", "pre"),
            ("2024-01-02 15:01", "post"),
        ):
            df = pd.DataFrame({"trade_time": pd.to_datetime([stamp])})
            notes: list[str] = []
            as_of, bar_time = data.latest_bar_time(df, freq="5min", notes=notes)
            self.assertEqual(as_of, "2024-01-02")
            self.assertEqual(bar_time, stamp)
            self.assertEqual(len(notes), 1)
            self.assertIn(f"session={expected}", notes[0])
            with self.assertRaises(ValueError):
                data.latest_bar_time(df, freq="5min", strict=True)

    def test_minute_accepts_auction_and_post_bars_as_latest(self):
        # 2026-11-14 Q7：09:25 集合竞价 / 15:0x 盘后固定价都是合法行情，取时间最大值，不回退前一日。
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
        self.assertEqual(bar_time, "2024-01-02 15:01")
        self.assertEqual(len(notes), 1)
        self.assertIn("盘后固定价", notes[0])

        with self.assertRaises(ValueError):
            data.latest_bar_time(df, freq="5min", strict=True)

    def test_minute_auction_only_frame_is_accepted(self):
        df = pd.DataFrame({"trade_time": pd.to_datetime(["2024-01-02 09:25"])})
        notes: list[str] = []
        as_of, bar_time = data.latest_bar_time(df, freq="5min", notes=notes)
        self.assertEqual((as_of, bar_time), ("2024-01-02", "2024-01-02 09:25"))
        self.assertIn("集合竞价", notes[0])
        # 不回退前一日：同一天的下一根 bar 不存在也不会拿别的日期顶替。
        self.assertEqual(bar_time[:10], as_of)

    def test_classify_bar_time_boundaries_and_timezone(self):
        cases = [
            ("2024-01-02 09:14", "closed"),
            ("2024-01-02 09:15", "pre"),
            ("2024-01-02 09:25", "pre"),
            ("2024-01-02 09:29", "pre"),
            ("2024-01-02 09:30", "continuous"),
            ("2024-01-02 11:30", "continuous"),
            ("2024-01-02 11:31", "closed"),
            ("2024-01-02 13:00", "continuous"),
            ("2024-01-02 15:00", "continuous"),
            ("2024-01-02 15:05", "post"),
        ]
        for stamp, expected in cases:
            self.assertEqual(data.classify_bar_time(pd.Timestamp(stamp), "1min"), expected, stamp)
        self.assertEqual(data.classify_bar_time(pd.Timestamp("2024-01-02"), "D"), "close")
        # naive 视为北京时间；aware 先换算再判定（UTC 01:25 = 北京 09:25）。
        aware = pd.Timestamp("2024-01-02 01:25", tz="UTC")
        self.assertEqual(data.classify_bar_time(aware, "1min"), "pre")
        self.assertEqual(data.market_timestamp(aware).hour, 9)

    def test_validate_bar_time_strict_and_lenient(self):
        clean = pd.DataFrame({"trade_time": pd.to_datetime(["2024-01-02 09:31", "2024-01-02 14:59"])})
        self.assertEqual(data.validate_bar_time(clean, freq="1min"), "continuous")
        dirty = pd.DataFrame(
            {"trade_time": pd.to_datetime(["2024-01-02 09:25", "2024-01-02 09:31", "2024-01-02 15:05"])}
        )
        with self.assertRaises(ValueError) as ctx:
            data.validate_bar_time(dirty, freq="1min")
        self.assertIn("pre", str(ctx.exception))
        self.assertIn("post", str(ctx.exception))
        self.assertEqual(data.validate_bar_time(dirty, freq="1min", strict=False), "post")
        daily = pd.DataFrame({"trade_date": pd.to_datetime(["2024-01-02"])})
        self.assertEqual(data.validate_bar_time(daily), "close")

    def test_continuous_bars_excludes_auction_and_post(self):
        df = pd.DataFrame(
            {
                "trade_time": pd.to_datetime(
                    ["2024-01-02 09:25", "2024-01-02 09:31", "2024-01-02 15:05"]
                ),
                "close": [10.0, 10.1, 10.2],
            }
        )
        kept = data.continuous_bars(df, freq="1min")
        self.assertEqual(len(kept), 1)
        self.assertEqual(float(kept["close"].iloc[0]), 10.1)
        self.assertEqual(len(data.continuous_bars(df.iloc[0:0], freq="1min")), 0)

    def test_empty_frame_raises(self):
        with self.assertRaises(ValueError):
            data.latest_bar_time(pd.DataFrame())

    def test_unknown_freq_raises(self):
        df = pd.DataFrame({"trade_date": pd.to_datetime(["2024-01-02"])})
        with self.assertRaises(ValueError):
            data.latest_bar_time(df, freq="1m")


class FakeUniverseClient:
    """多代码 Tushare 替身：按 ``ts_code`` 分发 daily / adj_factor，并记录调用。"""

    def __init__(self, daily: dict[str, pd.DataFrame], factors: dict[str, pd.DataFrame] | None = None):
        self.daily = daily
        self.factors = factors or {}
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _window(df: pd.DataFrame, start, end) -> pd.DataFrame:
        out = df.copy()
        if start:
            out = out[out["trade_date"] >= pd.Timestamp(start)]
        if end:
            out = out[out["trade_date"] <= pd.Timestamp(end)]
        return out

    def query(self, api_name, **kwargs):
        code = str(kwargs.get("ts_code") or "")
        self.calls.append((api_name, code))
        frame = self.daily.get(code) if api_name == "daily" else self.factors.get(code)
        if frame is None:
            return pd.DataFrame()
        return self._window(frame, kwargs.get("start_date"), kwargs.get("end_date"))


def _universe_frames(codes: tuple[str, ...], days: int = 6) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """构造多代码行情与复权因子（因子 1.0,1.5,2.0,...，每只股票基准不同）。"""
    dates = pd.bdate_range("2025-01-02", periods=days)
    daily: dict[str, pd.DataFrame] = {}
    factors: dict[str, pd.DataFrame] = {}
    for offset, code in enumerate(codes):
        close = np.arange(len(dates), dtype=float) + 10.0 + offset
        daily[code] = pd.DataFrame(
            {
                "ts_code": [code] * len(dates),
                "trade_date": dates,
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "pre_close": close - 1.0,
                "vol": (np.arange(len(dates), dtype=float) + 1.0) * 1000 + offset * 100,
                "amount": (np.arange(len(dates), dtype=float) + 1.0) * 10000 + offset * 1000,
            }
        )
        factors[code] = pd.DataFrame(
            {"trade_date": dates, "adj_factor": 1.0 + 0.5 * np.arange(len(dates), dtype=float) + offset}
        )
    return daily, factors


class DailyDatasetTests(unittest.TestCase):
    """plan-20261103-011：日线缓存按 trade_date 分区 + 横截面 scan。

    覆盖验收：分区落盘形状、scan 与逐股读取逐值一致、热路径不再联网/逐股 load、
    逐股缓存变化即失效重建、缺口代码回填、``end`` 截止语义、面板 API 兼容。
    """

    CODES = ("000001.SZ", "600519.SH")
    START = "2025-01-02"
    END = "2025-01-09"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.daily, self.factors = _universe_frames(self.CODES)

    def tearDown(self):
        self.tmp.cleanup()

    @contextmanager
    def env(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(data, "DAILY_DIR", self.root))
            stack.enter_context(patch.object(data, "_DAILY_MEM", {}))
            stack.enter_context(patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")))
            stack.enter_context(
                patch.object(data, "last_open_date", side_effect=lambda x: pd.Timestamp(x).normalize())
            )
            yield

    def seed(self, codes: tuple[str, ...] | None = None, adj: str = "qfq") -> None:
        """用真实 load_daily 写逐股缓存（等价旧缓存形态）。"""
        codes = codes or self.CODES
        client = FakeUniverseClient(self.daily, self.factors)
        for code in codes:
            data.load_daily(code, start=self.START, end=self.END, adj=adj, client=client)

    def scan(self, codes=None, **kwargs):
        return data.scan_daily(
            list(codes or self.CODES), start=kwargs.pop("start", self.START), end=kwargs.pop("end", self.END), **kwargs
        )

    def dataset_dir(self, adj: str = "qfq") -> Path:
        return self.root / "_dataset" / f"adj={data._adj_key(adj)}"

    def read_meta(self, adj: str = "qfq") -> dict:
        return json.loads((self.dataset_dir(adj) / "_meta.json").read_text(encoding="utf-8"))

    # --- 分区落盘形状 -----------------------------------------------------
    def test_scan_daily_writes_trade_date_partitions(self):
        with self.env():
            self.seed()
            out = self.scan()
        self.assertEqual(len(out), len(self.CODES) * 6)
        builds = sorted(self.dataset_dir().glob("build=*"))
        self.assertEqual(len(builds), 1)
        partitions = sorted(p.name for p in builds[0].glob("trade_date=*"))
        self.assertEqual(
            partitions,
            [f"trade_date={d}" for d in ("20250102", "20250103", "20250106", "20250107", "20250108", "20250109")],
        )
        part_file = next(builds[0].glob("trade_date=*/part-*.parquet"))
        stored = pd.read_parquet(part_file)
        # 分区列由目录名提供，不重复落进文件（否则 scan 时同名列冲突）。
        self.assertNotIn("trade_date", stored.columns)
        self.assertEqual(set(stored["ts_code"]), set(self.CODES))

        meta = self.read_meta()
        self.assertEqual(meta["version"], data.DATASET_VERSION)
        self.assertEqual(meta["adj"], "qfq")
        self.assertEqual(meta["end_eff"], "20250109")
        self.assertEqual(sorted(meta["codes"]), sorted(self.CODES))
        self.assertEqual(meta["rows"], len(out))

    def test_scan_daily_matches_per_code_load_daily(self):
        with self.env():
            self.seed()
            scanned = self.scan()
            per_code = pd.concat(
                [data.load_daily(code, start=self.START, end=self.END, adj="qfq") for code in self.CODES],
                ignore_index=True,
            ).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
        self.assertEqual(scanned.attrs["adjustment"]["adj_source"], "daily_dataset")
        self.assertEqual(scanned.attrs["adjustment"]["adj"], "qfq")
        self.assertEqual(sorted(scanned.attrs["adjustment"]["adj_bases"]), sorted(self.CODES))
        columns = list(per_code.columns)
        pd.testing.assert_frame_equal(
            scanned[columns].reset_index(drop=True),
            per_code[columns],
            check_dtype=False,
        )

    def test_columns_argument_projects_without_extra_fetch(self):
        with self.env():
            self.seed()
            out = self.scan(columns=["ts_code", "trade_date", "close"])
        self.assertEqual(list(out.columns), ["ts_code", "trade_date", "close"])
        self.assertEqual(len(out), len(self.CODES) * 6)

    # --- 热路径：不再逐股 load / 不联网 -----------------------------------
    def test_warm_scan_reuses_dataset_without_touching_network(self):
        with self.env():
            self.seed()
            first = self.scan()
            build = self.read_meta()["build"]
            data._DAILY_MEM.clear()
            with patch.object(data, "_fetch_daily", side_effect=AssertionError("热路径不应联网")):
                second = self.scan()
            status = data.dataset_status("qfq")
        self.assertEqual(len(first), len(second))
        self.assertEqual(self.read_meta()["build"], build)  # 没有重建
        self.assertTrue(status["usable"])
        pd.testing.assert_frame_equal(first.reset_index(drop=True), second.reset_index(drop=True), check_dtype=False)

    # --- ADR-011：逐股缓存变化即失效 --------------------------------------
    def test_dataset_is_rebuilt_when_per_code_cache_changes(self):
        with self.env():
            self.seed()
            self.scan()
            first_build = self.read_meta()["build"]

            path, _ = data._cache_paths("000001.SZ", "qfq")
            edited = pd.read_parquet(path)
            edited["vol"] = edited["vol"] * 2
            edited.to_parquet(path, index=False)  # 文件签名变化 → 数据集不可再用

            data._DAILY_MEM.clear()
            out = self.scan()
            status = data.dataset_status("qfq")
        self.assertNotEqual(self.read_meta()["build"], first_build)
        self.assertTrue(status["usable"])
        bumped = out.loc[out["ts_code"] == "000001.SZ", "vol"].to_numpy(dtype=float)
        self.assertTrue(np.allclose(bumped, self.daily["000001.SZ"]["vol"].to_numpy(dtype=float) * 2))

    def test_legacy_per_code_cache_is_migrated_by_scan(self):
        """旧缓存形态（只有逐股 parquet+json，没有 _dataset）可直接迁移。"""
        with self.env():
            self.seed()
            self.assertFalse(self.dataset_dir().exists())
            out = self.scan()
        self.assertTrue((self.dataset_dir() / "_meta.json").exists())
        self.assertEqual(len(out), len(self.CODES) * 6)

    # --- 缺口回填 ---------------------------------------------------------
    def test_missing_code_is_fetched_and_added_to_dataset(self):
        third = "300750.SZ"
        daily, factors = _universe_frames((third,))
        with self.env():
            self.seed()
            self.scan()
            client = FakeUniverseClient(daily, factors)
            with patch.object(data, "get_client", return_value=client):
                out = data.scan_daily([third], start=self.START, end=self.END, adj="qfq")
            status = data.dataset_status("qfq")
        self.assertEqual(set(out["ts_code"]), {third})
        self.assertIn(("daily", third), client.calls)
        # 补齐后重新落盘的数据集覆盖全部已缓存代码，而不是只剩本次请求的那一只。
        self.assertEqual(status["codes"], len(self.CODES) + 1)

    # --- 时间语义 ---------------------------------------------------------
    def test_end_eff_limits_rows_and_is_recorded(self):
        with self.env():
            self.seed()
            # 请求截止更早的日期时，逐股缓存里的 qfq 基准（末行因子）不再匹配，
            # scan 会回退 load_daily 重拉；用替身客户端保证这条回退路径不联网。
            with patch.object(data, "get_client", return_value=FakeUniverseClient(self.daily, self.factors)):
                out = self.scan(end="2025-01-06")
            meta = self.read_meta()
        self.assertEqual(pd.Timestamp(out["trade_date"].max()), pd.Timestamp("2025-01-06"))
        self.assertEqual(meta["end_eff"], "20250106")
        self.assertEqual(meta["end"], "20250106")

    def test_unknown_adj_is_rejected(self):
        with self.env(), self.assertRaises(ValueError):
            data.scan_daily(list(self.CODES), adj="raw")

    # --- 面板 API 兼容 ----------------------------------------------------
    def test_load_daily_panel_matches_enriched_load_daily(self):
        with self.env():
            self.seed()
            panel = data.load_daily_panel(list(self.CODES), start=self.START, end=self.END)
            direct = {
                code: data.enrich(data.load_daily(code, start=self.START, end=self.END, adj="qfq"))
                for code in self.CODES
            }
        self.assertEqual(sorted(panel), sorted(self.CODES))
        for code, frame in panel.items():
            with self.subTest(code=code):
                self.assertIn("vol_ma20", frame.columns)  # enrich 指标在位（因子模块依赖）
                self.assertIn("ts_code", frame.columns)
                self.assertEqual(frame.attrs["adjustment"]["adj_source"], "daily")
                self.assertEqual(frame.attrs["adjustment"]["adj_base"], "2025-01-09")
                self.assertAlmostEqual(
                    frame.attrs["adjustment"]["base_factor"],
                    float(self.factors[code]["adj_factor"].iloc[-1]),
                )
                pd.testing.assert_frame_equal(frame, direct[code], check_dtype=False)

    def test_load_daily_panel_empty_codes(self):
        with self.env():
            self.assertEqual(data.load_daily_panel([]), {})


if __name__ == "__main__":
    unittest.main()
