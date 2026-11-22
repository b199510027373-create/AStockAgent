from __future__ import annotations

import unittest
from typing import ClassVar
from unittest import mock

import numpy as np
import pandas as pd

from astock import optimize
from astock import strategy_lab as lab
from astock.backtest import make_signals_params
from astock.factors import compute_ic
from astock.indicators import add_all
from astock.optimize import grid_search, walk_forward
from astock.strategy_lab import walk_forward_strategy
from astock.track import significance_table


def synthetic_daily(n: int = 400) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=n)
    close = 10 + np.cumsum(np.sin(np.arange(n) / 7) * 0.05)
    return add_all(
        pd.DataFrame(
            {
                "trade_date": dates,
                "open": close * 0.999,
                "high": close * 1.012,
                "low": close * 0.988,
                "close": close,
                "pre_close": np.r_[close[0], close[:-1]],
                "vol": 100000.0,
            }
        )
    )


class OptimizeTests(unittest.TestCase):
    def test_grid_and_walk_forward(self):
        daily = synthetic_daily(400)
        grid = grid_search(daily, "600000.SH", [2, 3], [5, 10], None, "sharpe")
        self.assertGreaterEqual(len(grid.grid), 2)
        wf = walk_forward(
            daily, "600000.SH", [2, 3], [5, 10], None,
            train_years=1.0, test_months=1, metric="sharpe",
        )
        self.assertGreaterEqual(wf.metrics["windows"], 1)
        self.assertFalse(wf.equity.empty)

    def test_macd_and_generic_walk_forward(self):
        daily = synthetic_daily(400)
        sig = make_signals_params(daily, "MACD", {"fast": 8, "slow": 20, "signal": 6})
        self.assertEqual(len(sig), len(daily))
        wf = walk_forward_strategy(
            daily, "600000.SH", "MA双均线",
            [{"fast": 2, "slow": 5}, {"fast": 3, "slow": 10}],
            None, 1.0, 1, "sharpe",
        )
        self.assertGreaterEqual(wf.metrics["windows"], 1)
        self.assertIn("best", wf.walk.columns)


class RuleWiringTests(unittest.TestCase):
    """评审 iss-mws2ln9l：优化/策略实验室必须与单票回测同口径（ST 5%、IPO 窗口）。"""

    META: ClassVar[dict[str, str]] = {"name": "*ST某某", "list_date": "2020-01-02"}

    def assert_forwarded(self, spy):
        calls = spy.call_args_list
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call.kwargs.get("name"), self.META["name"])
            self.assertEqual(call.kwargs.get("list_date"), self.META["list_date"])

    def test_grid_search_forwards_name_and_list_date(self):
        daily = synthetic_daily(400)
        with mock.patch.object(optimize, "run_backtest", wraps=optimize.run_backtest) as spy:
            optimize.grid_search(
                daily, "600000.SH", [2, 3], [5, 10], None, "sharpe",
                name="*ST某某", list_date="2020-01-02",
            )
        self.assert_forwarded(spy)

    def test_walk_forward_forwards_name_and_list_date(self):
        daily = synthetic_daily(400)
        with mock.patch.object(optimize, "run_backtest", wraps=optimize.run_backtest) as spy:
            optimize.walk_forward(
                daily, "600000.SH", [2, 3], [5, 10], None,
                train_years=1.0, test_months=1, metric="sharpe",
                name="*ST某某", list_date="2020-01-02",
            )
        self.assert_forwarded(spy)

    def test_strategy_grid_forwards_name_and_list_date(self):
        daily = synthetic_daily(400)
        with mock.patch.object(lab, "run_backtest", wraps=lab.run_backtest) as spy:
            lab.grid_strategy(
                daily, "600000.SH", "MA双均线",
                [{"fast": 2, "slow": 5}, {"fast": 3, "slow": 10}], None,
                name="*ST某某", list_date="2020-01-02",
            )
        self.assert_forwarded(spy)

    def test_strategy_walk_forward_forwards_name_and_list_date(self):
        daily = synthetic_daily(400)
        with mock.patch.object(lab, "run_backtest", wraps=lab.run_backtest) as spy:
            lab.walk_forward_strategy(
                daily, "600000.SH", "MA双均线",
                [{"fast": 2, "slow": 5}, {"fast": 3, "slow": 10}], None,
                1.0, 1, "sharpe", name="*ST某某", list_date="2020-01-02",
            )
        self.assert_forwarded(spy)

    def test_portfolio_passes_per_code_meta(self):
        daily = synthetic_daily(400)
        meta = {"600000.SH": self.META}
        with mock.patch.object(lab, "load_data", return_value=(daily, None)), \
                mock.patch.object(lab, "walk_forward_strategy", wraps=lab.walk_forward_strategy) as spy:
            lab.portfolio_walk_forward(
                ["600000.SH"], "MA双均线", "3年", 1.0, 1, "sharpe", meta=meta
            )
        self.assert_forwarded(spy)


class SignificanceTests(unittest.TestCase):
    def test_significance_table_shapes(self):
        rng = np.random.default_rng(1)
        n = 30
        detail = pd.DataFrame(
            {
                "排名": np.tile([1, 2, 3], 10),
                "超额T+1": rng.normal(0.01, 0.02, n),
                "超额T+5": rng.normal(0.02, 0.04, n),
                "超额T+20": rng.normal(0.03, 0.08, n),
            }
        )
        table = significance_table(detail)
        self.assertGreater(len(table), 0)
        self.assertIn("p值(block bootstrap)", table.columns)
        self.assertTrue(((table["p值(normal)"].dropna() >= 0) & (table["p值(normal)"].dropna() <= 1)).all())


class FactorICTest(unittest.TestCase):
    def test_compute_ic_on_synthetic_panel(self):
        rng = np.random.default_rng(2)
        dates = pd.bdate_range("2026-01-05", periods=20)
        factors = [
            "momentum_20", "momentum_60", "reversal_5", "volatility_20",
            "volume_ratio", "ma_gap_20", "rsi_14", "macd_hist", "atr_pct",
        ]
        rows = []
        for date in dates:
            for code in range(30):
                row = {"date": date, "code": f"{code:06d}.SZ", "fwd": rng.normal(0, 0.03)}
                for f in factors:
                    row[f] = rng.normal()
                rows.append(row)
        panel = pd.DataFrame(rows)
        result = compute_ic(panel, horizon=5)
        self.assertEqual(len(result.ic_table), len(factors))
        self.assertFalse(result.quantile_table.empty)


if __name__ == "__main__":
    unittest.main()
