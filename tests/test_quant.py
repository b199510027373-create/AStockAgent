from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from astock.backtest import CostConfig, run_backtest
from astock.data import _apply_minute_adj, resample_ohlcv
from astock.indicators import add_all
from astock.track import _forward_return


def sample_df() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=5)
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": [10.0, 10.0, 11.0, 12.0, 13.0],
            "high": [10.5, 10.5, 11.5, 12.5, 13.5],
            "low": [9.5, 9.5, 10.5, 11.5, 12.5],
            "close": [10.0, 10.2, 11.2, 12.2, 13.2],
            "pre_close": [9.9, 10.0, 10.2, 11.2, 12.2],
            "vol": [1000.0] * 5,
        }
    )


def zero_cost() -> CostConfig:
    return CostConfig(
        initial_cash=10_000,
        commission_rate=0.0,
        min_commission=0.0,
        stamp_duty=0.0,
        slippage=0.0,
    )


class BacktestRuleTests(unittest.TestCase):
    def test_signal_executes_next_open_not_same_day(self):
        df = sample_df()
        signals = pd.Series([1, 0, 0, 0, 0])
        result = run_backtest(df, signals, "600000.SH", zero_cost())

        self.assertEqual(len(result.trades), 1)
        trade = result.trades.iloc[0]
        self.assertEqual(trade["买入日期"], df.loc[1, "trade_date"])
        self.assertEqual(trade["卖出日期"], df.loc[2, "trade_date"])
        self.assertEqual(trade["股数"], 1000)
        self.assertAlmostEqual(result.metrics["total_return"], 0.10, places=6)

    def test_no_trade_when_cash_below_one_lot(self):
        df = sample_df()
        cfg = zero_cost()
        cfg.initial_cash = 500
        result = run_backtest(df, pd.Series([1, 1, 1, 1, 1]), "600000.SH", cfg)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.metrics["final_equity"], 500)

    def test_one_price_limit_up_blocks_buy(self):
        df = sample_df()
        # 次日一字涨停：pre_close=10，open=high=low=11
        df.loc[1, ["open", "high", "low"]] = [11.0, 11.0, 11.0]
        result = run_backtest(df, pd.Series([1, 1, 1, 1, 1]), "600000.SH", zero_cost())
        self.assertEqual(len(result.trades), 0)

    def test_t_plus_one_prevents_same_day_sell(self):
        df = sample_df()
        # 当天买入，信号当天转 0，也只能下一交易日卖
        signals = pd.Series([1, 0, 0, 0, 0])
        result = run_backtest(df, signals, "600000.SH", zero_cost())
        self.assertEqual(len(result.trades), 1)
        self.assertGreaterEqual(
            (result.trades.iloc[0]["卖出日期"] - result.trades.iloc[0]["买入日期"]).days, 1
        )

    def test_costs_reduce_equity(self):
        df = sample_df()
        cfg = CostConfig(
            initial_cash=10_000,
            commission_rate=0.00025,
            min_commission=5.0,
            stamp_duty=0.0005,
            slippage=0.0005,
        )
        result = run_backtest(df, pd.Series([1, 0, 0, 0, 0]), "600000.SH", cfg)
        self.assertLess(result.metrics["final_equity"], 11_000)


class IndicatorAndResampleTests(unittest.TestCase):
    def test_flat_price_indicators_have_no_inf(self):
        dates = pd.bdate_range("2024-01-02", periods=120)
        flat = pd.DataFrame(
            {
                "trade_date": dates,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "vol": 100.0,
            }
        )
        out = add_all(flat)
        numeric = out.select_dtypes(include=[np.number])
        self.assertFalse(np.isinf(numeric.to_numpy()).any())
        self.assertAlmostEqual(out["rsi14"].iloc[-1], 50.0, places=6)

    def test_weekly_resample_aggregation(self):
        dates = pd.bdate_range("2024-01-01", periods=10)
        daily = pd.DataFrame(
            {
                "trade_date": dates,
                "open": np.arange(1, 11, dtype=float),
                "high": np.arange(1, 11, dtype=float),
                "low": np.arange(1, 11, dtype=float),
                "close": np.arange(1, 11, dtype=float),
                "vol": [100.0] * 10,
                "amount": [1000.0] * 10,
            }
        )
        weekly = resample_ohlcv(daily, "W-FRI")
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly.iloc[0]["open"], 1.0)
        self.assertEqual(weekly.iloc[0]["close"], 5.0)
        self.assertEqual(weekly.iloc[0]["vol"], 500.0)
        self.assertEqual(weekly.iloc[1]["open"], 6.0)
        self.assertEqual(weekly.iloc[1]["close"], 10.0)


class MinuteAdjAndTrackingTests(unittest.TestCase):
    def test_minute_qfq_uses_latest_factor_as_base(self):
        dates = pd.bdate_range("2026-09-01", periods=2)
        df = pd.DataFrame(
            {
                "trade_date": dates,
                "open": [10.0, 10.5],
                "high": [10.2, 10.8],
                "low": [9.9, 10.4],
                "close": [10.0, 10.5],
                "pre_close": [9.8, 10.0],
                "vol": [100.0, 100.0],
                "amount": [1000.0, 1000.0],
            }
        )
        factors = pd.DataFrame({"trade_date": dates, "adj_factor": [1.0, 2.0]})
        out = _apply_minute_adj(df, factors)
        self.assertAlmostEqual(float(out.iloc[0]["close"]), 5.0, places=6)
        self.assertAlmostEqual(float(out.iloc[1]["close"]), 10.5, places=6)

    def test_forward_return_horizons(self):
        dates = pd.bdate_range("2026-09-01", periods=5)
        df = pd.DataFrame({"trade_date": dates, "close": [10.0, 11.0, 12.0, 13.0, 14.0]})
        self.assertAlmostEqual(_forward_return(df, dates[0], 2), 0.2, places=6)
        self.assertIsNone(_forward_return(df, dates[0], 20))


if __name__ == "__main__":
    unittest.main()
