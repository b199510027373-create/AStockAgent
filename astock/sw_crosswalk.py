"""SW2014 -> SW2021 L1 crosswalk and switch-date boundary calibration.

There is no official Shenwan crosswalk.  This module builds an *empirical*,
auditable mapping from the ``index_member`` history that ``astock.pit`` already
caches: every stock with both a SW2014 and a SW2021 L1 assignment around the
2021-12-13 switch contributes one old -> new pair, so every edge carries a
stock count and a share of both sides.

The module deliberately does not change the PIT main flow (``astock/pit.py``).
It only reads the same membership snapshots and writes separate artefacts:

- ``cache/pit/sw_crosswalk_edges.parquet``        old -> new edge table
- ``cache/pit/sw_crosswalk_summary.json``         classification + unmatched lists
- ``cache/pit/sw_crosswalk_distribution_<index>_<start>_<end>.parquet``
- ``cache/pit/sw_crosswalk_metrics_<index>_<start>_<end>.parquet``
- ``cache/pit/sw_crosswalk_meta_<index>_<start>_<end>.json``  cache key
- ``cache/pit/sw_crosswalk_bridge_<index>_<start>_<end>.json``

Every artifact cache is keyed by its inputs: the edge cache checks bridge
dates, thresholds and membership fingerprints; the distribution cache also
checks the index and window, so changing ``--start/--end`` can never silently
return a different window's rows.

Structural edge rule (documented, testable, and reported with the data):
an edge is *structural* when it covers >= 20% of the old industry's stocks or
>= 50% of the new industry's stocks.  Other edges are minority
reclassifications kept only for completeness, never used to aggregate across
versions.  Thresholds live in :data:`STRUCTURAL_OLD_SHARE` /
:data:`STRUCTURAL_NEW_SHARE` so a reviewer can re-run with a different cut.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from .pit import PIT_DIR, SW_SWITCH_DATE, build_index_snapshot, sw_l1_members

# Last day on which the SW2014 L1 membership is still effective.  ``index_member``
# closes most SW2014 rows with out_date=2021-12-10 (out_date is exclusive), so
# 2021-12-09 is the last day that still describes the full SW2014 universe.
SW2014_BRIDGE_DATE = pd.Timestamp("2021-12-09")
# First day of SW2021 L1 membership.
SW2021_BRIDGE_DATE = SW_SWITCH_DATE

STRUCTURAL_OLD_SHARE = 0.20
STRUCTURAL_NEW_SHARE = 0.50

EDGES_FILE = "sw_crosswalk_edges.parquet"
SUMMARY_FILE = "sw_crosswalk_summary.json"
BRIDGE_FILE_TEMPLATE = "sw_crosswalk_bridge_{index}_{start}_{end}.json"
DIST_FILE_TEMPLATE = "sw_crosswalk_distribution_{index}_{start}_{end}.parquet"
METRICS_FILE_TEMPLATE = "sw_crosswalk_metrics_{index}_{start}_{end}.parquet"
META_FILE_TEMPLATE = "sw_crosswalk_meta_{index}_{start}_{end}.json"

SW2014_MEMBERS_FILE = "sw2014_l1_members.parquet"
SW2021_MEMBERS_FILE = "sw2021_l1_members.parquet"

_EDGE_COLUMNS = [
    "sw2014_l1",
    "sw2021_l1",
    "n_stocks",
    "share_of_sw2014",
    "share_of_sw2021",
    "is_structural",
]


def _atomic_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_parquet(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_parquet(path) if path.exists() else None
    except Exception:
        return None


def _members_fingerprint(members: pd.DataFrame | None) -> str | None:
    """Stable content hash for a membership frame (used as a cache key)."""
    if members is None or members.empty:
        return None
    columns = ["code", "industry", "in_date", "out_date"]
    missing = sorted(set(columns) - set(members.columns))
    if missing:
        return None
    frame = members[columns].copy()
    frame["code"] = frame["code"].astype(str)
    frame["industry"] = frame["industry"].astype(str)
    for column in ("in_date", "out_date"):
        parsed = pd.to_datetime(frame[column], errors="coerce")
        frame[column] = parsed.dt.strftime("%Y-%m-%d").fillna("")
    frame = frame.sort_values(columns).reset_index(drop=True)
    payload = "\n".join("\t".join(row) for row in frame.itertuples(index=False, name=None))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_members_fingerprint(members: pd.DataFrame | None, path: Path) -> str | None:
    if members is not None:
        return _members_fingerprint(members)
    return _members_fingerprint(_read_parquet(path))


def _frame_fingerprint(frame: pd.DataFrame | None) -> str | None:
    if frame is None or frame.empty:
        return None
    values = pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()
    return hashlib.sha256(values).hexdigest()


def _cache_matches(
    summary: dict[str, Any],
    *,
    old_date: Any,
    new_date: Any,
    old_share: float,
    new_share: float,
    old_fingerprint: str | None,
    new_fingerprint: str | None,
) -> bool:
    """Return True only when cached edges were built from the requested inputs."""
    try:
        thresholds = summary["thresholds"]
        inputs = summary["inputs"]
        return (
            summary["bridge_old_date"] == pd.Timestamp(old_date).strftime("%Y-%m-%d")
            and summary["bridge_new_date"] == pd.Timestamp(new_date).strftime("%Y-%m-%d")
            and float(thresholds["structural_old_share"]) == float(old_share)
            and float(thresholds["structural_new_share"]) == float(new_share)
            and inputs["sw2014_members"] == old_fingerprint
            and inputs["sw2021_members"] == new_fingerprint
        )
    except (KeyError, TypeError, ValueError):
        return False


def active_industry_conflicts(
    members: pd.DataFrame | None,
    day: Any,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List codes with more than one active membership row on ``day``.

    ``active_industries`` keeps the latest ``in_date`` and, for exact ties,
    the last row in cache order.  Surfacing conflicts makes that tie-break
    auditable instead of silent.
    """
    if members is None or members.empty:
        return []
    required = {"code", "industry", "in_date", "out_date"}
    if required - set(members.columns):
        return []
    day_ts = pd.Timestamp(day).normalize()
    frame = members[["code", "industry", "in_date", "out_date"]].copy()
    frame["code"] = frame["code"].astype(str)
    frame["industry"] = frame["industry"].astype(str)
    frame["in_date"] = pd.to_datetime(frame["in_date"], errors="coerce")
    frame["out_date"] = pd.to_datetime(frame["out_date"], errors="coerce")
    frame = frame.dropna(subset=["code", "industry", "in_date"])
    active = frame[
        (frame["in_date"] <= day_ts)
        & (frame["out_date"].isna() | (frame["out_date"] > day_ts))
    ]
    rows = []
    for code, group in active.groupby("code", sort=True):
        if len(group) <= 1:
            continue
        rows.append(
            {
                "code": str(code),
                "n_records": int(len(group)),
                "n_industries": int(group["industry"].nunique()),
                "industries": sorted(set(group["industry"])),
                "in_dates": sorted(group["in_date"].dt.strftime("%Y-%m-%d")),
            }
        )
    return rows[:limit]


def active_industries(members: pd.DataFrame | None, day: Any) -> pd.Series:
    """Return code -> L1 industry active on ``day`` (``out_date`` exclusive)."""
    if members is None or members.empty:
        return pd.Series(dtype=object, name="industry")
    required = {"code", "industry", "in_date", "out_date"}
    missing = sorted(required - set(members.columns))
    if missing:
        raise ValueError(f"membership frame missing columns: {missing}")
    day_ts = pd.Timestamp(day).normalize()
    frame = members[["code", "industry", "in_date", "out_date"]].copy()
    frame["code"] = frame["code"].astype(str)
    frame["industry"] = frame["industry"].astype(str)
    frame["in_date"] = pd.to_datetime(frame["in_date"], errors="coerce")
    frame["out_date"] = pd.to_datetime(frame["out_date"], errors="coerce")
    frame = frame.dropna(subset=["code", "industry", "in_date"])
    frame = frame[frame["industry"].str.len() > 0]
    active = frame[(frame["in_date"] <= day_ts) & (frame["out_date"].isna() | (frame["out_date"] > day_ts))]
    if active.empty:
        return pd.Series(dtype=object, name="industry")
    active = active.sort_values(["code", "in_date"]).drop_duplicates("code", keep="last")
    series = active.set_index("code")["industry"]
    series.index = series.index.astype(str)
    series.name = "industry"
    return series


def paired_assignments(
    old_members: pd.DataFrame | None,
    new_members: pd.DataFrame | None,
    old_date: Any = SW2014_BRIDGE_DATE,
    new_date: Any = SW2021_BRIDGE_DATE,
) -> pd.DataFrame:
    """Inner-join SW2014 and SW2021 L1 assignments on stock code."""
    old = active_industries(old_members, old_date).rename("sw2014_l1")
    new = active_industries(new_members, new_date).rename("sw2021_l1")
    if old.empty or new.empty:
        return pd.DataFrame(columns=["code", "sw2014_l1", "sw2021_l1"])
    out = pd.concat([old, new], axis=1, join="inner").reset_index()
    out = out.rename(columns={out.columns[0]: "code"})
    out["code"] = out["code"].astype(str)
    return out[["code", "sw2014_l1", "sw2021_l1"]].sort_values("code").reset_index(drop=True)


def pair_coverage(
    old_members: pd.DataFrame | None,
    new_members: pd.DataFrame | None,
    old_date: Any = SW2014_BRIDGE_DATE,
    new_date: Any = SW2021_BRIDGE_DATE,
) -> dict[str, Any]:
    """Coverage of the bridge: shared codes, one-sided codes, label changes."""
    old = active_industries(old_members, old_date)
    new = active_industries(new_members, new_date)
    old_codes, new_codes = set(old.index), set(new.index)
    shared = old_codes & new_codes
    pairs = paired_assignments(old_members, new_members, old_date, new_date)
    changed = int((pairs["sw2014_l1"] != pairs["sw2021_l1"]).sum()) if not pairs.empty else 0
    return {
        "sw2014_codes": len(old_codes),
        "sw2021_codes": len(new_codes),
        "paired_codes": len(shared),
        "only_sw2014_codes": len(old_codes - new_codes),
        "only_sw2021_codes": len(new_codes - old_codes),
        "label_changed_codes": changed,
        "label_changed_share": round(changed / len(shared), 6) if shared else 0.0,
        "only_sw2014_samples": sorted(old_codes - new_codes)[:10],
        "only_sw2021_samples": sorted(new_codes - old_codes)[:10],
    }


def edge_table(
    pairs: pd.DataFrame,
    *,
    old_share: float = STRUCTURAL_OLD_SHARE,
    new_share: float = STRUCTURAL_NEW_SHARE,
) -> pd.DataFrame:
    """Aggregate stock pairs into an auditable old -> new edge table."""
    if pairs is None or pairs.empty:
        return pd.DataFrame(columns=_EDGE_COLUMNS)
    counts = (
        pairs.groupby(["sw2014_l1", "sw2021_l1"], dropna=False)
        .size()
        .rename("n_stocks")
        .reset_index()
    )
    counts["n_stocks"] = counts["n_stocks"].astype(int)
    counts["share_of_sw2014"] = counts["n_stocks"] / counts.groupby("sw2014_l1")["n_stocks"].transform("sum")
    counts["share_of_sw2021"] = counts["n_stocks"] / counts.groupby("sw2021_l1")["n_stocks"].transform("sum")
    counts["is_structural"] = (counts["share_of_sw2014"] >= old_share) | (
        counts["share_of_sw2021"] >= new_share
    )
    counts = counts.sort_values(["sw2014_l1", "n_stocks", "sw2021_l1"], ascending=[True, False, True])
    return counts[_EDGE_COLUMNS].reset_index(drop=True)


def classify_mapping(
    edges: pd.DataFrame,
    *,
    old_universe: list[str] | None = None,
    new_universe: list[str] | None = None,
) -> dict[str, Any]:
    """Split structural edges into 1:1 / 1:N / N:1 and list unmatched labels."""
    old_universe = sorted(old_universe) if old_universe else sorted(edges["sw2014_l1"].unique())
    new_universe = sorted(new_universe) if new_universe else sorted(edges["sw2021_l1"].unique())
    structural = edges[edges["is_structural"]] if not edges.empty else edges
    old_targets = {
        str(old): sorted(group["sw2021_l1"].astype(str))
        for old, group in structural.groupby("sw2014_l1")
    }
    new_sources = {
        str(new): sorted(group["sw2014_l1"].astype(str))
        for new, group in structural.groupby("sw2021_l1")
    }
    one_to_many = {old: targets for old, targets in old_targets.items() if len(targets) > 1}
    many_to_one = {new: sources for new, sources in new_sources.items() if len(sources) > 1}
    one_to_one = {old: targets[0] for old, targets in old_targets.items() if len(targets) == 1}
    renamed_old = {old: new for old, new in one_to_one.items() if old != new}
    retired_old = sorted(set(old_universe) - set(new_universe))
    new_labels = sorted(set(new_universe) - set(old_universe))
    return {
        "one_to_one": one_to_one,
        "one_to_many": one_to_many,
        "many_to_one": many_to_one,
        "renamed_old": renamed_old,
        "retired_old_labels": retired_old,
        "new_labels": new_labels,
        "old_without_structural_successor": sorted(set(old_universe) - set(old_targets)),
        "new_without_structural_source": sorted(set(new_universe) - set(new_sources)),
    }


def backmap_table(edges: pd.DataFrame, *, new_universe: list[str] | None = None) -> pd.DataFrame:
    """Map each SW2021 L1 label back to its structural SW2014 source(s).

    ``mapping`` is ``unique`` when exactly one structural edge points at the new
    label, ``ambiguous`` when several old labels feed it, and ``none`` when no
    structural edge points at it (a genuinely new taxonomy bucket).  A
    continuous industry series can only use the ``unique`` rows without
    pooling stocks from different old industries.
    """
    columns = ["sw2021_l1", "sw2014_backmap", "n_sources", "mapping"]
    if edges is None or edges.empty:
        return pd.DataFrame(columns=columns)
    structural = edges[edges["is_structural"]].copy()
    structural["sw2014_l1"] = structural["sw2014_l1"].astype(str)
    structural["sw2021_l1"] = structural["sw2021_l1"].astype(str)
    rows: list[dict[str, Any]] = []
    for new, group in structural.groupby("sw2021_l1", sort=True):
        sources = sorted(set(group["sw2014_l1"]))
        rows.append(
            {
                "sw2021_l1": str(new),
                "sw2014_backmap": sources[0] if len(sources) == 1 else "",
                "n_sources": len(sources),
                "mapping": "unique" if len(sources) == 1 else "ambiguous",
            }
        )
    covered = {row["sw2021_l1"] for row in rows}
    extra = (set(new_universe) if new_universe else set()) - covered
    for new in sorted(set(edges["sw2021_l1"].astype(str)) | extra):
        if new in covered:
            continue
        rows.append({"sw2021_l1": new, "sw2014_backmap": "", "n_sources": 0, "mapping": "none"})
    return pd.DataFrame(rows, columns=columns).sort_values("sw2021_l1").reset_index(drop=True)


def label_change_breakdown(pairs: pd.DataFrame, edges: pd.DataFrame) -> dict[str, Any]:
    """Split label changes into structural taxonomy moves and minority noise."""
    empty = {
        "structural_label_changed_codes": 0,
        "structural_label_changed_share": 0.0,
        "residual_label_changed_codes": 0,
        "residual_label_changed_share": 0.0,
    }
    if pairs is None or pairs.empty or edges is None or edges.empty:
        return empty
    merged = pairs.merge(
        edges[["sw2014_l1", "sw2021_l1", "is_structural"]],
        on=["sw2014_l1", "sw2021_l1"],
        how="left",
    )
    merged["is_structural"] = merged["is_structural"].fillna(False).astype(bool)
    changed = merged[merged["sw2014_l1"] != merged["sw2021_l1"]]
    structural = changed[changed["is_structural"]]
    total = len(merged)
    return {
        "structural_label_changed_codes": int(len(structural)),
        "structural_label_changed_share": round(len(structural) / total, 6) if total else 0.0,
        "residual_label_changed_codes": int(len(changed) - len(structural)),
        "residual_label_changed_share": round((len(changed) - len(structural)) / total, 6) if total else 0.0,
    }


def _edge_summary(edges: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for row in edges[edges["is_structural"]].itertuples(index=False):
        rows.append(
            {
                "sw2014_l1": str(row.sw2014_l1),
                "sw2021_l1": str(row.sw2021_l1),
                "n_stocks": int(row.n_stocks),
                "share_of_sw2014": round(float(row.share_of_sw2014), 4),
                "share_of_sw2021": round(float(row.share_of_sw2021), 4),
            }
        )
    return rows


def _unmatched_lists(
    pairs: pd.DataFrame,
    old_members: pd.DataFrame | None,
    new_members: pd.DataFrame | None,
    classification: dict[str, Any],
) -> dict[str, Any]:
    old_all = sorted(set(old_members["industry"].astype(str))) if old_members is not None and not old_members.empty else []
    new_all = sorted(set(new_members["industry"].astype(str))) if new_members is not None and not new_members.empty else []
    paired_old = set(pairs["sw2014_l1"].astype(str)) if not pairs.empty else set()
    paired_new = set(pairs["sw2021_l1"].astype(str)) if not pairs.empty else set()
    return {
        "sw2014_l1_total": old_all,
        "sw2021_l1_total": new_all,
        "sw2014_no_paired_stock": sorted(set(old_all) - paired_old),
        "sw2021_no_paired_stock": sorted(set(new_all) - paired_new),
        "sw2014_without_structural_successor": classification["old_without_structural_successor"],
        "sw2021_without_structural_source": classification["new_without_structural_source"],
        "retired_old_labels": classification["retired_old_labels"],
        "new_labels": classification["new_labels"],
    }


def build_crosswalk_artifacts(
    *,
    refresh: bool = False,
    old_members: pd.DataFrame | None = None,
    new_members: pd.DataFrame | None = None,
    old_date: Any = SW2014_BRIDGE_DATE,
    new_date: Any = SW2021_BRIDGE_DATE,
    old_share: float = STRUCTURAL_OLD_SHARE,
    new_share: float = STRUCTURAL_NEW_SHARE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build (or load) the edge table and its JSON audit summary.

    The cache is validated against bridge dates, both structural thresholds
    and fingerprints of the membership inputs, so changing any of them forces
    a rebuild even without ``refresh=True``.
    """
    edges_path = PIT_DIR / EDGES_FILE
    summary_path = PIT_DIR / SUMMARY_FILE
    old_fingerprint = _cache_members_fingerprint(old_members, PIT_DIR / SW2014_MEMBERS_FILE)
    new_fingerprint = _cache_members_fingerprint(new_members, PIT_DIR / SW2021_MEMBERS_FILE)
    if not refresh and old_fingerprint is not None and new_fingerprint is not None:
        cached = _read_parquet(edges_path)
        if cached is not None and not cached.empty and summary_path.exists():
            try:
                cached_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                cached_summary = None
            if cached_summary is not None and _cache_matches(
                cached_summary,
                old_date=old_date,
                new_date=new_date,
                old_share=old_share,
                new_share=new_share,
                old_fingerprint=old_fingerprint,
                new_fingerprint=new_fingerprint,
            ):
                return cached, cached_summary

    old = old_members if old_members is not None else sw_l1_members("SW2014", refresh=refresh)
    new = new_members if new_members is not None else sw_l1_members("SW2021", refresh=refresh)
    pairs = paired_assignments(old, new, old_date, new_date)
    edges = edge_table(pairs, old_share=old_share, new_share=new_share)
    old_universe = sorted(set(old["industry"].astype(str))) if old is not None and not old.empty else []
    new_universe = sorted(set(new["industry"].astype(str))) if new is not None and not new.empty else []
    classification = classify_mapping(edges, old_universe=old_universe, new_universe=new_universe)
    coverage = pair_coverage(old, new, old_date, new_date)
    coverage.update(label_change_breakdown(pairs, edges))
    summary: dict[str, Any] = {
        "switch_date": SW_SWITCH_DATE.strftime("%Y-%m-%d"),
        "bridge_old_date": pd.Timestamp(old_date).strftime("%Y-%m-%d"),
        "bridge_new_date": pd.Timestamp(new_date).strftime("%Y-%m-%d"),
        "thresholds": {
            "structural_old_share": float(old_share),
            "structural_new_share": float(new_share),
        },
        "inputs": {
            "sw2014_members": _members_fingerprint(old),
            "sw2021_members": _members_fingerprint(new),
        },
        "n_sw2014_l1": len(old_universe),
        "n_sw2021_l1": len(new_universe),
        "coverage": coverage,
        "structural_pairs": _edge_summary(edges),
        "backmap": backmap_table(edges, new_universe=new_universe).to_dict(orient="records"),
        "duplicate_active_codes": {
            "sw2014": active_industry_conflicts(old, old_date),
            "sw2021": active_industry_conflicts(new, new_date),
        },
        **classification,
        "unmatched": _unmatched_lists(pairs, old, new, classification),
    }
    _atomic_parquet(edges, edges_path)
    _atomic_json(summary, summary_path)
    return edges, summary


def distribution_table(snapshot: pd.DataFrame, summary: dict[str, Any]) -> pd.DataFrame:
    """Long table: date x industry -> stock count and weight shares.

    ``is_sw`` marks labels that belong to the active taxonomy; rows that fell
    back to ``stock_basic`` names are kept but marked ``False``.
    ``weight_share`` uses all index members as denominator (the index's own
    distribution), while ``sw_weight_share`` uses only real SW members, which
    matches :func:`snapshot_metrics` and is the column to use for cross-version
    industry comparisons.
    """
    columns = [
        "date",
        "industry",
        "industry_version",
        "is_sw",
        "n_stocks",
        "weight",
        "weight_share",
        "sw_weight_share",
    ]
    if snapshot is None or snapshot.empty:
        return pd.DataFrame(columns=columns)
    frame = snapshot.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["industry"] = frame["industry"].astype(str)
    universes = {
        "SW2014": _version_universe(summary, "SW2014"),
        "SW2021": _version_universe(summary, "SW2021"),
    }
    versions = frame["industry_version"].astype(str) if "industry_version" in frame.columns else pd.Series("", index=frame.index)
    frame["is_sw"] = [
        industry in universes.get(version, set())
        for industry, version in zip(frame["industry"], versions)
    ]
    grouped = (
        frame.groupby(["date", "industry", "is_sw"], as_index=False)
        .agg(
            industry_version=("industry_version", "first"),
            n_stocks=("code", "nunique"),
            weight=("weight", "sum"),
        )
    )
    totals = grouped.groupby("date")["weight"].transform("sum")
    grouped["weight_share"] = (grouped["weight"] / totals.where(totals != 0)).fillna(0.0)
    sw_totals = grouped[grouped["is_sw"]].groupby("date")["weight"].sum()
    grouped["sw_weight_share"] = (
        grouped["weight"] / grouped["date"].map(sw_totals)
    ).fillna(0.0)
    return grouped[columns].sort_values(
        ["date", "n_stocks", "industry"], ascending=[True, False, True]
    ).reset_index(drop=True)


def _ambiguous_labels(summary: dict[str, Any]) -> tuple[set[str], set[str]]:
    pre = set(summary.get("one_to_many", {}))
    pre |= set(summary.get("unmatched", {}).get("sw2014_without_structural_successor", []))
    post = set(summary.get("many_to_one", {}))
    post |= set(summary.get("unmatched", {}).get("sw2021_without_structural_source", []))
    return pre, post


def _version_universe(summary: dict[str, Any], version: str) -> set[str]:
    key = "sw2014_l1_total" if version == "SW2014" else "sw2021_l1_total"
    return set(summary.get("unmatched", {}).get(key, []))


def snapshot_metrics(snapshot: pd.DataFrame, summary: dict[str, Any]) -> pd.DataFrame:
    """Per-snapshot distribution metrics, ambiguity and non-SW-label exposure.

    Distribution metrics (industry count, top share, HHI, ambiguity) use only
    rows whose ``industry`` is a real L1 label of the active taxonomy.  Rows
    that fell back to ``stock_basic`` names are counted separately instead of
    being mixed into the taxonomy comparison.
    """
    columns = [
        "date",
        "industry_version",
        "n_members",
        "n_sw_members",
        "n_sw_industries",
        "n_all_labels",
        "top_industry",
        "top_weight_share",
        "hhi_count",
        "hhi_weight",
        "ambiguous_labels",
        "ambiguous_share_count",
        "ambiguous_share_weight",
        "non_sw_labels",
        "non_sw_share_count",
        "non_sw_share_weight",
    ]
    if snapshot is None or snapshot.empty:
        return pd.DataFrame(columns=columns)
    pre_labels, post_labels = _ambiguous_labels(summary)
    frame = snapshot.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame["industry"] = frame["industry"].astype(str)
    frame["weight"] = pd.to_numeric(frame["weight"], errors="coerce").fillna(0.0)
    rows: list[dict[str, Any]] = []
    for date, chunk in frame.groupby("date"):
        version = str(chunk["industry_version"].iloc[0])
        labels = pre_labels if version == "SW2014" else post_labels if version == "SW2021" else set()
        universe = _version_universe(summary, version) if version in {"SW2014", "SW2021"} else set()
        sw = chunk[chunk["industry"].isin(universe)]
        non_sw = chunk[~chunk["industry"].isin(universe)]
        n_members = int(len(chunk))
        n_sw = int(len(sw))
        total_weight_all = float(chunk["weight"].sum())
        total_weight_sw = float(sw["weight"].sum())
        counts = sw.groupby("industry")["code"].nunique()
        weights = sw.groupby("industry")["weight"].sum()
        weight_share = weights / total_weight_sw if total_weight_sw else weights * 0.0
        count_share = counts / n_sw if n_sw else counts * 0.0
        top_industry = str(weight_share.idxmax()) if len(weight_share) else ""
        ambiguous = sw["industry"].isin(labels)
        non_sw_labels = sorted(set(non_sw["industry"].astype(str)))
        rows.append(
            {
                "date": pd.Timestamp(date),
                "industry_version": version,
                "n_members": n_members,
                "n_sw_members": n_sw,
                "n_sw_industries": int(counts.size),
                "n_all_labels": int(chunk["industry"].nunique()),
                "top_industry": top_industry,
                "top_weight_share": round(float(weight_share.max()) if len(weight_share) else 0.0, 6),
                "hhi_count": round(float((count_share**2).sum()), 6),
                "hhi_weight": round(float((weight_share**2).sum()), 6),
                "ambiguous_labels": ",".join(sorted(labels)),
                "ambiguous_share_count": round(float(ambiguous.mean()) if n_sw else 0.0, 6),
                "ambiguous_share_weight": round(
                    float(sw.loc[ambiguous, "weight"].sum() / total_weight_sw) if total_weight_sw else 0.0,
                    6,
                ),
                "non_sw_labels": ",".join(non_sw_labels),
                "non_sw_share_count": round(len(non_sw) / n_members, 6) if n_members else 0.0,
                "non_sw_share_weight": round(
                    float(non_sw["weight"].sum() / total_weight_all) if total_weight_all else 0.0, 6
                ),
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values("date").reset_index(drop=True)


def bridge_diff(
    snapshot: pd.DataFrame,
    edges: pd.DataFrame,
    old_day: Any,
    new_day: Any,
) -> dict[str, Any]:
    """Quantify label churn for one index between two snapshot dates.

    Weight shares use the ``new_day`` weights, i.e. the portfolio that a
    post-switch reader would actually hold.  ``comparable`` is conservative:
    the new label must back-map to exactly one old label *and* the stock's old
    label must be that source, so a continuous industry series can be built
    without pooling stocks from different old industries.
    """
    required = {"date", "code", "industry", "weight"}
    if snapshot is None or snapshot.empty:
        raise ValueError("snapshot is empty")
    missing = sorted(required - set(snapshot.columns))
    if missing:
        raise ValueError(f"snapshot missing columns: {missing}")
    frame = snapshot.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    old_day_ts, new_day_ts = pd.Timestamp(old_day).normalize(), pd.Timestamp(new_day).normalize()
    old = frame[frame["date"] == old_day_ts][["code", "industry", "weight"]].rename(
        columns={"industry": "sw2014_l1", "weight": "w_old"}
    )
    new = frame[frame["date"] == new_day_ts][["code", "industry", "weight"]].rename(
        columns={"industry": "sw2021_l1", "weight": "w_new"}
    )
    if old.empty or new.empty:
        raise ValueError(f"no snapshot rows on {old_day_ts.date()} or {new_day_ts.date()}")
    merged = old.merge(new, on="code", how="inner")
    merged = merged.merge(
        edges[["sw2014_l1", "sw2021_l1", "is_structural"]],
        on=["sw2014_l1", "sw2021_l1"],
        how="left",
    )
    merged["is_structural"] = merged["is_structural"].fillna(False).astype(bool)
    merged["changed"] = merged["sw2014_l1"] != merged["sw2021_l1"]
    total_weight = float(merged["w_new"].sum())
    changed = merged[merged["changed"]]
    structural = changed[changed["is_structural"]]
    residual = changed[~changed["is_structural"]]

    structural_edges = edges[edges["is_structural"]]
    targets = structural_edges.groupby("sw2014_l1")["sw2021_l1"].agg(lambda s: sorted(set(s)))
    forward_clean = merged.apply(
        lambda row: bool(
            row["sw2014_l1"] in targets.index
            and targets.loc[row["sw2014_l1"]] == [row["sw2021_l1"]]
        ),
        axis=1,
    )
    back = backmap_table(edges)
    merged = merged.merge(back, on="sw2021_l1", how="left")
    merged["backward_clean"] = (merged["mapping"] == "unique") & (merged["sw2014_backmap"] == merged["sw2014_l1"])

    def _share(mask: pd.Series) -> float:
        return round(float(merged.loc[mask, "w_new"].sum() / total_weight), 6) if total_weight else 0.0

    def _count_share(mask: pd.Series) -> float:
        return round(float(mask.mean()), 6) if len(merged) else 0.0

    # Taxonomy-only effect: same stocks, same (new-day) weights, old vs new
    # labels, restricted to real L1 labels on both sides (fallback names out).
    old_universe = set(edges["sw2014_l1"].astype(str))
    new_universe = set(edges["sw2021_l1"].astype(str))
    sw_pairs = merged[merged["sw2014_l1"].isin(old_universe) & merged["sw2021_l1"].isin(new_universe)]
    old_counts = sw_pairs.groupby("sw2014_l1")["code"].nunique()
    new_counts = sw_pairs.groupby("sw2021_l1")["code"].nunique()
    old_weights = sw_pairs.groupby("sw2014_l1")["w_new"].sum()
    new_weights = sw_pairs.groupby("sw2021_l1")["w_new"].sum()

    flows = (
        changed.groupby(["sw2014_l1", "sw2021_l1"])
        .agg(n_stocks=("code", "size"), weight_share=("w_new", "sum"))
        .reset_index()
        .sort_values(["n_stocks", "sw2014_l1", "sw2021_l1"], ascending=[False, True, True])
    )
    top_flows = [
        {
            "sw2014_l1": str(row.sw2014_l1),
            "sw2021_l1": str(row.sw2021_l1),
            "n_stocks": int(row.n_stocks),
            "weight_share": round(float(row.weight_share / total_weight), 6) if total_weight else 0.0,
        }
        for row in flows.head(12).itertuples(index=False)
    ]
    def _hhi(series: pd.Series) -> float:
        total = float(series.sum())
        if total <= 0:
            return 0.0
        share = series / total
        return round(float((share**2).sum()), 6)

    return {
        "available": True,
        "old_date": old_day_ts.strftime("%Y-%m-%d"),
        "new_date": new_day_ts.strftime("%Y-%m-%d"),
        "n_labels_old": int(old_counts.size),
        "n_labels_new": int(new_counts.size),
        "hhi_count_old": _hhi(old_counts),
        "hhi_count_new": _hhi(new_counts),
        "hhi_weight_old": _hhi(old_weights),
        "hhi_weight_new": _hhi(new_weights),
        "paired_codes": int(len(merged)),
        "only_old_codes": int(len(old) - len(merged)),
        "only_new_codes": int(len(new) - len(merged)),
        "changed_codes": int(len(changed)),
        "changed_share": _count_share(merged["changed"]),
        "changed_weight_share": _share(merged["changed"]),
        "structural_changed_codes": int(len(structural)),
        "structural_changed_share": _count_share(merged["changed"] & merged["is_structural"]),
        "structural_changed_weight_share": _share(merged["changed"] & merged["is_structural"]),
        "residual_changed_codes": int(len(residual)),
        "residual_changed_share": _count_share(merged["changed"] & ~merged["is_structural"]),
        "residual_changed_weight_share": _share(merged["changed"] & ~merged["is_structural"]),
        "forward_clean_share": _count_share(forward_clean),
        "forward_clean_weight_share": _share(forward_clean),
        "comparable_codes": int(merged["backward_clean"].sum()),
        "comparable_share": _count_share(merged["backward_clean"]),
        "comparable_weight_share": _share(merged["backward_clean"]),
        "ambiguous_backmap_codes": int((merged["mapping"] == "ambiguous").sum()),
        "unmapped_backmap_codes": int((merged["mapping"] == "none").sum()),
        "top_flows": top_flows,
    }


def switch_bridge_diff(snapshot: pd.DataFrame, edges: pd.DataFrame) -> dict[str, Any]:
    """Bridge the last snapshot before the switch to the first one after it."""
    if snapshot is None or snapshot.empty:
        return {"available": False}
    frame = snapshot.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    dates = sorted(pd.Timestamp(d) for d in frame["date"].unique())
    pre = [d for d in dates if d < SW_SWITCH_DATE]
    post = [d for d in dates if d >= SW_SWITCH_DATE]
    if not pre or not post:
        return {"available": False}
    return bridge_diff(frame, edges, pre[-1], post[0])


def month_end_snapshots(snapshot: pd.DataFrame) -> pd.DataFrame:
    """Keep the last snapshot inside each calendar month."""
    if snapshot is None or snapshot.empty:
        return snapshot
    frame = snapshot.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    dates = frame[["date"]].drop_duplicates().copy()
    dates["period"] = dates["date"].dt.to_period("M")
    last = dates.groupby("period")["date"].max().to_list()
    return frame[frame["date"].isin(last)].reset_index(drop=True)


def window_dates(snapshot: pd.DataFrame, n: int = 6) -> list[pd.Timestamp]:
    """Return up to ``n`` month-end snapshots before and after the switch."""
    ends = month_end_snapshots(snapshot)
    dates = sorted(pd.to_datetime(ends["date"]).dt.normalize().unique())
    pre = [pd.Timestamp(d) for d in dates if pd.Timestamp(d) < SW_SWITCH_DATE][-n:]
    post = [pd.Timestamp(d) for d in dates if pd.Timestamp(d) >= SW_SWITCH_DATE][:n]
    return pre + post


def _month_chunks(start: Any, end: Any, months: int = 3) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    chunks: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cursor = start_ts
    while cursor <= end_ts:
        chunk_end = min(cursor + pd.DateOffset(months=months) - pd.Timedelta(days=1), end_ts)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + pd.Timedelta(days=1)
    return chunks


def snapshot_window(
    index_code: str = "000300.SH",
    start: Any = "2021-06-01",
    end: Any = "2022-05-31",
    *,
    refresh: bool = False,
) -> pd.DataFrame:
    """Build a multi-month snapshot in <=3-month chunks.

    ``index_weight`` caps a single response at ~6000 rows (20 dates for a
    300-name index); chunking keeps every request below that cap so early
    months are not silently dropped.
    """
    frames = []
    for chunk_start, chunk_end in _month_chunks(start, end):
        part = build_index_snapshot(index_code, chunk_start, chunk_end, refresh=refresh)
        if part is not None and not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame(
            columns=["date", "code", "weight", "name", "industry", "industry_version", "total_mv", "is_st"]
        )
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out = out.drop_duplicates(["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)
    return out


def _window_slug(start: Any, end: Any) -> tuple[str, str]:
    return (
        pd.Timestamp(start).strftime("%Y%m%d"),
        pd.Timestamp(end).strftime("%Y%m%d"),
    )


def _distribution_paths(index_code: str, start: Any, end: Any) -> dict[str, Path]:
    start_slug, end_slug = _window_slug(start, end)
    return {
        "distribution": PIT_DIR / DIST_FILE_TEMPLATE.format(index=index_code, start=start_slug, end=end_slug),
        "metrics": PIT_DIR / METRICS_FILE_TEMPLATE.format(index=index_code, start=start_slug, end=end_slug),
        "bridge": PIT_DIR / BRIDGE_FILE_TEMPLATE.format(index=index_code, start=start_slug, end=end_slug),
        "meta": PIT_DIR / META_FILE_TEMPLATE.format(index=index_code, start=start_slug, end=end_slug),
    }


def _distribution_meta(
    index_code: str,
    start: Any,
    end: Any,
    summary: dict[str, Any],
    edges: pd.DataFrame,
) -> dict[str, Any]:
    start_slug, end_slug = _window_slug(start, end)
    return {
        "index": index_code,
        "start": start_slug,
        "end": end_slug,
        "bridge_old_date": summary["bridge_old_date"],
        "bridge_new_date": summary["bridge_new_date"],
        "thresholds": summary["thresholds"],
        "edges_fingerprint": _frame_fingerprint(edges),
    }


def build_distribution_artifacts(
    index_code: str = "000300.SH",
    start: Any = "2021-06-01",
    end: Any = "2022-05-31",
    *,
    refresh: bool = False,
    snapshot: pd.DataFrame | None = None,
    old_date: Any = SW2014_BRIDGE_DATE,
    new_date: Any = SW2021_BRIDGE_DATE,
    old_share: float = STRUCTURAL_OLD_SHARE,
    new_share: float = STRUCTURAL_NEW_SHARE,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build (or load) distribution and per-snapshot metric parquet files.

    The parquet/JSON names include ``index``/``start``/``end`` and the cache
    only hits when the stored meta matches the current bridge dates,
    thresholds and edge fingerprint.  A different window therefore always
    rebuilds instead of silently reusing another window's rows.
    """
    paths = _distribution_paths(index_code, start, end)
    edges, summary = build_crosswalk_artifacts(
        refresh=refresh,
        old_date=old_date,
        new_date=new_date,
        old_share=old_share,
        new_share=new_share,
    )
    meta = _distribution_meta(index_code, start, end, summary, edges)
    if not refresh and paths["meta"].exists():
        try:
            cached_meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
        except Exception:
            cached_meta = None
        cached_dist = _read_parquet(paths["distribution"])
        cached_metrics = _read_parquet(paths["metrics"])
        if (
            cached_meta == meta
            and cached_dist is not None
            and not cached_dist.empty
            and cached_metrics is not None
            and not cached_metrics.empty
        ):
            return cached_dist, cached_metrics

    snap = snapshot if snapshot is not None else snapshot_window(index_code, start, end, refresh=refresh)
    dist = distribution_table(snap, summary)
    metrics = snapshot_metrics(snap, summary)
    _atomic_parquet(dist, paths["distribution"])
    _atomic_parquet(metrics, paths["metrics"])
    diff = switch_bridge_diff(snap, edges)
    if diff.get("available"):
        _atomic_json(diff, paths["bridge"])
    elif paths["bridge"].exists():
        paths["bridge"].unlink()
    _atomic_json(meta, paths["meta"])
    return dist, metrics


def export_audit_docs(
    edges: pd.DataFrame,
    metrics: pd.DataFrame,
    *,
    docs_dir: Path = Path("docs"),
) -> tuple[Path, Path]:
    """Write the two committed audit CSVs from cached artifacts.

    The metrics CSV keeps the 6 month-ends before the switch, the switch-day
    snapshot itself, and the 6 month-ends after it; other snapshots in the
    window (e.g. 2021-12-01) are intentionally excluded so the table stays a
    6+1+6 comparison rather than an arbitrary sample.
    """
    docs_dir.mkdir(parents=True, exist_ok=True)
    crosswalk_path = docs_dir / "sw2014_sw2021_crosswalk.csv"
    metrics_path = docs_dir / "sw2014_sw2021_distribution_metrics.csv"
    audit_edges = edges.copy()
    for column in ("share_of_sw2014", "share_of_sw2021"):
        if column in audit_edges:
            audit_edges[column] = audit_edges[column].astype(float).round(6)
    audit_edges.to_csv(
        crosswalk_path,
        index=False,
        encoding="utf-8-sig",
    )
    audit = metrics.copy()
    audit["date"] = pd.to_datetime(audit["date"]).dt.normalize()
    last_per_month = audit.groupby(audit["date"].dt.to_period("M"))["date"].transform("max")
    keep = (audit["date"] == last_per_month) | (audit["date"] == SW_SWITCH_DATE)
    audit = audit[keep].sort_values("date").reset_index(drop=True)
    audit.to_csv(
        metrics_path,
        index=False,
        encoding="utf-8-sig",
    )
    return crosswalk_path, metrics_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SW2014 -> SW2021 L1 crosswalk artifacts")
    parser.add_argument("--index", default="000300.SH")
    parser.add_argument("--start", default="2021-06-01")
    parser.add_argument("--end", default="2022-05-31")
    parser.add_argument("--old-date", default=SW2014_BRIDGE_DATE.strftime("%Y-%m-%d"))
    parser.add_argument("--new-date", default=SW2021_BRIDGE_DATE.strftime("%Y-%m-%d"))
    parser.add_argument("--old-share", type=float, default=STRUCTURAL_OLD_SHARE)
    parser.add_argument("--new-share", type=float, default=STRUCTURAL_NEW_SHARE)
    parser.add_argument("--export-docs", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    edges, summary = build_crosswalk_artifacts(
        refresh=args.refresh,
        old_date=args.old_date,
        new_date=args.new_date,
        old_share=args.old_share,
        new_share=args.new_share,
    )
    dist, metrics = build_distribution_artifacts(
        args.index,
        args.start,
        args.end,
        refresh=args.refresh,
        old_date=args.old_date,
        new_date=args.new_date,
        old_share=args.old_share,
        new_share=args.new_share,
    )
    print(f"edges: {len(edges)} ({int(edges['is_structural'].sum())} structural)")
    print(f"paired codes: {summary['coverage']['paired_codes']}, label changed: {summary['coverage']['label_changed_codes']}")
    print(f"1:N: {summary['one_to_many']}")
    print(f"N:1: {summary['many_to_one']}")
    print(f"new L1 without structural source: {summary['new_without_structural_source']}")
    print(
        f"thresholds: old>={summary['thresholds']['structural_old_share']:.2f}, "
        f"new>={summary['thresholds']['structural_new_share']:.2f}; "
        f"bridge {summary['bridge_old_date']} -> {summary['bridge_new_date']}"
    )
    print(f"distribution rows: {len(dist)}; snapshot metrics rows: {len(metrics)}")
    paths = _distribution_paths(args.index, args.start, args.end)
    print(f"artifacts: {paths['metrics'].name}, {paths['meta'].name}")
    if paths["bridge"].exists():
        diff = json.loads(paths["bridge"].read_text(encoding="utf-8"))
        if diff.get("available"):
            print(
                f"switch bridge {diff['old_date']} -> {diff['new_date']}: "
                f"changed {diff['changed_share']:.1%} of codes, {diff['changed_weight_share']:.1%} of weight; "
                f"conservatively comparable {diff['comparable_weight_share']:.1%} of weight"
            )
    if args.export_docs:
        crosswalk_path, metrics_path = export_audit_docs(edges, metrics)
        print(f"exported: {crosswalk_path}, {metrics_path}")


if __name__ == "__main__":
    main()
