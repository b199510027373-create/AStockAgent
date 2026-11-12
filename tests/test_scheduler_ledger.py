from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import astock.ledger as ledger
import astock.scheduler as scheduler
from astock import jobs
from astock.use_cases import RunResult


def make_result(
    data_as_of: str = "",
    *,
    status: str = "success",
    message: str = "",
    run_id: str = "run_test",
) -> RunResult:
    payload = {"data_as_of": data_as_of, "run_id": run_id, "csv_path": "/tmp/a.csv",
               "json_path": "/tmp/a.json"}
    return RunResult(run_id, status, payload, {"message": message})


def fake_calendar() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", "2026-12-31")
    return pd.DataFrame({"cal_date": dates, "is_open": [1] * len(dates)})


class FrozenDateTime(datetime):
    """可注入的 datetime.now()，用于驱动 loop() 与 15:30 到期边界。"""

    fixed = datetime(2026, 11, 6, 16, 41)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


def last_completed_by_clock(now: datetime) -> pd.Timestamp:
    """模拟 data.last_completed_trade_date：15:00 后返回当天，否则返回前一天。"""
    return pd.Timestamp("2026-11-06") if now.hour >= 15 else pd.Timestamp("2026-11-05")


class LedgerJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = patch.object(ledger, "DB_PATH", self.root / "test.db")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_status_machine_and_attempts_roundtrip(self):
        ledger.save_job(
            "2026-11-02:close",
            trade_date="2026-11-02",
            slot="close",
            status="pending",
        )
        self.assertEqual(ledger.get_job("2026-11-02:close")["status"], "pending")

        ledger.update_job(
            "2026-11-02:close",
            status="running",
            attempts=1,
            started_at="2026-11-02T16:40:00",
            late_by_min=2.5,
        )
        job = ledger.get_job("2026-11-02:close")
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["attempts"], 1)
        self.assertEqual(job["late_by_min"], 2.5)

        ledger.update_job(
            "2026-11-02:close",
            status="stale",
            as_of="2026-10-30",
            error="data not ready",
            finished_at="2026-11-02T16:41:00",
        )
        job = ledger.get_job("2026-11-02:close")
        self.assertEqual(job["status"], "stale")
        self.assertEqual(job["as_of"], "2026-10-30")

        ledger.update_job("2026-11-02:close", status="failed", error="boom")
        self.assertEqual(ledger.get_job("2026-11-02:close")["status"], "failed")

        ledger.update_job("2026-11-02:close", status="success", error=None, run_id="rec_1")
        self.assertEqual(ledger.get_job("2026-11-02:close")["status"], "success")
        self.assertEqual(ledger.latest_success_job("close")["run_id"], "rec_1")
        self.assertEqual(len(ledger.list_jobs(status="success")), 1)
        self.assertEqual(len(ledger.list_jobs(status="failed")), 0)

    def test_invalid_status_and_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            ledger.save_job("2026-11-02:close", status="unknown")
        with self.assertRaises(ValueError):
            ledger.list_jobs(status="unknown")
        ledger.save_job("2026-11-02:close", status="pending")
        with self.assertRaises(ValueError):
            ledger.update_job("2026-11-02:close", nope=1)
        with self.assertRaises(ValueError):
            ledger.update_job("2026-11-02:close", status="unknown")

    def test_old_jobs_table_gets_as_of_column(self):
        db = self.root / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE jobs (
                   job_key TEXT PRIMARY KEY, trade_date TEXT, slot TEXT, status TEXT,
                   attempts INTEGER DEFAULT 0, started_at TEXT, finished_at TEXT,
                   error TEXT, late_by_min REAL, run_id TEXT)"""
        )
        conn.execute(
            "INSERT INTO jobs (job_key, trade_date, slot, status) "
            "VALUES ('2026-11-02:close', '2026-11-02', 'close', 'success')"
        )
        conn.commit()
        conn.close()

        with patch.object(ledger, "DB_PATH", db):
            job = ledger.get_job("2026-11-02:close")
            self.assertIsNone(job["as_of"])
            with ledger.transaction() as check:
                columns = {row["name"] for row in check.execute("PRAGMA table_info(jobs)")}
            self.assertIn("as_of", columns)


class SchedulerLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cal_patch = patch.object(scheduler, "load_trade_cal", return_value=fake_calendar())
        self.patches = [
            patch.object(ledger, "DB_PATH", self.root / "test.db"),
            patch.object(scheduler, "STATE_PATH", self.root / "state.json"),
            patch.object(scheduler, "LOG_PATH", self.root / "scheduler.log"),
            patch.object(scheduler, "ALERTS_PATH", self.root / "scheduler_alerts.jsonl"),
            self.cal_patch,
        ]
        for p in self.patches:
            p.start()
        self.refresh_patch = patch.object(
            scheduler, "refresh_tracking", return_value=(0, "skip")
        )
        self.refresh_mock = self.refresh_patch.start()

    def tearDown(self):
        self.refresh_patch.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _success(self, day: str, *, run_id: str = "run_test") -> None:
        ledger.save_job(
            f"{day}:close",
            trade_date=day,
            slot="close",
            status="success",
            attempts=1,
            as_of=day,
            run_id=run_id,
            late_by_min=0.0,
            finished_at=f"{day}T16:41:00",
        )

    def test_legacy_state_migrates_once_without_overwriting_db(self):
        scheduler.STATE_PATH.write_text(
            json.dumps(
                {
                    "jobs": {
                        "2026-11-02:close": {
                            "trade_date": "2026-11-02",
                            "slot": "close",
                            "status": "success",
                            "attempts": 1,
                            "late_by_min": 0.0,
                        },
                        "2026-11-03:close": {
                            "trade_date": "2026-11-03",
                            "slot": "close",
                            "status": "success",
                            "run_id": "from_json",
                        },
                        "2026-11-04:close": {"status": "weird"},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        ledger.save_job(
            "2026-11-03:close",
            trade_date="2026-11-03",
            slot="close",
            status="failed",
            run_id="from_db",
        )

        original = scheduler.STATE_PATH.read_text(encoding="utf-8")
        self.assertEqual(scheduler.migrate_legacy_state(), 2)
        self.assertEqual(scheduler.migrate_legacy_state(), 0)
        self.assertEqual(scheduler.STATE_PATH.read_text(encoding="utf-8"), original)

        self.assertEqual(ledger.get_job("2026-11-02:close")["status"], "success")
        untouched = ledger.get_job("2026-11-03:close")
        self.assertEqual(untouched["status"], "failed")
        self.assertEqual(untouched["run_id"], "from_db")
        self.assertEqual(ledger.get_job("2026-11-04:close")["status"], "failed")

    def test_recover_interrupted_running_job(self):
        now = datetime(2026, 11, 6, 17, 0)
        ledger.save_job(
            "2026-11-05:close",
            trade_date="2026-11-05",
            slot="close",
            status="running",
            attempts=1,
            started_at=(now - timedelta(minutes=240)).isoformat(timespec="seconds"),
        )
        ledger.save_job(
            "2026-11-06:close",
            trade_date="2026-11-06",
            slot="close",
            status="running",
            attempts=1,
            started_at=(now - timedelta(minutes=5)).isoformat(timespec="seconds"),
        )
        recovered = scheduler.recover_interrupted_jobs(now)
        self.assertEqual(recovered, ["2026-11-05:close"])
        stale = ledger.get_job("2026-11-05:close")
        self.assertEqual(stale["status"], "failed")
        self.assertIn("interrupted", stale["error"])
        self.assertGreaterEqual(stale["attempts"], 1)
        self.assertEqual(ledger.get_job("2026-11-06:close")["status"], "running")

    def test_sync_due_job_marks_missing_pending_only_after_run_time(self):
        day = pd.Timestamp("2026-11-06")
        with patch.object(scheduler, "last_completed_trade_date", return_value=day):
            self.assertIsNone(scheduler.sync_due_job(datetime(2026, 11, 6, 10, 0)))
            self.assertIsNone(ledger.get_job("2026-11-06:close"))
            self.assertEqual(
                scheduler.sync_due_job(datetime(2026, 11, 6, 16, 40)), "2026-11-06:close"
            )
        job = ledger.get_job("2026-11-06:close")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["slot"], "close")

        self._success("2026-11-06")
        with patch.object(scheduler, "last_completed_trade_date", return_value=day):
            self.assertEqual(
                scheduler.sync_due_job(datetime(2026, 11, 6, 17, 0)), "2026-11-06:close"
            )
        self.assertEqual(ledger.get_job("2026-11-06:close")["status"], "success")

    def test_heartbeat_alerts_only_after_two_missing_trading_days(self):
        reference = pd.Timestamp("2026-11-06")
        with patch.object(scheduler, "last_completed_trade_date", return_value=reference):
            self._success("2026-11-04")
            self._success("2026-11-05")
            one_day = scheduler.heartbeat_check(datetime(2026, 11, 6, 17, 0))
            self.assertTrue(one_day["ok"])
            self.assertEqual(one_day["streak"], 1)
            self.assertFalse(one_day["alerted"])
            self.assertFalse(scheduler.ALERTS_PATH.exists())

            ledger.update_job("2026-11-05:close", status="failed", error="boom")
            two_days = scheduler.heartbeat_check(datetime(2026, 11, 6, 17, 5))
            self.assertFalse(two_days["ok"])
            self.assertEqual(two_days["streak"], 2)
            self.assertTrue(two_days["alerted"])
            self.assertEqual(two_days["threshold_days"], 2)
            self.assertEqual(two_days["missed_days"][0]["trade_date"], "2026-11-06")
            self.assertEqual(two_days["missed_days"][1]["trade_date"], "2026-11-05")

            again = scheduler.heartbeat_check(datetime(2026, 11, 6, 17, 10))
            self.assertFalse(again["ok"])
            self.assertFalse(again["alerted"])

        lines = scheduler.ALERTS_PATH.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["kind"], "scheduler_heartbeat")
        self.assertEqual(record["threshold_days"], 2)
        self.assertEqual(record["streak"], 2)

    def test_heartbeat_threshold_is_configurable(self):
        reference = pd.Timestamp("2026-11-06")
        with patch.object(scheduler, "last_completed_trade_date", return_value=reference), patch.dict(
            os.environ, {"ASTOCK_HEARTBEAT_THRESHOLD_DAYS": "3"}
        ):
            self._success("2026-11-04")
            result = scheduler.heartbeat_check(datetime(2026, 11, 6, 17, 0))
        self.assertTrue(result["ok"])
        self.assertEqual(result["streak"], 2)
        self.assertEqual(result["threshold_days"], 3)
        self.assertFalse(scheduler.ALERTS_PATH.exists())

    def test_acceptance_report_reproduces_five_successful_days(self):
        days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
        for day in days:
            self._success(day)
        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            report = scheduler.acceptance_report(5, datetime(2026, 11, 6, 17, 0))
        self.assertTrue(report["all_success"])
        self.assertEqual(report["passed"], 5)
        self.assertEqual([row["trade_date"] for row in report["rows"]], days)

        ledger.update_job("2026-11-04:close", status="stale", error="data not ready")
        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            report = scheduler.acceptance_report(5, datetime(2026, 11, 6, 17, 30))
        self.assertFalse(report["all_success"])
        self.assertEqual(report["passed"], 4)
        self.assertEqual(report["rows"][2]["status"], "stale")

    def test_five_day_acceptance_is_reproducible_through_run_once(self):
        days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
        for day in days:
            with patch.object(
                scheduler, "last_completed_trade_date", return_value=pd.Timestamp(day)
            ), patch.object(
                scheduler, "run_recommendation", return_value=make_result(day)
            ):
                result = scheduler.run_once(slot="close", now=datetime.fromisoformat(f"{day}T16:40:00"))
            self.assertIsNotNone(result)
            job = ledger.get_job(f"{day}:close")
            self.assertEqual(job["status"], "success")
            self.assertEqual(job["as_of"], day)
            self.assertEqual(job["attempts"], 1)
            self.assertEqual(job["late_by_min"], 0.0)

        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            report = scheduler.acceptance_report(5, datetime(2026, 11, 6, 18, 0))
        self.assertTrue(report["all_success"])
        self.assertEqual(report["passed"], 5)
        self.assertEqual(self.refresh_mock.call_count, 5)
        # 新路径只写 jobs 表；旧 JSON 不应被创建或改写（ADR-004）。
        self.assertFalse(scheduler.STATE_PATH.exists())

    def test_startup_maintenance_marks_due_job_without_empty_history_alert(self):
        reference = pd.Timestamp("2026-11-06")
        with patch.object(scheduler, "last_completed_trade_date", return_value=reference):
            result = scheduler.startup_maintenance(datetime(2026, 11, 6, 16, 45))
        self.assertEqual(result["due"], "2026-11-06:close")
        self.assertEqual(ledger.get_job("2026-11-06:close")["status"], "pending")
        heartbeat = result["heartbeat"]
        self.assertTrue(heartbeat["ok"])
        self.assertFalse(heartbeat["initialized"])
        self.assertEqual(heartbeat["streak"], 0)
        self.assertEqual(heartbeat["missed_days"], [])
        self.assertFalse(heartbeat["alerted"])
        self.assertFalse(scheduler.ALERTS_PATH.exists())

    def test_report_at_1530_excludes_not_yet_due_today(self):
        days = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
        for day in days:
            self._success(day)
        with patch.object(
            scheduler, "last_completed_trade_date", side_effect=last_completed_by_clock
        ):
            report = scheduler.acceptance_report(5, datetime(2026, 11, 6, 15, 30))
        self.assertTrue(report["all_success"])
        self.assertEqual([row["trade_date"] for row in report["rows"]], days)

    def test_heartbeat_at_1530_does_not_count_today(self):
        self._success("2026-11-03")
        self._success("2026-11-04")
        ledger.save_job(
            "2026-11-05:close",
            trade_date="2026-11-05",
            slot="close",
            status="failed",
            error="boom",
        )
        with patch.object(
            scheduler, "last_completed_trade_date", side_effect=last_completed_by_clock
        ):
            result = scheduler.heartbeat_check(datetime(2026, 11, 6, 15, 30))
        self.assertTrue(result["ok"])
        self.assertTrue(result["initialized"])
        self.assertEqual(result["reference_trade_date"], "2026-11-05")
        self.assertEqual(result["streak"], 1)
        self.assertFalse(result["alerted"])
        self.assertFalse(scheduler.ALERTS_PATH.exists())

    def test_heartbeat_dedup_uses_persisted_alert_file(self):
        self._success("2026-11-04")
        ledger.save_job(
            "2026-11-05:close",
            trade_date="2026-11-05",
            slot="close",
            status="failed",
            error="boom",
        )
        scheduler.ALERTS_PATH.write_text(
            json.dumps(
                {
                    "kind": "scheduler_heartbeat",
                    "reference_trade_date": "2026-11-06",
                    "threshold_days": 2,
                    "streak": 2,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            result = scheduler.heartbeat_check(datetime(2026, 11, 6, 17, 0))
        self.assertFalse(result["alerted"])
        lines = scheduler.ALERTS_PATH.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_run_once_survives_ledger_write_failure(self):
        with patch.object(
            ledger, "save_job", side_effect=sqlite3.OperationalError("database is locked")
        ), patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            self.assertIsNone(
                scheduler.run_once(slot="close", now=datetime(2026, 11, 6, 16, 41))
            )

    def test_startup_maintenance_survives_ledger_write_failure(self):
        with patch.object(
            ledger, "save_job", side_effect=sqlite3.OperationalError("database is locked")
        ), patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            result = scheduler.startup_maintenance(datetime(2026, 11, 6, 16, 45))
        self.assertIsNone(result["due"])
        self.assertTrue(result["heartbeat"]["ok"])

    def test_loop_runs_due_job_once_and_records_success(self):
        stop = threading.Event()
        calls: list[dict] = []

        def fake_run(**kwargs):
            calls.append(kwargs)
            stop.set()
            return make_result("2026-11-06")

        FrozenDateTime.fixed = datetime(2026, 11, 6, 16, 41)
        with patch.object(scheduler, "datetime", FrozenDateTime), patch.object(
            scheduler, "_run_time", return_value=(16, 40)
        ), patch.object(scheduler, "_is_trading_day", return_value=True), patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ), patch.object(scheduler, "run_recommendation", side_effect=fake_run):
            scheduler.loop(stop)
        self.assertEqual(len(calls), 1)
        job = ledger.get_job("2026-11-06:close")
        self.assertEqual(job["status"], "success")
        self.assertEqual(job["attempts"], 1)

    def test_loop_waits_600_after_stale_and_does_not_rerun(self):
        stop = threading.Event()
        waits: list[float | None] = []
        calls: list[dict] = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            stop.set()
            return True

        def fake_run(**kwargs):
            calls.append(kwargs)
            return make_result("2026-11-05", status="stale", message="not ready")

        FrozenDateTime.fixed = datetime(2026, 11, 6, 16, 41)
        with patch.object(stop, "wait", side_effect=fake_wait), patch.object(
            scheduler, "datetime", FrozenDateTime
        ), patch.object(scheduler, "_run_time", return_value=(16, 40)), patch.object(
            scheduler, "_is_trading_day", return_value=True
        ), patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ), patch.object(scheduler, "run_recommendation", side_effect=fake_run):
            scheduler.loop(stop)
        self.assertEqual(len(calls), 1)
        self.assertIn(600, waits)
        self.assertEqual(ledger.get_job("2026-11-06:close")["status"], "stale")

    def test_loop_survives_startup_failure(self):
        stop = threading.Event()
        waits: list[float | None] = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            stop.set()
            return True

        FrozenDateTime.fixed = datetime(2026, 11, 6, 16, 41)
        with patch.object(
            scheduler, "startup_maintenance", side_effect=RuntimeError("db locked")
        ), patch.object(stop, "wait", side_effect=fake_wait), patch.object(
            scheduler, "datetime", FrozenDateTime
        ):
            scheduler.loop(stop)
        self.assertEqual(waits, [600])


class JobsCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(ledger, "DB_PATH", root / "test.db"),
            patch.object(scheduler, "STATE_PATH", root / "state.json"),
            patch.object(scheduler, "LOG_PATH", root / "scheduler.log"),
            patch.object(scheduler, "ALERTS_PATH", root / "scheduler_alerts.jsonl"),
            patch.object(scheduler, "load_trade_cal", return_value=fake_calendar()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    @staticmethod
    def _success(day: str) -> None:
        ledger.save_job(
            f"{day}:close",
            trade_date=day,
            slot="close",
            status="success",
            attempts=1,
            as_of=day,
            finished_at=f"{day}T16:41:00",
        )

    def _run_cli(self, *argv: str) -> tuple[int, str]:
        out = StringIO()
        with patch.object(sys, "argv", ["astock.jobs", *argv]), redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                jobs.main()
        return int(ctx.exception.code), out.getvalue()

    def test_report_cli_returns_zero_for_five_successful_days(self):
        for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"):
            self._success(day)
        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            code, output = self._run_cli("--report", "5")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["all_success"])
        self.assertEqual(payload["passed"], 5)

    def test_heartbeat_cli_returns_two_when_threshold_is_hit(self):
        for day in ("2026-11-02", "2026-11-03", "2026-11-04"):
            self._success(day)
        with patch.object(
            scheduler, "last_completed_trade_date", return_value=pd.Timestamp("2026-11-06")
        ):
            code, output = self._run_cli("--heartbeat")
        self.assertEqual(code, 2)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["streak"], 2)

    def test_report_at_1530_returns_zero_for_completed_days(self):
        days = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
        for day in days:
            self._success(day)
        FrozenDateTime.fixed = datetime(2026, 11, 6, 15, 30)
        with patch.object(scheduler, "datetime", FrozenDateTime), patch.object(
            scheduler, "last_completed_trade_date", side_effect=last_completed_by_clock
        ):
            code, output = self._run_cli("--report", "5")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["all_success"])

    def test_heartbeat_cli_at_1530_returns_zero_when_only_yesterday_failed(self):
        self._success("2026-11-03")
        self._success("2026-11-04")
        ledger.save_job(
            "2026-11-05:close",
            trade_date="2026-11-05",
            slot="close",
            status="failed",
            error="boom",
        )
        FrozenDateTime.fixed = datetime(2026, 11, 6, 15, 30)
        with patch.object(scheduler, "datetime", FrozenDateTime), patch.object(
            scheduler, "last_completed_trade_date", side_effect=last_completed_by_clock
        ):
            code, output = self._run_cli("--heartbeat")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["reference_trade_date"], "2026-11-05")
        self.assertEqual(payload["streak"], 1)


if __name__ == "__main__":
    unittest.main()
