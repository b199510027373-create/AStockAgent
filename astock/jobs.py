from __future__ import annotations

import argparse
from datetime import datetime

from .scheduler import _acquire_lock, _release_lock, _run_time, loop, run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="AStockAgent recommendation job runner")
    parser.add_argument("--once", action="store_true", help="run one due job then exit")
    parser.add_argument("--slot", default="close", choices=["close", "premarket", "manual"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

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
