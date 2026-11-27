"""backfill JSON 契约与 ledger 入库门槛（plan-20261109-001 / DATA-01）。

覆盖：
* ``backfill_technical`` 写出的批次带 schema_version=2/run_id/data_as_of/
  tradable_from/diagnostics，并能通过 ``validate_recommendation_run``；
* ``ledger.sync_json_dir`` 跳过没有 schema_version 的历史文件（不再补
  ``legacy_*`` run_id 入账），合法批次仍按契约字段入库。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from astock import backfill, contracts, ledger

CODES = ["600519.SH", "000001.SZ", "300750.SZ"]


def _daily(offset: float) -> pd.DataFrame:
    dates = pd.bdate_range("2026-06-01", periods=80)
    close = [10.0 + i * 0.1 + offset for i in range(80)]
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": close,
            "high": [c * 1.02 for c in close],
            "low": [c * 0.98 for c in close],
            "close": close,
            "vol": [1_000_000.0] * 80,
            "amount": [100_000_000.0] * 80,
            # 与 load_many 返回的 enrich 后日线保持同样的技术指标口径，
            # 避免把 NaN 指标带进契约（contracts 拒绝非有限浮点）。
            "ma20": [c * 0.99 for c in close],
            "ma60": [c * 0.98 for c in close],
            "ma120": [c * 0.97 for c in close],
            "rsi14": [55.0] * 80,
            "dif": [0.05] * 80,
            "dea": [0.03] * 80,
            "macd_hist": [0.04] * 80,
            "atr14": [0.3] * 80,
            "vol_ma20": [1_000_000.0] * 80,
        }
    )


def _basic() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": CODES,
            "name": ["贵州茅台", "平安银行", "宁德时代"],
            "industry": ["白酒", "银行", "电池"],
        }
    )


def _run_backfill(
    rec_dir: Path, *, next_open: str | None = "2026-09-14"
) -> tuple[int, str, dict | None]:
    daily = {code: _daily(offset) for offset, code in enumerate(CODES)}
    next_open_ts = None if next_open is None else pd.Timestamp(next_open)
    with (
        patch.object(backfill, "REC_DIR", rec_dir),
        patch.object(backfill, "parse_universe", return_value=CODES),
        patch.object(backfill, "load_many", return_value=daily),
        patch.object(backfill, "stock_basic", return_value=_basic()),
        patch.object(backfill, "next_open_date", return_value=next_open_ts),
    ):
        created, status = backfill.backfill_technical(days=1, top_n=2, candidate_n=3)
    files = sorted(rec_dir.glob("recommend_*_backfill.json"))
    payload = json.loads(files[0].read_text(encoding="utf-8")) if files else None
    return created, status, payload


def _valid_payload(**overrides) -> dict:
    payload = {
        "schema_version": 2,
        "run_id": "rec_contract",
        "generated_at": "2026-01-06T18:00:00",
        "data_as_of": "2026-01-06",
        "tradable_from": "2026-01-07",
        "source": "test",
        "market": "测试批次",
        "params": {},
        "diagnostics": {},
        "table": [],
        "evidence": {},
    }
    payload.update(overrides)
    return payload


class BackfillContractTests(unittest.TestCase):
    def test_backfill_payload_carries_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec_dir = Path(tmp)
            created, _status, payload = _run_backfill(rec_dir)

        assert payload is not None
        self.assertEqual(created, 1)
        self.assertEqual(payload["schema_version"], 2)
        self.assertTrue(payload["run_id"].startswith("backfill_"))
        data_as_of = payload["data_as_of"]
        self.assertEqual(payload["run_id"], f"backfill_{data_as_of.replace('-', '')}")
        self.assertEqual(payload["as_of"], data_as_of)
        self.assertEqual(payload["tradable_from"], "2026-09-14")
        diagnostics = payload["diagnostics"]
        for key in ("generated_at", "data_as_of", "tradable_from", "candidate_n"):
            self.assertIn(key, diagnostics)
        self.assertEqual(diagnostics["data_as_of"], data_as_of)
        self.assertEqual(diagnostics["tradable_from"], "2026-09-14")
        # 契约校验必须直接通过（含 allow_nan=False 的 JSON 安全检查）。
        contracts.validate_recommendation_run(payload, source="backfill_technical")

    def test_backfill_payload_imports_into_ledger_with_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec_dir = Path(tmp)
            _created, _status, payload = _run_backfill(rec_dir)
            assert payload is not None
            with patch.object(ledger, "DB_PATH", Path(tmp) / "ledger.db"):
                imported = ledger.sync_json_dir(rec_dir)
                run = ledger.get_run(payload["run_id"])

        self.assertEqual(imported, 1)
        assert run is not None
        self.assertEqual(run["schema_version"], 2)
        self.assertEqual(run["data_as_of"], payload["data_as_of"])
        self.assertEqual(run["tradable_from"], "2026-09-14")

    def test_missing_next_open_skips_batch_instead_of_guessing(self):
        # 交易日历缺失时不能臆造 data_as_of+1（可能是周末/节假日，DATA-03 会让
        # 批次永远没有跟踪结果）：应告警并跳过，不落盘。
        with tempfile.TemporaryDirectory() as tmp:
            rec_dir = Path(tmp)
            with self.assertLogs("astock.backfill", level="WARNING") as logs:
                created, status, payload = _run_backfill(rec_dir, next_open=None)
            self.assertEqual(created, 0)
            self.assertIsNone(payload)
            self.assertEqual(list(rec_dir.glob("recommend_*_backfill.json")), [])
            self.assertIn("tradable_from", status)

        self.assertTrue(any("tradable_from" in line for line in logs.output))


class SyncJsonDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rec_dir = self.root / "recommendations"
        self.rec_dir.mkdir()
        self.patch = patch.object(ledger, "DB_PATH", self.root / "ledger.db")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_legacy_file_without_schema_version_is_skipped(self):
        legacy = {
            "generated_at": "2026-01-05T18:00:00",
            "as_of": "2026-01-05",
            "table": [],
        }
        (self.rec_dir / "recommend_20260105_legacy.json").write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertLogs("astock.ledger", level="WARNING") as logs:
            imported = ledger.sync_json_dir(self.rec_dir)

        self.assertEqual(imported, 0)
        self.assertEqual(ledger.stats()["runs"], 0)
        self.assertEqual(ledger.get_run("legacy_recommend_20260105_legacy"), None)
        self.assertTrue(any("schema_version" in line for line in logs.output))

    def test_valid_file_still_imports_with_contract_fields(self):
        payload = _valid_payload()
        # 守卫：合法用例本身必须能过 RecommendationRun 契约。
        contracts.validate_recommendation_run(payload, source="test")
        (self.rec_dir / "recommend_20260106_120000.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        imported = ledger.sync_json_dir(self.rec_dir)

        self.assertEqual(imported, 1)
        run = ledger.get_run("rec_contract")
        assert run is not None
        self.assertEqual(run["schema_version"], 2)
        self.assertEqual(run["data_as_of"], "2026-01-06")
        self.assertEqual(run["tradable_from"], "2026-01-07")

    def test_incomplete_v2_file_is_rejected_by_contract(self):
        payload = _valid_payload(run_id="rec_missing_tradable")
        del payload["tradable_from"]
        (self.rec_dir / "recommend_20260107_120000.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertLogs("astock.ledger", level="WARNING") as logs:
            imported = ledger.sync_json_dir(self.rec_dir)

        self.assertEqual(imported, 0)
        self.assertEqual(ledger.stats()["runs"], 0)
        self.assertIsNone(ledger.get_run("rec_missing_tradable"))
        self.assertTrue(any("tradable_from" in line for line in logs.output))

    def test_malformed_v2_file_does_not_abort_remaining_sync(self):
        bad = _valid_payload(run_id="rec_bad_table")
        bad["table"] = {"代码": "600519.SH"}  # 形状不对：应为 list[dict]
        good = _valid_payload(run_id="rec_good")
        for name, payload in (
            ("recommend_20260108_120000.json", bad),
            ("recommend_20260109_120000.json", good),
        ):
            (self.rec_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

        with self.assertLogs("astock.ledger", level="WARNING") as logs:
            imported = ledger.sync_json_dir(self.rec_dir)

        self.assertEqual(imported, 1)
        self.assertEqual(ledger.stats()["runs"], 1)
        self.assertIsNone(ledger.get_run("rec_bad_table"))
        self.assertIsNotNone(ledger.get_run("rec_good"))
        self.assertTrue(any("rec_bad_table" in line or "table" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
