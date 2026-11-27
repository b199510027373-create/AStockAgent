from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from collections.abc import Iterable
from datetime import time as clock_time
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as pds

from . import indicators as ta
from .config import CACHE_DIR, DEFAULT_START
from .contracts import DataContractError, validate_bars
from .tushare_client import TushareClient, TushareError, get_client

DAILY_DIR = CACHE_DIR / "daily"
DAILY_DIR.mkdir(parents=True, exist_ok=True)
CAL_PATH = CACHE_DIR / "trade_cal.parquet"

# 复权因子必须从该证券上市起取，不能用请求区间推断基准（ADR-008 D1）：
# 供应商会回溯调整整条因子序列，拿区间首日当基准会让同一只股票在不同区间里
# 得到不同的后复权价格。19900101 用于覆盖 A 股最早上市日。
ADJ_HISTORY_START = "19900101"

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
        with contextlib.suppress(StopIteration):
            cache.pop(next(iter(cache)))
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


def _load_trade_cal_impl(start: str = "20150101", end: str | None = None, refresh: bool = False) -> pd.DataFrame:
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
        except (OSError, ValueError, KeyError, TypeError):  # 缓存缺失/损坏/缺列时按空日历处理
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
            cal = client.query("trade_cal", exchange="SSE", start_date=start, end_date=end)
            if cal.empty:
                raise TushareError("trade_cal 返回为空，请检查 token/代理")
            cal["cal_date"] = pd.to_datetime(cal["cal_date"])
    else:
        cal = client.query("trade_cal", exchange="SSE", start_date=start, end_date=end)
        if cal.empty:
            raise TushareError("trade_cal 返回为空，请检查 token/代理")
        cal["cal_date"] = pd.to_datetime(cal["cal_date"])

    cal = cal.drop_duplicates("cal_date").sort_values("cal_date").reset_index(drop=True)
    cal["is_open"] = pd.to_numeric(cal["is_open"], errors="coerce").fillna(0).astype(int)
    _atomic_parquet(cal, CAL_PATH)
    _CAL_CACHE[key] = cal
    begin = pd.Timestamp(start)
    return cal[cal["cal_date"] >= begin].copy()


def load_trade_cal(start: str = "20150101", end: str | None = None, refresh: bool = False) -> pd.DataFrame:
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
    closed = cal[(cal["cal_date"] >= start_ts) & (cal["cal_date"] <= end_ts) & (cal["is_open"] == 0)]["cal_date"]
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


def _adj_key(adj: str | None) -> str:
    """复权口径的缓存/数据集目录名（``qfq``/``hfq``/``raw``）。"""
    return adj or "raw"


def _cache_suffix(adj: str | None) -> str:
    # qfq 口径 v3：保留原始价列（raw_*），用于涨跌停/交易规则判断
    return (adj or "raw") + ("_v3" if adj else "")


def _cache_paths(ts_code: str, adj: str | None) -> tuple[Path, Path]:
    stem = f"{ts_code.replace('.', '_')}_{_cache_suffix(adj)}"
    return DAILY_DIR / f"{stem}.parquet", DAILY_DIR / f"{stem}.json"


# --------------------------------------------------------------------------- #
# 日线横截面数据集（plan-20261103-011）
#
# 旧缓存逐 (code, adj) 一个 parquet，横截面研究只能"逐股 load + concat"，PIT
# 因子对齐被 O(股票数) 的文件读放大拖慢。这里把同一口径的逐股缓存按
# ``trade_date`` 重排成 hive 分区的 Parquet dataset::
#
#     cache/daily/_dataset/adj=<adj>/build=<build_id>/trade_date=YYYYMMDD/part-000.parquet
#
# 读取用 pyarrow.dataset，可直接按日期区间/代码集合下推过滤；写入前统一
# 数值 dtype，避免同一 dataset 内 arrow schema 冲突。数据集只是逐股缓存的
# **派生索引**，不新增数据来源：meta 记录每个 code 的文件签名与
# ``base_factor``/``adj_base``，任何逐股缓存变化（重拉/复权基准变化）都会
# 让数据集失效并按 ADR-011 的同一套规则重建，复权口径不回退。
# --------------------------------------------------------------------------- #

DATASET_VERSION = 1
"""分区数据集布局版本；布局或 meta 语义变化时递增，旧数据集自动失效重建。"""

_DATASET_LOCK = threading.RLock()


def _dataset_root(adj: str | None) -> Path:
    return DAILY_DIR / "_dataset" / f"adj={_adj_key(adj)}"


def _dataset_meta_path(adj: str | None) -> Path:
    return _dataset_root(adj) / "_meta.json"


def _file_signature(path: Path) -> dict[str, int] | None:
    """逐股缓存文件的轻量签名；文件不存在返回 ``None``。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"mtime_ns": int(stat.st_mtime_ns), "size": int(stat.st_size)}


def _iter_cache_codes(adj: str | None) -> list[str]:
    """从逐股缓存文件名反推证券代码（旧缓存迁移的发现入口）。"""
    tail = f"_{_cache_suffix(adj)}.parquet"
    codes = []
    for path in DAILY_DIR.glob(f"*{tail}"):
        codes.append(path.name[: -len(tail)].replace("_", "."))
    return sorted(set(codes))


def _normalize_codes(codes: Iterable[str] | str | None) -> list[str]:
    if codes is None:
        return []
    if isinstance(codes, str):
        codes = [codes]
    out: list[str] = []
    for code in codes:
        text = str(code).strip().upper()
        if text and text not in out:
            out.append(text)
    return out


def _order_daily_columns(df: pd.DataFrame) -> pd.DataFrame:
    """按 ``load_daily`` 的列序重排，保证面板帧与逐股读取可逐列对比。"""
    head = [c for c in RAW_COLS if c in df.columns]
    raw = [c for c in df.columns if c.startswith("raw_")]
    tail = [c for c in df.columns if c not in head and c not in raw]
    return df[head + raw + tail]


def _write_dataset_partitions(df: pd.DataFrame, build_dir: Path) -> int:
    """按 ``trade_date`` 写 hive 分区；分区列不落文件（避免与分区名重复）。

    单次 ``pds.write_dataset`` 比逐分区 ``DataFrame.to_parquet`` 快一个量级：
    后者每个文件都要走一遍 pandas→arrow 转换与 writer 初始化，700+ 个分区时
    写盘本身就是几十秒。原子性由 meta 保证——build 目录名唯一，meta 只在写
    完后原子替换，读者不会看到一个半成品 build。
    """
    dates = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
    table = pa.Table.from_pandas(df.drop(columns=["trade_date"]), preserve_index=False)
    table = table.append_column("trade_date", pa.array(dates.to_numpy(), type=pa.string()))
    pds.write_dataset(
        table,
        str(build_dir),
        format="parquet",
        partitioning=["trade_date"],
        partitioning_flavor="hive",
        existing_data_behavior="overwrite_or_ignore",
    )
    return len(list(build_dir.glob("trade_date=*")))


def _cleanup_dataset_builds(root: Path, keep: str) -> None:
    """只保留当前 build（旧目录被 Windows 句柄占用时留到下次清理）。"""
    for old in root.glob("build=*"):
        if old.name != f"build={keep}":
            shutil.rmtree(old, ignore_errors=True)


def _read_dataset_meta(adj: str | None) -> dict:
    return _read_meta(_dataset_meta_path(adj))


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # 元数据缺失/非 JSON 时返回空 dict
        return {}


def _clean_adj_factors(fct: pd.DataFrame | None, *, ts_code: str, source: str) -> pd.DataFrame:
    """校验并归一化复权因子（ADR-008 D1）。

    空表、缺列、NaN、inf、``<= 0`` 一律抛 :class:`DataContractError`；
    不 ffill/bfill、不填充 1.0、不回退 qfq——因子异常时宁可报错，也不给出错误口径。
    """
    if fct is None or fct.empty:
        raise DataContractError(
            "AdjFactor",
            f"{ts_code} adj_factor 复权因子为空（{source}）",
            invalid=["adj_factor"],
        )
    missing = [c for c in ("trade_date", "adj_factor") if c not in fct.columns]
    if missing:
        raise DataContractError(
            "AdjFactor",
            f"{ts_code} 复权因子缺少列 {missing}（{source}）；实际列: {list(fct.columns)}",
            missing=missing,
        )
    out = fct[["trade_date", "adj_factor"]].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out["adj_factor"] = pd.to_numeric(out["adj_factor"], errors="coerce")
    bad = out["adj_factor"].isna() | ~np.isfinite(out["adj_factor"]) | (out["adj_factor"] <= 0)
    if bad.any():
        sample = [
            {"trade_date": str(pd.Timestamp(r["trade_date"]).date()), "adj_factor": r["adj_factor"]}
            for r in out.loc[bad].head(3).to_dict("records")
        ]
        raise DataContractError(
            "AdjFactor",
            f"{ts_code} 复权因子存在 {int(bad.sum())} 行非法值（缺失/NaN/inf/<=0，{source}）：{sample}",
            invalid=["adj_factor"],
        )
    return out.drop_duplicates("trade_date").sort_values("trade_date").reset_index(drop=True)


def _query_adj_history(client: TushareClient, ts_code: str, end: str) -> pd.DataFrame:
    """拉取自 ``ADJ_HISTORY_START`` 起的全部 adj_factor（ADR-008 D1）。

    单次 ``query`` 会被服务端行数上限截断：实测 000001.SZ 从 19900101 起
    单次查询只返回 6000 行（最早 20020304），而该股票 19910403 就上市；
    换成 ``query_all`` 分页后为 8692 行、最早 19910403。截断落在最早一端时
    hfq 基准会静默取错，因此这里优先分页，并把覆盖校验交给调用方。
    """
    params = {"ts_code": ts_code, "start_date": ADJ_HISTORY_START, "end_date": end}
    query_all = getattr(client, "query_all", None)
    if callable(query_all):
        df = query_all("adj_factor", **params)
        if isinstance(df, pd.DataFrame):
            return df
    return client.query("adj_factor", **params)


def _check_factor_coverage(fct: pd.DataFrame, raw: pd.DataFrame, adj: str, *, ts_code: str) -> None:
    """因子表必须覆盖行情区间两端（ADR-008 D1）。

    服务端对单次查询有行数上限，截断若落在最早一段，``_adj_base`` 会拿到
    错误的"最早因子"且全序列整体缩放错误、没有任何异常——这里显式报错。
    """
    first_raw = pd.Timestamp(raw["trade_date"].min())
    last_raw = pd.Timestamp(raw["trade_date"].max())
    first_fct = pd.Timestamp(fct["trade_date"].min())
    last_fct = pd.Timestamp(fct["trade_date"].max())
    if adj == "hfq" and first_fct > first_raw:
        raise DataContractError(
            "AdjFactor",
            f"{ts_code} 复权因子最早 {first_fct:%Y-%m-%d} 晚于行情最早 "
            f"{first_raw:%Y-%m-%d}（{adj}）：因子历史疑似被截断，"
            "hfq 基准必须是该证券最早可用交易日的因子",
            invalid=["adj_factor"],
        )
    if adj == "qfq" and last_fct < last_raw:
        raise DataContractError(
            "AdjFactor",
            f"{ts_code} 复权因子最新 {last_fct:%Y-%m-%d} 早于行情最新 {last_raw:%Y-%m-%d}（{adj}）：因子历史疑似被截断",
            invalid=["adj_factor"],
        )


def _adj_base(factors: pd.DataFrame, adj: str) -> tuple[float, pd.Timestamp]:
    """复权基准因子与基准日（ADR-008 D1）。

    - ``qfq``：区间最新复权因子为基准（等价于最新收盘价）；
    - ``hfq``：该证券**最早可用交易日**的因子为基准，令其为 1.0；
      不得把最新因子当 1.0（那是 qfq），也不得假设最早因子恰好等于 1.0
      （供应商会回溯调整整条因子序列）。
    """
    row = factors.iloc[-1] if adj == "qfq" else factors.iloc[0]
    return float(row["adj_factor"]), pd.Timestamp(row["trade_date"])


def _adjustment_meta(adj: str | None, base_factor: float | None, base_date: Any) -> dict[str, Any]:
    """复权标注（ADR-008 D1）：``adj``/``adj_base``/``base_factor``/``adj_source``。"""
    return {
        "adj": adj or "none",
        "adj_base": (None if base_date is None else pd.Timestamp(base_date).strftime("%Y-%m-%d")),
        "base_factor": None if base_factor is None else float(base_factor),
        "adj_source": "daily",
    }


def _cache_base_valid(meta: dict, cached: pd.DataFrame, adj: str | None, end_eff: Any = None) -> bool:
    """缓存 meta 的 ``base_factor`` 与缓存数据不一致时视为失效（ADR-008 §5）。

    只做可自证的内部一致性检查，不联网：

    - 任何复权口径都必须带 ``adj_base``：旧版 meta 缺它说明基准口径不可考
      （历史 qfq 缓存曾以"缓存末行因子"为基准、hfq 曾以 1.0 为基准），
      一律失效重拉，避免命中缓存后返回 ``adj_base=None`` 的标注；
    - qfq：基准必须等于缓存中 ``trade_date <= end_eff`` 的最后一行因子。
      ``_fetch_daily`` 用请求 ``end`` 的最新因子做基准，若命中缓存时改用
      缓存末行（可能更晚）的因子，同一个请求会随缓存状态漂移；
    - hfq：缓存必须覆盖基准日且该日因子等于 ``base_factor``。缓存按请求区间
      切片，通常不含上市首日，无法自证时按失效处理（CACHE-01：基准不可证即失效）。
    """
    if not adj:
        return True
    stored = meta.get("base_factor")
    try:
        stored = float(stored)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if not np.isfinite(stored) or stored <= 0:
        return False
    if not meta.get("adj_base"):
        return False
    if "adj_factor" not in cached.columns or cached.empty:
        return False
    ordered = cached.sort_values("trade_date").reset_index(drop=True)
    horizon = pd.Timestamp(end_eff) if end_eff is not None else ordered["trade_date"].max()
    if adj == "qfq":
        window = ordered[ordered["trade_date"] <= horizon]
        if window.empty:
            return False
        return bool(np.isclose(float(window["adj_factor"].iloc[-1]), stored))
    row = ordered[ordered["trade_date"] == pd.Timestamp(meta["adj_base"])]
    if row.empty:
        return False  # 基准日不在缓存窗口内，无法自证 → 失效重拉
    return bool(np.isclose(float(row["adj_factor"].iloc[0]), stored))


def _fetch_daily(
    client: TushareClient,
    ts_code: str,
    start: str,
    end: str,
    adj: str | None,
) -> pd.DataFrame:
    raw = client.query("daily", ts_code=ts_code, start_date=start, end_date=end)
    if raw.empty:
        return pd.DataFrame()
    # 供应商列漂移（缺 ts_code/trade_date/OHLC/vol/amount）在这里显式报错，
    # 不再让 KeyError 被上层 except 吞成"没有行情"（评审 D18）。
    raw = validate_bars(raw, source=f"{ts_code} daily")
    raw = raw.sort_values("trade_date").reset_index(drop=True)

    adjust_meta = _adjustment_meta(adj, None, None)
    if adj:
        # 因子必须取全历史：hfq 的基准是"最早可用交易日"，不能用请求区间首日。
        # 用分页 query_all 拉取，并校验因子表两端覆盖行情（服务端截断会让
        # hfq 基准静默取错）。
        fct = _clean_adj_factors(
            _query_adj_history(client, ts_code, end),
            ts_code=ts_code,
            source=f"{adj} 日线复权因子",
        )
        _check_factor_coverage(fct, raw, adj, ts_code=ts_code)
        merged = raw.merge(fct, on="trade_date", how="left")
        missing = merged["adj_factor"].isna()
        if missing.any():
            dates = merged.loc[missing, "trade_date"].head(3).dt.strftime("%Y-%m-%d").tolist()
            raise DataContractError(
                "AdjFactor",
                f"{ts_code} 以下交易日缺少 adj_factor（{adj}）：{dates}；禁止用 1.0 填充或回退 qfq",
                invalid=["adj_factor"],
            )
        # 保留原始价：涨跌停/ST/新股规则必须用交易所原始价判断
        for col in PRICE_COLS:
            if col in merged.columns:
                merged[f"raw_{col}"] = merged[col].astype(float)
        # 口径基准（ADR-008 D1）：
        # - qfq：以最新复权因子为基准（可能对应今天，即使今天日线尚未更新）
        # - hfq：以该证券最早可用交易日的因子为基准并令其为 1.0，
        #   价格 = raw * adj_factor / base_factor；不得把最新因子当 1.0（那是 qfq），
        #   也不得全市场共用一个基准日。
        base_factor, base_date = _adj_base(fct, adj)
        ratio = merged["adj_factor"] / base_factor
        for col in PRICE_COLS:
            if col in merged.columns:
                merged[col] = merged[col].astype(float) * ratio
        merged["change"] = merged["close"] - merged["pre_close"]
        merged["pct_chg"] = merged["close"] / merged["pre_close"].replace(0, np.nan) * 100 - 100
        raw = merged
        adjust_meta = _adjustment_meta(adj, base_factor, base_date)

    keep = [c for c in RAW_COLS if c in raw.columns]
    keep += [f"raw_{c}" for c in PRICE_COLS if f"raw_{c}" in raw.columns]
    if adj and "adj_factor" in raw.columns:
        keep.append("adj_factor")
    out = validate_bars(
        raw[keep].reset_index(drop=True),
        require_adj=bool(adj),
        source=f"{ts_code} daily",
    )
    out.attrs["adjustment"] = adjust_meta
    return out


def load_daily(
    ts_code: str,
    start: str | None = None,
    end: str | None = None,
    adj: str | None = "qfq",
    refresh: bool = False,
    client: TushareClient | None = None,
) -> pd.DataFrame:
    """加载日线，默认前复权。

    - ``adj="qfq"``（默认）：以最新复权因子为基准做前复权；
    - ``adj="hfq"``：标准后复权，以该证券最早可用交易日的因子为基准（令其为 1.0）；
      因子=raw*adj_factor/base_factor，最早交易日价格与 raw 相同但量级可跨区间比较；
    - ``adj=None``：不复权（原始成交价），涨跌停/成交异常判断用。
    - 复权因子的 metadata 写在 ``df.attrs["adjustment"]``（adj/adj_base/adj_source）。
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
        except (OSError, ValueError):
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
            covers = cached["trade_date"].min() <= start_eff and cached["trade_date"].max() >= end_eff
            # 缓存 meta 的 base_factor 与数据不一致即失效（ADR-008 §5），
            # 否则会把按旧基准计算的价格继续当作 qfq/hfq 返回。
            # qfq 还要按请求 end_eff 取基准行，避免命中缓存后基准随缓存窗口漂移。
            cache_ok = fetched_today and covers and _cache_base_valid(meta, cached, adj, end_eff=end_eff)

    if cache_ok and cached is not None:
        result = cached[(cached["trade_date"] >= start_eff) & (cached["trade_date"] <= pd.Timestamp(end))].copy()
        adjust_meta = _adjustment_meta(adj, meta.get("base_factor"), meta.get("adj_base"))
    else:
        result = _fetch_daily(client, ts_code, _fmt(start_eff), _fmt(end_eff), adj)
        if result.empty:
            raise TushareError(f"{ts_code} 无行情数据（区间 {_fmt(start_eff)}-{_fmt(end_eff)}）")
        _atomic_parquet(result, path)
        fresh = dict(result.attrs.get("adjustment") or {})
        adjust_meta = _adjustment_meta(adj, fresh.get("base_factor"), fresh.get("adj_base"))
        _atomic_text(
            json.dumps(
                {
                    "ts_code": ts_code,
                    "adj": adj,
                    "base_factor": adjust_meta["base_factor"],
                    "adj_base": adjust_meta["adj_base"],
                    "adj_source": "daily",
                    "start": _fmt(start_eff),
                    "end": _fmt(end_eff),
                    "rows": len(result),
                    "fetched_at": _today().strftime("%Y-%m-%d"),
                },
                ensure_ascii=False,
            ),
            meta_path,
        )

    result = result.sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)
    # 缓存与增量两条路径统一过契约：旧 parquet 缺列时显式报错，而不是返回残表。
    result = validate_bars(
        result,
        require_adj=bool(adj),
        allow_empty=True,
        source=f"load_daily({ts_code})",
    )
    result.attrs["adjustment"] = adjust_meta
    _cache_put(_DAILY_MEM, mem_key, result)
    return result.copy()


def build_daily_dataset(
    codes: Iterable[str] | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    adj: str | None = "qfq",
    write: bool = True,
) -> dict[str, Any]:
    """把逐股日线缓存重排成按 ``trade_date`` 分区的 Parquet dataset。

    旧缓存是逐 ``(code, adj)`` 一个文件，横截面研究只能逐股 ``load_daily`` 再
    ``concat``。本函数只读这些**已有的本地缓存**、不联网，按日期分区写出:

    ``DAILY_DIR/_dataset/adj=<adj>/build=<id>/trade_date=YYYYMMDD/part-000.parquet``

    写入 ``_meta.json``（原子替换）记录 ``build``/``end_eff``/逐 code 文件签名与
    ``base_factor``/``adj_base``。数据集是派生索引而非新数据源：逐股缓存一旦
    重拉（文件签名变化）或按请求 ``end_eff`` 无法自证 ``base_factor``（ADR-011），
    对应代码就被排除、数据集整体失效并按同一套契约重建。

    Args:
        codes: 要纳入的代码；``None`` 时从现有逐股缓存文件名反推（旧缓存迁移入口）。
        start: 数据集保留的最早日期（默认 ``DEFAULT_START``）。
        end: 请求截止日；实际按最近已收盘交易日 ``end_eff`` 切片。
        adj: ``qfq``/``hfq``/``None``（原始价），与逐股缓存口径一一对应。
        write: ``False`` 时只统计不落盘（预估行数/可纳入代码）。

    Returns:
        含 ``build``（未写出为 ``None``）、``start``/``end``/``end_eff``、
        ``codes``（逐 code 签名与复权基准）、``rows``、``partition_files``、
        ``skipped``（未纳入原因，便于排查）的 dict。
    """
    adj = _normalize_adj(adj)
    start_eff = pd.Timestamp(start or DEFAULT_START).normalize()
    end_ts = pd.Timestamp(end or _fmt(_today())).normalize()
    end_eff = pd.Timestamp(last_open_date(end_ts) or end_ts).normalize()
    today = _today().strftime("%Y-%m-%d")
    code_list = _normalize_codes(codes) if codes is not None else _iter_cache_codes(adj)

    frames: list[pd.DataFrame] = []
    included: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    for code in code_list:
        path, meta_path = _cache_paths(code, adj)
        signature = _file_signature(path)
        if signature is None:
            skipped[code] = "missing_cache"
            continue
        try:
            cached = pd.read_parquet(path)
            cached = validate_bars(
                cached,
                require_adj=bool(adj),
                allow_empty=True,
                source=f"build_daily_dataset({code})",
            )
        except DataContractError as exc:
            # 契约问题不静默吞掉：记入 skipped，调用方回退逐股 load_daily 时会显式报错。
            skipped[code] = f"contract:{str(exc)[:80]}"
            continue
        except (OSError, ValueError) as exc:
            skipped[code] = f"unreadable:{type(exc).__name__}"
            continue
        if cached.empty:
            skipped[code] = "empty"
            continue
        meta = _read_meta(meta_path)
        # ADR-011：base_factor 与缓存数据不一致 / adj_base 不可考 → 不进入数据集。
        if not _cache_base_valid(meta, cached, adj, end_eff=end_eff):
            skipped[code] = "base_factor_invalid"
            continue
        if cached["trade_date"].max() < end_eff:
            # 与 load_daily 的 covers 判定对齐：缓存没覆盖 end_eff 即不算当日可用。
            skipped[code] = "stale_end"
            continue
        window = cached[(cached["trade_date"] >= start_eff) & (cached["trade_date"] <= end_eff)]
        if window.empty:
            skipped[code] = "no_rows_in_window"
            continue
        frames.append(window)
        included[code] = {
            **signature,
            "rows": len(window),
            "base_factor": meta.get("base_factor"),
            "adj_base": meta.get("adj_base"),
            "fetched_at": meta.get("fetched_at"),
            "first_date": pd.Timestamp(window["trade_date"].min()).strftime("%Y%m%d"),
            "last_date": pd.Timestamp(window["trade_date"].max()).strftime("%Y%m%d"),
        }

    summary: dict[str, Any] = {
        "version": DATASET_VERSION,
        "adj": _adj_key(adj),
        "build": None,
        "start": None,
        "end": None,
        "end_eff": _fmt(end_eff),
        "requested_start": _fmt(start_eff),
        "built_at": None,
        "built_day": today,
        "codes": included,
        "rows": 0,
        "partition_files": 0,
        "skipped": skipped,
        "root": str(_dataset_root(adj)),
    }
    if not frames:
        return summary

    work = pd.concat(frames, ignore_index=True)
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.normalize()
    work["ts_code"] = work["ts_code"].astype(str)
    work = work.drop_duplicates(["ts_code", "trade_date"]).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    # 同一个 dataset 里 arrow schema 必须一致：数值列统一 float64。
    for column in work.columns:
        if column not in ("ts_code", "trade_date"):
            work[column] = pd.to_numeric(work[column], errors="coerce").astype("float64")

    build_id = f"{_today():%Y%m%d}-{time.time_ns()}"
    summary.update(
        {
            "build": build_id,
            "start": work["trade_date"].min().strftime("%Y%m%d"),
            "end": work["trade_date"].max().strftime("%Y%m%d"),
            "built_at": pd.Timestamp.now().isoformat(timespec="seconds"),
            "rows": len(work),
            "partition_files": int(work["trade_date"].nunique()),
        }
    )
    if not write:
        return summary

    root = _dataset_root(adj)
    build_dir = root / f"build={build_id}"
    with _DATASET_LOCK:
        summary["partition_files"] = _write_dataset_partitions(work, build_dir)
        _atomic_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            _dataset_meta_path(adj),
        )
        _cleanup_dataset_builds(root, keep=build_id)
    return summary


def _dataset_usable(
    meta: dict,
    adj: str | None,
    codes: Iterable[str] | None,
    start_eff: pd.Timestamp,
    end_eff: pd.Timestamp,
) -> bool:
    """数据集能否原样服务本次请求（不读逐股 parquet，只查 meta + 文件签名）。

    判定与 ``load_daily`` 的 ``cache_ok`` 同源：必须是今天构建、逐个 code 的缓存
    仍是构建时那一份（mtime/size 一致）且 ``fetched_at`` 为今天，复权基准按
    请求 ``end_eff`` 自证过。任何一条不满足就重建，绝不用过期/错基准数据冒充。
    """
    if not meta or meta.get("version") != DATASET_VERSION:
        return False
    if meta.get("adj") != _adj_key(adj) or not meta.get("build"):
        return False
    if meta.get("built_day") != _today().strftime("%Y-%m-%d"):
        return False
    try:
        if pd.Timestamp(meta["end_eff"]) != pd.Timestamp(end_eff):
            return False
        # 数据集覆盖到请求起点即可；请求更早时只要构建时就是按更早起点切的
        # （requested_start <= 请求 start_eff）也算可用——缓存里本来就没有更早的行，
        # 否则「请求 DEFAULT_START + 缓存只有近三年」会每次扫描都白重建一遍。
        if pd.Timestamp(meta["start"]) > pd.Timestamp(start_eff) and pd.Timestamp(
            meta.get("requested_start") or "9999-12-31"
        ) > pd.Timestamp(start_eff):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    table = meta.get("codes") or {}
    wanted = _normalize_codes(codes) if codes is not None else None
    if wanted is not None and not set(wanted) <= set(table):
        return False
    today = _today().strftime("%Y-%m-%d")
    for code, record in table.items():
        if wanted is not None and code not in wanted:
            continue
        if record.get("fetched_at") != today:
            return False
        signature = _file_signature(_cache_paths(code, adj)[0])
        if not signature:
            return False
        if signature["mtime_ns"] != record.get("mtime_ns") or signature["size"] != record.get("size"):
            return False
    return True


def _scan_dataset(
    meta: dict,
    adj: str | None,
    codes: list[str] | None,
    start_eff: pd.Timestamp,
    end_eff: pd.Timestamp,
) -> pd.DataFrame:
    """直接 scan 分区数据集（日期区间/代码集合下推到 parquet 扫描）。

    列投影故意不在这里做：``_finalize_scan`` 的 BarFrame 契约要校验完整字段，
    投影留给 ``scan_daily`` 在契约校验之后裁剪（列下推省下的时间远小于一次误判）。
    """
    root = _dataset_root(adj) / f"build={meta['build']}"
    partitioning = pds.partitioning(pa.schema([("trade_date", pa.string())]), flavor="hive")
    dataset = pds.dataset(str(root), format="parquet", partitioning=partitioning)
    expression = (pds.field("trade_date") >= _fmt(start_eff)) & (pds.field("trade_date") <= _fmt(end_eff))
    if codes:
        expression &= pds.field("ts_code").isin(list(codes))
    # 727 个分区文件时 pyarrow 默认多线程严重争用（实测 34s → 1.7s），必须串行读。
    table = dataset.to_table(filter=expression, use_threads=False)
    out = table.to_pandas()
    out["trade_date"] = pd.to_datetime(out["trade_date"], format="%Y%m%d")
    return out


def _finalize_scan(
    df: pd.DataFrame,
    adj: str | None,
    meta: dict | None,
    *,
    from_dataset: bool,
) -> pd.DataFrame:
    """统一排序/去重/契约校验，并写复权标注（不把 hfq 当不复权）。"""
    if df is None or df.empty:
        empty = pd.DataFrame()
        empty.attrs["adjustment"] = _adjustment_meta(adj, None, None)
        return empty
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out["ts_code"] = out["ts_code"].astype(str)
    out = out.drop_duplicates(["ts_code", "trade_date"]).sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    out = validate_bars(
        out,
        require_adj=bool(adj),
        allow_empty=True,
        source="scan_daily 分区数据集" if from_dataset else "scan_daily",
    )
    out = _order_daily_columns(out)
    adjust = _adjustment_meta(adj, None, None)
    # 横截面帧混了多只股票、各有自己的复权基准：base_factor/adj_base 不唯一，
    # 只标口径与来源，逐 code 基准放在 adj_bases 里供审计。
    adjust["adj_source"] = "daily_dataset" if from_dataset else "daily"
    if meta and meta.get("build"):
        adjust["dataset_build"] = meta["build"]
    bases = {
        code: record.get("adj_base")
        for code, record in ((meta or {}).get("codes") or {}).items()
        if record.get("adj_base")
    }
    if bases:
        adjust["adj_bases"] = bases
    out.attrs["adjustment"] = adjust
    return out


def dataset_status(adj: str | None = "qfq") -> dict[str, Any]:
    """当前分区数据集的状态（审计/排障用；不触发重建、不联网）。"""
    adj = _normalize_adj(adj)
    meta = _read_dataset_meta(adj)
    status: dict[str, Any] = {
        "adj": _adj_key(adj),
        "root": str(_dataset_root(adj)),
        "available": bool(meta.get("build")),
        "version": meta.get("version"),
        "build": meta.get("build"),
        "built_day": meta.get("built_day"),
        "built_at": meta.get("built_at"),
        "start": meta.get("start"),
        "end": meta.get("end"),
        "end_eff": meta.get("end_eff"),
        "codes": len(meta.get("codes") or {}),
        "rows": meta.get("rows"),
        "usable": False,
    }
    if status["available"]:
        try:
            status["usable"] = _dataset_usable(
                meta,
                adj,
                None,
                pd.Timestamp(meta.get("start") or DEFAULT_START),
                pd.Timestamp(meta.get("end_eff") or _fmt(_today())),
            )
        except (TypeError, ValueError):
            status["usable"] = False
    return status


def scan_daily(
    codes: Iterable[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    adj: str | None = "qfq",
    columns: Iterable[str] | None = None,
    refresh: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """横截面读取日线：直接 scan 按 ``trade_date`` 分区的数据集。

    与逐股 ``load_daily`` 的语义保持一致（同一 ``adj`` 口径、同一 ``end_eff``
    截止、同一 ADR-011 复权基准校验），但只读一个分区数据集、不再逐股拼接：

    - 数据集可用（今天构建、逐股缓存未变、基准自证）→ 直接 scan；
    - 不可用 → 先用本地逐股缓存重建（不联网），仍缺的代码回退 ``load_daily``，
      并把补齐后的缓存重新落成数据集，下一次扫描即命中；
    - 返回列与 ``load_daily`` 相同（含 ``raw_*``/``adj_factor``），行按
      ``trade_date, ts_code`` 排序。

    Args:
        codes: 证券代码集合；``None`` 表示数据集内全部代码。
        start/end: 日期区间；``end`` 按最近已收盘交易日截止。
        adj: ``qfq``（默认）/``hfq``/``None``；口径与逐股缓存一一对应，不做回退。
        columns: 需要的列（至少含 ``ts_code``/``trade_date``）；``None`` 取全部。
        refresh: 绕过数据集并强制重拉逐股缓存。
        diagnostics: 传入 list 时逐条记录回退失败（不吞成“没有行情”）。
    """
    adj = _normalize_adj(adj)
    end = end or _fmt(_today())
    start = start or DEFAULT_START
    end_eff = pd.Timestamp(last_open_date(end) or pd.Timestamp(end)).normalize()
    start_eff = pd.Timestamp(start).normalize()
    requested = _normalize_codes(codes) if codes is not None else None
    missing: list[str] = []
    frames: list[pd.DataFrame] = []
    used_dataset = False
    meta: dict = {}

    with _DATASET_LOCK:
        meta = _read_dataset_meta(adj)
        usable = not refresh and _dataset_usable(meta, adj, requested, start_eff, end_eff)
        if not usable:
            scope = requested if requested is not None else _iter_cache_codes(adj)
            meta = build_daily_dataset(scope, start=_fmt(start_eff), end=end, adj=adj)
            usable = _dataset_usable(meta, adj, requested, start_eff, end_eff)
        if usable:
            frame = _scan_dataset(meta, adj, requested, start_eff, end_eff)
            frames.append(frame)
            used_dataset = True
            present = set(frame["ts_code"].astype(str)) if not frame.empty else set()
            missing = [code for code in (requested or []) if code not in present]
        else:
            missing = list(requested) if requested is not None else _iter_cache_codes(adj)

    loaded: list[str] = []
    if missing:
        for code in missing:
            try:
                part = load_daily(code, start=_fmt(start_eff), end=end, adj=adj, refresh=refresh)
            except DataContractError:
                raise  # 契约问题必须显式报错，不能降级成“缺一只股票”
            except Exception as exc:  # noqa: BLE001 - 单只失败不拖垮整批，但要留痕
                if diagnostics is not None:
                    diagnostics.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if part is not None and not part.empty:
                frames.append(part)
                loaded.append(code)
        if loaded:
            # 补齐的缓存重新落盘，让下一次横截面扫描直接命中数据集（失败不影响本次结果）。
            # scope 取「现有缓存里的全部代码」，避免一次窄请求把数据集缩成单只股票。
            scope = sorted(set(_iter_cache_codes(adj)) | set(loaded))
            with contextlib.suppress(Exception), _DATASET_LOCK:
                build_daily_dataset(scope, start=_fmt(start_eff), end=end, adj=adj)

    if not frames:
        if diagnostics:
            raise TushareError(f"扫描日线失败：{diagnostics[0].get('error')}")
        return _finalize_scan(pd.DataFrame(), adj, meta, from_dataset=used_dataset)

    out = pd.concat(frames, ignore_index=True)
    out = _finalize_scan(out, adj, meta, from_dataset=used_dataset and not loaded)
    if columns is not None:
        requested_columns = [str(c) for c in columns]
        keep = [c for c in out.columns if c in requested_columns]
        if keep:
            out = out[keep]
    return out


def load_daily_panel(
    codes: Iterable[str],
    *,
    start: str | None = None,
    end: str | None = None,
    adj: str | None = "qfq",
    years: int | None = None,
    enrich_frames: bool = True,
    refresh: bool = False,
    diagnostics: list[dict[str, Any]] | None = None,
    progress: Any = None,
) -> dict[str, pd.DataFrame]:
    """横截面日线面板：``load_many`` 的分区数据集实现，返回同构 dict。

    每只股票一个 frame（含 ``ts_code`` 列与 enrich 指标、逐 code 的复权标注），
    与 :func:`astock.recommend.load_many` 的输出形状一致，可原位替换：

    - 数据来自 :func:`scan_daily`（默认 qfq，口径与逐股读取逐值一致）；
    - ``years`` 给定时区间为最近 N 年（默认 ``start=DEFAULT_START``）；
    - ``enrich_frames`` 为真时补技术指标（因子模块依赖 ``vol_ma20``/``rsi14`` 等）；
    - 单只失败写入 ``diagnostics``（``{"code","error"}``），不中断整批。

    Args:
        codes: 证券代码集合。
        start/end: 日期区间（``years`` 优先于 ``start``）。
        adj: 复权口径。
        years: 最近 N 年；``None`` 时用 ``start``。
        enrich_frames: 是否追加技术指标。
        refresh: 强制重拉逐股缓存。
        diagnostics: 失败留痕列表。
        progress: 可选的 ``progress(value, desc=...)`` 回调。

    Returns:
        ``{ts_code: enriched DataFrame}``；无数据的代码不出现。
    """
    code_list = _normalize_codes(codes)
    if not code_list:
        return {}
    end = end or _fmt(_today())
    if start is None:
        start = _fmt(pd.Timestamp(_today()) - pd.Timedelta(days=365 * max(1, int(years)))) if years else DEFAULT_START
    if progress is not None:
        with contextlib.suppress(TypeError):
            progress(0.05, desc=f"扫描 {len(code_list)} 只股票日线（分区数据集）")

    frame = scan_daily(
        code_list,
        start=start,
        end=end,
        adj=adj,
        refresh=refresh,
        diagnostics=diagnostics,
    )
    result: dict[str, pd.DataFrame] = {}
    if frame.empty:
        return result
    for code, chunk in frame.groupby("ts_code", sort=False):
        code = str(code)
        item = _order_daily_columns(chunk.reset_index(drop=True))
        meta = _read_meta(_cache_paths(code, _normalize_adj(adj))[1])
        item.attrs["adjustment"] = _adjustment_meta(adj, meta.get("base_factor"), meta.get("adj_base"))
        result[code] = enrich(item) if enrich_frames else item
    if progress is not None:
        with contextlib.suppress(TypeError):
            progress(0.6, desc=f"日线面板 {len(result)}/{len(code_list)} 只就绪")
    return result


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
        except (OSError, ValueError, KeyError, TypeError):  # 缓存损坏/缺列时当作无缓存
            cached = None
        if (
            cached is not None
            and not cached.empty
            and (
                meta.get("fetched_at") == _today().strftime("%Y-%m-%d")
                and cached["trade_date"].min() <= pd.Timestamp(start)
                and cached["trade_date"].max() >= (last_open_date(end) or pd.Timestamp(end))
            )
        ):
            cached = validate_bars(
                cached[
                    (cached["trade_date"] >= pd.Timestamp(start)) & (cached["trade_date"] <= pd.Timestamp(end))
                ].copy(),
                allow_empty=True,
                source=f"index_daily({ts_code})",
            )
            return cached

    df = client.query("index_daily", ts_code=ts_code, start_date=_fmt(start), end_date=_fmt(end))
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
    df["vol"] = pd.to_numeric(df["vol"], errors="coerce").fillna(0.0) / 100.0 if "vol" in df.columns else 0.0
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0) / 1000.0 if "amount" in df.columns else 0.0
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
    """复权因子全历史（自上市起，含最新一日），供分钟线复权使用。内存缓存 1 小时。

    必须从 ``ADJ_HISTORY_START`` 起取：hfq 的基准是该证券最早可用交易日的因子，
    只取近几月会得到错误的基准（ADR-008 D1）。因子异常抛 :class:`DataContractError`。
    """
    code = ts_code.strip().upper()
    end = end or _fmt(_today())
    key = (code, end)
    if key in _ADJ_MEM:
        ts, cached = _ADJ_MEM[key]
        if time.time() - ts < _ADJ_TTL:
            return cached.copy()

    client = client or get_client()
    df = _clean_adj_factors(
        _query_adj_history(client, code, end),
        ts_code=code,
        source=f"分钟线复权因子(end={end})",
    )
    _cache_put(_ADJ_MEM, key, (time.time(), df), maxsize=512)
    return df.copy()


def _apply_minute_adj(
    df: pd.DataFrame,
    factors: pd.DataFrame,
    adj: str = "qfq",
    *,
    ts_code: str = "",
) -> pd.DataFrame:
    """按交易日日线复权因子调整分钟 OHLC（ADR-008 D1）。

    - qfq：以最新已发布因子为基准；
    - hfq：以该证券最早可用交易日因子为基准（令其为 1.0）；
    - 缺因子/NaN/inf/``<=0`` 一律抛 :class:`DataContractError`，不 ffill/bfill；
    - 盘中当日 bar 例外：Tushare 当日 adj_factor 收盘后才发布，而 ``rt_min``
      的当日 bar 盘中就存在。仅当"缺因子的行都是今天、且因子表末行早于今天"
      时，用最近已发布因子复权，并在 metadata 标 ``partial=true`` /
      ``factor_as_of``（下游图注可提示"盘中近似"）；其它任何缺因子仍硬失败；
    - 分钟路径不另设覆盖断言：因子历史由 ``_query_adj_history`` 分页拉取，
      截断若落在行情窗口之内，会表现为窗口内某些交易日缺因子，由上面的
      缺因子硬失败（盘中当日例外）直接拦下——此时再做首末覆盖比较恒为真，
      属不可达代码，故不保留（评审 iss-mwixznmz-2f393e）；截断恰好早于窗口
      起点、且窗口内每天都有因子的情形无法在分钟内自证，只能靠分页完整性，
      边界见 ADR-008 第 8 条；
    - vol/amount 保持原始值（复权只作用于价格）；
    - metadata 写 ``out.attrs["adjustment"]``：adj/adj_base/adj_source=daily，
      分钟 hfq 不与日线 hfq 拼连续序列。
    """
    if adj not in ("qfq", "hfq"):
        return df
    if df is None or df.empty:
        return df
    fct = _clean_adj_factors(factors, ts_code=ts_code or "?", source=f"分钟线({ts_code or '?'})")
    base_factor, base_date = _adj_base(fct, adj)
    merged = df.merge(fct, on="trade_date", how="left")
    missing = merged["adj_factor"].isna()
    intraday = pd.Series(False, index=merged.index)
    partial = False
    factor_as_of: str | None = None
    if missing.any():
        today = pd.Timestamp(_today()).normalize()
        last_factor_day = pd.Timestamp(fct["trade_date"].max()).normalize()
        days = pd.to_datetime(merged["trade_date"]).dt.normalize()
        # 只放行"当日盘中且因子还没发布"这一种缺因子情形
        intraday = missing & (days == today) & (last_factor_day < today)
        unexpected = missing & ~intraday
        if unexpected.any():
            dates = merged.loc[unexpected, "trade_date"].head(3).dt.strftime("%Y-%m-%d").tolist()
            raise DataContractError(
                "AdjFactor",
                f"{ts_code or '?'} 分钟线以下交易日缺少 adj_factor（{adj}）：{dates}；禁止用 1.0 填充或回退 qfq",
                invalid=["adj_factor"],
            )
        merged.loc[intraday, "adj_factor"] = float(fct["adj_factor"].iloc[-1])
        partial = True
        factor_as_of = last_factor_day.strftime("%Y-%m-%d")
    ratio = merged["adj_factor"] / base_factor
    for col in ("open", "high", "low", "close", "pre_close"):
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce") * ratio
    merged["change"] = merged["close"] - merged["pre_close"]
    merged["pct_chg"] = merged["close"] / merged["pre_close"].replace(0, np.nan) * 100 - 100
    out = merged.drop(columns=["adj_factor"])
    meta = _adjustment_meta(adj, base_factor, base_date)
    if partial:
        # 盘中当日 bar 用前一交易日因子近似：图注/下游必须能看到这一点。
        meta["partial"] = True
        meta["factor_as_of"] = factor_as_of
    out.attrs["adjustment"] = meta
    return out


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
    - ``adj="hfq"``：按每日复权因子、以最早可用交易日因子为基准做后复权
    - ``adj=None``：不复权（原始成交价）
    - 复权标注写在 ``df.attrs["adjustment"]``（adj/adj_base/adj_source=daily），
      分钟 hfq 是"逐日独立"序列，不与日线 hfq 拼连续序列（ADR-008 D1）
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
        df = _normalize_minute(client.query("rt_min", ts_code=code, freq=MINUTE_FREQ_MAP[freq]))
        if df.empty:
            # 非交易时段/接口异常时，退回最近几天 stk_mins，取最后一个交易日
            today = pd.Timestamp.today().normalize()
            hist = _query_stk_mins(client, code, freq, today - pd.Timedelta(days=7), today)
            df = _normalize_minute(hist)
            if not df.empty:
                df = df[df["trade_date"] == df["trade_date"].max()].reset_index(drop=True)
            if df.empty:
                raise TushareError(f"{code} 分钟线为空：rt_min / stk_mins 均无数据")
    else:
        today = pd.Timestamp.today().normalize()
        cal = load_trade_cal()
        open_days = cal.loc[(cal["is_open"] == 1) & (cal["cal_date"] <= today), "cal_date"]
        start = open_days.sort_values().tail(days).min() if len(open_days) else today - pd.Timedelta(days=days * 2 + 7)
        df = _normalize_minute(_query_stk_mins(client, code, freq, start, today))
        # stk_mins 通常只含已完成的交易日；把当日 rt_min 补上
        try:
            rt = _normalize_minute(client.query("rt_min", ts_code=code, freq=MINUTE_FREQ_MAP[freq]))
            if not rt.empty:
                df = pd.concat([df, rt], ignore_index=True) if not df.empty else rt
        except Exception:  # noqa: BLE001, S110 - 实时分钟线是可选增强（权限/额度受限），失败保留日线
            pass
        if df.empty:
            raise TushareError(f"{code} 历史分钟线为空（stk_mins 无返回）")
        df = df.drop_duplicates("trade_time").sort_values("trade_time").reset_index(drop=True)
        dates = df["trade_date"].drop_duplicates().sort_values().tail(days)
        df = df[df["trade_date"].isin(dates)].reset_index(drop=True)

    # 单只股票接口：旧版 rt_min/stk_mins 返回不带 ts_code，用请求参数补全后再校验
    df = df.copy()
    df["ts_code"] = code

    adjust_meta: dict[str, Any] = {}
    if adj and not df.empty:
        factors = load_adj_factors(code, end=_fmt(_today()), client=client)
        df = _apply_minute_adj(df, factors, adj, ts_code=code)
        adjust_meta = dict(df.attrs.get("adjustment") or {})

    df = validate_bars(df, allow_empty=True, source=f"load_minute({code},{freq})")
    if adjust_meta:
        df.attrs["adjustment"] = adjust_meta
    _cache_put(_MINUTE_MEM, key, (now, df), maxsize=128)
    return df.copy()


def _empty_with_attrs(df: pd.DataFrame | None) -> pd.DataFrame:
    """空帧也要带着输入 frame 的 ``attrs`` 返回（plan-20261120-001 评审 P2）。

    空区间（停牌整段/窗口内无交易日）走快速返回时若直接 ``pd.DataFrame()``，
    ``attrs["adjustment"]`` 会丢，图注/回测就退回调用方传的 adj，标注与数据脱节。
    """
    out = pd.DataFrame()
    attrs = getattr(df, "attrs", None)
    if attrs:
        out.attrs.update(attrs)
    return out


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """日线聚合到周/月/季线。rule: W-FRI / ME / QE。

    周期标签用该周期内最后一个实际交易日，而不是自然月末，
    避免图上出现 10-31 这种尚未发生的日期。
    """
    if df is None or df.empty:
        return _empty_with_attrs(df)
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
    out = out.sort_values("trade_date").reset_index(drop=True)
    # 聚合会新建 DataFrame，attrs（如 attrs["adjustment"] 复权标注）默认丢失；
    # 周期线沿用日线口径，故显式带回（plan-20261114-003 验收⑥）。
    if df.attrs:
        out.attrs.update(df.attrs)
    return out


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """追加技术指标；保留输入 frame 的 ``attrs``（复权标注不因加指标而丢失）。"""
    if df is None or df.empty:
        return _empty_with_attrs(df)
    out = ta.add_all(df)
    # indicators.add_all 通过 join/replace 生成新 frame，必须显式带回
    # attrs["adjustment"]，否则回测/图表链路拿不到 qfq|hfq|none 与 adj_base。
    if df.attrs:
        out.attrs.update(df.attrs)
    return out


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
#: 行情时区：bar 时间语义一律按北京时间解释（ADR-005/ADR-018，Q7）。
MARKET_TZ = ZoneInfo("Asia/Shanghai")
#: bar 所处时段（Q7）：集合竞价 / 连续竞价 / 盘后固定价 / 其他（休市）/ 日线收盘。
BAR_SESSIONS = ("pre", "continuous", "post", "closed", "close")
#: A 股连续竞价两段闭区间（午休 11:30–13:00 不是交易时段）。
_MORNING_OPEN = clock_time(9, 30)
_MORNING_CLOSE = clock_time(11, 30)
_AFTERNOON_OPEN = clock_time(13, 0)
_AFTERNOON_CLOSE = clock_time(15, 0)
#: 集合竞价 bar 的最早时间（09:15 起接受申报，09:25 出成交价）。
_PRE_OPEN = clock_time(9, 15)


def _freq_key(freq: str) -> str:
    key = _PERIOD_FREQ_ALIASES.get(freq, freq)
    if key not in BAR_FREQS:
        raise ValueError(f"未知频率: {freq}（允许 {BAR_FREQS}）")
    return key


def market_timestamp(value: Any) -> pd.Timestamp:
    """把任意 bar 时间戳归一到 ``Asia/Shanghai``：naive 视为本地时间，aware 直接换算。"""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        return stamp.tz_localize(MARKET_TZ)
    return stamp.tz_convert(MARKET_TZ)


def classify_bar_time(stamp: Any, freq: str = "D") -> str:
    """bar 时间属于哪个时段（Q7 唯一判定入口，返回值见 :data:`BAR_SESSIONS`）。

    - ``pre``：09:15–09:29 集合竞价（09:25 的成交价 bar 是合法行情，不是脏数据）；
    - ``continuous``：09:30–11:30、13:00–15:00 连续竞价；
    - ``post``：15:00 之后（盘后固定价格交易）；
    - ``closed``：其余时间（含午休、开盘前、隔夜）；
    - ``close``：日/周/月线（收盘价 bar，不按盘中时段判定）。

    时区按 :data:`MARKET_TZ` 解释：naive 视为北京时间，aware 先换算再判定。
    """
    key = _freq_key(freq)
    if key not in _MINUTE_FREQS:
        return "close"
    clock = market_timestamp(stamp).time()
    if _PRE_OPEN <= clock < _MORNING_OPEN:
        return "pre"
    if _MORNING_OPEN <= clock <= _MORNING_CLOSE or _AFTERNOON_OPEN <= clock <= _AFTERNOON_CLOSE:
        return "continuous"
    if clock > _AFTERNOON_CLOSE:
        return "post"
    return "closed"


def bar_sessions(df: pd.DataFrame, *, freq: str = "D") -> pd.Series:
    """逐行标注时段（索引与 ``df`` 对齐）；无时间列时返回空 Series。

    分钟档按 ``trade_time`` 判定，日/周/月线统一 ``close``；缺列时退回 ``trade_date``，
    两列都没有时返回空 Series（时点语义由调用方显式判定，不猜）。
    """
    if df is None or df.empty:
        return pd.Series(dtype=object)
    column = "trade_time" if "trade_time" in df.columns else "trade_date"
    if column not in df.columns:
        return pd.Series(dtype=object)
    stamps = pd.to_datetime(df[column], errors="coerce")
    return stamps.map(lambda value: classify_bar_time(value, freq) if pd.notna(value) else "closed")


def continuous_bars(df: pd.DataFrame, *, freq: str = "D") -> pd.DataFrame:
    """只保留连续竞价 bar（Q7：集合竞价/盘后固定价不参与 MA/BOLL/MACD 等连续指标）。"""
    sessions = bar_sessions(df, freq=freq)
    if sessions.empty:
        return df.iloc[0:0].copy()
    return df.loc[sessions.to_numpy() == "continuous"].copy()


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

    Q7（2026-11-14 裁决）：分钟档的集合竞价（09:25）与盘后固定价（15:0x）bar 都是
    **合法行情**，默认直接接受并取时间最大值，不再跳过、也不回退前一日；时段由
    :func:`classify_bar_time` 标注，连续指标由 :func:`continuous_bars` 排除。
    ``strict=True`` 仅保留给数据校验/研究场景：最新 bar 不是连续竞价 bar 时抛 ``ValueError``；
    通用的硬校验入口是 :func:`validate_bar_time`。

    工具与渲染器**不得**自行 ``strftime`` 拼时点，一律经此函数。
    """
    key = _freq_key(freq)
    if df is None or df.empty:
        raise ValueError("空 DataFrame 没有有效 bar，无法确定 data_as_of/data_time")

    column = "trade_time" if "trade_time" in df.columns else "trade_date"
    if column not in df.columns:
        raise ValueError("缺少 trade_date/trade_time 列，无法确定时点")
    series = pd.to_datetime(df[column], errors="coerce").dropna()
    if series.empty:
        raise ValueError(f"{column} 全部无效，无法确定时点")
    # 取时间最大值而不是 iloc[-1]：调用方可能传入 sort desc 或未排序的 DataFrame。
    stamp = pd.Timestamp(series.sort_values().iloc[-1])
    as_of = stamp.strftime("%Y-%m-%d")
    if key in _MINUTE_FREQS:
        data_time = stamp.strftime("%Y-%m-%d %H:%M")
        session = classify_bar_time(stamp, key)
        if strict and session != "continuous":
            raise ValueError(
                f"最新分钟 bar {data_time} 的 session={session}（非连续竞价），"
                "strict=True 拒绝该数据；需要渲染请改用 strict=False 或 validate_bar_time"
            )
        if notes is not None and session != "continuous":
            label = {"pre": "集合竞价", "post": "盘后固定价"}.get(session, "非连续竞价时段")
            notes.append(f"最新分钟 bar {data_time} 属{label}（session={session}）")
    else:
        data_time = f"{as_of} {_DAILY_BAR_CLOSE}"
        session = "close"
    if data_time[:10] != as_of:  # pragma: no cover - 构造上恒成立，守住不变量
        raise ValueError(f"data_time {data_time} 与 data_as_of {as_of} 不同日")
    return as_of, data_time


def validate_bar_time(df: pd.DataFrame, *, freq: str = "D", strict: bool = True) -> str:
    """校验数据里存在合法交易时段的 bar，返回最新 bar 的时段（Q7 硬校验入口）。

    ``strict=True``（默认）时，出现集合竞价/盘后固定价/休市 bar 或全天无连续竞价 bar
    一律抛 ``ValueError``；``strict=False`` 只返回时段、不抛错。渲染层**不调用**本函数
    （图上要能画 09:25 bar），它只服务数据质量门禁与研究口径。
    """
    key = _freq_key(freq)
    if df is None or df.empty:
        raise ValueError("空 DataFrame 没有有效 bar")
    sessions = bar_sessions(df, freq=key)
    if sessions.empty:
        raise ValueError("缺少 trade_date/trade_time 列，无法判定 bar 时段")
    counts = sessions.value_counts()
    session = str(sessions.iloc[-1])
    if not strict:
        return session
    allowed = {"continuous"} if key in _MINUTE_FREQS else {"close"}
    bad = sorted(set(counts.index) - allowed)
    if bad:
        detail = "、".join(f"{name}×{int(counts[name])}" for name in bad)
        raise ValueError(
            f"{key} 频率下出现非法时段的 bar：{detail}；"
            f"合法时段为 {sorted(allowed)}（集合竞价/盘后固定价只能在渲染层标注，不能进数据门禁）"
        )
    return session
