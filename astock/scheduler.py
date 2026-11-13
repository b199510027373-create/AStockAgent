from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from . import ledger
from .config import BASE_DIR
from .data import last_completed_trade_date, load_trade_cal
from .track import refresh_tracking
from .use_cases import run_recommendation

REC_DIR = BASE_DIR / "data" / "recommendations"
REC_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = REC_DIR / "scheduler.log"
# 旧版调度状态：只作为一次性迁移源读取，不再写入（ADR-004）。
STATE_PATH = REC_DIR / "scheduler_state.json"
LOCK_PATH = REC_DIR / "scheduler.lock"
ALERTS_PATH = REC_DIR / "scheduler_alerts.jsonl"
CATCHUP_END_HOUR = 23
RUNNING_STALE_MIN = 180
DEFAULT_HEARTBEAT_THRESHOLD_DAYS = 2
HEARTBEAT_LOOKBACK_DAYS = 60

_LOCK_HANDLE = None
_THREAD = None


def _log(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _run_time() -> tuple[int, int]:
    raw = os.getenv("ASTOCK_RECOMMEND_TIME", "16:40").strip()
    try:
        hour, minute = [int(x) for x in raw.split(":")[:2]]
        return max(0, min(23, hour)), max(0, min(59, minute))
    except Exception:
        return 16, 40


def _due_reference(now: datetime) -> datetime:
    """close 任务尚未到期时，以当天 00:00 作为“最近已完成交易日”的参考。

    last_completed_trade_date 在 15:00 后就会返回当天，但 close 任务 16:40 才到期。
    若不回退参考时刻，15:00–16:40 之间会把当天未到期的任务算作漏跑。
    """
    hour, minute = _run_time()
    if (now.hour, now.minute) < (hour, minute):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now


def _heartbeat_threshold() -> int:
    raw = os.getenv("ASTOCK_HEARTBEAT_THRESHOLD_DAYS", str(DEFAULT_HEARTBEAT_THRESHOLD_DAYS))
    try:
        return max(1, int(raw))
    except Exception:
        return DEFAULT_HEARTBEAT_THRESHOLD_DAYS


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_trading_day(day: pd.Timestamp) -> bool:
    try:
        cal = load_trade_cal()
        rows = cal[cal["cal_date"] == pd.Timestamp(day).normalize()]
        return bool(len(rows)) and int(rows.iloc[0]["is_open"]) == 1
    except Exception:
        return pd.Timestamp(day).weekday() < 5


def _next_run(now: datetime) -> datetime:
    hour, minute = _run_time()
    day = pd.Timestamp(now.date())
    for offset in range(0, 15):
        candidate_day = day + pd.Timedelta(days=offset)
        if not _is_trading_day(candidate_day):
            continue
        candidate = datetime(
            candidate_day.year, candidate_day.month, candidate_day.day, hour, minute
        )
        if candidate > now:
            return candidate
    return now + timedelta(days=1)


def _read_state() -> dict:
    """读取旧版 scheduler_state.json（只读，见 ADR-004 迁移说明）。"""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _job_key(trade_date: pd.Timestamp, slot: str) -> str:
    return f"{pd.Timestamp(trade_date):%Y-%m-%d}:{slot}"


def _recent_trading_days(now: datetime, count: int) -> list[pd.Timestamp]:
    """最近 count 个已收盘交易日，按时间倒序。"""
    reference = last_completed_trade_date(_due_reference(now))
    if reference is None:
        return []
    reference = pd.Timestamp(reference).normalize()
    try:
        cal = load_trade_cal()
        rows = cal[
            (cal["is_open"].astype(int) == 1) & (pd.to_datetime(cal["cal_date"]) <= reference)
        ]
        days = sorted({pd.Timestamp(x).normalize() for x in rows["cal_date"]}, reverse=True)
        if days:
            return days[:count]
    except Exception:
        pass
    days = []
    day = reference
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= pd.Timedelta(days=1)
    return days


def migrate_legacy_state() -> int:
    """把旧 scheduler_state.json 的 jobs 一次性补进 jobs 表（只插不覆盖）。"""
    state = _read_state()
    jobs = state.get("jobs") or {}
    migrated = 0
    for key, job in jobs.items():
        if not isinstance(job, dict) or ledger.get_job(key) is not None:
            continue
        status = str(job.get("status") or "failed")
        if status not in ledger.JOB_STATUSES:
            status = "failed"
        try:
            ledger.save_job(
                key,
                trade_date=job.get("trade_date"),
                slot=job.get("slot") or (key.rsplit(":", 1)[-1] if ":" in key else None),
                status=status,
                attempts=int(job.get("attempts", 0) or 0),
                started_at=job.get("started_at"),
                finished_at=job.get("finished_at"),
                error=job.get("error"),
                late_by_min=job.get("late_by_min"),
                run_id=job.get("run_id"),
                as_of=job.get("as_of"),
            )
            migrated += 1
        except Exception:  # noqa: BLE001 - 单条脏数据不阻断整体迁移
            _log(f"旧状态迁移失败：{key} {traceback.format_exc()}")
    if migrated:
        _log(f"旧调度状态迁移完成：{migrated} 条写入 jobs 表")
    return migrated


def recover_interrupted_jobs(
    now: datetime | None = None, stale_after_min: int = RUNNING_STALE_MIN
) -> list[str]:
    """把超时仍处于 running 的行标记为 failed，避免状态机卡死。"""
    now = now or datetime.now()
    recovered: list[str] = []
    for job in ledger.list_jobs(status="running", limit=500):
        started_raw = job.get("started_at")
        try:
            started = datetime.fromisoformat(started_raw) if started_raw else None
        except Exception:
            started = None
        if started is not None and (now - started).total_seconds() < stale_after_min * 60:
            continue
        reason = (
            f"interrupted: running since {started_raw}"
            if started_raw
            else "interrupted: running without started_at"
        )
        ledger.update_job(
            job["job_key"],
            status="failed",
            attempts=max(1, int(job.get("attempts") or 0)),
            error=reason,
            finished_at=_now_iso(),
        )
        recovered.append(job["job_key"])
        _log(f"恢复中断任务：{job['job_key']} -> failed ({reason})")
    return recovered


def _prepare_ledger(now: datetime | None = None) -> None:
    migrate_legacy_state()
    recover_interrupted_jobs(now)


def sync_due_job(now: datetime | None = None) -> str | None:
    """交易日过 run_time 后，若 close 行缺失则插入 pending，表示“该跑但没跑”。"""
    now = now or datetime.now()
    hour, minute = _run_time()
    today = pd.Timestamp(now.date()).normalize()
    if not _is_trading_day(today):
        return None
    if (now.hour, now.minute) < (hour, minute):
        return None
    trade_date = last_completed_trade_date(now)
    if trade_date is None or pd.Timestamp(trade_date).normalize() != today:
        return None
    key = _job_key(trade_date, "close")
    if ledger.get_job(key) is None:
        ledger.save_job(
            key,
            trade_date=f"{pd.Timestamp(trade_date):%Y-%m-%d}",
            slot="close",
            status="pending",
        )
        _log(f"登记待运行任务：{key}")
    return key


def _alert_signature(reference: str, streak: int, threshold: int) -> str:
    return f"{reference}:{streak}:{threshold}"


def _last_alert_signature() -> str | None:
    """从告警文件读取最后一次心跳签名，保证重启后仍能去重。"""
    try:
        lines = ALERTS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if record.get("kind") != "scheduler_heartbeat":
            continue
        signature = record.get("signature")
        if isinstance(signature, str) and signature:
            return signature
        try:
            return _alert_signature(
                str(record.get("reference_trade_date")),
                int(record.get("streak")),
                int(record.get("threshold_days")),
            )
        except Exception:
            return None
    return None


def heartbeat_check(
    now: datetime | None = None,
    alert: bool = True,
    threshold: int | None = None,
) -> dict[str, Any]:
    """检查最近交易日 close 任务是否连续缺失；达到阈值写告警。"""
    now = now or datetime.now()
    threshold = threshold or _heartbeat_threshold()
    reference = last_completed_trade_date(_due_reference(now))
    if reference is None:
        return {
            "ok": False,
            "initialized": False,
            "reason": "no_trade_date",
            "threshold_days": threshold,
        }
    reference_ts = pd.Timestamp(reference).normalize()

    days = _recent_trading_days(now, HEARTBEAT_LOOKBACK_DAYS)
    missed: list[dict[str, Any]] = []
    last_success: dict[str, Any] | None = None
    records: list[tuple[pd.Timestamp, dict[str, Any] | None]] = []
    for day in days:
        key = _job_key(day, "close")
        job = ledger.get_job(key)
        records.append((day, job))
        status = job["status"] if job else "missing"
        if status == "success":
            last_success = {
                "job_key": key,
                "trade_date": f"{day:%Y-%m-%d}",
                "finished_at": job.get("finished_at") if job else None,
            }
            break
        missed.append(
            {
                "job_key": key,
                "trade_date": f"{day:%Y-%m-%d}",
                "status": status,
                "attempts": int((job or {}).get("attempts") or 0),
            }
        )

    # 空库/新部署：仅有当天刚登记的 pending 行时不算“连续漏跑”。
    # 是否有历史必须查全表，不能只看最近 60 个交易日窗口，否则长期停摆会被误判为“无历史”。
    today = pd.Timestamp(now.date()).normalize()
    today_key = f"{today:%Y-%m-%d}"
    recent_close = ledger.list_jobs(slot="close", limit=2)
    initialized = any(
        (job.get("trade_date") or "") < today_key or job.get("status") != "pending"
        for job in recent_close
    )
    if not initialized:
        _log("心跳检查：尚无调度历史，跳过告警")
        return {
            "ok": True,
            "initialized": False,
            "reason": "no_history",
            "at": _now_iso(),
            "threshold_days": threshold,
            "reference_trade_date": f"{reference_ts:%Y-%m-%d}",
            "streak": 0,
            "missed_days": [],
            "last_success": None,
            "alerted": False,
            "alert_path": None,
        }

    # 以窗口内最早一条 job 为下界，避免把安装前的空白历史算成漏跑；
    # 窗口内没有任何行但全表有历史时，说明已停摆超过整个窗口，streak 取窗口长度。
    window_days = [day for day, job in records if job is not None]
    if window_days:
        earliest = min(window_days)
        missed = [item for item in missed if pd.Timestamp(item["trade_date"]) >= earliest]
    streak = len(missed)
    ok = streak < threshold
    alerted = False
    if not ok and alert:
        signature = _alert_signature(f"{reference_ts:%Y-%m-%d}", streak, threshold)
        if signature != _last_alert_signature():
            record = {
                "kind": "scheduler_heartbeat",
                "at": _now_iso(),
                "reference_trade_date": f"{reference_ts:%Y-%m-%d}",
                "threshold_days": threshold,
                "streak": streak,
                "missed_days": missed,
                "last_success": last_success,
                "signature": signature,
            }
            try:
                ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with ALERTS_PATH.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                alerted = True
            except Exception:  # noqa: BLE001 - 告警写盘失败不能拖垮调度
                _log("心跳告警写盘失败：" + traceback.format_exc())
            _log(
                f"ALERT 心跳异常：连续 {streak} 个交易日无成功 close 任务"
                f"（阈值 {threshold}），最近成功={last_success and last_success['trade_date']}"
            )

    return {
        "ok": ok,
        "initialized": True,
        "at": _now_iso(),
        "threshold_days": threshold,
        "reference_trade_date": f"{reference_ts:%Y-%m-%d}",
        "streak": streak,
        "missed_days": missed,
        "last_success": last_success,
        "alerted": alerted,
        "alert_path": str(ALERTS_PATH) if alerted else None,
    }


def acceptance_report(days: int = 5, now: datetime | None = None) -> dict[str, Any]:
    """连续 N 个交易日 close 任务全 success 的可复现验收报表。"""
    now = now or datetime.now()
    days = max(1, int(days))
    trade_days = sorted(_recent_trading_days(now, days))
    rows: list[dict[str, Any]] = []
    for day in trade_days:
        key = _job_key(day, "close")
        job = ledger.get_job(key)
        rows.append(
            {
                "trade_date": f"{day:%Y-%m-%d}",
                "job_key": key,
                "status": job["status"] if job else "missing",
                "attempts": int((job or {}).get("attempts") or 0),
                "late_by_min": (job or {}).get("late_by_min"),
                "as_of": (job or {}).get("as_of"),
                "finished_at": (job or {}).get("finished_at"),
                "error": (job or {}).get("error"),
            }
        )
    passed = sum(1 for row in rows if row["status"] == "success")
    return {
        "days": days,
        "requested": days,
        "all_success": len(rows) == days and passed == days,
        "passed": passed,
        "rows": rows,
        "generated_at": _now_iso(),
    }


def startup_maintenance(now: datetime | None = None) -> dict[str, Any]:
    """进程启动自检：旧状态迁移、中断恢复、该跑登记、心跳检查。

    任一环节的台账异常都只记录日志，不向调度线程抛出。
    """
    now = now or datetime.now()
    migrated = 0
    try:
        migrated = migrate_legacy_state()
    except Exception:  # noqa: BLE001 - 启动自检不能用异常终止调度线程
        _log("启动自检-迁移失败：" + traceback.format_exc())
    recovered: list[str] = []
    try:
        recovered = recover_interrupted_jobs(now)
    except Exception:  # noqa: BLE001
        _log("启动自检-中断恢复失败：" + traceback.format_exc())
    due: str | None = None
    try:
        due = sync_due_job(now)
    except Exception:  # noqa: BLE001
        _log("启动自检-到期登记失败：" + traceback.format_exc())
    heartbeat: dict[str, Any] = {}
    try:
        heartbeat = heartbeat_check(now, alert=True)
    except Exception:  # noqa: BLE001
        _log("启动自检-心跳检查失败：" + traceback.format_exc())
        heartbeat = {"ok": False, "initialized": False, "error": "heartbeat_failed"}
    _log(
        "启动自检："
        f"迁移={migrated} 恢复={len(recovered)} due={due} "
        f"心跳={'ok' if heartbeat.get('ok') else 'ALERT'}"
    )
    return {
        "migrated": migrated,
        "recovered": recovered,
        "due": due,
        "heartbeat": heartbeat,
    }


def _acquire_lock():
    """Single-process scheduler lock. Returns an open handle or None."""
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        return _LOCK_HANDLE
    handle = None
    try:
        handle = LOCK_PATH.open("a+")
        handle.seek(0)
        if handle.read(1) == "":
            handle.write(" ")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            msvcrt = __import__("msvcrt")

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = __import__("fcntl")

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_HANDLE = handle
        return handle
    except Exception:
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass
        return None


def _release_lock() -> None:
    global _LOCK_HANDLE
    if _LOCK_HANDLE is None:
        return
    try:
        _LOCK_HANDLE.seek(0)
        if os.name == "nt":
            msvcrt = __import__("msvcrt")

            msvcrt.locking(_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = __import__("fcntl")

            fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        _LOCK_HANDLE.close()
    except Exception:
        pass
    _LOCK_HANDLE = None


def run_once(
    force: bool = False,
    slot: str | None = None,
    now: datetime | None = None,
):
    """Run one recommendation job.

    Idempotency key is (last completed trade_date, slot). A premarket run
    (e.g. 08:31 using prior close) therefore cannot consume the 16:40 close slot.
    State machine lives in the ledger jobs table (ADR-004).
    """
    now = now or datetime.now()
    try:
        _prepare_ledger(now)
    except Exception:  # noqa: BLE001 - 台账不可用时跳过本轮，由 loop 重试
        _log("台账预检失败，本轮跳过：" + traceback.format_exc())
        return None
    hour, minute = _run_time()
    slot = slot or ("close" if (now.hour, now.minute) >= (hour, minute) else "premarket")
    trade_date = last_completed_trade_date(now)
    if trade_date is None:
        _log("无法确定最近交易日，跳过")
        return None
    key = _job_key(trade_date, slot)
    scheduled = datetime(trade_date.year, trade_date.month, trade_date.day, hour, minute)
    late_by = max(0.0, (now - scheduled).total_seconds() / 60.0)
    try:
        previous = ledger.get_job(key)
    except Exception:  # noqa: BLE001
        _log("读取任务状态失败，本轮跳过：" + traceback.format_exc())
        return None
    if not force and previous is not None and previous.get("status") == "success":
        _log(f"任务已完成（{key}），跳过")
        return None

    attempts = int((previous or {}).get("attempts") or 0) + 1
    started_at = _now_iso()
    _log(f"开始自动生成每日推荐（{key}，late_by={late_by:.0f}m，attempt={attempts}）")
    try:
        ledger.save_job(
            key,
            trade_date=f"{trade_date:%Y-%m-%d}",
            slot=slot,
            status="running",
            attempts=attempts,
            started_at=started_at,
            late_by_min=round(late_by, 1),
        )
    except Exception:  # noqa: BLE001 - 写 running 失败时不启动推荐，避免状态失真
        _log("写入 running 状态失败，本轮跳过：" + traceback.format_exc())
        return None
    try:
        result = run_recommendation(
            mode="generate",
            universe_text="",
            top_n=5,
            candidate_n=15,
            news_days=7,
            use_llm=True,
        )
        payload = result.payload or {}
        data_as_of = str(
            payload.get("data_as_of") or result.diagnostics.get("data_as_of") or ""
        )
        run_id = result.run_id
        finished_at = _now_iso()
        if result.status == "stale":
            ledger.update_job(
                key,
                status="stale",
                as_of=data_as_of or None,
                run_id=run_id,
                finished_at=finished_at,
                error=result.message,
            )
            _log(f"数据未更新，本次不记成功：{result.message}")
            return None
        if result.status == "failed":
            raise RuntimeError(result.message)

        as_of_ts = pd.Timestamp(data_as_of) if data_as_of else None
        if as_of_ts is None or as_of_ts.normalize() < pd.Timestamp(trade_date):
            message = f"as_of={data_as_of} < expected={trade_date:%Y-%m-%d}"
            ledger.update_job(
                key,
                status="stale",
                as_of=data_as_of or None,
                run_id=run_id,
                finished_at=finished_at,
                error=message,
            )
            _log(f"数据未更新：{message}，本次不记成功")
            return None

        ledger.update_job(
            key,
            status="success",
            as_of=data_as_of,
            run_id=run_id,
            finished_at=finished_at,
            error=None,
        )
        # 推荐成功后顺手结算 T+N 前向收益：横幅不必等用户打开跟踪页
        try:
            _settled, message = refresh_tracking()
            _log(f"跟踪结算：{message}")
        except Exception:  # noqa: BLE001 - 结算失败不影响本次推荐状态
            _log("跟踪结算失败：" + traceback.format_exc())
        _log("完成：" + result.status)
        return result
    except Exception as exc:  # noqa: BLE001
        try:
            ledger.update_job(
                key,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=_now_iso(),
            )
        except Exception:  # noqa: BLE001 - 失败状态也写不进去时仍要留日志
            _log("写入 failed 状态失败：" + traceback.format_exc())
        _log("失败：" + traceback.format_exc())
        return None


def loop(stop_event: threading.Event | None = None) -> None:
    stop = stop_event or threading.Event()
    hour, minute = _run_time()
    _log(f"调度器启动，交易日 {hour:02d}:{minute:02d} 运行；补偿窗口至 {CATCHUP_END_HOUR}:00")
    if stop.is_set():
        return
    try:
        startup_maintenance()
    except Exception:  # noqa: BLE001 - 启动自检异常不能让调度线程退出
        _log("启动自检异常：" + traceback.format_exc())
        stop.wait(600)
    while not stop.is_set():
        try:
            now = datetime.now()
            # 墙钟判断：休眠/唤醒后重新计算，不做 sleep 倒计时减法
            if (
                _is_trading_day(pd.Timestamp(now.date()))
                and (now.hour, now.minute) >= (hour, minute)
                and now.hour <= CATCHUP_END_HOUR
            ):
                key = sync_due_job(now)
                job = ledger.get_job(key) if key else None
                if key and (job is None or job.get("status") != "success"):
                    result = run_once(slot="close", now=now)
                    heartbeat_check(now, alert=True)
                    if result is None:
                        stop.wait(600)
                    continue

            target = _next_run(now)
            _log(f"下次运行：{target:%Y-%m-%d %H:%M}")
            while not stop.is_set():
                now = datetime.now()
                remaining = (target - now).total_seconds()
                if remaining <= 0:
                    break
                stop.wait(min(60.0, remaining))
        except Exception:  # noqa: BLE001 - 单轮异常后等待重试，避免线程静默死亡
            _log("调度循环异常：" + traceback.format_exc())
            stop.wait(600)


def start_background() -> threading.Thread | None:
    global _THREAD
    if os.getenv("ASTOCK_AUTO_RECOMMEND", "1").strip() in {"0", "false", "False"}:
        return None
    if _THREAD is not None and _THREAD.is_alive():
        return _THREAD
    if _acquire_lock() is None:
        _log("已有调度器实例持有锁，本进程不启动自动推荐")
        return None

    def _target() -> None:
        try:
            loop()
        finally:
            _release_lock()

    thread = threading.Thread(target=_target, name="astock-recommend-scheduler", daemon=True)
    _THREAD = thread
    thread.start()
    return thread


if __name__ == "__main__":
    if _acquire_lock() is None:
        print("已有调度器实例在运行，退出。")
        raise SystemExit(1)
    try:
        loop()
    finally:
        _release_lock()
