"""板块 / IPO / 停牌交易规则（plan-20261103-009）。

覆盖：创业板/科创板 20%、北交所 30%、ST 5%、创业板 2020-08-24 切换、
IPO 无涨跌幅窗口（创业板/科创板 5 日、北交所首日、主板注册制前后）、
停牌（0 量）与缺 bar 不产生虚假成交。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from astock import events
from astock.backtest import (
    CostConfig,
    _has_trading_volume,
    _listing_offsets,
    _one_price_limit,
    _one_price_limit_by_rule,
    _suspected_intraday_halt,
    board_of,
    ipo_unbounded_days,
    limit_pct,
    limit_rule,
    run_backtest,
)
from astock.events import security_meta
from astock.tushare_client import TushareError


def zero_cost(initial_cash: float = 100_000.0) -> CostConfig:
    return CostConfig(
        initial_cash=initial_cash,
        commission_rate=0.0,
        min_commission=0.0,
        stamp_duty=0.0,
        slippage=0.0,
    )


def bars(opens, vols=None, start: str = "2024-01-02") -> pd.DataFrame:
    n = len(opens)
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": [float(x) for x in opens],
            "high": [float(x) * 1.02 for x in opens],
            "low": [float(x) * 0.98 for x in opens],
            "close": [float(x) for x in opens],
            "pre_close": [float(x) for x in opens],
            "vol": [1000.0] * n if vols is None else [float(v) for v in vols],
        }
    )


def one_price_row(pre: float, price: float, date: str = "2024-01-03") -> pd.Series:
    """一字板行：high == low == price，原始价与复权价一致。"""
    return pd.Series(
        {
            "trade_date": pd.Timestamp(date),
            "raw_pre_close": pre,
            "raw_high": price,
            "raw_low": price,
            "pre_close": pre,
            "high": price,
            "low": price,
        }
    )


class BoardOfTests(unittest.TestCase):
    def test_board_mapping(self):
        cases = {
            "600519.SH": "main",
            "000001.SZ": "main",
            "002594.SZ": "main",
            "300750.SZ": "chinext",
            "301001.SZ": "chinext",
            "688981.SH": "star",
            "689009.SH": "star",
            "430047.BJ": "bse",
            "830799.BJ": "bse",
            "871981.BJ": "bse",
            "920001.BJ": "bse",
            "830799": "bse",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                self.assertEqual(board_of(code), expected)

    def test_unknown_prefix_falls_back_to_main(self):
        self.assertEqual(board_of(""), "main")
        self.assertEqual(board_of("999999.XX"), "main")
        # 88 开头是同花顺行业指数（如 880001），不是北交所股票。
        self.assertEqual(board_of("880001"), "main")


class LimitPctTests(unittest.TestCase):
    """验收 1：创业板/科创板 20%、北交所 30%、ST 5% 各有单元测试。"""

    def test_main_board_ten_percent(self):
        self.assertEqual(limit_pct("600519.SH"), 0.10)
        self.assertEqual(limit_pct("000001.SZ"), 0.10)

    def test_chinext_twenty_percent(self):
        self.assertEqual(limit_pct("300750.SZ"), 0.20)
        self.assertEqual(limit_pct("301001.SZ"), 0.20)

    def test_star_twenty_percent(self):
        self.assertEqual(limit_pct("688981.SH"), 0.20)
        self.assertEqual(limit_pct("689009.SH"), 0.20)

    def test_bse_thirty_percent(self):
        for code in ("430047.BJ", "830799.BJ", "871981.BJ", "920001.BJ"):
            with self.subTest(code=code):
                self.assertEqual(limit_pct(code), 0.30)

    def test_st_five_percent(self):
        self.assertEqual(limit_pct("600000.SH", "*ST某某"), 0.05)
        self.assertEqual(limit_pct("000001.SZ", "ST某某"), 0.05)
        # 创业板/科创板 ST 现实仍是 20%，这里保持 5% 属于保守方向（README 已说明）。
        self.assertEqual(limit_pct("300001.SZ", "ST某某"), 0.05)

    def test_chinext_reform_switch(self):
        before = limit_rule("300001.SZ", trade_date="2020-08-21")
        after = limit_rule("300001.SZ", trade_date="2020-08-24")
        self.assertEqual(before.up, 0.10)
        self.assertEqual(after.up, 0.20)
        # 主板不受创业板改革影响。
        self.assertEqual(limit_rule("600000.SH", trade_date="2020-08-24").up, 0.10)

    def test_st_rule_by_date(self):
        rule = limit_rule("600000.SH", name="*ST某某", trade_date="2024-01-02")
        self.assertEqual((rule.up, rule.down), (0.05, 0.05))
        self.assertEqual(limit_rule("600000.SH", trade_date="2024-01-02").up, 0.10)


class IpoRuleTests(unittest.TestCase):
    """验收 2：IPO 首日无涨跌幅与后续 N 日规则的明确实现。"""

    def test_chinext_first_five_days_unbounded(self):
        self.assertEqual(ipo_unbounded_days("300750.SZ", "2024-05-06"), 5)
        for day in range(5):
            with self.subTest(listing_day=day):
                self.assertTrue(
                    limit_rule("300750.SZ", list_date="2024-05-06", listing_day=day).unbounded
                )
        self.assertEqual(
            limit_rule("300750.SZ", list_date="2024-05-06", listing_day=5).up, 0.20
        )

    def test_star_first_five_days_unbounded(self):
        for day in range(5):
            with self.subTest(listing_day=day):
                self.assertTrue(
                    limit_rule("688981.SH", list_date="2024-05-06", listing_day=day).unbounded
                )
        self.assertEqual(
            limit_rule("688981.SH", list_date="2024-05-06", listing_day=5).up, 0.20
        )

    def test_bse_only_first_day_unbounded(self):
        self.assertEqual(ipo_unbounded_days("830799.BJ", "2024-05-06"), 1)
        self.assertTrue(limit_rule("830799.BJ", list_date="2024-05-06", listing_day=0).unbounded)
        self.assertEqual(
            limit_rule("830799.BJ", list_date="2024-05-06", listing_day=1).up, 0.30
        )

    def test_main_board_registration_ipo_window(self):
        # 2023-04-10 起主板注册制新股前 5 个交易日不设涨跌幅。
        self.assertTrue(
            limit_rule("600000.SH", list_date="2024-05-06", listing_day=0).unbounded
        )
        self.assertTrue(
            limit_rule("600000.SH", list_date="2024-05-06", listing_day=4).unbounded
        )
        self.assertEqual(
            limit_rule("600000.SH", list_date="2024-05-06", listing_day=5).up, 0.10
        )

    def test_main_board_old_first_day_44_36(self):
        rule = limit_rule("600000.SH", list_date="2020-05-06", listing_day=0)
        self.assertEqual((rule.up, rule.down), (0.44, 0.36))
        self.assertEqual(
            limit_rule("600000.SH", list_date="2020-05-06", listing_day=1).up, 0.10
        )

    def test_old_chinext_first_day_44_36(self):
        rule = limit_rule("300001.SZ", list_date="2019-05-06", listing_day=0)
        self.assertEqual((rule.up, rule.down), (0.44, 0.36))

    def test_ipo_window_requires_full_history(self):
        # 数据从上市日之后开始 → 无法证明还在前 5 日，返回 None（按常规限价，不乐观假设）。
        late = pd.bdate_range("2024-06-03", periods=3)
        self.assertIsNone(_listing_offsets(late, "2024-05-06"))
        full = pd.bdate_range("2024-05-06", periods=3)
        self.assertEqual(_listing_offsets(full, "2024-05-06").tolist(), [0, 1, 2])
        pre_listing = pd.bdate_range("2024-05-02", periods=3)
        self.assertEqual(_listing_offsets(pre_listing, "2024-05-06").tolist(), [-2, -1, 0])


class OnePriceLimitTests(unittest.TestCase):
    def test_main_board_ten_percent_one_price(self):
        rule = limit_rule("600000.SH", trade_date="2024-01-03")
        self.assertTrue(_one_price_limit_by_rule(one_price_row(10, 11), rule, "buy"))
        self.assertTrue(_one_price_limit_by_rule(one_price_row(10, 9), rule, "sell"))

    def test_chinext_twenty_percent_one_price(self):
        rule = limit_rule("300750.SZ", trade_date="2024-01-03")
        # 10% 的一字板在创业板不是限价，可以成交（旧实现会误判为买不到）。
        self.assertFalse(_one_price_limit_by_rule(one_price_row(10, 11), rule, "buy"))
        self.assertTrue(_one_price_limit_by_rule(one_price_row(10, 12), rule, "buy"))

    def test_bse_thirty_percent_one_price(self):
        rule = limit_rule("830799.BJ", trade_date="2024-01-03")
        self.assertFalse(_one_price_limit_by_rule(one_price_row(10, 12), rule, "buy"))
        self.assertTrue(_one_price_limit_by_rule(one_price_row(10, 13), rule, "buy"))

    def test_ipo_window_ignores_one_price(self):
        rule = limit_rule("300750.SZ", list_date="2024-01-03", listing_day=0)
        self.assertTrue(rule.unbounded)
        self.assertFalse(_one_price_limit_by_rule(one_price_row(10, 25), rule, "buy"))

    def test_raw_price_nan_falls_back_to_adjusted_columns(self):
        row = one_price_row(10, 11)
        row["raw_pre_close"] = float("nan")
        row["raw_high"] = float("nan")
        row["raw_low"] = float("nan")
        rule = limit_rule("600000.SH", trade_date="2024-01-03")
        self.assertTrue(_one_price_limit_by_rule(row, rule, "buy"))

    def test_legacy_helper_keeps_first_day_behavior(self):
        row = one_price_row(10, 11, date="2024-01-03")
        self.assertTrue(_one_price_limit(row, 0.10, "buy"))
        self.assertFalse(_one_price_limit(row, 0.10, "buy", list_date="2024-01-03"))


class SuspensionTests(unittest.TestCase):
    """验收 3：临时停牌/全天停牌不产生虚假成交。"""

    def test_zero_volume_day_does_not_trade(self):
        df = bars([10.0, 10.0, 10.0], vols=[1000, 0, 1000])
        result = run_backtest(df, pd.Series([1, 0, 0]), "600000.SH", zero_cost())
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.state["shares"], 0)

    def test_resumption_executes_on_first_tradable_bar(self):
        df = bars([10.0, 10.0, 10.0], vols=[1000, 0, 1000])
        result = run_backtest(df, pd.Series([1, 1, 1]), "600000.SH", zero_cost())
        self.assertGreater(result.state["shares"], 0)
        self.assertEqual(result.state["entry_date"], df.loc[2, "trade_date"])

    def test_zero_volume_blocks_sell(self):
        df = bars([10.0, 10.0, 10.0, 10.0], vols=[1000, 1000, 0, 1000])
        result = run_backtest(df, pd.Series([1, 0, 0, 0]), "600000.SH", zero_cost())
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["卖出日期"], df.loc[3, "trade_date"])

    def test_missing_halt_bar_trades_only_on_existing_bars(self):
        # 停牌日在日线里没有行：只有 d1、d3 两根 bar，信号在 d3 复牌开盘成交。
        df = bars([10.0, 10.0], start="2024-01-02")
        df.loc[1, "trade_date"] = pd.Timestamp("2024-01-04")
        result = run_backtest(df, pd.Series([1, 1]), "600000.SH", zero_cost())
        self.assertGreater(result.state["shares"], 0)
        self.assertEqual(result.state["entry_date"], pd.Timestamp("2024-01-04"))
        self.assertEqual(len(result.equity), 2)

    def test_has_trading_volume_fallbacks(self):
        self.assertFalse(_has_trading_volume(pd.Series({"vol": 0.0})))
        self.assertFalse(_has_trading_volume(pd.Series({"vol": float("nan"), "amount": 0.0})))
        self.assertTrue(_has_trading_volume(pd.Series({"vol": float("nan"), "amount": 1.0})))
        # 没有量额列时按可成交处理，避免导出数据被静默判成停牌。
        self.assertTrue(_has_trading_volume(pd.Series({"open": 10.0})))

    def test_pre_listing_rows_do_not_trade(self):
        df = bars([10.0, 10.0, 10.0], start="2024-01-02")
        # 上市日 = 第 2 根 bar；prev_signal=1 会让第 1 根（上市前）尝试成交。
        result = run_backtest(
            df, pd.Series([1, 1, 1]), "600000.SH", zero_cost(),
            list_date="2024-01-03", prev_signal=1,
        )
        self.assertEqual(result.state["entry_date"], pd.Timestamp("2024-01-03"))


class IpoBacktestTests(unittest.TestCase):
    def test_chinext_ipo_day_one_price_is_tradable(self):
        # 上市第 2 个交易日（listing_day=1）一字 20%：在无涨跌幅窗口内，可以买。
        df = bars([10.0, 12.0, 12.0], vols=[1000, 1000, 1000])
        df.loc[1, ["high", "low", "close"]] = 12.0
        df.loc[1, "pre_close"] = 10.0
        result = run_backtest(
            df, pd.Series([1, 0, 0]), "300750.SZ", zero_cost(), list_date=df.loc[0, "trade_date"]
        )
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["买入日期"], df.loc[1, "trade_date"])

    def test_old_listing_data_does_not_assume_no_limit(self):
        # 数据从上市日之后才开始：不能证明还在前 5 日，20% 一字板必须买不到。
        df = bars([10.0, 12.0, 12.0])
        df.loc[1, ["high", "low", "close"]] = 12.0
        df.loc[1, "pre_close"] = 10.0
        result = run_backtest(
            df, pd.Series([1, 0, 0]), "300750.SZ", zero_cost(), list_date="2024-01-01"
        )
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.state["shares"], 0)


class ListingOffsetGapTests(unittest.TestCase):
    """IPO 窗口按工作日计数：停牌缺口不会把恢复限价后的 bar 错放进无涨跌幅窗口。"""

    def test_offsets_count_business_days_not_bars(self):
        dates = [pd.Timestamp("2024-05-06"), pd.Timestamp("2024-05-07"), pd.Timestamp("2024-05-17")]
        offsets = _listing_offsets(pd.Series(dates), "2024-05-06")
        # 05-06 到 05-17 之间隔了 9 个工作日（含停牌/缺 bar 的日子）。
        self.assertEqual(offsets.tolist(), [0, 1, 9])

    def test_suspension_gap_ends_ipo_window(self):
        df = bars([10.0, 10.0, 12.5, 12.5], start="2024-05-06")
        df.loc[2, "trade_date"] = pd.Timestamp("2024-05-17")
        df.loc[3, "trade_date"] = pd.Timestamp("2024-05-20")
        df.loc[2, ["open", "high", "low", "close"]] = 12.5
        df.loc[2, "pre_close"] = 10.0
        # signals[1]=1 → 第 3 根 bar 尝试买入，第 4 根 bar 卖出（让 trades 可判别）。
        # listing_day=9 ≥ 5：已恢复 20% 限价，12.5 高于涨停价 12.0，买不到。
        # 若退回 bar 序号实现（listing_day=2），会误判成无涨跌幅窗口而放行成交。
        result = run_backtest(
            df, pd.Series([0, 1, 0, 0]), "300750.SZ", zero_cost(), list_date="2024-05-06"
        )
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.state["shares"], 0)

        # 对照：pre_close 改为 12.5（20% 涨停价 15.0），同一根 bar 不是一字板，可成交。
        relaxed = df.copy()
        relaxed.loc[2, "pre_close"] = 12.5
        control = run_backtest(
            relaxed, pd.Series([0, 1, 0, 0]), "300750.SZ", zero_cost(), list_date="2024-05-06"
        )
        self.assertEqual(len(control.trades), 1)
        trade = control.trades.iloc[0]
        # 卖出日期必须晚于买入日期：防止夹具倒序让「持有天数」等后续断言失真。
        self.assertLess(trade["买入日期"], trade["卖出日期"])


class IntradayHaltTests(unittest.TestCase):
    """验收 3：无涨跌幅窗口内异常振幅按"可能盘中临停"拒绝成交（保守近似）。"""

    def _ipo_df(self, high: float, low: float) -> pd.DataFrame:
        df = bars([10.0, 12.0, 12.0], start="2024-05-06")
        df.loc[1, ["open", "high", "low", "close"]] = [12.0, high, low, 12.0]
        df.loc[1, "pre_close"] = 10.0
        return df

    def test_large_amplitude_in_unbounded_window_does_not_trade(self):
        df = self._ipo_df(high=15.0, low=10.5)  # 振幅 42.9% ≥ 30%
        result = run_backtest(
            df, pd.Series([1, 0, 0]), "300750.SZ", zero_cost(), list_date=df.loc[0, "trade_date"]
        )
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.state["shares"], 0)

    def test_moderate_amplitude_in_unbounded_window_still_trades(self):
        df = self._ipo_df(high=12.0, low=10.0)  # 振幅 20% < 30%
        result = run_backtest(
            df, pd.Series([1, 0, 0]), "300750.SZ", zero_cost(), list_date=df.loc[0, "trade_date"]
        )
        self.assertEqual(len(result.trades), 1)

    def test_helper_only_applies_to_unbounded_rules(self):
        row = pd.Series({"high": 15.0, "low": 10.0})
        unbounded = limit_rule("300750.SZ", list_date="2024-05-06", listing_day=0)
        bounded = limit_rule("600000.SH", trade_date="2024-05-06")
        self.assertTrue(_suspected_intraday_halt(row, unbounded))
        self.assertFalse(_suspected_intraday_halt(row, bounded))


class EndToEndRuleTests(unittest.TestCase):
    """验收 1/2 的端到端用例：ST 5% 与旧规则首日 44%/36% 必须真的挡住成交。"""

    def test_st_five_percent_one_price_blocks_buy(self):
        df = bars([10.0, 10.5, 10.5])
        df.loc[1, ["open", "high", "low", "close"]] = 10.5
        df.loc[1, "pre_close"] = 10.0
        signals = pd.Series([1, 0, 0])
        st = run_backtest(df, signals, "600000.SH", zero_cost(), name="*ST某某")
        self.assertEqual(len(st.trades), 0)
        self.assertEqual(st.state["shares"], 0)
        # 同一根 bar 在非 ST 10% 口径下不是一字板，可以成交（证明 name 真的生效）。
        normal = run_backtest(df, signals, "600000.SH", zero_cost())
        self.assertEqual(len(normal.trades), 1)
        self.assertEqual(normal.trades.iloc[0]["买入日期"], df.loc[1, "trade_date"])

    def test_old_rule_first_day_44_percent_blocks_one_price(self):
        df = bars([14.4, 14.5, 14.5], start="2020-05-06")
        df.loc[0, ["open", "high", "low", "close"]] = 14.4
        df.loc[0, "pre_close"] = 10.0
        result = run_backtest(
            df, pd.Series([0, 0, 0]), "600000.SH", zero_cost(),
            list_date=df.loc[0, "trade_date"], prev_signal=1,
        )
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.state["shares"], 0)

    def test_old_rule_listing_day_below_44_percent_is_tradable(self):
        df = bars([12.0, 12.5, 12.5], start="2020-05-06")
        df.loc[0, ["open", "high", "low", "close"]] = 12.0
        df.loc[0, "pre_close"] = 10.0
        # 首日 44% 限价 14.4：12.0 不是一字板；若按常规 10% 会误判为买不到。
        result = run_backtest(
            df, pd.Series([0, 0, 0]), "600000.SH", zero_cost(),
            list_date=df.loc[0, "trade_date"], prev_signal=1,
        )
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["买入日期"], df.loc[0, "trade_date"])

    def test_old_rule_next_bar_above_44_percent_yet_tradable(self):
        # 14.5 高于首日 44% 涨停价，但已到第 2 个交易日（10% 限价 15.84），不是一字。
        df = bars([14.4, 14.5, 14.5], start="2020-05-06")
        df.loc[0, ["open", "high", "low", "close"]] = 14.4
        df.loc[0, "pre_close"] = 10.0
        df.loc[1, ["open", "high", "low", "close"]] = 14.5
        df.loc[1, "pre_close"] = 14.4
        result = run_backtest(
            df, pd.Series([1, 1, 0]), "600000.SH", zero_cost(),
            list_date=df.loc[0, "trade_date"], prev_signal=1,
        )
        self.assertGreater(result.state["shares"], 0)
        self.assertEqual(result.state["entry_date"], df.loc[1, "trade_date"])


class SecurityMetaTests(unittest.TestCase):
    def test_meta_from_stock_basic(self):
        basic = pd.DataFrame(
            {
                "ts_code": ["600000.SH", "300001.SZ"],
                "name": ["*ST某某", "某某股份"],
                "list_date": [pd.Timestamp("2020-01-02"), pd.NaT],
            }
        )
        with mock.patch("astock.events.stock_basic_all", return_value=basic):
            self.assertEqual(security_meta("600000.SH"), ("*ST某某", pd.Timestamp("2020-01-02")))
            self.assertEqual(security_meta("300001.SZ"), ("某某股份", None))
            self.assertEqual(security_meta("000001.SZ"), ("", None))

    def test_meta_offline_falls_back_to_regular_limits(self):
        with mock.patch("astock.events.stock_basic_all", side_effect=RuntimeError("offline")):
            self.assertEqual(security_meta("600000.SH"), ("", None))


class StockBasicCoverageTests(unittest.TestCase):
    """退市/暂停上市代码要能取到名称与上市日；更旧的缓存不得静默回退。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patch_dir = mock.patch("astock.events.EVENT_DIR", Path(self._tmp.name))
        patch_dir.start()
        self.addCleanup(patch_dir.stop)
        for name in ("_BASIC", "_BASIC_ALL", "_BASIC_DAY", "_BASIC_ALL_DAY"):
            patcher = mock.patch(f"astock.events.{name}", None)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _frames() -> dict[str, pd.DataFrame]:
        return {
            "L": pd.DataFrame(
                {
                    "ts_code": ["600000.SH"],
                    "name": ["浦发银行"],
                    "industry": ["银行"],
                    "market": ["主板"],
                    "list_date": ["19991110"],
                }
            ),
            "D": pd.DataFrame(
                {
                    "ts_code": ["600001.SH"],
                    "name": ["*ST某某"],
                    "industry": ["钢铁"],
                    "market": ["主板"],
                    "list_date": ["19980101"],
                }
            ),
            "P": pd.DataFrame(
                {
                    "ts_code": ["600002.SH"],
                    "name": ["某某暂停"],
                    "industry": ["化工"],
                    "market": ["主板"],
                    "list_date": ["20100101"],
                }
            ),
        }

    def _client(self, frames, fail: tuple[str, ...] = ()) -> mock.Mock:
        client = mock.Mock()

        def query(api: str, **kwargs):
            status = kwargs.get("list_status")
            if status in fail:
                raise TushareError(f"{status} unavailable")
            return frames[status].copy()

        client.query.side_effect = query
        return client

    def _write_cache(self, filename: str, day: str) -> None:
        path = Path(self._tmp.name) / filename
        pd.DataFrame(
            {
                "ts_code": ["600000.SH"],
                "name": ["浦发银行"],
                "industry": ["银行"],
                "market": ["主板"],
                "list_date": [pd.Timestamp("1999-11-10")],
            }
        ).to_parquet(path, index=False)
        path.with_suffix(".json").write_text(day, encoding="utf-8")

    def test_stock_basic_all_covers_delisted_and_suspended(self):
        frames = self._frames()
        with mock.patch("astock.events.get_client", return_value=self._client(frames)):
            basic_all = events.stock_basic_all()
            pool = events.stock_basic()
        self.assertEqual(set(basic_all["ts_code"]), {"600000.SH", "600001.SH", "600002.SH"})
        # 推荐/选股用的在市池不能被退市股污染。
        self.assertEqual(set(pool["ts_code"]), {"600000.SH"})
        listed = basic_all.loc[basic_all["ts_code"] == "600001.SH", "list_date"].iloc[0]
        self.assertEqual(pd.Timestamp(listed), pd.Timestamp("1998-01-01"))

    def test_delisted_query_failure_still_returns_listed_pool(self):
        frames = self._frames()
        client = self._client(frames, fail=("D",))
        with mock.patch("astock.events.get_client", return_value=client):
            basic_all = events.stock_basic_all()
        self.assertEqual(set(basic_all["ts_code"]), {"600000.SH", "600002.SH"})

    def test_stale_cache_is_not_a_silent_fallback(self):
        self._write_cache("stock_basic.parquet", "20000101")
        client = self._client(self._frames(), fail=("L",))
        with mock.patch("astock.events.get_client", return_value=client), self.assertRaises(TushareError):
            events.stock_basic()

    def test_same_day_cache_falls_back_when_offline(self):
        self._write_cache("stock_basic.parquet", events._today_str())
        client = self._client(self._frames(), fail=("L",))
        with mock.patch("astock.events.get_client", return_value=client):
            out = events.stock_basic()
        self.assertEqual(out["ts_code"].tolist(), ["600000.SH"])

    def test_in_process_memo_is_refreshed_across_days(self):
        stale = self._frames()["L"].copy()
        stale["name"] = ["旧名称"]
        fresh = self._frames()["L"].copy()
        fresh["name"] = ["新名称"]
        with (
            mock.patch("astock.events._BASIC", stale),
            mock.patch("astock.events._BASIC_DAY", "20000101"),
            mock.patch("astock.events._load_basic", return_value=fresh) as loader,
        ):
            out = events.stock_basic()
            loader.assert_called_once_with("stock_basic.parquet", ("L",), False)
            self.assertEqual(events._BASIC_DAY, events._today_str())
        # 长驻进程跨天后必须换掉内存里的旧股票池，而不是静默复用。
        self.assertEqual(out["name"].iloc[0], "新名称")

    def test_in_process_memo_reused_within_day(self):
        cached = self._frames()["L"].copy()
        with (
            mock.patch("astock.events._BASIC", cached),
            mock.patch("astock.events._BASIC_DAY", events._today_str()),
            mock.patch("astock.events._load_basic") as loader,
        ):
            out = events.stock_basic()
        loader.assert_not_called()
        self.assertEqual(out["ts_code"].tolist(), ["600000.SH"])

    def test_all_memo_is_refreshed_across_days(self):
        # 回测真正取名称/上市日走 security_meta → stock_basic_all，必须与 stock_basic 对称。
        stale = self._frames()["L"].copy()
        fresh = self._frames()["D"].copy()
        with (
            mock.patch("astock.events._BASIC_ALL", stale),
            mock.patch("astock.events._BASIC_ALL_DAY", "20000101"),
            mock.patch("astock.events._load_basic", return_value=fresh) as loader,
        ):
            out = events.stock_basic_all()
            loader.assert_called_once_with("stock_basic_all.parquet", ("L", "D", "P"), False)
            self.assertEqual(events._BASIC_ALL_DAY, events._today_str())
        self.assertEqual(out["ts_code"].tolist(), ["600001.SH"])

    def test_all_memo_reused_within_day(self):
        cached = self._frames()["D"].copy()
        with (
            mock.patch("astock.events._BASIC_ALL", cached),
            mock.patch("astock.events._BASIC_ALL_DAY", events._today_str()),
            mock.patch("astock.events._load_basic") as loader,
        ):
            out = events.stock_basic_all()
        loader.assert_not_called()
        self.assertEqual(out["ts_code"].tolist(), ["600001.SH"])


if __name__ == "__main__":
    unittest.main()
