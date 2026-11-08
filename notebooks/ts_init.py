"""旧 notebook 的兼容数据入口。

已迁移到项目根目录的 astock 包：
- 无系统代理的 Tushare 客户端
- parquet 缓存、复权、交易日历
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astock.config import TUSHARE_API_URL, TUSHARE_TOKEN  # noqa: E402
from astock.tushare_client import TushareClient  # noqa: E402

TOKEN = TUSHARE_TOKEN
PROXY_URL = TUSHARE_API_URL
DATA_DIR = "data"


def get_pro() -> TushareClient:
    return TushareClient(TOKEN, PROXY_URL)


def ensure_data_dir() -> None:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)


def fetch_stock_data(
    ts_code: str,
    start_date: str = "20250101",
    end_date: str = "20260601",
) -> pd.DataFrame:
    ensure_data_dir()
    file_path = Path(DATA_DIR) / f"{ts_code.replace('.', '_')}.csv"

    if file_path.exists():
        df = pd.read_csv(file_path)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        print(f"从缓存加载 {ts_code}")
        return df

    print(f"正在拉取 {ts_code}...")
    df = get_pro().daily(
        ts_code=ts_code, start_date=start_date, end_date=end_date
    )
    df.to_csv(file_path, index=False)
    print(f"已保存 {ts_code}")
    return df


print("ts_init 已更新（使用 astock 数据层）")
