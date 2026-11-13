from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from datetime import time as clock_time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import indicators as ta
from .config import CACHE_DIR, DEFAULT_START
from .contracts import validate_bars
from .tushare_client import TushareClient, TushareError, get_client

DAILY_DIR = CACHE_DIR / "daily"
DAILY_DIR.mkdir(parents=True, exist_ok=True)
CAL_PATH = CACHE_DIR / "trade_cal.parquet"

PRICE_COLS = ["open", "high", "low", "close", "pre_close"]
RAW_COLS = [
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
]

_CAL_CACHE: dict[tuple[str, str], pd.DataFrame] = {}
_CAL_LOCK = threading.RLock()
_DAILY_MEM: dict[tuple[str, str, str], pd.DataFrame] = {}

MINUTE_FREQ_MAP = {
    "1min": "1MIN",
    "5min": "5MIN",
    "15min": "15MIN",
    "30min": "30MIN",
    "60min": "60MIN",
}
_MINUTE_MEM: dict[tuple[str, str, int, str], tuple[float, pd.DataFrame]] = {}
_MINUTE_TTL = 30.0
_ADJ_MEM: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
_ADJ_TTL = 3600.0


def _fmt(value: str | pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def _atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _atomic_text(text: str, path: Path) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _cache_put(cache: dict, key, value, maxsize: int = 512) -> None:
    """Small bounded module cache; evict oldest insertion when full."""
    if len(cache) >= maxsize and key not in cache:
        try:
            cache.pop(next(iter(cache)))
        except StopIteration:
            pass
    cache[key] = value


def last_completed_trade_date(now: str | pd.Timestamp | None = None) -> pd.Timestamp | None:
    """最近一个“已完成收盘”的交易日。

    盘前（15:00 前）即使今天是交易日，也只能用上一交易日的数据；
    盘后/收盘后返回今天（若今天开市）。
    """
    now_ts = pd.Timestamp.now() if now is None else pd.Timestamp(now)
    cal = load_trade_cal()
    open_days = cal.loc[cal["is_open"] == 1, "cal_date"].sort_values()
    today = now_ts.normalize()
    if now_ts.weekday() < 5 and now_ts.hour < 15:
        open_days = open_days[open_days < today]
    else:
        open_days = open_days[open_days <= today]
    return open_days.max() if len(open_days) else None


def next_open_date(day: str | pd.Timestamp) -> pd.Timestamp | None:
    cal = load_trade_cal()
    start = pd.Timestamp(day).normalize()
    future = cal.loc[(cal["is_open"] == 1) & (cal["cal_date"] > start), "cal_date"]
    return future.min() if len(future) else None


def _today() -> pd.Timestamp:
    return pd.Timestamp.today().normalize()


def _load_trade_cal_impl(
    start: str = "20150101", end: str | None = None, refresh: bool = False
) -> pd.DataFrame:
    """交易日历（SSE），本地缓存；返回 cal_date / is_open / pretrade_date。"""
    key = (start, end or "auto")
    if key in _CAL_CACHE and not refresh:
        return _CAL_CACHE[key].copy()

    end = end or _fmt(_today() + timedelta(days=370))
    client = get_client()

    if CAL_PATH.exists() and not refresh:
        try:
            cal = pd.read_parquet(CAL_PATH)
            cal["cal_date"] = pd.to_datetime(cal["cal_date"])
        except Exception:
            cal = pd.DataFrame()
        if not cal.empty:
            last = cal["cal_date"].max()
            if last < pd.Timestamp(end):
                missing = client.query(
                    "trade_cal",
                    exchange="SSE",
                    start_date=_fmt(last + timedelta(days=1)),
                    end_date=end,
                )
                if not missing.empty:
                    missing["cal_date"] = pd.to_datetime(missing["cal_date"])
                    cal = pd.concat([cal, missing], ignore_index=True)
        else:
            cal = client.query(
                "trade_cal", exchange="SSE", start_date=start, end_date=end
            )
            if cal.empty:
                raise TushareError("trade_cal 返回为空，请检查 token/代理")
            cal["cal_date"] = pd.to_datetime(cal["cal_date"])
    else:
        cal = client.query(
            "trade_cal", exchange="SSE", start_date=start, end_date=end
        )
        if cal.empty:
            raise TushareError("trade_cal 返回为空，请检查 token/代理")
        cal["cal_date"] = pd.to_datetime(cal["cal_date"])

    cal = (
        cal.drop_duplicates("cal_date")
        .sort_values("cal_date")
        .reset_index(drop=True)
    )
    cal["is_open"] = pd.to_numeric(cal["is_open"], errors="coerce").fillna(0).astype(int)
    _atomic_parquet(cal, CAL_PATH)
    _CAL_CACHE[key] = cal
    begin = pd.Timestamp(start)
    return cal[cal["cal_date"] >= begin].copy()


def load_trade_cal(
    start: str = "20150101", end: str | None = None, refresh: bool = False
) -> pd.DataFrame:
    # 多个加载线程会同时调用；日历只允许一个线程写缓存
    with _CAL_LOCK:
        return _load_trade_cal_impl(start=start, end=end, refresh=refresh)


def last_open_date(end: str | pd.Timestamp) -> pd.Timestamp | None:
    cal = load_trade_cal()
    end_ts = pd.Timestamp(end)
    open_days = cal.loc[cal["is_open"] == 1, "cal_date"]
    valid = open_days[open_days <= end_ts]
    return valid.max() if len(valid) else None


def non_trading_days(start: str | pd.Timestamp, end: str | pd.Timestamp) -> list[str]:
    """区间内的工作日休市日（周末由 Plotly rangebreaks 的 bounds 处理）。"""
    cal = load_trade_cal()
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    closed = cal[
        (cal["cal_date"] >= start_ts)
        & (cal["cal_date"] <= end_ts)
        & (cal["is_open"] == 0)
    ]["cal_date"]
    return [d.strftime("%Y-%m-%d") for d in closed if d.weekday() < 5]


def _normalize_adj(adj: str | None) -> str | None:
    """复权口径归一：``qfq``/``hfq``/``None``（原始价）。

    未知口径显式报错，绝不静默按前复权处理（评审 P0：raw 切换失效的根因）。
    """
    if adj in (None, ""):
        return None
    if adj not in ("qfq", "hfq"):
        raise ValueError(f"未知复权方式: {adj!r}（只支持 qfq/hfq/None）")
    return adj


def _cache_paths(ts_code: str, adj: str | None) -> tuple[Path, Path]:
    # qfq 口径 v3：保留原始价列（raw_*），用于涨跌停/交易规则判断
    suffix = (adj or "raw") + ("_v3" if adj else "")
    stem = f"{ts_code.replace('.', '_')}_{suffix}"
    return DAILY_DIR / f"{stem}.parquet", DAILY_DIR / f"{stem}.json"


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fetch_daily(
    client: TushareClient,
    ts_code: str,
    start: str,
    end: str,
    adj: str | None,
) -> pd.DataFrame:
    raw = client.query(
        "daily", ts_code=ts_code, start_date=start, end_date=end
    )
    if raw.empty:
        return pd.DataFrame()
    # 供应商列漂移（缺 ts_code/trade_date/OHLC/vol/amount）在这里显式报错，
    # 不再让 KeyError 被上层 except 吞成"没有行情"（评审 D18）。
    raw = validate_bars(raw, source=f"{ts_code} daily")
    raw = raw.sort_values("trade_date").reset_index(drop=True)

    if adj:
        fct = client.query(
            "adj_factor", ts_code=ts_code, start_date=start, end_date=end
        )
        if fct.empty:
            raise TushareError(f"{ts_code} 复权因子为空，无法计算 {adj}")
        fct["trade_date"] = pd.to_datetime(fct["trade_date"])
        fct = (
            fct.sort_values("trade_date")
            .drop_duplicates("trade_date")
            .reset_index(drop=True)
        )
        merged = raw.merge(
            fct[["trade_date", "adj_factor"]], on="trade_date", how="left"
        )
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
        # 保留原始价：涨跌停/ST/新股规则必须用交易所原始价判断
        for col in PRICE_COLS:
            if col in merged.columns:
                merged[f"raw_{col}"] = merged[col].astype(float)
        # 口径基准（ADR-008）：
        # - qfq：以区间内最新复权因子为基准（可能对应今天，即使今天日线尚未更新）
        # - hfq：以原始因子 1.0 为基准，价格随因子累乘放大
        base = float(fct["adj_factor"].iloc[-1]) if adj == "qfq" else 1.0
        ratio = merged["adj_factor"] / base if base else 1.0
        for col in PRICE_COLS:
            if col in merged.columns:
                merged[col] = merged[col].astype(float) * ratio
        merged["change"] = merged["close"] - merged["pre_close"]
        merged["pct_chg"] = merged["close"] / merged["pre_close"].replace(0, np.nan) * 100 - 100
        raw = merged

    keep = [c for c in RAW_COLS if c in raw.columns]
    keep += [f"raw_{c}" for c in PRICE_COLS if f"raw_{c}" in raw.columns]
    if adj and "adj_factor" in raw.columns:
        keep.append("adj_factor")
    return validate_bars(
        raw[keep].reset_index(drop=True),
        require_adj=bool(adj),
        source=f"{ts_code} daily",
    )


def load_daily(
    ts_code: str,
    start: str | None = None,
    end: str | None = None,
    adj: str | None = "qfq",
    refresh: bool = False,
    client: TushareClient | None = None,
) -> pd.DataFrame:
    """加载日线，默认前复权。

    - ``adj="qfq"``（默认）：以区间最新复权因子为基准做前复权；
    - ``adj="hfq"``：以原始因子 1.0 为基准做后复权；
    - ``adj=None``：不复权（原始成交价），涨跌停/成交异常判断用。
    - 本地 parquet 缓存，按代码/复权方式分文件
    - 当天已更新过则直接用缓存；否则增量重拉
    - 返回按 trade_date 升序、DatetimeIndex 之外的普通列
    """
    ts_code = ts_code.strip().upper()
    if not ts_code:
        return pd.DataFrame()
    adj = _normalize_adj(adj)

    end = end or _fmt(_today())
    start = start or DEFAULT_START
    end_eff = last_open_date(end) or pd.Timestamp(end)
    start_eff = pd.Timestamp(start)

    mem_key = (ts_code, adj or "raw", f"{_fmt(start_eff)}-{_fmt(end_eff)}")
    if mem_key in _DAILY_MEM and not refresh:
        return _DAILY_MEM[mem_key].copy()

    client = client or get_client()
    path, meta_path = _cache_paths(ts_code, adj)
    meta = _read_meta(meta_path)
    cached: pd.DataFrame | None = None
    cache_ok = False

    if path.exists() and not refresh:
        try:
            cached = pd.read_parquet(path)
        except Exception:
            # 文件损坏/半写：当作无缓存重拉（不算契约问题）
            cached = None
        if cached is not None:
            # 旧缓存缺列/类型漂移必须显式报错，不再静默重拉掩盖问题（D18）
            cached = validate_bars(
                cached,
                require_adj=bool(adj),
                allow_empty=True,
                source=f"load_daily 缓存({ts_code})",
            )
        if cached is not None and not cached.empty:
            fetched_today = meta.get("fetched_at") == _today().strftime("%Y-%m-%d")
            covers = (
                cached["trade_date"].min() <= start_eff
                and cached["trade_date"].max() >= end_eff
            )
            cache_ok = fetched_today and covers

    if cache_ok and cached is not None:
        result = cached[
            (cached["trade_date"] >= start_eff)
            & (cached["trade_date"] <= pd.Timestamp(end))
        ].copy()
    else:
        result = _fetch_daily(client, ts_code, _fmt(start_eff), _fmt(end_eff), adj)
        if result.empty:
            raise TushareError(f"{ts_code} 无行情数据（区间 {_fmt(start_eff)}-{_fmt(end_eff)}）")
        _atomic_parquet(result, path)
        base_factor = None
        if "adj_factor" in result.columns and len(result):
            try:
                base_factor = float(result["adj_factor"].iloc[-1])
            except Exception:
                base_factor = None
        _atomic_text(
            json.dumps(
                {
                    "ts_code": ts_code,
                    "adj": adj,
                    "base_factor": base_factor,
                    "start": _fmt(start_eff),
                    "end": _fmt(end_eff),
                    "rows": int(len(result)),
                    "fetched_at": _today().strftime("%Y-%m-%d"),
                },
                ensure_ascii=False,
            ),
            meta_path,
        )

    result = (
        result.sort_values("trade_date")
        .drop_duplicates("trade_date")
        .reset_index(drop=True)
    )
    # 缓存与增量两条路径统一过契约：旧 parquet 缺列时显式报错，而不是返回残表。
    result = validate_bars(
        result,
        require_adj=bool(adj),
        allow_empty=True,
        source=f"load_daily({ts_code})",
    )
    _cache_put(_DAILY_MEM, mem_key, result)
    return result.copy()


def load_index_daily(
    ts_code: str = "000300.SH",
    start: str | None = None,
    end: str | None = None,
    client: TushareClient | None = None,
) -> pd.DataFrame:
    """指数日线（基准用），不复权，带简单缓存。"""
    client = client or get_client()
    start = start or DEFAULT_START
    end = end or _fmt(_today())
    path = DAILY_DIR / f"{ts_code.replace('.', '_')}_index.parquet"
    meta_path = path.with_suffix(".json")
    meta = _read_meta(meta_path)

    if path.exists():
        try:
            cached = pd.read_parquet(path)
            cached["trade_date"] = pd.to_datetime(cached["trade_date"])
        except Exception:
            cached = None
        if cached is not None and not cached.empty and (
            meta.get("fetched_at") == _today().strftime("%Y-%m-%d")
            and cached["trade_date"].min() <= pd.Timestamp(start)
            and cached["trade_date"].max() >= (last_open_date(end) or pd.Timestamp(end))
        ):
            cached = validate_bars(
                cached[
                    (cached["trade_date"] >= pd.Timestamp(start))
                    & (cached["trade_date"] <= pd.Timestamp(end))
                ].copy(),
                allow_empty=True,
                source=f"index_daily({ts_code})",
            )
            return cached

    df = client.query(
        "index_daily", ts_code=ts_code, start_date=_fmt(start), end_date=_fmt(end)
    )
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").reset_index(drop=True)
    df = validate_bars(df, source=f"index_daily({ts_code})")
    _atomic_parquet(df, path)
    _atomic_text(
        json.dumps(
            {"ts_code": ts_code, "fetched_at": _today().strftime("%Y-%m-%d")},
            ensure_ascii=False,
        ),
        meta_path,
    )
    return df


_MINUTE_CHUNK_DAYS = {
    "1min": 10,
    "5min": 20,
    "15min": 40,
    "30min": 80,
    "60min": 80,
}
_MINUTE_HIST_TTL = 300.0


def _normalize_minute(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if "time" in df.columns and "trade_time" not in df.columns:
        df = df.rename(columns={"time": "trade_time"})
    if "trade_time" not in df.columns:
        raise TushareError(f"分钟线缺少 time/trade_time 字段: {list(df.columns)}")

    df = df.copy()
    df["trade_time"] = pd.to_datetime(df["trade_time"])
    df = df.sort_values("trade_time").reset_index(drop=True)
    df["trade_date"] = df["trade_time"].dt.normalize()

    for col in ("open", "high", "low", "close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

    # stk_mins / rt_min：vol 股 -> 手；amount 元 -> 千元
    df["vol"] = (
        pd.to_numeric(df["vol"], errors="coerce").fillna(0.0) / 100.0
        if "vol" in df.columns
        else 0.0
    )
    df["amount"] = (
        pd.to_numeric(df["amount"], errors="coerce").fillna(0.0) / 1000.0
        if "amount" in df.columns
        else 0.0
    )
    df["pre_close"] = df["close"].shift(1)
    df["change"] = df["close"].diff()
    df["pct_chg"] = df["close"].pct_change() * 100.0

    cols = [
        "ts_code",
        "trade_date",
        "trade_time",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
    ]
    return df[[c for c in cols if c in df.columns]]


def _query_stk_mins(
    client: TushareClient,
    ts_code: str,
    freq: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """分块拉取 stk_mins，避开单次返回行数上限。"""
    frames: list[pd.DataFrame] = []
    chunk_days = _MINUTE_CHUNK_DAYS[freq]
    cursor = start.normalize()
    end = end.normalize()
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        df = client.query(
            "stk_mins",
            ts_code=ts_code,
            freq=MINUTE_FREQ_MAP[freq],
            start_date=f"{cursor:%Y-%m-%d} 09:00:00",
            end_date=f"{chunk_end:%Y-%m-%d} 15:30:00",
        )
        if df is not None and not df.empty:
            frames.append(df)
        cursor = chunk_end + timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_adj_factors(
    ts_code: str,
    end: str | None = None,
    client: TushareClient | None = None,
) -> pd.DataFrame:
    """复权因子（含最新一日），供分钟线前复权使用。内存缓存 1 小时。"""
    code = ts_code.strip().upper()
    end = end or _fmt(_today())
    key = (code, end)
    if key in _ADJ_MEM:
        ts, cached = _ADJ_MEM[key]
        if time.time() - ts < _ADJ_TTL:
            return cached.copy()

    client = client or get_client()
    df = client.query(
        "adj_factor", ts_code=code, start_date="20150101", end_date=end
    )
    if df.empty:
        raise TushareError(f"{code} 复权因子为空")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = (
        df.sort_values("trade_date")
        .drop_duplicates("trade_date")
        .reset_index(drop=True)
    )
    df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
    _cache_put(_ADJ_MEM, key, (time.time(), df), maxsize=512)
    return df.copy()


def _apply_minute_adj(
    df: pd.DataFrame, factors: pd.DataFrame, adj: str = "qfq"
) -> pd.DataFrame:
    """按日复权因子调整分钟 OHLC：qfq 以最新因子为基准，hfq 以原始因子 1.0 为基准；量/额不变。"""
    if adj not in ("qfq", "hfq"):
        return df
    if df is None or df.empty or factors is None or factors.empty:
        return df
    base = float(factors["adj_factor"].iloc[-1]) if adj == "qfq" else 1.0
    if not base:
        return df
    merged = df.merge(
        factors[["trade_date", "adj_factor"]], on="trade_date", how="left"
    )
    merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
    ratio = merged["adj_factor"] / base
    for col in ("open", "high", "low", "close", "pre_close"):
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce") * ratio
    merged["change"] = merged["close"] - merged["pre_close"]
    merged["pct_chg"] = (
        merged["close"] / merged["pre_close"].replace(0, np.nan) * 100 - 100
    )
    return merged.drop(columns=["adj_factor"])


def load_minute(
    ts_code: str,
    freq: str = "5min",
    days: int = 1,
    adj: str | None = "qfq",
    refresh: bool = False,
    client: TushareClient | None = None,
) -> pd.DataFrame:
    """分钟线加载。

    - ``days <= 1``：走 ``rt_min`` 实时接口，取当日（或最近交易日）
    - ``days > 1``：走 ``stk_mins`` 历史接口，按块拉取，再补当日 ``rt_min``
    - ``adj="qfq"``：按每日复权因子、以最新因子为基准做前复权（默认）
    - ``adj="hfq"``：以原始因子 1.0 为基准做后复权
    - ``adj=None``：不复权（原始成交价）
    - vol 统一为手、amount 统一为千元
    - 当日缓存 30 秒，历史缓存 5 分钟
    """
    code = ts_code.strip().upper()
    if freq not in MINUTE_FREQ_MAP:
        raise ValueError(f"未知分钟周期: {freq}")
    adj = _normalize_adj(adj)
    days = max(1, int(days))

    key = (code, freq, days, adj or "raw")
    now = time.time()
    ttl = _MINUTE_TTL if days <= 1 else _MINUTE_HIST_TTL
    if not refresh and key in _MINUTE_MEM:
        ts, cached = _MINUTE_MEM[key]
        if now - ts < ttl:
            return cached.copy()

    client = client or get_client()

    if days <= 1:
        df = _normalize_minute(
            client.query("rt_min", ts_code=code, freq=MINUTE_FREQ_MAP[freq])
        )
        if df.empty:
            # 非交易时段/接口异常时，退回最近几天 stk_mins，取最后一个交易日
            today = pd.Timestamp.today().normalize()
            hist = _query_stk_mins(
                client, code, freq, today - pd.Timedelta(days=7), today
            )
            df = _normalize_minute(hist)
            if not df.empty:
                df = df[df["trade_date"] == df["trade_date"].max()].reset_index(drop=True)
            if df.empty:
                raise TushareError(
                    f"{code} 分钟线为空：rt_min / stk_mins 均无数据"
                )
    else:
        today = pd.Timestamp.today().normalize()
        cal = load_trade_cal()
        open_days = cal.loc[(cal["is_open"] == 1) & (cal["cal_date"] <= today), "cal_date"]
        start = (
            open_days.sort_values().tail(days).min()
            if len(open_days)
            else today - pd.Timedelta(days=days * 2 + 7)
        )
        df = _normalize_minute(_query_stk_mins(client, code, freq, start, today))
        # stk_mins 通常只含已完成的交易日；把当日 rt_min 补上
        try:
            rt = _normalize_minute(
                client.query("rt_min", ts_code=code, freq=MINUTE_FREQ_MAP[freq])
            )
            if not rt.empty:
                df = pd.concat([df, rt], ignore_index=True) if not df.empty else rt
        except Exception:
            pass
        if df.empty:
            raise TushareError(f"{code} 历史分钟线为空（stk_mins 无返回）")
        df = (
            df.drop_duplicates("trade_time")
            .sort_values("trade_time")
            .reset_index(drop=True)
        )
        dates = df["trade_date"].drop_duplicates().sort_values().tail(days)
        df = df[df["trade_date"].isin(dates)].reset_index(drop=True)

    # 单只股票接口：旧版 rt_min/stk_mins 返回不带 ts_code，用请求参数补全后再校验
    df = df.copy()
    df["ts_code"] = code

    if adj and not df.empty:
        factors = load_adj_factors(code, end=_fmt(_today()), client=client)
        df = _apply_minute_adj(df, factors, adj)

    df = validate_bars(
        df, allow_empty=True, source=f"load_minute({code},{freq})"
    )
    _cache_put(_MINUTE_MEM, key, (now, df), maxsize=128)
    return df.copy()


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """日线聚合到周/月/季线。rule: W-FRI / ME / QE。

    周期标签用该周期内最后一个实际交易日，而不是自然月末，
    避免图上出现 10-31 这种尚未发生的日期。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["_date"] = pd.to_datetime(x["trade_date"])
    x = x.set_index("trade_date").sort_index()
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum",
        "amount": "sum",
    }
    agg = {k: v for k, v in agg.items() if k in x.columns}
    g = x.resample(rule, label="right", closed="right")
    out = g.agg(agg).dropna(subset=["close"])
    last_dates = g["_date"].max().reindex(out.index)
    out["trade_date"] = last_dates.to_numpy()
    out = out.reset_index(drop=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    return out.sort_values("trade_date").reset_index(drop=True)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    return ta.add_all(df)


def period_frame(df: pd.DataFrame, period: str) -> pd.DataFrame:
    rules = {"day": None, "week": "W-FRI", "month": "ME", "quarter": "QE"}
    if period not in rules:
        raise ValueError(f"未知周期: {period}")
    rule = rules[period]
    out = df.copy() if rule is None else resample_ohlcv(df, rule)
    return enrich(out)


#: ``latest_bar_time`` 接受的频率（与 ChartSpec ``data.period`` 同构：分钟档带 min）。
BAR_FREQS = ("D", "W", "M", "1min", "5min", "15min", "30min", "60min")
_PERIOD_FREQ_ALIASES = {"day": "D", "week": "W", "month": "M"}
_MINUTE_FREQS = ("1min", "5min", "15min", "30min", "60min")
_DAILY_BAR_CLOSE = "15:00"
#: A 股连续竞价两段闭区间（午休 11:30–13:00 不是交易时段）。
_MORNING_OPEN = clock_time(9, 30)
_MORNING_CLOSE = clock_time(11, 30)
_AFTERNOON_OPEN = clock_time(13, 0)
_AFTERNOON_CLOSE = clock_time(15, 0)


def latest_bar_time(
    df: pd.DataFrame,
    *,
    freq: str = "D",
    strict: bool = False,
    notes: list[str] | None = None,
) -> tuple[str, str]:
    """返回 ``(data_as_of, data_time)``，是全局唯一的时点格式化入口（ADR-018 C1）。

    冻结语义见《接口冻结清单》§6.1/§6.2：

    - ``data_as_of``：``YYYY-MM-DD``，最后一根有效 bar 所在交易日；
    - ``data_time``：``YYYY-MM-DD HH:MM``，日/周/月线固定 ``15:00``，分钟线取 bar 结束时间；
    - 不变量：``data_time[:10] == data_as_of``；空 DataFrame 抛 ``ValueError``
      （不允许返回空串骗过必填校验）。

    分钟档的越界 bar（集合竞价 09:25、盘后 15:01 等）默认**跳过**并取时段内最后一根，
    因为真实行情源会带这类 bar，硬失败会让整张图不可用；跳过时若传了 ``notes`` 会追加一条
    说明供图注/日志留痕。``strict=True`` 时改为抛 ``ValueError``（数据校验场景）。
    若时段内一根都没有，两种模式都抛错。

    工具与渲染器**不得**自行 ``strftime`` 拼时点，一律经此函数。
    """
    key = _PERIOD_FREQ_ALIASES.get(freq, freq)
    if key not in BAR_FREQS:
        raise ValueError(f"未知频率: {freq}（允许 {BAR_FREQS}）")
    if df is None or df.empty:
        raise ValueError("空 DataFrame 没有有效 bar，无法确定 data_as_of/data_time")

    column = "trade_time" if "trade_time" in df.columns else "trade_date"
    if column not in df.columns:
        raise ValueError("缺少 trade_date/trade_time 列，无法确定时点")
    series = pd.to_datetime(df[column], errors="coerce").dropna()
    if series.empty:
        raise ValueError(f"{column} 全部无效，无法确定时点")
    # 取时间最大值而不是 iloc[-1]：调用方可能传入 sort desc 或未排序的 DataFrame。
    stamps = series.sort_values()

    if key in _MINUTE_FREQS:
        session = stamps[stamps.map(_is_session_time)]
        if session.empty:
            stamp = pd.Timestamp(stamps.iloc[-1])
            raise ValueError(
                f"分钟 bar 时间 {stamp} 全部不在交易时段 "
                f"{_MORNING_OPEN.strftime('%H:%M')}–{_MORNING_CLOSE.strftime('%H:%M')} / "
                f"{_AFTERNOON_OPEN.strftime('%H:%M')}–{_AFTERNOON_CLOSE.strftime('%H:%M')}，拒绝该数据"
            )
        if strict and len(session) != len(stamps):
            stamp = pd.Timestamp(stamps[~stamps.map(_is_session_time)].iloc[-1])
            raise ValueError(
                f"分钟 bar 时间 {stamp} 不在交易时段 "
                f"{_MORNING_OPEN.strftime('%H:%M')}–{_MORNING_CLOSE.strftime('%H:%M')} / "
                f"{_AFTERNOON_OPEN.strftime('%H:%M')}–{_AFTERNOON_CLOSE.strftime('%H:%M')}，拒绝该数据"
            )
        skipped = len(stamps) - len(session)
        if skipped and notes is not None:
            notes.append(f"已跳过 {skipped} 根交易时段外的分钟 bar（集合竞价/盘后）")
        stamp = pd.Timestamp(session.iloc[-1])
        as_of = stamp.strftime("%Y-%m-%d")
        data_time = stamp.strftime("%Y-%m-%d %H:%M")
    else:
        stamp = pd.Timestamp(stamps.iloc[-1])
        as_of = stamp.strftime("%Y-%m-%d")
        data_time = f"{as_of} {_DAILY_BAR_CLOSE}"
    if data_time[:10] != as_of:  # pragma: no cover - 构造上恒成立，守住不变量
        raise ValueError(f"data_time {data_time} 与 data_as_of {as_of} 不同日")
    return as_of, data_time


def _is_session_time(stamp: Any) -> bool:
    """分钟 bar 的时间戳是否落在连续竞价两段闭区间内。"""
    clock = pd.Timestamp(stamp).time()
    return (
        _MORNING_OPEN <= clock <= _MORNING_CLOSE
        or _AFTERNOON_OPEN <= clock <= _AFTERNOON_CLOSE
    )
