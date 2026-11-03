"""Point-in-time index universe snapshots.

Sources (verified 2026-11-02):
- index_weight: historical index constituents and weights (2005+)
- stock_basic L/D: listed + delisted securities
- daily_basic: historical market cap
- bak_basic: historical name/industry snapshots (2016-11+)
- index_classify(SW2014) + index_member: pre-2016 SW L1 industry history
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CACHE_DIR
from .tushare_client import get_client

PIT_DIR = CACHE_DIR / "pit"
PIT_DIR.mkdir(parents=True, exist_ok=True)

# 申万 2021 版行业分类自 2021-12-13 起启用
SW_SWITCH_DATE = pd.Timestamp("2021-12-13")

INDEX_CODES = {
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "中证1000": "000852.SH",
    "创业板指": "399006.SZ",
    "上证50": "000016.SH",
}


def _fmt(day) -> str:
    return pd.Timestamp(day).strftime("%Y%m%d")


def _atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _read_parquet(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_parquet(path) if path.exists() else None
    except Exception:
        return None


def index_members(index_code: str = "000300.SH", start="2021-01-01", end=None, refresh=False) -> pd.DataFrame:
    end = end or pd.Timestamp.today()
    path = PIT_DIR / f"members_{index_code}_{_fmt(start)}_{_fmt(end)}.parquet"
    if not refresh:
        cached = _read_parquet(path)
        if cached is not None and not cached.empty:
            return cached
    df = get_client().query(
        "index_weight",
        index_code=index_code,
        start_date=_fmt(start),
        end_date=_fmt(end),
        fields="index_code,con_code,trade_date,weight",
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "code", "weight"])
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["trade_date"], errors="coerce"),
            "code": df["con_code"].astype(str),
            "weight": pd.to_numeric(df["weight"], errors="coerce"),
        }
    ).dropna(subset=["date", "code"])
    out = out.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)
    _atomic_parquet(out, path)
    return out


def security_master(refresh=False) -> pd.DataFrame:
    path = PIT_DIR / "security_master.parquet"
    if not refresh:
        cached = _read_parquet(path)
        if cached is not None and not cached.empty:
            return cached
    frames = []
    for status in ("L", "D"):
        df = get_client().query(
            "stock_basic",
            exchange="",
            list_status=status,
            fields="ts_code,name,industry,market,list_date,delist_date",
        )
        if df is None or df.empty:
            continue
        df = df.copy()
        df["status"] = status
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["code", "name", "industry", "market", "list_date", "delist_date", "status"])
    out = pd.concat(frames, ignore_index=True).drop_duplicates("ts_code")
    out = out.rename(columns={"ts_code": "code"})
    out["name"] = out["name"].fillna("").astype(str)
    out["industry"] = out["industry"].fillna("").astype(str)
    out["list_date"] = pd.to_datetime(out["list_date"], format="%Y%m%d", errors="coerce")
    out["delist_date"] = pd.to_datetime(out["delist_date"], format="%Y%m%d", errors="coerce")
    _atomic_parquet(out, path)
    return out


def _caps_on(date) -> dict[str, float]:
    path = PIT_DIR / f"caps_{_fmt(date)}.parquet"
    cached = _read_parquet(path)
    if cached is not None and not cached.empty:
        return dict(zip(cached["code"], cached["total_mv"]))
    df = get_client().query(
        "daily_basic",
        trade_date=_fmt(date),
        fields="ts_code,total_mv",
    )
    if df is None or df.empty:
        return {}
    out = pd.DataFrame(
        {
            "code": df["ts_code"].astype(str),
            "total_mv": pd.to_numeric(df["total_mv"], errors="coerce"),
        }
    ).dropna(subset=["total_mv"])
    _atomic_parquet(out, path)
    return dict(zip(out["code"], out["total_mv"]))


def _bak_industry_on(date) -> dict[str, tuple[str, str]]:
    """Return code -> (name, industry) from bak_basic (2016-11+)."""
    path = PIT_DIR / f"industry_{_fmt(date)}.parquet"
    cached = _read_parquet(path)
    if cached is not None and not cached.empty:
        return {str(r["code"]): (str(r["name"]), str(r["industry"])) for _, r in cached.iterrows()}
    df = get_client().query(
        "bak_basic",
        trade_date=_fmt(date),
        fields="trade_date,ts_code,name,industry",
    )
    if df is None or df.empty:
        return {}
    out = pd.DataFrame(
        {
            "code": df["ts_code"].astype(str),
            "name": df.get("name", "").fillna("").astype(str),
            "industry": df.get("industry", "").fillna("").astype(str),
        }
    ).drop_duplicates("code")
    _atomic_parquet(out, path)
    return {str(r["code"]): (str(r["name"]), str(r["industry"])) for _, r in out.iterrows()}


def sw_l1_members(version: str = "SW2014", refresh=False) -> pd.DataFrame:
    """SW L1 industry membership with in_date/out_date.

    version: SW2014 or SW2021. ``index_member`` without ``is_new`` returns
    current + historical members; one call per L1 industry.
    """
    version = str(version).upper()
    if version not in {"SW2014", "SW2021"}:
        raise ValueError(f"unsupported SW version: {version}")
    path = PIT_DIR / f"{version.lower()}_l1_members.parquet"
    if not refresh:
        cached = _read_parquet(path)
        if cached is not None and not cached.empty:
            return cached
    classify = get_client().query(
        "index_classify",
        src=version,
        fields="index_code,industry_name,level,industry_code,src",
    )
    if classify is None or classify.empty:
        return pd.DataFrame(columns=["code", "industry", "in_date", "out_date", "version"])
    l1 = classify[classify["level"].astype(str).str.upper() == "L1"]
    frames = []
    for _, row in l1.iterrows():
        index_code = str(row["index_code"])
        industry = str(row["industry_name"])
        try:
            members = get_client().query(
                "index_member",
                index_code=index_code,
                fields="index_code,con_code,in_date,out_date,is_new",
            )
        except Exception:
            continue
        if members is None or members.empty:
            continue
        df = pd.DataFrame(
            {
                "code": members["con_code"].astype(str),
                "industry": industry,
                "in_date": pd.to_datetime(members["in_date"], format="%Y%m%d", errors="coerce"),
                "out_date": pd.to_datetime(members["out_date"], format="%Y%m%d", errors="coerce"),
                "version": version,
            }
        )
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["code", "industry", "in_date", "out_date", "version"])
    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["code", "in_date"]).drop_duplicates(["code", "industry", "in_date"])
    out = out.sort_values(["code", "in_date"]).reset_index(drop=True)
    _atomic_parquet(out, path)
    return out


def sw_industry_on(date) -> dict[str, str]:
    """Map code -> SW L1 industry active on ``date``.

    Uses SW2014 before 2021-12-13 and SW2021 from that date onward.
    """
    day = pd.Timestamp(date).normalize()
    version = "SW2021" if day >= SW_SWITCH_DATE else "SW2014"
    members = sw_l1_members(version)
    if members is None or members.empty:
        return {}
    active = members[
        (members["in_date"] <= day)
        & (members["out_date"].isna() | (members["out_date"] > day))
    ].copy()
    if active.empty:
        return {}
    # 同一股票若有多条，取最近一次进入的行业
    active = active.sort_values("in_date").drop_duplicates("code", keep="last")
    return dict(zip(active["code"], active["industry"]))


def _industry_on(date) -> dict[str, tuple[str, str]]:
    """SW L1 industry + bak_basic historical name (name only from bak)."""
    sw = sw_industry_on(date)
    bak = _bak_industry_on(date)
    if not sw:
        return bak
    return {
        code: (bak.get(code, ("", ""))[0], industry)
        for code, industry in sw.items()
    }


def build_index_snapshot(
    index_code: str = "000300.SH",
    start="2021-01-01",
    end=None,
    refresh=False,
) -> pd.DataFrame:
    """Build date/code/weight/name/industry/total_mv/is_st snapshot."""
    end = end or pd.Timestamp.today()
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    cache_path = PIT_DIR / f"snapshot_v2_{index_code}_{_fmt(start)}_{_fmt(end)}.parquet"
    if not refresh:
        cached = _read_parquet(cache_path)
        if cached is not None and not cached.empty:
            return cached

    members = index_members(index_code, start, end, refresh=refresh)
    if members.empty:
        return pd.DataFrame(columns=["date", "code", "weight", "name", "industry", "industry_version", "total_mv", "is_st"])

    # SW2021 从 2021-12-13 起生效。index_weight 只有月末快照，补一个切换日快照：
    # 成分/权重沿用上一个快照，行业按 SW2021 重新取，保证 12-13 之后不再用 SW2014。
    if start_ts <= SW_SWITCH_DATE <= end_ts:
        existing = set(pd.to_datetime(members["date"]).dt.normalize())
        if SW_SWITCH_DATE not in existing:
            prior = members[pd.to_datetime(members["date"]) < SW_SWITCH_DATE]
            if not prior.empty:
                base_date = pd.to_datetime(prior["date"]).max()
                base = members[pd.to_datetime(members["date"]) == base_date].copy()
                base["date"] = SW_SWITCH_DATE
                members = pd.concat([members, base], ignore_index=True).drop_duplicates(["date", "code"])
                members = members.sort_values(["date", "code"]).reset_index(drop=True)
    master = security_master(refresh=refresh).set_index("code")
    frames = []
    for date, chunk in members.groupby("date"):
        caps = _caps_on(date)
        sw = sw_industry_on(date)
        if sw:
            bak = _bak_industry_on(date)
            ind = {code: (bak.get(code, ("", ""))[0], industry) for code, industry in sw.items()}
            industry_version = "SW2021" if pd.Timestamp(date) >= SW_SWITCH_DATE else "SW2014"
        else:
            ind = _bak_industry_on(date)
            industry_version = "BAK"
        rows = []
        for code in chunk["code"]:
            if code not in master.index:
                continue
            sec = master.loc[code]
            list_date = sec["list_date"]
            delist_date = sec["delist_date"]
            if pd.notna(list_date) and pd.Timestamp(list_date) > pd.Timestamp(date):
                continue
            if pd.notna(delist_date) and pd.Timestamp(delist_date) <= pd.Timestamp(date):
                continue
            name, industry = ind.get(code, (str(sec["name"]), str(sec["industry"])))
            if not name:
                name = str(sec["name"])
            rows.append(
                {
                    "date": pd.Timestamp(date),
                    "code": code,
                    "weight": float(chunk.loc[chunk["code"] == code, "weight"].iloc[0]),
                    "name": name,
                    "industry": industry or "其他",
                    "industry_version": industry_version,
                    "total_mv": caps.get(code, np.nan),
                    "is_st": int("ST" in name.upper()),
                }
            )
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame(columns=["date", "code", "weight", "name", "industry", "industry_version", "total_mv", "is_st"])
    out = pd.concat(frames, ignore_index=True).sort_values(["date", "code"]).reset_index(drop=True)
    _atomic_parquet(out, cache_path)
    return out


def snapshot_maps(snapshot: pd.DataFrame) -> tuple[dict[pd.Timestamp, set[str]], dict[pd.Timestamp, dict], dict[pd.Timestamp, dict]]:
    """Return membership, size, industry maps keyed by snapshot date."""
    membership: dict[pd.Timestamp, set[str]] = {}
    sizes: dict[pd.Timestamp, dict[str, float]] = {}
    industries: dict[pd.Timestamp, dict[str, str]] = {}
    for date, chunk in snapshot.groupby("date"):
        key = pd.Timestamp(date)
        membership[key] = set(chunk["code"].astype(str))
        sizes[key] = dict(zip(chunk["code"].astype(str), chunk["total_mv"]))
        industries[key] = dict(zip(chunk["code"].astype(str), chunk["industry"].astype(str)))
    return membership, sizes, industries


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build PIT index snapshot")
    parser.add_argument("--index", default="000300.SH")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    df = build_index_snapshot(args.index, args.start, args.end, args.refresh)
    print(f"{len(df)} rows, {df['date'].nunique() if len(df) else 0} snapshots, {df['code'].nunique() if len(df) else 0} codes")
    if len(df):
        print(df.head(3).to_string(index=False))


if __name__ == "__main__":
    main()
