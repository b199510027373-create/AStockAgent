"""Offline tests for the SW2014 -> SW2021 L1 crosswalk module.

The fixtures are synthetic so the tests document the *rules* (out_date is
exclusive, structural thresholds, back-map ambiguity) rather than freezing
today's Tushare data.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from astock import sw_crosswalk as sw


def _members(rows: list[tuple[str, str, str, str | None, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["code", "industry", "in_date", "out_date", "version"])


OLD_MEMBERS = _members(
    [
        ("A", "采掘", "2020-01-01", "2021-12-10", "SW2014"),
        ("B", "采掘", "2020-01-01", "2021-12-10", "SW2014"),
        ("C", "采掘", "2020-01-01", "2021-12-10", "SW2014"),
        ("X", "采掘", "2020-01-01", "2021-12-10", "SW2014"),
        ("D", "化工", "2020-01-01", "2021-12-10", "SW2014"),
        ("E", "化工", "2020-01-01", "2021-12-10", "SW2014"),
        ("F", "银行", "2020-01-01", "2021-12-10", "SW2014"),
        ("G", "综合", "2020-01-01", "2021-12-10", "SW2014"),
        ("H", "综合", "2020-01-01", "2021-12-10", "SW2014"),
        ("I", "传媒", "2020-01-01", "2021-12-10", "SW2014"),
        ("L", "电气设备", "2020-01-01", "2021-12-10", "SW2014"),
        ("Y", "钢铁", "2020-01-01", "2021-12-10", "SW2014"),
    ]
)

NEW_MEMBERS = _members(
    [
        ("A", "煤炭", "2021-12-13", None, "SW2021"),
        ("B", "煤炭", "2021-12-13", None, "SW2021"),
        ("C", "石油石化", "2021-12-13", None, "SW2021"),
        ("D", "基础化工", "2021-12-13", None, "SW2021"),
        ("E", "石油石化", "2021-12-13", None, "SW2021"),
        ("F", "银行", "2021-12-13", None, "SW2021"),
        ("G", "综合", "2021-12-13", None, "SW2021"),
        ("H", "社会服务", "2021-12-13", None, "SW2021"),
        ("I", "传媒", "2021-12-13", None, "SW2021"),
        ("J", "美容护理", "2021-12-13", None, "SW2021"),
        ("L", "电力设备", "2021-12-13", None, "SW2021"),
    ]
)

# 41 stock pairs with deliberately different structural shares.
CLASSIFY_PAIRS = [
    ("采掘", "煤炭", 6),
    ("采掘", "石油石化", 4),
    ("采掘", "银行", 1),
    ("化工", "基础化工", 8),
    ("化工", "石油石化", 2),
    ("综合", "综合", 2),
    ("综合", "社会服务", 2),
    ("休闲服务", "社会服务", 3),
    ("电气设备", "电力设备", 5),
    ("银行", "银行", 3),
    ("传媒", "传媒", 5),
]


def _expand(pairs: list[tuple[str, str, int]]) -> pd.DataFrame:
    rows = []
    for i, (old, new, n) in enumerate(pairs):
        for j in range(n):
            rows.append({"code": f"{i:02d}{j:03d}", "sw2014_l1": old, "sw2021_l1": new})
    return pd.DataFrame(rows)


CLASSIFY_EDGES = sw.edge_table(_expand(CLASSIFY_PAIRS))

# Mirrors the two edges that become structural at 10%/40%: 传媒->社会服务
# (old-side 10.7%) and 化工->美容护理 (new-side 46.2%).  The other counts are
# synthetic but keep every non-target edge below both default thresholds.
SENSITIVITY_PAIRS = [
    ("传媒", "传媒", 158),
    ("传媒", "社会服务", 19),
    ("化工", "基础化工", 360),
    ("化工", "石油石化", 26),
    ("化工", "美容护理", 12),
    ("休闲服务", "社会服务", 37),
    ("综合", "综合", 28),
    ("综合", "社会服务", 11),
    ("轻工制造", "纺织服饰", 91),
    ("轻工制造", "美容护理", 9),
    ("采掘", "煤炭", 55),
    ("采掘", "美容护理", 5),
]
SENSITIVITY_OLD_UNIVERSE = ["传媒", "化工", "休闲服务", "综合", "轻工制造", "采掘"]
SENSITIVITY_NEW_UNIVERSE = ["传媒", "社会服务", "基础化工", "石油石化", "美容护理", "综合", "纺织服饰", "煤炭"]


def _row(frame: pd.DataFrame, **where: str) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    for column, value in where.items():
        mask &= frame[column] == value
    selected = frame[mask]
    if len(selected) != 1:
        raise AssertionError(f"expected one row for {where}, got {len(selected)}")
    return selected.iloc[0]


class ActiveIndustriesTests(unittest.TestCase):
    def test_out_date_is_exclusive(self) -> None:
        active_before = sw.active_industries(OLD_MEMBERS, "2021-12-09")
        active_on = sw.active_industries(OLD_MEMBERS, "2021-12-10")
        self.assertEqual(len(active_before), 12)
        self.assertEqual(len(active_on), 0)
        self.assertEqual(len(sw.active_industries(NEW_MEMBERS, "2021-12-13")), 11)

    def test_latest_in_date_wins(self) -> None:
        frame = _members(
            [
                ("B", "化工", "2020-01-01", "2021-07-01", "SW2014"),
                ("B", "基础化工", "2021-06-01", None, "SW2021"),
            ]
        )
        self.assertEqual(sw.active_industries(frame, "2021-06-01")["B"], "基础化工")

    def test_missing_columns_raise(self) -> None:
        with self.assertRaises(ValueError):
            sw.active_industries(pd.DataFrame({"code": ["A"], "industry": ["银行"]}), "2021-12-09")

    def test_duplicate_active_codes_are_reported(self) -> None:
        frame = _members(
            [
                ("B", "化工", "2020-01-01", None, "SW2014"),
                ("B", "采掘", "2021-01-01", None, "SW2014"),
            ]
        )
        conflicts = sw.active_industry_conflicts(frame, "2021-12-09")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["code"], "B")
        self.assertEqual(conflicts[0]["n_records"], 2)
        self.assertEqual(conflicts[0]["industries"], ["化工", "采掘"])


class PairingTests(unittest.TestCase):
    def test_paired_assignments_inner_join(self) -> None:
        pairs = sw.paired_assignments(OLD_MEMBERS, NEW_MEMBERS)
        self.assertEqual(len(pairs), 10)
        self.assertEqual(set(pairs["code"]), {"A", "B", "C", "D", "E", "F", "G", "H", "I", "L"})

    def test_pair_coverage_counts(self) -> None:
        coverage = sw.pair_coverage(OLD_MEMBERS, NEW_MEMBERS)
        self.assertEqual(coverage["sw2014_codes"], 12)
        self.assertEqual(coverage["sw2021_codes"], 11)
        self.assertEqual(coverage["paired_codes"], 10)
        self.assertEqual(coverage["only_sw2014_codes"], 2)
        self.assertEqual(coverage["only_sw2021_codes"], 1)
        self.assertEqual(coverage["only_sw2014_samples"], ["X", "Y"])
        self.assertEqual(coverage["only_sw2021_samples"], ["J"])
        self.assertEqual(coverage["label_changed_codes"], 7)
        self.assertAlmostEqual(coverage["label_changed_share"], 0.7)


class EdgeTableTests(unittest.TestCase):
    def test_shares_and_structural_thresholds(self) -> None:
        edges = sw.edge_table(
            _expand(
                [
                    ("采掘", "煤炭", 18),
                    ("采掘", "石油石化", 2),
                    ("化工", "基础化工", 18),
                    ("化工", "石油石化", 2),
                    ("综合", "石油石化", 3),
                ]
            )
        )
        self.assertEqual(len(edges), 5)
        minority = _row(edges, sw2014_l1="采掘", sw2021_l1="石油石化")
        self.assertAlmostEqual(minority["share_of_sw2014"], 0.1)
        self.assertAlmostEqual(minority["share_of_sw2021"], 2 / 7)
        self.assertFalse(bool(minority["is_structural"]))
        self.assertFalse(bool(_row(edges, sw2014_l1="化工", sw2021_l1="石油石化")["is_structural"]))
        self.assertTrue(bool(_row(edges, sw2014_l1="综合", sw2021_l1="石油石化")["is_structural"]))
        self.assertTrue(bool(_row(edges, sw2014_l1="采掘", sw2021_l1="煤炭")["is_structural"]))

    def test_empty_pairs_returns_columns(self) -> None:
        edges = sw.edge_table(pd.DataFrame(columns=["code", "sw2014_l1", "sw2021_l1"]))
        self.assertEqual(list(edges.columns), sw._EDGE_COLUMNS)
        self.assertTrue(edges.empty)

    def test_classify_mapping(self) -> None:
        result = sw.classify_mapping(
            CLASSIFY_EDGES,
            old_universe=[
                "采掘",
                "化工",
                "综合",
                "休闲服务",
                "电气设备",
                "银行",
                "传媒",
                "钢铁",
            ],
            new_universe=[
                "煤炭",
                "石油石化",
                "基础化工",
                "综合",
                "社会服务",
                "电力设备",
                "银行",
                "传媒",
                "美容护理",
            ],
        )
        self.assertEqual(
            result["one_to_many"],
            {
                "采掘": ["煤炭", "石油石化"],
                "化工": ["基础化工", "石油石化"],
                "综合": ["社会服务", "综合"],
            },
        )
        self.assertEqual(
            result["many_to_one"],
            {"石油石化": ["化工", "采掘"], "社会服务": ["休闲服务", "综合"]},
        )
        self.assertEqual(
            result["renamed_old"],
            {"休闲服务": "社会服务", "电气设备": "电力设备"},
        )
        self.assertEqual(result["retired_old_labels"], ["休闲服务", "化工", "电气设备", "采掘", "钢铁"])
        self.assertEqual(
            result["new_labels"],
            ["基础化工", "煤炭", "电力设备", "石油石化", "社会服务", "美容护理"],
        )
        self.assertEqual(result["old_without_structural_successor"], ["钢铁"])
        self.assertEqual(result["new_without_structural_source"], ["美容护理"])
        self.assertEqual(_row(CLASSIFY_EDGES, sw2014_l1="采掘", sw2021_l1="银行")["is_structural"], False)

    def test_sensitivity_at_10_40_changes_one_to_many(self) -> None:
        base_edges = sw.edge_table(_expand(SENSITIVITY_PAIRS))
        loose_edges = sw.edge_table(_expand(SENSITIVITY_PAIRS), old_share=0.10, new_share=0.40)
        base = sw.classify_mapping(
            base_edges, old_universe=SENSITIVITY_OLD_UNIVERSE, new_universe=SENSITIVITY_NEW_UNIVERSE
        )
        loose = sw.classify_mapping(
            loose_edges, old_universe=SENSITIVITY_OLD_UNIVERSE, new_universe=SENSITIVITY_NEW_UNIVERSE
        )
        self.assertEqual(int(base_edges["is_structural"].sum()), 8)
        self.assertEqual(int(loose_edges["is_structural"].sum()), 10)
        self.assertEqual(set(base["one_to_many"]), {"化工", "综合"})
        self.assertEqual(set(loose["one_to_many"]), {"传媒", "化工", "综合"})
        self.assertEqual(base["new_without_structural_source"], ["美容护理"])
        self.assertEqual(loose["new_without_structural_source"], [])
        media = _row(loose_edges, sw2014_l1="传媒", sw2021_l1="社会服务")
        self.assertAlmostEqual(media["share_of_sw2014"], 19 / 177, places=6)
        cosmetics = _row(loose_edges, sw2014_l1="化工", sw2021_l1="美容护理")
        self.assertAlmostEqual(cosmetics["share_of_sw2021"], 12 / 26, places=6)

    def test_label_change_breakdown(self) -> None:
        pairs = _expand(CLASSIFY_PAIRS)
        breakdown = sw.label_change_breakdown(pairs, CLASSIFY_EDGES)
        self.assertEqual(breakdown["structural_label_changed_codes"], 30)
        self.assertEqual(breakdown["residual_label_changed_codes"], 1)
        self.assertAlmostEqual(breakdown["structural_label_changed_share"], 30 / 41, places=6)
        self.assertAlmostEqual(breakdown["residual_label_changed_share"], 1 / 41, places=6)

    def test_backmap_table(self) -> None:
        back = sw.backmap_table(
            CLASSIFY_EDGES,
            new_universe=["煤炭", "石油石化", "基础化工", "综合", "社会服务", "电力设备", "银行", "传媒", "美容护理"],
        )
        self.assertEqual(len(back), 9)
        self.assertEqual(_row(back, sw2021_l1="煤炭")["mapping"], "unique")
        self.assertEqual(_row(back, sw2021_l1="煤炭")["sw2014_backmap"], "采掘")
        self.assertEqual(_row(back, sw2021_l1="石油石化")["mapping"], "ambiguous")
        self.assertEqual(_row(back, sw2021_l1="石油石化")["sw2014_backmap"], "")
        self.assertEqual(_row(back, sw2021_l1="社会服务")["n_sources"], 2)
        self.assertEqual(_row(back, sw2021_l1="美容护理")["mapping"], "none")


SUMMARY = {
    "one_to_many": {"采掘": ["煤炭", "石油石化"], "化工": ["基础化工", "石油石化"]},
    "many_to_one": {"石油石化": ["化工", "采掘"]},
    "unmatched": {
        "sw2014_l1_total": ["采掘", "化工", "银行"],
        "sw2021_l1_total": ["煤炭", "石油石化", "基础化工", "银行", "美容护理"],
        "sw2021_without_structural_source": ["美容护理"],
    },
}


class SnapshotMetricsTests(unittest.TestCase):
    def _snapshot(self) -> pd.DataFrame:
        rows = [
            ("2021-11-30", "A", "采掘", 10.0, "SW2014"),
            ("2021-11-30", "B", "化工", 20.0, "SW2014"),
            ("2021-11-30", "C", "银行", 30.0, "SW2014"),
            ("2021-11-30", "D", "半导体", 40.0, "SW2014"),
            ("2021-12-31", "A", "煤炭", 10.0, "SW2021"),
            ("2021-12-31", "B", "基础化工", 20.0, "SW2021"),
            ("2021-12-31", "C", "银行", 30.0, "SW2021"),
            ("2021-12-31", "D", "半导体", 40.0, "SW2021"),
        ]
        return pd.DataFrame(rows, columns=["date", "code", "industry", "weight", "industry_version"])

    def test_sw_only_metrics_and_fallback_exposure(self) -> None:
        metrics = sw.snapshot_metrics(self._snapshot(), SUMMARY).set_index("date")
        pre = metrics.loc[pd.Timestamp("2021-11-30")]
        post = metrics.loc[pd.Timestamp("2021-12-31")]
        for row in (pre, post):
            self.assertEqual(row["n_sw_members"], 3)
            self.assertEqual(row["n_sw_industries"], 3)
            self.assertEqual(row["n_all_labels"], 4)
            self.assertEqual(row["top_industry"], "银行")
            self.assertAlmostEqual(row["top_weight_share"], 0.5)
            self.assertAlmostEqual(row["hhi_count"], 1 / 3, places=6)
            self.assertAlmostEqual(row["hhi_weight"], 0.388889, places=6)
            self.assertEqual(row["non_sw_labels"], "半导体")
            self.assertAlmostEqual(row["non_sw_share_count"], 0.25)
            self.assertAlmostEqual(row["non_sw_share_weight"], 0.4)
        self.assertAlmostEqual(pre["ambiguous_share_count"], 2 / 3, places=6)
        self.assertAlmostEqual(pre["ambiguous_share_weight"], 0.5)
        self.assertAlmostEqual(post["ambiguous_share_count"], 0.0)
        self.assertAlmostEqual(post["ambiguous_share_weight"], 0.0)


class DistributionTableTests(unittest.TestCase):
    def _snapshot(self) -> pd.DataFrame:
        rows = [
            ("2021-11-30", "A", "采掘", 10.0, "SW2014"),
            ("2021-11-30", "B", "化工", 20.0, "SW2014"),
            ("2021-11-30", "C", "银行", 30.0, "SW2014"),
            ("2021-11-30", "D", "半导体", 40.0, "SW2014"),
        ]
        return pd.DataFrame(rows, columns=["date", "code", "industry", "weight", "industry_version"])

    def test_fallback_rows_are_marked_and_denominators_are_explicit(self) -> None:
        dist = sw.distribution_table(self._snapshot(), SUMMARY)
        self.assertEqual(set(dist["is_sw"]), {True, False})
        bank = _row(dist, industry="银行")
        self.assertAlmostEqual(bank["weight_share"], 0.3)
        self.assertAlmostEqual(bank["sw_weight_share"], 0.5)
        fallback = _row(dist, industry="半导体")
        self.assertFalse(fallback["is_sw"])
        self.assertAlmostEqual(fallback["weight_share"], 0.4)
        self.assertAlmostEqual(fallback["sw_weight_share"], 40 / 60, places=6)


class BridgeDiffTests(unittest.TestCase):
    def _snapshot(self) -> pd.DataFrame:
        rows = [
            ("2021-12-01", "A", "采掘", 10.0, "SW2014"),
            ("2021-12-01", "B", "化工", 20.0, "SW2014"),
            ("2021-12-01", "C", "银行", 30.0, "SW2014"),
            ("2021-12-01", "D", "半导体", 40.0, "SW2014"),
            ("2021-12-13", "A", "煤炭", 10.0, "SW2021"),
            ("2021-12-13", "B", "基础化工", 20.0, "SW2021"),
            ("2021-12-13", "C", "银行", 30.0, "SW2021"),
            ("2021-12-13", "D", "半导体", 40.0, "SW2021"),
        ]
        return pd.DataFrame(rows, columns=["date", "code", "industry", "weight", "industry_version"])

    def test_bridge_diff_math(self) -> None:
        edges = sw.edge_table(sw.paired_assignments(OLD_MEMBERS, NEW_MEMBERS))
        diff = sw.bridge_diff(self._snapshot(), edges, "2021-12-01", "2021-12-13")
        self.assertEqual(diff["paired_codes"], 4)
        self.assertEqual(diff["changed_codes"], 2)
        self.assertAlmostEqual(diff["changed_share"], 0.5)
        self.assertAlmostEqual(diff["changed_weight_share"], 0.3)
        self.assertEqual(diff["structural_changed_codes"], 2)
        self.assertEqual(diff["residual_changed_codes"], 0)
        self.assertEqual(diff["forward_clean_share"], 0.25)
        self.assertAlmostEqual(diff["forward_clean_weight_share"], 0.3)
        self.assertEqual(diff["comparable_codes"], 3)
        self.assertAlmostEqual(diff["comparable_weight_share"], 0.6)
        self.assertEqual(diff["ambiguous_backmap_codes"], 0)
        self.assertEqual(diff["unmapped_backmap_codes"], 0)
        self.assertEqual(diff["n_labels_old"], 3)
        self.assertEqual(diff["n_labels_new"], 3)
        self.assertAlmostEqual(diff["hhi_count_old"], 1 / 3, places=6)
        self.assertAlmostEqual(diff["hhi_weight_old"], 0.388889, places=6)
        self.assertEqual(diff["top_flows"][0]["sw2014_l1"], "化工")
        self.assertEqual(diff["top_flows"][0]["sw2021_l1"], "基础化工")

    def test_switch_bridge_diff_picks_adjacent_dates(self) -> None:
        edges = sw.edge_table(sw.paired_assignments(OLD_MEMBERS, NEW_MEMBERS))
        diff = sw.switch_bridge_diff(self._snapshot(), edges)
        self.assertEqual(diff["old_date"], "2021-12-01")
        self.assertEqual(diff["new_date"], "2021-12-13")


class ArtifactTests(unittest.TestCase):
    def test_crosswalk_artifact_cache_hit(self) -> None:
        tmp = Path(".tmp-tests") / "crosswalk_artifact_cache"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            with patch.object(sw, "PIT_DIR", tmp):
                first_edges, first_summary = sw.build_crosswalk_artifacts(
                    refresh=True, old_members=OLD_MEMBERS, new_members=NEW_MEMBERS
                )
                self.assertTrue((tmp / sw.EDGES_FILE).exists())
                self.assertTrue((tmp / sw.SUMMARY_FILE).exists())
                with patch.object(sw, "sw_l1_members", side_effect=AssertionError("cache miss")):
                    second_edges, second_summary = sw.build_crosswalk_artifacts(
                        refresh=False, old_members=OLD_MEMBERS, new_members=NEW_MEMBERS
                    )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(first_edges), len(second_edges))
        self.assertEqual(first_summary["coverage"]["paired_codes"], 10)
        self.assertEqual(second_summary["coverage"]["paired_codes"], 10)
        self.assertTrue(
            any(row["sw2021_l1"] == "美容护理" and row["mapping"] == "none" for row in second_summary["backmap"])
        )

    def test_threshold_change_forces_rebuild(self) -> None:
        tmp = Path(".tmp-tests") / "crosswalk_threshold_cache"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            with patch.object(sw, "PIT_DIR", tmp):
                loose_edges, _ = sw.build_crosswalk_artifacts(
                    refresh=True, old_members=OLD_MEMBERS, new_members=NEW_MEMBERS
                )
                strict_edges, strict_summary = sw.build_crosswalk_artifacts(
                    refresh=False,
                    old_members=OLD_MEMBERS,
                    new_members=NEW_MEMBERS,
                    old_share=0.60,
                    new_share=0.90,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(strict_summary["thresholds"]["structural_old_share"], 0.60)
        self.assertEqual(int(loose_edges["is_structural"].sum()), 9)
        self.assertEqual(int(strict_edges["is_structural"].sum()), 7)

    def test_bridge_date_change_forces_rebuild(self) -> None:
        tmp = Path(".tmp-tests") / "crosswalk_date_cache"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            with patch.object(sw, "PIT_DIR", tmp):
                _, first = sw.build_crosswalk_artifacts(
                    refresh=True, old_members=OLD_MEMBERS, new_members=NEW_MEMBERS
                )
                _, second = sw.build_crosswalk_artifacts(
                    refresh=False,
                    old_members=OLD_MEMBERS,
                    new_members=NEW_MEMBERS,
                    old_date="2021-12-01",
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(first["bridge_old_date"], "2021-12-09")
        self.assertEqual(second["bridge_old_date"], "2021-12-01")

    def test_distribution_window_changes_artifact_key_and_rebuilds(self) -> None:
        tmp = Path(".tmp-tests") / "crosswalk_distribution_cache"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        summary = {
            "bridge_old_date": "2021-12-09",
            "bridge_new_date": "2021-12-13",
            "thresholds": {"structural_old_share": 0.2, "structural_new_share": 0.5},
            "one_to_many": {},
            "many_to_one": {},
            "unmatched": {
                "sw2014_l1_total": ["采掘", "化工", "银行"],
                "sw2021_l1_total": ["煤炭", "基础化工", "银行"],
            },
        }

        def fake_snapshot(index_code: str, start, end, refresh: bool = False) -> pd.DataFrame:
            days = ("2021-12-01", "2021-12-13") if pd.Timestamp(start).month == 6 else ("2021-12-13",)
            rows = []
            for day in days:
                rows.append(
                    {
                        "date": day,
                        "code": "A",
                        "weight": 1.0,
                        "industry": "银行",
                        "industry_version": "SW2014" if day.endswith("01") else "SW2021",
                    }
                )
            return pd.DataFrame(rows)

        try:
            with patch.object(sw, "PIT_DIR", tmp), patch.object(
                sw, "build_crosswalk_artifacts", return_value=(CLASSIFY_EDGES, summary)
            ), patch.object(sw, "snapshot_window", side_effect=fake_snapshot):
                _first_dist, first_metrics = sw.build_distribution_artifacts(
                    "000300.SH", "2021-06-01", "2021-08-31", refresh=True
                )
                _second_dist, second_metrics = sw.build_distribution_artifacts(
                    "000300.SH", "2021-09-01", "2021-11-30", refresh=False
                )
                self.assertTrue(
                    (tmp / "sw_crosswalk_metrics_000300.SH_20210601_20210831.parquet").exists()
                )
                self.assertTrue(
                    (tmp / "sw_crosswalk_meta_000300.SH_20210901_20211130.json").exists()
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(first_metrics), 2)
        self.assertEqual(len(second_metrics), 1)

    def _distribution_snapshot(self) -> pd.DataFrame:
        rows = [
            ("2021-06-30", "A", "采掘", 1.0, "SW2014"),
            ("2021-12-13", "A", "煤炭", 1.0, "SW2021"),
        ]
        return pd.DataFrame(rows, columns=["date", "code", "industry", "weight", "industry_version"])

    def test_distribution_same_window_hits_cache_after_parquet_roundtrip(self) -> None:
        tmp = Path(".tmp-tests") / "crosswalk_distribution_cache_hit"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        calls: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        snapshot = self._distribution_snapshot()

        def fake_snapshot(index_code: str, start, end, refresh: bool = False) -> pd.DataFrame:
            calls.append((pd.Timestamp(start), pd.Timestamp(end)))
            return snapshot.copy()

        real_build = sw.build_crosswalk_artifacts

        def build_with_members(*args, **kwargs):
            return real_build(*args, old_members=OLD_MEMBERS, new_members=NEW_MEMBERS, **kwargs)

        try:
            with patch.object(sw, "PIT_DIR", tmp), patch.object(
                sw, "build_crosswalk_artifacts", side_effect=build_with_members
            ), patch.object(sw, "snapshot_window", side_effect=fake_snapshot):
                first_dist, first_metrics = sw.build_distribution_artifacts(
                    "000300.SH", "2021-06-01", "2021-08-31", refresh=True
                )
                second_dist, second_metrics = sw.build_distribution_artifacts(
                    "000300.SH", "2021-06-01", "2021-08-31", refresh=False
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(calls), 1, "same window must not fetch the snapshot twice")
        pd.testing.assert_frame_equal(first_dist, second_dist)
        pd.testing.assert_frame_equal(first_metrics, second_metrics)

    def test_threshold_change_invalidates_distribution_meta(self) -> None:
        tmp = Path(".tmp-tests") / "crosswalk_distribution_threshold"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        calls: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        snapshot = self._distribution_snapshot()

        def fake_snapshot(index_code: str, start, end, refresh: bool = False) -> pd.DataFrame:
            calls.append((pd.Timestamp(start), pd.Timestamp(end)))
            return snapshot.copy()

        real_build = sw.build_crosswalk_artifacts

        def build_with_members(*args, **kwargs):
            return real_build(*args, old_members=OLD_MEMBERS, new_members=NEW_MEMBERS, **kwargs)

        try:
            with patch.object(sw, "PIT_DIR", tmp), patch.object(
                sw, "build_crosswalk_artifacts", side_effect=build_with_members
            ), patch.object(sw, "snapshot_window", side_effect=fake_snapshot):
                sw.build_distribution_artifacts("000300.SH", "2021-06-01", "2021-08-31", refresh=True)
                _, second_metrics = sw.build_distribution_artifacts(
                    "000300.SH",
                    "2021-06-01",
                    "2021-08-31",
                    refresh=False,
                    old_share=0.60,
                    new_share=0.90,
                )
            meta = json.loads(
                (tmp / "sw_crosswalk_meta_000300.SH_20210601_20210831.json").read_text(encoding="utf-8")
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(calls), 2, "threshold change must rebuild the distribution")
        self.assertEqual(meta["thresholds"]["structural_old_share"], 0.60)
        self.assertEqual(meta["thresholds"]["structural_new_share"], 0.90)
        self.assertFalse(second_metrics.empty)

    def test_export_audit_docs_keeps_month_ends_plus_switch_day(self) -> None:
        tmp = Path(".tmp-tests") / "crosswalk_docs_export"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        metrics = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2021-11-29", "2021-11-30", "2021-12-13", "2021-12-31", "2022-01-03"]
                ),
                "industry_version": ["SW2014"] * 2 + ["SW2021"] * 3,
            }
        )
        try:
            crosswalk_path, metrics_path = sw.export_audit_docs(CLASSIFY_EDGES, metrics, docs_dir=tmp)
            exported = pd.read_csv(metrics_path, encoding="utf-8-sig")
            edges = pd.read_csv(crosswalk_path, encoding="utf-8-sig")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(exported), 4)
        self.assertEqual(
            sorted(exported["date"]),
            ["2021-11-30", "2021-12-13", "2021-12-31", "2022-01-03"],
        )
        self.assertEqual(len(edges), len(CLASSIFY_EDGES))

    def test_snapshot_window_is_chunked_and_deduplicated(self) -> None:
        calls: list[tuple[pd.Timestamp, pd.Timestamp]] = []

        def fake_snapshot(index_code: str, start, end, refresh: bool = False) -> pd.DataFrame:
            calls.append((pd.Timestamp(start), pd.Timestamp(end)))
            rows = []
            for day in (pd.Timestamp(start), pd.Timestamp(end)):
                rows.append(
                    {
                        "date": day,
                        "code": "X",
                        "weight": 1.0,
                        "name": "X",
                        "industry": "银行",
                        "industry_version": "SW2021",
                        "total_mv": 1.0,
                        "is_st": False,
                    }
                )
            return pd.DataFrame(rows)

        with patch.object(sw, "build_index_snapshot", side_effect=fake_snapshot):
            frame = sw.snapshot_window("000300.SH", "2021-06-01", "2022-05-31")

        self.assertEqual(
            calls,
            [
                (pd.Timestamp("2021-06-01"), pd.Timestamp("2021-08-31")),
                (pd.Timestamp("2021-09-01"), pd.Timestamp("2021-11-30")),
                (pd.Timestamp("2021-12-01"), pd.Timestamp("2022-02-28")),
                (pd.Timestamp("2022-03-01"), pd.Timestamp("2022-05-31")),
            ],
        )
        self.assertEqual(len(frame), 8)
        self.assertEqual(len(frame[["date", "code"]].drop_duplicates()), 8)


class WindowTests(unittest.TestCase):
    def test_window_dates_six_before_and_six_after(self) -> None:
        dates = pd.date_range("2021-06-30", "2022-05-31", freq="ME")
        snapshot = pd.DataFrame({"date": dates})
        selected = sw.window_dates(snapshot, n=6)
        self.assertEqual(len(selected), 12)
        self.assertEqual(selected[0], pd.Timestamp("2021-06-30"))
        self.assertEqual(selected[5], pd.Timestamp("2021-11-30"))
        self.assertEqual(selected[6], pd.Timestamp("2021-12-31"))
        self.assertEqual(selected[-1], pd.Timestamp("2022-05-31"))


if __name__ == "__main__":
    unittest.main()
