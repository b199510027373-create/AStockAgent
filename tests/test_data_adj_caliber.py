"""ADR-008 D1 复权口径收口：hfq 基准 1.0 / 因子缺失硬失败 / 分钟复权标注。

覆盖 plan-20261114-003 验收：
- hfq = raw * adj_factor / 最早可用交易日因子，与 qfq（最新因子）不混用；
- 因子为空/缺行/NaN/inf/<=0 抛 DataContractError，不回退 qfq、不填充 1.0；
- 分钟线按交易日日线因子复权，attrs 带 adj/adj_base/adj_source=daily；
- raw 价格与 vol/amount 不被复权改写；
- 缓存 meta 的 base_factor 变化视为失效并重拉。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import astock.data as data
from astock.contracts import DataContractError


class FakeClient:
    """最小 Tushare 替身：daily + adj_factor，按日期区间过滤并记录调用参数。"""

    def __init__(self, daily: pd.DataFrame, factors: pd.DataFrame | None):
        self.daily = daily
        self.factors = factors
        self.calls: list[str] = []
        self.factor_kwargs: list[dict] = []
        self.query_all_calls: list[tuple[str, dict]] = []

    @staticmethod
    def _window(df: pd.DataFrame | None, start, end) -> pd.DataFrame:
        if df is None:
            return pd.DataFrame()
        out = df.copy()
        if out.empty or "trade_date" not in out.columns:
            return out
        if start:
            out = out[out["trade_date"] >= pd.Timestamp(start)]
        if end:
            out = out[out["trade_date"] <= pd.Timestamp(end)]
        return out.copy()

    def query(self, api_name, **kwargs):
        self.calls.append(api_name)
        if api_name == "daily":
            return self._window(
                self.daily, kwargs.get("start_date"), kwargs.get("end_date")
            )
        if api_name == "adj_factor":
            self.factor_kwargs.append(kwargs)
            return self._window(
                self.factors, kwargs.get("start_date"), kwargs.get("end_date")
            )
        raise AssertionError(f"unexpected api: {api_name}")

    def query_all(self, api_name, **kwargs):
        self.query_all_calls.append((api_name, kwargs))
        return self.query(api_name, **kwargs)


class FakeMinuteClient(FakeClient):
    def __init__(self, minute: pd.DataFrame, factors: pd.DataFrame | None):
        super().__init__(minute, factors)
        self.minute = minute

    def query(self, api_name, **kwargs):
        if api_name == "rt_min":
            self.calls.append(api_name)
            return self.minute.copy()
        return super().query(api_name, **kwargs)


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
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


def _factors(values=(2.0, 4.0, 8.0)) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
            "adj_factor": list(values),
        }
    )


def _minute_frame(*, include_today: bool = False) -> pd.DataFrame:
    dates = ["2025-01-02", "2025-01-03"] + (["2025-01-06"] if include_today else [])
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(dates),
            "trade_time": pd.to_datetime([f"{d} 09:35:00" for d in dates]),
            "open": [10.0, 11.0, 12.0][: len(dates)],
            "high": [10.5, 11.5, 12.5][: len(dates)],
            "low": [9.8, 10.8, 11.8][: len(dates)],
            "close": [10.0, 11.0, 12.0][: len(dates)],
            "pre_close": [10.0, 10.0, 11.0][: len(dates)],
            "vol": [100.0, 200.0, 300.0][: len(dates)],
            "amount": [1000.0, 2000.0, 3000.0][: len(dates)],
        }
    )


class DailyAdjCaliberTests(unittest.TestCase):
    def test_hfq_base_is_earliest_available_factor(self):
        fake = FakeClient(_daily_frame(), _factors())
        out = data._fetch_daily(fake, "600519.SH", "20250102", "20250106", "hfq")
        # raw * factor / 2.0（最早交易日因子），不是 factor/1.0
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.2)
        self.assertAlmostEqual(float(out["close"].iloc[1]), 11.2 * 2.0)
        self.assertAlmostEqual(float(out["close"].iloc[-1]), 12.2 * 4.0)
        self.assertEqual(
            out.attrs["adjustment"],
            {
                "adj": "hfq",
                "adj_base": "2025-01-02",
                "base_factor": 2.0,
                "adj_source": "daily",
            },
        )

    def test_hfq_base_ignores_requested_window(self):
        """请求区间从 01-03 开始，基准仍是全历史最早交易日 01-02。"""
        frame = _daily_frame().iloc[1:].reset_index(drop=True)
        fake = FakeClient(frame, _factors())
        out = data._fetch_daily(fake, "600519.SH", "20250103", "20250106", "hfq")
        self.assertAlmostEqual(float(out["close"].iloc[0]), 11.2 * 4.0 / 2.0)
        self.assertEqual(out.attrs["adjustment"]["adj_base"], "2025-01-02")
        # 因子查询必须从上市初开始，不能用请求区间推断基准
        self.assertEqual(fake.factor_kwargs[0]["start_date"], data.ADJ_HISTORY_START)
        # 走分页 query_all：单次 query 会被服务端行数上限截断（实测 000001.SZ
        # 单次只返回 6000 行、最早 20020304，分页后 8692 行、最早 19910403）
        self.assertEqual(fake.query_all_calls[0][0], "adj_factor")
        self.assertEqual(fake.query_all_calls[0][1]["start_date"], data.ADJ_HISTORY_START)

    def test_qfq_and_hfq_are_distinct_and_unmixed(self):
        frame = _daily_frame()
        qfq = data._fetch_daily(FakeClient(frame, _factors()), "600519.SH", "20250102", "20250106", "qfq")
        hfq = data._fetch_daily(FakeClient(frame, _factors()), "600519.SH", "20250102", "20250106", "hfq")
        # qfq 以最新因子 8.0 为基准
        self.assertAlmostEqual(float(qfq["close"].iloc[0]), 10.2 * 2.0 / 8.0)
        self.assertAlmostEqual(float(qfq["close"].iloc[-1]), 12.2)
        # hfq/qfq 的逐行比值恒等于 最新/最早 = 4.0，说明两者只是基准不同
        ratio = hfq["close"].to_numpy() / qfq["close"].to_numpy()
        self.assertTrue(np.allclose(ratio, 4.0))
        self.assertNotEqual(float(hfq["close"].iloc[-1]), float(qfq["close"].iloc[-1]))

    def test_hfq_price_and_return_follow_known_factor_series(self):
        """已知因子序列 2→4→8：价格 = raw*f/2，收益率 = 原始收益率×(f_t/f_{t-1})。"""
        raw = _daily_frame()
        factors = pd.Series([2.0, 4.0, 8.0])
        out = data._fetch_daily(
            FakeClient(raw, _factors()), "600519.SH", "20250102", "20250106", "hfq"
        )
        self.assertTrue(np.allclose(out["close"], raw["close"] * factors / 2.0))
        factor_ratio = factors / factors.shift(1)
        expected_ret = (raw["close"].pct_change() + 1.0) * factor_ratio - 1.0
        self.assertTrue(np.allclose(out["close"].pct_change().iloc[1:], expected_ret.iloc[1:]))
        # 交易所原始涨跌幅字段保持 raw 口径：复权只改价格列，不改交易所收益率语义
        self.assertTrue(
            np.allclose(out["pct_chg"], raw["close"] / raw["pre_close"] * 100 - 100)
        )

    def test_raw_columns_and_volume_are_never_adjusted(self):
        out = data._fetch_daily(FakeClient(_daily_frame(), _factors()), "600519.SH", "20250102", "20250106", "hfq")
        self.assertAlmostEqual(float(out["raw_close"].iloc[-1]), 12.2)
        self.assertAlmostEqual(float(out["raw_open"].iloc[0]), 10.0)
        self.assertAlmostEqual(float(out["vol"].iloc[0]), 1000.0)
        self.assertAlmostEqual(float(out["amount"].iloc[0]), 10000.0)
        self.assertNotAlmostEqual(float(out["close"].iloc[-1]), 12.2)

    def test_factor_failures_raise_data_contract_error(self):
        cases = {
            "empty": pd.DataFrame(),
            "zero": _factors((0.0, 4.0, 8.0)),
            "nan": _factors((2.0, np.nan, 8.0)),
            "inf": _factors((2.0, 4.0, np.inf)),
            "negative": _factors((2.0, -1.0, 8.0)),
        }
        for name, factors in cases.items():
            for adj in ("qfq", "hfq"):
                with self.subTest(case=name, adj=adj):
                    with self.assertRaises(DataContractError) as ctx:
                        data._fetch_daily(
                            FakeClient(_daily_frame(), factors),
                            "600519.SH",
                            "20250102",
                            "20250106",
                            adj,
                        )
                    self.assertIn("adj_factor", str(ctx.exception))

    def test_missing_factor_row_raises_instead_of_filling(self):
        factors = _factors().iloc[[0, 2]]  # 缺 2025-01-03
        with self.assertRaises(DataContractError) as ctx:
            data._fetch_daily(
                FakeClient(_daily_frame(), factors), "600519.SH", "20250102", "20250106", "qfq"
            )
        self.assertIn("2025-01-03", str(ctx.exception))
        self.assertIn("禁止用 1.0 填充或回退 qfq", str(ctx.exception))

    def test_missing_adj_factor_column_is_data_contract_error(self):
        with self.assertRaises(DataContractError):
            data._fetch_daily(
                FakeClient(_daily_frame(), pd.DataFrame({"trade_date": pd.to_datetime(["2025-01-02"])})),
                "600519.SH",
                "20250102",
                "20250106",
                "hfq",
            )

    def test_hfq_factor_history_truncated_at_start_raises(self):
        """因子表最早交易日晚于行情首日：hfq 基准不可证，必须报错而不是缩放全序列。"""
        factors = _factors().iloc[1:]  # 最早 2025-01-03，行情从 2025-01-02 开始
        with self.assertRaises(DataContractError) as ctx:
            data._fetch_daily(
                FakeClient(_daily_frame(), factors),
                "600519.SH",
                "20250102",
                "20250106",
                "hfq",
            )
        self.assertIn("疑似被截断", str(ctx.exception))
        self.assertIn("hfq", str(ctx.exception))

    def test_qfq_factor_history_truncated_at_end_raises(self):
        """因子表最新交易日早于行情末日：qfq 基准缺失，必须报错。"""
        factors = _factors().iloc[:2]  # 最新 2025-01-03，行情到 2025-01-06
        with self.assertRaises(DataContractError) as ctx:
            data._fetch_daily(
                FakeClient(_daily_frame(), factors),
                "600519.SH",
                "20250102",
                "20250106",
                "qfq",
            )
        self.assertIn("疑似被截断", str(ctx.exception))
        self.assertIn("qfq", str(ctx.exception))


class MinuteAdjCaliberTests(unittest.TestCase):
    def test_minute_hfq_uses_daily_factor_and_labels_metadata(self):
        out = data._apply_minute_adj(
            _minute_frame(), _factors(), "hfq", ts_code="600519.SH"
        )
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.0)  # 10.0 * 2 / 2
        self.assertAlmostEqual(float(out["close"].iloc[1]), 22.0)  # 11.0 * 4 / 2
        self.assertEqual(
            out.attrs["adjustment"],
            {
                "adj": "hfq",
                "adj_base": "2025-01-02",
                "base_factor": 2.0,
                "adj_source": "daily",
            },
        )
        # 量/额不复权
        self.assertAlmostEqual(float(out["vol"].iloc[1]), 200.0)
        self.assertAlmostEqual(float(out["amount"].iloc[1]), 2000.0)

    def test_minute_missing_factor_raises(self):
        factors = _factors().iloc[[0]]  # 缺 2025-01-03
        with self.assertRaises(DataContractError) as ctx:
            data._apply_minute_adj(_minute_frame(), factors, "qfq", ts_code="600519.SH")
        self.assertIn("2025-01-03", str(ctx.exception))

    def test_minute_empty_factors_raise(self):
        with self.assertRaises(DataContractError):
            data._apply_minute_adj(_minute_frame(), pd.DataFrame(), "qfq")
        with self.assertRaises(DataContractError):
            data._apply_minute_adj(_minute_frame(), _factors((2.0, 0.0, 8.0)), "hfq")

    def test_load_minute_surfaces_adjustment_attrs(self):
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
        fake = FakeMinuteClient(minute, _factors())
        with (
            patch.object(data, "_MINUTE_MEM", {}),
            patch.object(data, "_ADJ_MEM", {}),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-06")),
        ):
            out = data.load_minute(
                "600519.SH", freq="5min", days=1, adj="hfq", client=fake
            )
        self.assertAlmostEqual(float(out["close"].iloc[0]), 10.0)
        self.assertEqual(out.attrs["adjustment"]["adj"], "hfq")
        self.assertEqual(out.attrs["adjustment"]["adj_base"], "2025-01-02")
        self.assertEqual(out.attrs["adjustment"]["adj_source"], "daily")

    def test_minute_intraday_bar_uses_latest_published_factor(self):
        """盘中当日 bar 已存在、当日 adj_factor 尚未发布（评审 P1-1a）。

        只有"缺的都是今天且因子表末行早于今天"才用最近已发布因子复权，
        并标 partial/factor_as_of；否则交易时段 K 线（ui 默认 qfq）整体不可用。
        """
        minute = _minute_frame(include_today=True)
        factors = _factors().iloc[:2]  # 只有 01-02/01-03 的因子，01-06 未发布
        with patch.object(data, "_today", return_value=pd.Timestamp("2025-01-06")):
            out = data._apply_minute_adj(minute, factors, "qfq", ts_code="600519.SH")
        # qfq 基准 = 最新已发布因子 4.0，当日 bar 用同一因子 → 价格不缩放
        self.assertAlmostEqual(float(out["close"].iloc[-1]), 12.0)
        meta = out.attrs["adjustment"]
        self.assertEqual(meta["adj"], "qfq")
        self.assertEqual(meta["adj_base"], "2025-01-03")
        self.assertEqual(meta["base_factor"], 4.0)
        self.assertTrue(meta["partial"])
        self.assertEqual(meta["factor_as_of"], "2025-01-03")

    def test_minute_missing_non_intraday_factor_still_raises(self):
        """非当日缺因子不得被盘中例外吞掉：仍必须硬失败。"""
        minute = _minute_frame(include_today=True)
        factors = _factors().iloc[:2]  # 缺 2025-01-06
        with patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")):
            with self.assertRaises(DataContractError) as ctx:
                data._apply_minute_adj(minute, factors, "qfq", ts_code="600519.SH")
        self.assertIn("2025-01-06", str(ctx.exception))
        self.assertIn("禁止用 1.0 填充或回退 qfq", str(ctx.exception))

    def test_minute_hfq_factor_history_truncated_at_start_raises(self):
        """分钟 hfq 的因子历史若缺早期段落，必须硬失败（评审 iss-mwixbcyf-8383d6）。

        截断在"行情窗口之前"时表现为窗口内早期交易日缺因子；分钟路径与日线
        路径同一把尺子，不得用最近因子顶替 hfq 基准。
        """
        factors = _factors().iloc[[1, 2]]  # 只有 01-03/01-06，缺 01-02
        with patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")):
            with self.assertRaises(DataContractError) as ctx:
                data._apply_minute_adj(
                    _minute_frame(), factors, "hfq", ts_code="600519.SH"
                )
        self.assertIn("2025-01-02", str(ctx.exception))
        self.assertEqual(ctx.exception.contract, "AdjFactor")

    def test_minute_qfq_factor_history_truncated_at_end_raises(self):
        """分钟 qfq 的因子表若缺最新交易日，同样必须报错而不是沿用旧因子。"""
        factors = _factors().iloc[:2]  # 只有 01-02/01-03，缺 01-06（非当日）
        with patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")):
            with self.assertRaises(DataContractError) as ctx:
                data._apply_minute_adj(
                    _minute_frame(include_today=True), factors, "qfq", ts_code="600519.SH"
                )
        self.assertIn("2025-01-06", str(ctx.exception))

    def test_load_minute_requests_factor_history_from_listing(self):
        """分钟线复权因子也必须自上市起分页拉取（评审 P2-5）。"""
        minute = pd.DataFrame(
            {
                "time": ["2025-01-02 09:35:00"],
                "open": [10.0],
                "high": [10.1],
                "low": [9.9],
                "close": [10.0],
                "vol": [1000],
                "amount": [10000],
            }
        )
        fake = FakeMinuteClient(minute, _factors())
        with (
            patch.object(data, "_MINUTE_MEM", {}),
            patch.object(data, "_ADJ_MEM", {}),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-06")),
        ):
            data.load_minute("600519.SH", freq="5min", days=1, adj="qfq", client=fake)
        self.assertEqual([c[0] for c in fake.query_all_calls], ["adj_factor"])
        params = fake.query_all_calls[0][1]
        self.assertEqual(params["start_date"], data.ADJ_HISTORY_START)
        self.assertEqual(params["end_date"], data._fmt(pd.Timestamp("2025-01-06")))


class DailyCacheBaseInvalidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, client, adj="hfq", end="2025-01-06"):
        return data.load_daily(
            "600519.SH", start="2025-01-02", end=end, adj=adj, client=client
        )

    def _patch(self):
        return (
            patch.object(data, "DAILY_DIR", self.root),
            patch.object(data, "_DAILY_MEM", {}),
            patch.object(data, "_today", return_value=pd.Timestamp("2025-01-10")),
            patch.object(data, "last_open_date", side_effect=lambda x: pd.Timestamp(x)),
        )

    def test_cache_is_invalidated_when_base_factor_changes(self):
        p1, p2, p3, p4 = self._patch()
        with p1, p2, p3, p4:
            first = self._load(FakeClient(_daily_frame(), _factors()))
            self.assertEqual(first.attrs["adjustment"]["base_factor"], 2.0)
            _, meta_path = data._cache_paths("600519.SH", "hfq")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["base_factor"], 2.0)
            self.assertEqual(meta["adj_base"], "2025-01-02")

            meta["base_factor"] = 99.0  # 模拟复权因子被回溯调整后的旧缓存
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            data._DAILY_MEM.clear()

            fake2 = FakeClient(_daily_frame(), _factors())
            second = self._load(fake2)
            self.assertIn("daily", fake2.calls)  # 失效后必须重拉
            self.assertEqual(second.attrs["adjustment"]["base_factor"], 2.0)

    def test_legacy_hfq_cache_without_adj_base_is_invalidated(self):
        p1, p2, p3, p4 = self._patch()
        with p1, p2, p3, p4:
            self._load(FakeClient(_daily_frame(), _factors()))
            _, meta_path = data._cache_paths("600519.SH", "hfq")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.pop("adj_base", None)
            meta["base_factor"] = 1.0  # 旧版 hfq 以 1.0 为基准
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            data._DAILY_MEM.clear()

            fake2 = FakeClient(_daily_frame(), _factors())
            self._load(fake2)
            self.assertIn("daily", fake2.calls)

    def test_qfq_cache_hit_keeps_metadata(self):
        p1, p2, p3, p4 = self._patch()
        with p1, p2, p3, p4:
            self._load(FakeClient(_daily_frame(), _factors()), adj="qfq")
            data._DAILY_MEM.clear()
            fake2 = FakeClient(_daily_frame(), _factors())
            out = self._load(fake2, adj="qfq")
            self.assertEqual(fake2.calls, [])  # 基准未变，缓存命中
            self.assertEqual(out.attrs["adjustment"]["base_factor"], 8.0)
            self.assertEqual(out.attrs["adjustment"]["adj_base"], "2025-01-06")
            self.assertEqual(out.attrs["adjustment"]["adj"], "qfq")

    def test_hfq_cache_without_base_day_in_window_is_invalidated(self):
        """缓存按请求区间切片，通常不含上市首日 → 基准不可证即失效重拉（评审 P2-3）。"""
        p1, p2, p3, p4 = self._patch()
        with p1, p2, p3, p4:
            data.load_daily(
                "600519.SH", start="2025-01-03", end="2025-01-06", adj="hfq",
                client=FakeClient(_daily_frame(), _factors()),
            )
            _, meta_path = data._cache_paths("600519.SH", "hfq")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["adj_base"], "2025-01-02")  # 基准日不在缓存窗口内
            data._DAILY_MEM.clear()

            fake2 = FakeClient(_daily_frame(), _factors())
            out = data.load_daily(
                "600519.SH", start="2025-01-03", end="2025-01-06", adj="hfq", client=fake2
            )
            self.assertIn("daily", fake2.calls)  # 无法自证 → 必须重拉
            self.assertEqual(out.attrs["adjustment"]["base_factor"], 2.0)

    def test_qfq_cache_base_follows_requested_end(self):
        """同一缓存窗口、不同 end：qfq 基准必须按请求 end 取最新因子（评审 P2-4）。"""
        p1, p2, p3, p4 = self._patch()
        with p1, p2, p3, p4:
            self._load(FakeClient(_daily_frame(), _factors()), adj="qfq", end="2025-01-06")
            data._DAILY_MEM.clear()

            fake2 = FakeClient(_daily_frame(), _factors())
            out = self._load(fake2, adj="qfq", end="2025-01-03")
            self.assertIn("daily", fake2.calls)  # 请求 end 更早 → 基准不同 → 重拉
            self.assertEqual(out.attrs["adjustment"]["adj_base"], "2025-01-03")
            self.assertEqual(out.attrs["adjustment"]["base_factor"], 4.0)

    def test_legacy_qfq_cache_without_adj_base_is_invalidated(self):
        """旧版 qfq meta 没有 adj_base：命中会让标注退化成 None → 必须失效（ADR-008:42）。"""
        p1, p2, p3, p4 = self._patch()
        with p1, p2, p3, p4:
            self._load(FakeClient(_daily_frame(), _factors()), adj="qfq")
            _, meta_path = data._cache_paths("600519.SH", "qfq")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.pop("adj_base", None)
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            data._DAILY_MEM.clear()

            fake2 = FakeClient(_daily_frame(), _factors())
            out = self._load(fake2, adj="qfq")
            self.assertIn("daily", fake2.calls)
            self.assertEqual(out.attrs["adjustment"]["adj_base"], "2025-01-06")


if __name__ == "__main__":
    unittest.main()
