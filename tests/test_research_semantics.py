"""ADR-010 C1/C2 回归：研究语义动作词、候选池分位与旧批次兼容映射。"""

import ast
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from astock import contracts, events, ledger, recommend, scoring, use_cases

ROOT = Path(recommend.__file__).resolve().parents[1]


class ActionVocabularyTests(unittest.TestCase):
    def test_legacy_words_map_to_research_semantics(self):
        self.assertEqual(contracts.normalize_action("关注"), "技术面强")
        self.assertEqual(contracts.normalize_action("观察"), "技术面中性")
        self.assertEqual(contracts.normalize_action("回避"), "技术面弱")

    def test_research_values_and_blanks_are_preserved(self):
        for value in contracts.RESEARCH_ACTIONS:
            self.assertEqual(contracts.normalize_action(value), value)
        for value in (None, "", "  ", "nan", "None", float("nan")):
            self.assertEqual(contracts.normalize_action(value), "")

    def test_unknown_value_passes_through(self):
        self.assertEqual(contracts.normalize_action("跑赢大盘"), "跑赢大盘")


class ActionShortTests(unittest.TestCase):
    def test_short_label_strips_research_prefix(self):
        self.assertEqual(contracts.action_short("技术面强"), "强")
        self.assertEqual(contracts.action_short("技术面中性"), "中性")
        # 旧批次动作词先归一化，再取短标签。
        self.assertEqual(contracts.action_short("回避"), "弱")
        self.assertEqual(contracts.action_short(None), "")


class PercentileBasisTests(unittest.TestCase):
    def test_live_and_backfill_share_the_candidate_pool_basis(self):
        rows = [{"composite": float(i), "code": f"{i:06d}"} for i in range(40)]

        # live 与 backfill 都走 assign_percentiles：先截断到候选池，再在池内排名。
        pool = recommend.assign_percentiles(rows, 15)

        self.assertEqual(len(pool), 15)
        self.assertEqual(pool[0]["code"], "000039")
        expected = recommend.pool_percentile([r["composite"] for r in pool])
        self.assertEqual([r["percentile"] for r in pool], expected)
        self.assertEqual(pool[0]["percentile"], 100.0)
        self.assertAlmostEqual(pool[-1]["percentile"], round(100 / 15, 1))
        # 候选池外的行不参与分位，也不会被误标（池 = 综合分前 15 = rows[25:]）。
        self.assertFalse(any("percentile" in r for r in rows[:25]))



    def test_percentile_is_monotonic_and_bounded(self):
        ranked = recommend.pool_percentile([50.0, 60.0, 70.0, 90.0])
        self.assertEqual(len(ranked), 4)
        self.assertTrue(all(0.0 <= x <= 100.0 for x in ranked))
        self.assertEqual(ranked, sorted(ranked))
        self.assertEqual(ranked[0], 25.0)
        self.assertEqual(ranked[-1], 100.0)

    def test_non_finite_scores_yield_none(self):
        ranked = recommend.pool_percentile([float("nan"), 60.0, float("inf")])
        self.assertIsNone(ranked[0])
        self.assertIsNone(ranked[2])
        self.assertTrue(math.isfinite(ranked[1]))


class NormalizeTableTests(unittest.TestCase):
    def test_legacy_table_is_mapped_and_percentile_filled(self):
        legacy = pd.DataFrame(
            [
                {
                    "排名": 1,
                    "代码": "600519.SH",
                    "名称": "贵州茅台",
                    "行业": "白酒",
                    "综合分": 82.0,
                    "技术分": 80.0,
                    "事件分": 86.0,
                    "建议": "关注",
                    "理由": "旧批次",
                }
            ]
        )
        table = recommend.normalize_table(legacy)
        self.assertNotIn("建议", table.columns)
        self.assertIn("技术分位", table.columns)
        self.assertEqual(table.iloc[0]["技术面"], "技术面强")
        self.assertEqual(table.iloc[0]["技术分位"], recommend.MISSING_PERCENTILE)
        self.assertLess(
            list(table.columns).index("技术分位"), list(table.columns).index("技术分")
        )

    def test_new_table_keeps_numeric_percentile(self):
        table = recommend.normalize_table(
            pd.DataFrame(
                [{"排名": 1, "综合分": 82.0, "技术分位": 92.4, "技术面": "技术面中性"}]
            )
        )
        self.assertEqual(table.iloc[0]["技术分位"], 92.4)
        self.assertEqual(table.iloc[0]["技术面"], "技术面中性")


def _row(**overrides) -> dict:
    row = {
        "code": "600519.SH",
        "name": "贵州茅台",
        "industry": "白酒",
        "composite": 82.0,
        "percentile": 92.0,
        "technical_score": 80.0,
        "event_score": 86.0,
        "action": "技术面强",
        "close": 1500.0,
        "ret20": 0.032,
        "rsi": 58.0,
        "reason": "趋势向上",
    }
    row.update(overrides)
    return row


class ReportMarkdownTests(unittest.TestCase):
    def test_report_shows_research_action_and_percentile(self):
        text = recommend._report_markdown(
            [_row()], "震荡", {}, "2026-11-06", "2026-11-09", candidate_n=15
        )
        # 句内用短标签，避免「技术面：技术面强」的前缀重复。
        self.assertIn("**技术面**：强", text)
        self.assertNotIn("技术面：技术面强", text)
        # 分位口径写进文案：候选池内 rank(pct)，展示时带 N。
        self.assertIn("候选池分位（N=15）：92%", text)
        self.assertNotIn("**建议**", text)
        # 免责声明保留：合规文案不属于投资动作词。
        self.assertIn("不构成投资建议", text)

    def test_report_handles_missing_percentile(self):
        text = recommend._report_markdown(
            [_row(percentile=None, action="关注")], "震荡", {}, "2026-11-06", candidate_n=15
        )
        self.assertIn("**技术面**：强", text)
        self.assertIn("候选池分位（N=15）：—", text)
        self.assertNotIn("关注", text)

    def test_percentile_basis_label_always_names_the_pool(self):
        self.assertEqual(recommend.percentile_basis(15), "候选池分位（N=15）")


class PersistedPoolSnapshotTests(unittest.TestCase):
    """落盘 JSON 必须能自证分位：table 只有入选行，复算依赖 diagnostics.candidate_pool。"""

    def test_saved_json_pool_snapshot_reproduces_table_percentiles(self):
        rows = [
            _row(code=f"{i:06d}", name=f"股票{i}", composite=float(i), percentile=None)
            for i in range(20)
        ]
        pool = recommend.assign_percentiles(rows, 15)
        diagnostics = {
            "candidate_n": len(pool),
            "percentile_basis": "candidate_pool",
            "candidate_pool": [
                {"code": r["code"], "composite": float(r["composite"])} for r in pool
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(recommend, "REC_DIR", Path(tmp)),
                # _save 经 ledger.save_run 落库；隔离，避免假批次写进真实 data/astock.db。
                patch.object(ledger, "DB_PATH", Path(tmp) / "ledger.db"),
            ):
                _, json_path = recommend._save(
                    pool[:5],
                    "震荡",
                    {},
                    {},
                    "2026-11-06",
                    "2026-11-09",
                    "rec_pool",
                    "2026-11-06T18:00:00",
                    params={"candidate_n": 15},
                    diagnostics=diagnostics,
                )
            payload = json.loads(Path(json_path).read_text(encoding="utf-8"))

        snapshot = payload["diagnostics"]["candidate_pool"]
        self.assertEqual(len(snapshot), 15)
        recomputed = recommend.pool_percentile([p["composite"] for p in snapshot])
        by_code = {p["code"]: pct for p, pct in zip(snapshot, recomputed)}
        self.assertEqual(len(payload["table"]), 5)
        for row in payload["table"]:
            # 用落盘的池快照复算，逐行与 table 的技术分位一致（可审计）。
            self.assertEqual(row["技术分位"], by_code[row["代码"]])

    def test_pool_label_uses_actual_pool_size_when_fewer_rows_than_candidate_n(self):
        rows = [{"composite": float(i), "code": f"{i:06d}"} for i in range(8)]
        pool = recommend.assign_percentiles(rows, 15)
        self.assertEqual(len(pool), 8)
        # backfill 的 market/params 用 len(pool) 生成 N，不能写死 candidate_n=15。
        self.assertEqual(recommend.percentile_basis(len(pool)), "候选池分位（N=8）")
        self.assertEqual(
            [r["percentile"] for r in pool],
            recommend.pool_percentile([r["composite"] for r in pool]),
        )


class LatestContextTests(unittest.TestCase):
    """app 的「最新批次摘要」不能拼出「技术面技术面强」，旧批次也要归一化。"""

    def test_context_shortens_action_and_keeps_percentile(self):
        try:
            import app as app_module
        except Exception as exc:  # pragma: no cover - optional heavy import
            raise unittest.SkipTest(f"app import failed: {exc}") from exc

        payload = {
            "diagnostics": {"candidate_n": 12},
            "params": {"candidate_n": 15},
            "table": [
                {
                    "代码": "600519.SH",
                    "名称": "贵州茅台",
                    "综合分": 91.0,
                    "技术分位": 92.0,
                    "技术面": "技术面强",
                    "理由": "趋势向上",
                },
                {
                    "代码": "000001.SZ",
                    "名称": "平安银行",
                    "综合分": 80.0,
                    "建议": "关注",
                    "理由": "旧批次",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            rec_dir = Path(tmp) / "data" / "recommendations"
            rec_dir.mkdir(parents=True)
            (rec_dir / "latest.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(app_module, "BASE_DIR", tmp):
                text = app_module._latest_recommendation_context()

        self.assertNotIn("技术面技术面", text)
        self.assertIn("技术面：强｜趋势向上", text)
        self.assertIn("技术面：强｜旧批次", text)
        # N 以实际池大小（diagnostics.candidate_n）为准，而不是 UI 上限 15。
        self.assertIn("候选池分位(N=12)92.0%", text)


class LedgerCompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.db"
        self.patch = patch.object(ledger, "DB_PATH", self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_old_database_gains_new_columns(self):
        import sqlite3

        conn = sqlite3.connect(self.db)
        conn.executescript(
            """CREATE TABLE runs (
                   run_id TEXT PRIMARY KEY, schema_version INTEGER, kind TEXT,
                   data_as_of TEXT, tradable_from TEXT, generated_at TEXT,
                   source TEXT, status TEXT, error TEXT, params_json TEXT,
                   diagnostics_json TEXT, json_path TEXT, csv_path TEXT);
               CREATE TABLE items (
                   run_id TEXT, rank INTEGER, code TEXT, name TEXT, industry TEXT,
                   composite REAL, technical REAL, event REAL, action TEXT, reason TEXT,
                   PRIMARY KEY (run_id, rank));"""
        )
        conn.close()
        conn = ledger._connect()
        try:
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        finally:
            conn.close()
        self.assertIn("report_md", run_columns)
        self.assertIn("status_line", run_columns)
        self.assertIn("percentile", item_columns)
        self.assertIn("close", item_columns)
        self.assertIn("ret20", item_columns)
        self.assertIn("rsi", item_columns)

    def test_legacy_action_word_is_saved_as_research_semantics(self):
        payload = {
            "run_id": "rec_legacy",
            "generated_at": "2026-11-06T18:00:00",
            "data_as_of": "2026-11-06",
            "table": [{"排名": 1, "代码": "600519.SH", "名称": "贵州茅台",
                       "行业": "白酒", "综合分": 82.0, "技术分": 80.0,
                       "事件分": 86.0, "技术分位": "—", "建议": "回避",
                       "理由": "旧批次"}],
        }
        ledger.save_run(payload)
        items = ledger.get_run("rec_legacy")["items"]
        self.assertEqual(items[0]["action"], "技术面弱")
        self.assertIsNone(items[0]["percentile"])


class RenderCompatTests(unittest.TestCase):
    def test_ledger_view_maps_legacy_action_and_missing_percentile(self):
        run = {
            "run_id": "rec_hist",
            "data_as_of": "2026-11-06",
            "tradable_from": "2026-11-09",
            "generated_at": "2026-11-06T18:00:00",
            "items": [
                {"rank": 1, "code": "600519.SH", "name": "贵州茅台", "industry": "白酒",
                 "composite": 82.0, "percentile": None, "technical": 80.0,
                 "event": 86.0, "action": "关注", "reason": "旧批次"}
            ],
        }
        result = use_cases._ledger_view(run, mode="latest")
        table = result.payload["table"]
        self.assertNotIn("建议", table.columns)
        self.assertEqual(table.iloc[0]["技术面"], "技术面强")
        self.assertEqual(table.iloc[0]["技术分位"], recommend.MISSING_PERCENTILE)


class SiteWideVocabularyTests(unittest.TestCase):
    """全站回归：运行时代码不得再出现投资动作词或买卖建议措辞。"""

    def _sources(self):
        files = [*sorted((ROOT / "astock").rglob("*.py")), ROOT / "app.py"]
        return [p for p in files if p.is_file()]

    def _strings(self, path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield node.value

    def test_no_action_words_outside_compat_mapping(self):
        allowed = {ROOT / "astock" / "contracts.py"}
        offenders = []
        for path in self._sources():
            if path in allowed:
                continue
            for text in self._strings(path):
                for word in ("关注", "观察", "回避"):
                    if word in text:
                        offenders.append(f"{path.relative_to(ROOT)}: {word}")
        self.assertEqual(offenders, [])

    def test_no_trade_instruction_phrases(self):
        offenders = []
        for path in self._sources():
            for text in self._strings(path):
                for phrase in ("建议买入", "建议卖出", "给操作建议"):
                    if phrase in text:
                        offenders.append(f"{path.relative_to(ROOT)}: {phrase}")
        self.assertEqual(offenders, [])

    def test_prompts_use_research_semantics(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("你是A股投顾", source)
        self.assertNotIn("给操作建议", source)
        self.assertGreaterEqual(source.count("不给出买卖"), 3)


class EventScoringCalibrationTests(unittest.TestCase):
    def test_events_reexports_the_single_point_implementation(self):
        self.assertIs(events.score_events, scoring.score_events)
        self.assertIs(events.sentiment, scoring.sentiment)

    def test_run_params_carry_event_scoring_snapshot(self):
        params = recommend._build_run_params(5, 15, 7, True)
        snapshot = params["event_scoring"]
        self.assertEqual(snapshot["version"], scoring.EVENT_SCORING_VERSION)
        self.assertEqual(snapshot["type_weights"], scoring.EVENT_TYPE_WEIGHTS)
        self.assertEqual(snapshot["rating_weight"], scoring.EVENT_RATING_WEIGHT)
        self.assertEqual(snapshot["policy_bonus"], scoring.EVENT_POLICY_BONUS)
        self.assertEqual(snapshot["calibration"]["samples"], scoring.CALIBRATION_SAMPLE_COUNT)
        self.assertFalse(snapshot["calibration"]["llm_used"])
        # 快照必须能直接 JSON 落盘，且不携带动作/置信度字段（ADR-009）。
        payload = json.dumps(params, ensure_ascii=False)
        self.assertNotIn('"action"', payload)
        self.assertNotIn('"confidence"', payload)

    def test_scoring_module_does_not_import_llm(self):
        tree = ast.parse((ROOT / "astock" / "scoring.py").read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertFalse(
            [name for name in imported if "llm" in name.lower() or "openai" in name.lower()]
        )

    def test_labeled_samples_cover_classes_and_volume(self):
        samples = scoring.load_labeled_samples()
        self.assertGreaterEqual(len(samples), 100)
        self.assertEqual(len(samples), scoring.CALIBRATION_SAMPLE_COUNT)
        labels = {sample["label"] for sample in samples}
        self.assertEqual(labels, {-1, 0, 1})
        self.assertGreaterEqual(sum(1 for sample in samples if sample["label"] == 0), 20)
        cases = {sample["case_id"] for sample in samples}
        self.assertEqual(len(cases), scoring.CALIBRATION_CASE_COUNT)
        for case_id in cases:
            items = [sample for sample in samples if sample["case_id"] == case_id]
            self.assertEqual(len(items), 3, case_id)
            self.assertEqual(len({sample["case_strength"] for sample in items}), 1, case_id)

    def test_calibrated_weights_improve_accuracy_and_ic(self):
        samples = scoring.load_labeled_samples()
        legacy = scoring.evaluate_event_config(
            samples, scoring.legacy_event_config(), now="2026-11-28"
        )
        current = scoring.evaluate_event_config(samples, now="2026-11-28")
        self.assertEqual(current["type_weights"], scoring.EVENT_TYPE_WEIGHTS)
        self.assertGreaterEqual(current["item_accuracy"], 0.95)
        self.assertGreaterEqual(current["case_accuracy"], 0.95)
        self.assertIsNotNone(current["ic"])
        self.assertGreater(current["item_accuracy"], legacy["item_accuracy"])
        self.assertGreater(current["case_accuracy"], legacy["case_accuracy"])
        self.assertGreater(current["ic"], legacy["ic"])

    def test_weight_override_changes_aggregation(self):
        evidence = [
            {"kind": "公告", "title": "公司签订重大合同", "pub_time": "2026-11-28"},
            {"kind": "政策", "title": "公司业绩预减", "pub_time": "2026-11-28"},
        ]
        announcement_heavy = {"type_weights": {"公告": 2.0, "政策": 0.1}}
        policy_heavy = {"type_weights": {"公告": 0.1, "政策": 2.0}}
        high, _ = scoring.score_events(evidence, config=announcement_heavy, now="2026-11-28")
        low, _ = scoring.score_events(evidence, config=policy_heavy, now="2026-11-28")
        self.assertGreater(high, 50.0)
        self.assertLess(low, 50.0)

    def test_ranking_comparison_reports_before_and_after(self):
        candidates = [
            {
                "code": "600001.SH",
                "name": "公告驱动",
                "technical_score": 90.0,
                "evidence": [
                    {"kind": "公告", "title": "公司签订5亿元重大合同", "pub_time": "2026-11-27"},
                    {"kind": "新闻", "title": "公司召开投资者交流会", "pub_time": "2026-11-27"},
                ],
            },
            {
                "code": "600002.SH",
                "name": "新闻驱动",
                "technical_score": 90.0,
                "evidence": [
                    {"kind": "新闻", "title": "公司股价涨停", "pub_time": "2026-11-27"},
                    {"kind": "新闻", "title": "公司签订大额合同", "pub_time": "2026-11-27"},
                    {"kind": "公告", "title": "关于召开临时股东大会的通知", "pub_time": "2026-11-27"},
                ],
            },
            {
                "code": "600003.SH",
                "name": "研报驱动",
                "technical_score": 90.0,
                "evidence": [
                    {"kind": "研报", "title": "维持买入评级", "rating": "买入", "pub_time": "2026-11-27"}
                ],
            },
            {
                "code": "600004.SH",
                "name": "中性事件",
                "technical_score": 90.0,
                "evidence": [
                    {"kind": "公告", "title": "关于召开临时股东大会的通知", "pub_time": "2026-11-27"}
                ],
            },
        ]
        result = scoring.compare_event_rankings(candidates, now="2026-11-27")
        rows = {row["code"]: row for row in result["rows"]}
        self.assertEqual(len(rows), 4)
        self.assertIsNotNone(result["rank_correlation"])
        self.assertEqual(result["top_n_overlap"], 4)
        # 公告权重上调、新闻权重下调后，公告驱动应超过新闻驱动。
        self.assertGreater(rows["600001.SH"]["event_score_new"], rows["600001.SH"]["event_score_old"])
        self.assertLess(rows["600002.SH"]["event_score_new"], rows["600002.SH"]["event_score_old"])
        self.assertLess(rows["600001.SH"]["rank_new"], rows["600002.SH"]["rank_new"])
        self.assertGreater(rows["600001.SH"]["rank_old"], rows["600002.SH"]["rank_old"])
        self.assertEqual(rows["600001.SH"]["rank_delta"], 1)

    def test_score_events_handles_empty_and_malformed_evidence(self):
        score, stats = scoring.score_events([])
        self.assertEqual(score, 50.0)
        self.assertEqual(stats["n"], 0)

        score, stats = scoring.score_events(
            [
                {"kind": "公告", "title": "", "pub_time": "not-a-date", "rating": "未知评级"},
                {"kind": "未知类型", "title": "公司公告", "pub_time": None},
                {"kind": "研报", "title": "公司公告", "rating": "未知评级", "pub_time": "2026-11-28"},
            ],
            now="2026-11-28",
        )
        self.assertEqual(stats["n"], 3)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 100.0)

    def test_calibration_grid_selects_config_from_samples(self):
        samples = scoring.load_labeled_samples()[:12]
        result = scoring.calibrate_event_weights(samples, now="2026-11-28", top_k=1)
        self.assertEqual(result["grid_points"], 648)
        self.assertEqual(len(result["top"]), 1)
        self.assertIn("item_accuracy", result["metrics"])
        self.assertIn("公告", result["config"]["type_weights"])

    def test_label_loader_rejects_invalid_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.csv"
            path.write_text(
                "case_id,label,case_strength,item_label,kind,title,rating,strength\n"
                "c1,2,1.0,1,公告,公司签订重大合同,,1.0\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                scoring.load_labeled_samples(path)

    def test_cli_emits_auditable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.csv"
            path.write_text(
                "case_id,label,case_strength,item_label,kind,title,rating,strength\n"
                "c1,1,1.0,1,公告,公司签订重大合同,,1.0\n"
                "c1,1,1.0,0,公告,公司召开股东大会,,1.0\n"
                "c1,1,1.0,-1,公告,公司业绩预减,,1.0\n",
                encoding="utf-8",
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = scoring.main(["--labels", str(path), "--now", "2026-11-28"])

        payload = json.loads(buffer.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["samples"], 3)
        selected = payload["selected"]["type_weights"]
        self.assertEqual(set(selected), set(scoring.EVENT_TYPE_WEIGHTS))
        self.assertTrue(all(0.0 < float(value) <= 1.2 for value in selected.values()))
        self.assertIn("legacy", payload)
        self.assertIn("calibrated", payload)


if __name__ == "__main__":
    unittest.main()
