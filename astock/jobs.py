from __future__ import annotations

import argparse
import json
from datetime import datetime

from .scheduler import (
    _acquire_lock,
    _release_lock,
    _run_time,
    acceptance_report,
    heartbeat_check,
    loop,
    run_once,
    startup_maintenance,
)


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="AStockAgent recommendation job runner")
    parser.add_argument("--once", action="store_true", help="run one due job then exit")
    parser.add_argument("--slot", default="close", choices=["close", "premarket", "manual"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--heartbeat", action="store_true", help="check job heartbeat and exit")
    parser.add_argument(
        "--report",
        type=int,
        metavar="DAYS",
        help="print the last N trading days acceptance report and exit",
    )
    parser.add_argument(
        "--maintenance", action="store_true", help="migrate legacy state / recover / due / heartbeat"
    )
    args = parser.parse_args()

    if args.heartbeat:
        result = heartbeat_check(alert=False)
        _print(result)
        return_code = 0 if result.get("ok") else 2
        raise SystemExit(return_code)
    if args.report is not None:
        report = acceptance_report(days=args.report)
        _print(report)
        raise SystemExit(0 if report.get("all_success") else 2)
    if args.maintenance:
        _print(startup_maintenance())
        return

    if _acquire_lock() is None:
        print("已有调度器实例在运行，退出。")
        raise SystemExit(1)
    try:
        if args.once:
            hour, minute = _run_time()
            now = datetime.now()
            if args.slot == "close" and (now.hour, now.minute) < (hour, minute):
                print("未到收盘运行时间，跳过。")
                return
            run_once(force=args.force, slot=args.slot)
        else:
            loop()
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
