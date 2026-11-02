from __future__ import annotations

import time
from datetime import timedelta

import pandas as pd

from .config import CACHE_DIR
from .data import load_trade_cal
from .events import stock_basic
from .tushare_client import get_client

_CAP_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CAP_TTL = 3600.0
_PIT_MEM: dict[tuple[str, int], pd.DataFrame] = {}


def _latest_daily_basic() -> pd.DataFrame:
    key = "latest"
    if key in _CAP_CACHE:
        ts, df = _CAP_CACHE[key]
        if time.time() - ts < _CAP_TTL:
            return df.copy()

    cal = load_trade_cal()
    open_days = cal.loc[cal["is_open"] == 1, "cal_date"].sort_values(ascending=False)
    today = pd.Timestamp.today().normalize()
    open_days = open_days[open_days <= today].head(10)
    for day in open_days:
        df = get_client().query(
            "daily_basic",
            trade_date=pd.Timestamp(day).strftime("%Y%m%d"),
            fields="ts_code,total_mv,circ_mv,pe_ttm",
        )
        if df is not None and not df.empty:
            _CAP_CACHE[key] = (time.time(), df)
            return df.copy()
    return pd.DataFrame()


def latest_size_map() -> dict[str, float]:
    cap = _latest_daily_basic()
    if cap.empty:
        return {}
    cap = cap.copy()
    cap["total_mv"] = pd.to_numeric(cap["total_mv"], errors="coerce")
    return {
        str(code): float(size)
        for code, size in zip(cap["ts_code"], cap["total_mv"])
        if pd.notna(size)
    }


def market_cap_universe(top_n: int = 100) -> tuple[list[str], dict[str, float]]:
    """按最新总市值排序取前 N 只（沪深，剔除北交所），返回代码和市值映射。"""
    basic = stock_basic()
    cap = _latest_daily_basic()
    if cap.empty:
        raise ValueError("daily_basic 返回为空，无法构建市值股票池")
    merged = basic.merge(cap[["ts_code", "total_mv"]], on="ts_code", how="inner")
    merged = merged[merged["ts_code"].str.endswith((".SH", ".SZ"))]
    merged["total_mv"] = pd.to_numeric(merged["total_mv"], errors="coerce")
    merged = merged.dropna(subset=["total_mv"]).sort_values("total_mv", ascending=False)
    merged = merged.head(int(top_n))
    codes = merged["ts_code"].astype(str).tolist()
    sizes = dict(zip(merged["ts_code"], merged["total_mv"].astype(float)))
    return codes, sizes


def _cap_on(date: pd.Timestamp) -> pd.DataFrame:
    key = pd.Timestamp(date).strftime("%Y%m%d")
    if key in _PIT_MEM:
        return _PIT_MEM[key].copy()
    df = get_client().query(
        "daily_basic",
        trade_date=key,
        fields="ts_code,total_mv,circ_mv",
    )
    if df is None:
        df = pd.DataFrame()
    if not df.empty:
        df = df[df["ts_code"].astype(str).str.endswith((".SH", ".SZ"))]
        df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce")
        df = df.dropna(subset=["total_mv"])
    _PIT_MEM[key] = df
    return df.copy()


def pit_market_cap_universe(
    dates: list[pd.Timestamp],
    top_n: int = 100,
) -> tuple[list[str], dict[pd.Timestamp, set[str]], dict[pd.Timestamp, dict[str, float]]]:
    """Point-in-time 市值股票池：每个调仓日按当日总市值选前 N。

    仍只覆盖“今天仍上市”的股票，但避免了“用今天成分回看历史”的最大偏差。
    """
    membership: dict[pd.Timestamp, set[str]] = {}
    sizes: dict[pd.Timestamp, dict[str, float]] = {}
    for raw in dates:
        date = pd.Timestamp(raw).normalize()
        df = _cap_on(date)
        if df.empty:
            continue
        top = df.sort_values("total_mv", ascending=False).head(int(top_n))
        codes = set(top["ts_code"].astype(str))
        membership[date] = codes
        sizes[date] = dict(zip(top["ts_code"].astype(str), top["total_mv"].astype(float)))
    union = sorted(set().union(*membership.values())) if membership else []
    return union, membership, sizes
