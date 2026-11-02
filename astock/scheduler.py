from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import BASE_DIR
from .data import last_completed_trade_date, load_trade_cal
from .recommend import StaleDataError, run_daily_recommendation

REC_DIR = BASE_DIR / "data" / "recommendations"
REC_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = REC_DIR / "scheduler.log"
STATE_PATH = REC_DIR / "scheduler_state.json"
LOCK_PATH = REC_DIR / "scheduler.lock"
CATCHUP_END_HOUR = 23

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
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_state(state: dict) -> None:
    try:
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _job_key(trade_date: pd.Timestamp, slot: str) -> str:
    return f"{pd.Timestamp(trade_date):%Y-%m-%d}:{slot}"


def _job_done(state: dict, key: str) -> bool:
    return ((state.get("jobs") or {}).get(key) or {}).get("status") == "success"


def _set_job(state: dict, key: str, **values) -> None:
    jobs = state.setdefault("jobs", {})
    job = jobs.setdefault(key, {})
    job.update(values)


def _acquire_lock():
    """Single-process scheduler lock. Returns an open handle or None."""
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        return _LOCK_HANDLE
    try:
        handle = LOCK_PATH.open("a+")
        handle.seek(0)
        if handle.read(1) == "":
            handle.write(" ")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_HANDLE = handle
        return handle
    except Exception:
        try:
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
            import msvcrt

            msvcrt.locking(_LOCK_HANDLE.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

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
    """
    now = now or datetime.now()
    hour, minute = _run_time()
    slot = slot or ("close" if (now.hour, now.minute) >= (hour, minute) else "premarket")
    trade_date = last_completed_trade_date(now)
    if trade_date is None:
        _log("无法确定最近交易日，跳过")
        return None
    key = _job_key(trade_date, slot)
    hour, minute = _run_time()
    scheduled = datetime(trade_date.year, trade_date.month, trade_date.day, hour, minute)
    late_by = max(0.0, (now - scheduled).total_seconds() / 60.0)
    state = _read_state()
    if not force and _job_done(state, key):
        _log(f"任务已完成（{key}），跳过")
        return None

    _log(f"开始自动生成每日推荐（{key}，late_by={late_by:.0f}m）")
    _set_job(
        state,
        key,
        status="running",
        slot=slot,
        trade_date=f"{trade_date:%Y-%m-%d}",
        late_by_min=round(late_by, 1),
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    _atomic_write_state(state)
    try:
        result = run_daily_recommendation(
            universe_text="",
            top_n=5,
            candidate_n=15,
            news_days=7,
            use_llm=True,
        )
        data_as_of = getattr(result, "data_as_of", "") or getattr(result, "as_of", "")
        as_of_ts = pd.Timestamp(data_as_of) if data_as_of else None
        run_id = getattr(result, "run_id", "")
        if as_of_ts is None or as_of_ts.normalize() < pd.Timestamp(trade_date):
            _set_job(
                state,
                key,
                status="stale",
                as_of=data_as_of,
                run_id=run_id,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                error=f"as_of={data_as_of} < expected={trade_date:%Y-%m-%d}",
            )
            _atomic_write_state(state)
            _log(f"数据未更新：as_of={data_as_of} < 预期 {trade_date:%Y-%m-%d}，本次不记成功")
            return None

        _set_job(
            state,
            key,
            status="success",
            as_of=data_as_of,
            run_id=run_id,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=None,
        )
        state["last_run_date"] = datetime.now().strftime("%Y-%m-%d")
        state["last_status"] = result.status
        state["last_csv"] = result.csv_path
        state["last_json"] = result.json_path
        _atomic_write_state(state)
        _log("完成：" + result.status)
        return result
    except StaleDataError as exc:
        _set_job(
            state,
            key,
            status="stale",
            as_of=None,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            error=str(exc),
        )
        state["last_error"] = f"StaleDataError: {exc}"
        state["last_error_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_state(state)
        _log(f"数据未更新，本次不发布：{exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        attempts = int(((state.get("jobs") or {}).get(key) or {}).get("attempts", 0)) + 1
        _set_job(
            state,
            key,
            status="failed",
            attempts=attempts,
            error=f"{type(exc).__name__}: {exc}",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        state["last_error_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write_state(state)
        _log("失败：" + traceback.format_exc())
        return None


def loop(stop_event: threading.Event | None = None) -> None:
    stop = stop_event or threading.Event()
    hour, minute = _run_time()
    _log(f"调度器启动，交易日 {hour:02d}:{minute:02d} 运行；补偿窗口至 {CATCHUP_END_HOUR}:00")
    while not stop.is_set():
        now = datetime.now()
        # 墙钟判断：休眠/唤醒后重新计算，不做 sleep 倒计时减法
        if (
            _is_trading_day(pd.Timestamp(now.date()))
            and (now.hour, now.minute) >= (hour, minute)
            and now.hour <= CATCHUP_END_HOUR
        ):
            trade_date = last_completed_trade_date(now)
            key = _job_key(trade_date, "close") if trade_date is not None else ""
            state = _read_state()
            if key and not _job_done(state, key):
                result = run_once(slot="close", now=now)
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
