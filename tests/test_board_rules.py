"""板块 / IPO / 停牌交易规则（plan-20261103-009）。

覆盖：创业板/科创板 20%、北交所 30%、ST 5%、创业板 2020-08-24 切换、
IPO 无涨跌幅窗口（创业板/科创板 5 日、北交所首日、主板注册制前后）、
停牌（0 量）与缺 bar 不产生虚假成交。
"""

from __future__ import annotations

import unittest

import pandas as pd

from astock.backtest import (
    CostConfig,
    _has_trading_volume,
    _listing_offsets,
    _one_price_limit,
    _one_price_limit_by_rule,
    board_of,
    ipo_unbounded_days,
    limit_pct,
    limit_rule,
    run_backtest,
)


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


if __name__ == "__main__":
    unittest.main()
