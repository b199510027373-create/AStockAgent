"""兼容旧 notebook 的数据引擎入口。

实际实现已经迁移到 astock.data / astock.tushare_client：
- 自动绕过 Windows 系统代理（Clash 等），避免 502
- 自带 parquet 缓存、复权、交易日历
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from astock.config import TUSHARE_API_URL, TUSHARE_TOKEN
from astock.data import load_daily
from astock.tushare_client import TushareClient

TOKEN = TUSHARE_TOKEN
PROXY_URL = TUSHARE_API_URL

DATA_DIR = Path("data")
CACHE_DAYS = 1


def get_pro() -> TushareClient:
    """返回轻量客户端；支持 client.daily(...) 这类旧写法。"""
    return TushareClient(TOKEN, PROXY_URL)


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_cache_path(ts_code: str) -> str:
    return str(DATA_DIR / f"{ts_code.replace('.', '_')}.csv")


def is_cache_valid(ts_code: str) -> bool:
    path = Path(get_cache_path(ts_code))
    if not path.exists():
        return False
    return path.stat().st_mtime >= (pd.Timestamp.today() - pd.Timedelta(days=CACHE_DAYS)).timestamp()


def fetch_stock_data(
    ts_code: str,
    start_date: str = "20250101",
    end_date: str | None = None,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """拉取日线（不复权），带本地 parquet 缓存。"""
    try:
        df = load_daily(
            ts_code,
            start=start_date,
            end=end_date,
            adj=None,
            refresh=force_refresh,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {ts_code}: {exc}")
        return pd.DataFrame()

    if df.empty:
        print(f"[WARN] {ts_code} 无数据")
        return pd.DataFrame()
    print(f"[OK] {ts_code}: {len(df)} rows")
    return df


def batch_fetch_stocks(
    ts_codes: list[str],
    start_date: str = "20250101",
    end_date: str | None = None,
) -> dict[str, pd.DataFrame]:
    results: dict[str, pd.DataFrame] = {}
    for ts_code in ts_codes:
        df = fetch_stock_data(ts_code, start_date, end_date)
        if not df.empty:
            results[ts_code] = df
    return results


if os.getenv("ASTOCK_VERBOSE"):
    print(f"AStockAgent 数据引擎就绪：{PROXY_URL}")
