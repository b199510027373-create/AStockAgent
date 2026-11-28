"""ADR-010 C1/C2 回归：研究语义动作词、候选池分位与旧批次兼容映射。"""

import ast
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from astock import contracts, ledger, recommend, use_cases

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


class FactorContributionTests(unittest.TestCase):
    """因子贡献必须能逐条复算技术分，而不是只给一个总分。"""

    @staticmethod
    def _frame(*, rsi: float = 60.0, vol_ratio: float = 1.5) -> pd.DataFrame:
        n = 130
        close = pd.Series([10.0 + i * 0.1 for i in range(n)])
        return pd.DataFrame(
            {
                "close": close,
                "high": close,
                "vol": 1000.0 * vol_ratio,
                "ma20": close.rolling(20).mean(),
                "ma60": close.rolling(60).mean(),
                "ma120": close.rolling(120).mean(),
                "rsi14": rsi,
                "dif": 0.5,
                "dea": 0.2,
                "macd_hist": 0.3,
                "atr14": close * 0.01,
                "vol_ma20": 1000.0,
            }
        )

    def test_contributions_replay_score_and_clip(self):
        result = recommend.technical_score(self._frame())
        self.assertIsNotNone(result)
        # 50 + 8 + 8 + 5 + 4 + 7 + 3 + 5 + 4 + 5 + 3 = 102 → 截断到 100。
        self.assertEqual(result["score_before_clip"], 102.0)
        self.assertEqual(result["technical_score"], 100.0)
        self.assertEqual(
            round(sum(c["delta"] for c in result["contributions"]), 1), 100.0
        )
        clip = [c for c in result["contributions"] if c["factor"] == "clip"]
        self.assertEqual(len(clip), 1)
        self.assertEqual(clip[0]["delta"], -2.0)

    def test_no_clip_when_score_inside_band(self):
        result = recommend.technical_score(self._frame(rsi=85.0, vol_ratio=1.0))
        self.assertIsNotNone(result)
        self.assertEqual(result["score_before_clip"], 85.0)
        self.assertEqual(result["technical_score"], 85.0)
        self.assertFalse(
            any(c["factor"] == "clip" for c in result["contributions"])
        )
        self.assertEqual(
            round(sum(c["delta"] for c in result["contributions"]), 1), 85.0
        )


class EvidenceIdTests(unittest.TestCase):
    def test_id_is_deterministic_and_traceable(self):
        ev = {
            "kind": "announcement",
            "pub_time": "2026-11-27 09:00:00",
            "title": "关于回购的公告",
        }
        first = recommend.make_evidence_id("rec_1", "600519.SH", ev)
        second = recommend.make_evidence_id("rec_1", "600519.SH", dict(ev))
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("ev_"))
        self.assertEqual(len(first), 15)
        self.assertNotEqual(
            first, recommend.make_evidence_id("rec_2", "600519.SH", ev)
        )
        self.assertNotEqual(
            first, recommend.make_evidence_id("rec_1", "000001.SZ", ev)
        )


class _EvidenceChainFixtures(unittest.TestCase):
    @staticmethod
    def _run() -> dict:
        items = [
            {
                "rank": 1, "code": "600519.SH", "name": "贵州茅台", "industry": "白酒",
                "composite": 85.1, "percentile": 92.0, "technical": 84.6,
                "event": 86.0, "action": "技术面强", "close": 1500.0,
                "ret20": 0.032, "rsi": 58.0, "reason": "趋势向上",
            },
            {
                "rank": 2, "code": "000001.SZ", "name": "平安银行", "industry": "银行",
                "composite": 74.0, "percentile": 61.0, "technical": 70.0,
                "event": 81.0, "action": "技术面中性", "close": 11.0,
                "ret20": 0.01, "rsi": 55.0, "reason": "震荡偏多",
            },
            {
                "rank": 3, "code": "600036.SH", "name": "招商银行", "industry": "银行",
                "composite": 70.0, "percentile": 40.0, "technical": 68.0,
                "event": 74.0, "action": "技术面中性", "close": 35.0,
                "ret20": -0.01, "rsi": 48.0, "reason": "震荡",
            },
        ]
        return {
            "run_id": "rec_demo",
            "data_as_of": "2026-11-27",
            "tradable_from": "2026-11-30",
            "generated_at": "2026-11-27T18:00:00",
            "source": "live",
            "items": items,
            "items_detail": {
                "600519.SH": {
                    "scores": {
                        "composite": 85.1, "technical": 84.6, "technical_raw": 78.0,
                        "technical_rank": 90.0, "score_before_clip": 80.0,
                        "event": 86.0, "percentile": 92.0,
                    },
                    "weights": {"technical": 0.65, "event": 0.35},
                    "technical_blend": {"absolute": 0.45, "rank": 0.55},
                    "factor_contributions": [
                        {"factor": "base", "label": "基础分", "delta": 50.0,
                         "value": None, "unit": "", "detail": "起始 50 分"},
                        {"factor": "ma20", "label": "站上MA20", "delta": 8.0,
                         "value": 0.02, "unit": "ratio", "detail": "收盘 / MA20"},
                        {"factor": "ma60", "label": "站上MA60", "delta": 5.0,
                         "value": 0.04, "unit": "ratio", "detail": "收盘 / MA60"},
                        {"factor": "macd_hist", "label": "MACD 红柱", "delta": 7.0,
                         "value": 0.3, "unit": "", "detail": "MACD 柱 > 0"},
                        {"factor": "rsi", "label": "RSI 中性偏强", "delta": 5.0,
                         "value": 58.0, "unit": "", "detail": "45 ≤ RSI ≤ 70"},
                        {"factor": "ret20", "label": "20日动量为正", "delta": 5.0,
                         "value": 0.032, "unit": "ratio", "detail": "ret20 > 0"},
                        {"factor": "clip", "label": "技术分截断", "delta": -2.0,
                         "value": 80.0, "unit": "", "detail": "80.0 → 78.0"},
                    ],
                    "signals": {"close": 1500.0, "trend": "多头"},
                    "event_stats": {"n": 2},
                }
            },
            "evidence": [
                {"code": "600519.SH", "kind": "announcement",
                 "pub_time": "2026-11-27T09:00:00", "title": "回购公告",
                 "source": "交易所", "url": "", "content": "",
                 "evidence_id": "ev_abc123456789"},
            ],
            "tracking": [
                {"code": "600519.SH", "rank": 1, "horizon": 5, "ret": 0.03,
                 "excess": 0.01, "entry_date": "2026-11-30",
                 "exit_date": "2026-12-05"},
            ],
        }


class RecommendationDetailTests(_EvidenceChainFixtures):
    def test_detail_exposes_data_as_of_factors_evidence_and_peers(self):
        detail = recommend.recommendation_detail(self._run(), "600519.SH")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["data_as_of"], "2026-11-27")
        self.assertEqual(detail["tradable_from"], "2026-11-30")
        self.assertEqual(detail["rank"], 1)
        self.assertEqual(detail["batch_size"], 3)
        self.assertEqual(detail["peer"]["n"], 3)
        self.assertEqual(detail["peer"]["same_industry_rank"], 1)
        self.assertEqual(detail["peer"]["same_industry_count"], 1)
        self.assertEqual(len(detail["peer"]["top"]), 3)
        self.assertEqual(
            [c["factor"] for c in detail["factor_contributions"]],
            ["base", "ma20", "ma60", "macd_hist", "rsi", "ret20", "clip"],
        )
        scores = detail["scores"]
        self.assertAlmostEqual(
            sum(c["delta"] for c in detail["factor_contributions"]), 78.0
        )
        self.assertAlmostEqual(
            scores["technical"],
            0.45 * scores["technical_raw"] + 0.55 * scores["technical_rank"],
            places=1,
        )
        self.assertAlmostEqual(
            scores["composite"],
            0.65 * scores["technical"] + 0.35 * scores["event"],
            places=1,
        )
        self.assertEqual(detail["evidence"][0]["evidence_id"], "ev_abc123456789")
        self.assertEqual(detail["tracking"]["5"]["ret"], 0.03)

        markdown = recommend.recommendation_detail_markdown(
            self._run(), "600519.SH"
        )
        self.assertIn("rec_demo", markdown)
        self.assertIn("2026-11-27", markdown)
        self.assertIn("因子贡献", markdown)
        self.assertIn("ev_abc123456789", markdown)
        self.assertIn("同批入选对比", markdown)
        self.assertIn("0.45×技术原始分 78.0", markdown)
        self.assertIn("0.55×批内排名分 90.0", markdown)
        self.assertIn("展示技术 84.6", markdown)
        self.assertIn("80.0 → 78.0", markdown)
        self.assertNotIn("截断前合计", markdown)

    def test_old_batch_degrades_without_fake_factors(self):
        run = self._run()
        run.pop("items_detail")
        old = {
            "code": "600519.SH", "kind": "news",
            "pub_time": "2026-11-27T08:00:00", "title": "旧新闻", "source": "媒体",
        }
        run["evidence"] = [old]
        detail = recommend.recommendation_detail(run, "600519.SH")
        self.assertEqual(detail["factor_contributions"], [])
        self.assertTrue(detail["notes"])
        self.assertTrue(detail["evidence"][0]["evidence_id"].startswith("ev_"))
        self.assertIsNone(detail["scores"]["score_before_clip"])

    def test_missing_code_returns_none(self):
        self.assertIsNone(recommend.recommendation_detail(self._run(), "999999.SZ"))
        self.assertIsNone(recommend.recommendation_detail(None, "600519.SH"))
        self.assertIn("未找到", recommend.recommendation_detail_markdown(
            self._run(), "999999.SZ"
        ))


class BatchReviewTests(_EvidenceChainFixtures):
    def _tracking(self) -> list[dict]:
        rows = []
        for code, rank, horizon, ret, excess in (
            ("600519.SH", 1, 1, 0.02, 0.01),
            ("000001.SZ", 2, 1, 0.00, -0.01),
            ("600519.SH", 1, 5, 0.03, 0.01),
            ("000001.SZ", 2, 5, -0.01, -0.02),
            ("600519.SH", 1, 20, -0.03, -0.04),
            ("000001.SZ", 2, 20, 0.02, 0.01),
            ("600036.SH", 3, 5, 0.10, 0.09),  # TopN 之外，不参与
        ):
            rows.append(
                {"code": code, "rank": rank, "horizon": horizon, "ret": ret,
                 "excess": excess, "entry_date": "2026-11-30",
                 "exit_date": "2026-12-05"}
            )
        return rows

    def test_topn_metrics_and_settled_horizon_drawdown(self):
        review = recommend.batch_review(
            self._run(), tracking=self._tracking(), top_n=2, price_frames={}
        )
        self.assertEqual(review["topn_size"], 2)
        self.assertEqual(review["settled_total"], 6)
        by_horizon = {row["horizon"]: row for row in review["horizons"]}
        self.assertEqual(by_horizon[5]["samples"], 2)
        self.assertAlmostEqual(by_horizon[5]["mean_ret"], 0.01)
        self.assertAlmostEqual(by_horizon[5]["mean_excess"], -0.005)
        self.assertAlmostEqual(by_horizon[5]["hit_rate"], 0.5)
        self.assertEqual(by_horizon[5]["coverage"], 1.0)
        self.assertEqual(review["drawdown"]["caliber"], "settled_horizons")
        self.assertAlmostEqual(review["drawdown"]["value"], -0.0149, places=4)

    def test_daily_nav_drawdown_from_price_frames(self):
        run = self._run()
        run["tradable_from"] = "2026-11-20"
        dates = pd.to_datetime(["2026-11-20", "2026-11-23", "2026-11-24"])
        frames = {
            "A": pd.DataFrame(
                {"trade_date": dates, "open": [100.0, 101.0, 102.0],
                 "close": [100.0, 110.0, 90.0]}
            ),
            "B": pd.DataFrame(
                {"trade_date": dates, "open": [50.0, 51.0, 52.0],
                 "close": [50.0, 55.0, 45.0]}
            ),
        }
        review = recommend.batch_review(
            run, tracking=self._tracking(), top_n=2, price_frames=frames
        )
        drawdown = review["drawdown"]
        self.assertEqual(drawdown["caliber"], "daily_nav")
        self.assertEqual(drawdown["n_codes"], 2)
        self.assertAlmostEqual(drawdown["value"], -0.1818, places=4)

    def test_review_markdown_lists_topn_and_hit_rate(self):
        review = recommend.batch_review(
            self._run(), tracking=self._tracking(), top_n=2, price_frames={}
        )
        markdown = recommend.batch_review_markdown(self._run(), review)
        self.assertIn("批次复盘", markdown)
        self.assertIn("T+5", markdown)
        self.assertIn("命中率", markdown)
        self.assertIn("回撤", markdown)
        self.assertIn("不构成 alpha 声明", markdown)


class BriefExportTests(_EvidenceChainFixtures):
    def test_brief_contains_traceable_ids_and_topn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = recommend.export_batch_brief(self._run(), out_dir=tmp)
            self.assertTrue(Path(path).exists())
            text = Path(path).read_text(encoding="utf-8")
        self.assertIn("单批简报", text)
        self.assertIn("rec_demo", text)
        self.assertIn("2026-11-27", text)
        self.assertIn("因子贡献", text)
        self.assertIn("ev_abc123456789", text)
        self.assertIn("T+5", text)
        self.assertIn("run_id", text)
        self.assertIn("evidence_id", text)


class EvidenceIdNormalizationTests(unittest.TestCase):
    def test_pub_time_representations_share_one_id(self):
        base = {
            "kind": "announcement",
            "title": "回购公告",
            "source": "交易所",
        }
        ids = {
            ledger.make_evidence_id(
                "rec_1", "600519.SH", {**base, "pub_time": pub_time}
            )
            for pub_time in (
                "2026-11-27T09:00:00",
                "2026-11-27 09:00:00",
                pd.Timestamp("2026-11-27 09:00:00"),
            )
        }
        self.assertEqual(len(ids), 1)
        self.assertRegex(ids.pop(), r"^ev_[0-9a-f]{12}$")


class OldLedgerBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patch = patch.object(ledger, "DB_PATH", self.dir / "ledger.db")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_old_evidence_row_gets_same_id_as_archive(self):
        with ledger.transaction() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, schema_version, generated_at, data_as_of, "
                "tradable_from, source, status) VALUES (?,?,?,?,?,?,?)",
                (
                    "rec_old", 2, "2026-11-27T18:00:00", "2026-11-27",
                    "2026-11-30", "live", "success",
                ),
            )
            # 旧库写入的是空格分隔时间，且 evidence_id 为空。
            conn.execute(
                "INSERT INTO evidence (run_id, code, kind, pub_time, title, source) "
                "VALUES (?,?,?,?,?,?)",
                (
                    "rec_old", "600519.SH", "announcement",
                    "2026-11-27 09:00:00", "回购公告", "交易所",
                ),
            )
        run = ledger.get_run("rec_old")
        self.assertIsNotNone(run)
        self.assertEqual(run["items_detail"], {})
        expected = ledger.make_evidence_id(
            "rec_old",
            "600519.SH",
            {
                "kind": "announcement",
                "pub_time": "2026-11-27T09:00:00",
                "title": "回购公告",
            },
        )
        self.assertEqual(run["evidence"][0]["evidence_id"], expected)


class ReviewWindowTests(unittest.TestCase):
    def test_window_is_bounded_and_failures_are_visible(self):
        run = _EvidenceChainFixtures._run()
        run["tradable_from"] = "2026-11-20"
        ends: list[pd.Timestamp] = []

        def fake_load(code, start=None, end=None, adj=None):
            ends.append(pd.Timestamp(end))
            if code == "000001.SZ":
                raise RuntimeError("日线缺失")
            dates = pd.bdate_range("2026-11-20", periods=5)
            return pd.DataFrame(
                {
                    "trade_date": dates,
                    "open": [100.0] * 5,
                    "close": [100.0, 101.0, 99.0, 102.0, 100.0],
                }
            )

        with patch.object(recommend, "load_daily", side_effect=fake_load):
            review = recommend.batch_review(run, tracking=[], top_n=2)

        self.assertTrue(ends)
        limit = pd.Timestamp("2026-11-20") + pd.offsets.BDay(20)
        self.assertTrue(all(end <= limit for end in ends))
        failed = review["drawdown"]["failed_codes"]
        self.assertEqual([x["code"] for x in failed], ["000001.SZ"])
        self.assertIn("日线缺失", failed[0]["error"])
        self.assertEqual(review["drawdown"]["window"]["end"], ends[0].date().isoformat())


class ArchitectureLayeringTests(unittest.TestCase):
    def test_ledger_does_not_import_recommend(self):
        tree = ast.parse(Path(ledger.__file__).read_text(encoding="utf-8"))
        modules = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        self.assertFalse(any(m == "recommend" or m.endswith(".recommend") for m in modules))


class BriefWeightsTests(_EvidenceChainFixtures):
    def test_brief_uses_recorded_weights(self):
        run = self._run()
        detail = run["items_detail"]["600519.SH"]
        detail["weights"] = {"technical": 0.70, "event": 0.30}
        detail["scores"]["composite"] = 0.70 * 84.6 + 0.30 * 86.0
        review = recommend.batch_review(
            run, tracking=self._tracking_for_brief(), top_n=2, price_frames={}
        )
        with tempfile.TemporaryDirectory() as tmp:
            text = Path(
                recommend.export_batch_brief(run, review=review, out_dir=tmp)
            ).read_text(encoding="utf-8")
        self.assertIn("0.70×技术 84.6", text)
        self.assertIn("0.30×事件 86.0", text)

    @staticmethod
    def _tracking_for_brief() -> list[dict]:
        return [
            {"code": "600519.SH", "rank": 1, "horizon": 5, "ret": 0.03,
             "excess": 0.01, "entry_date": "2026-11-30", "exit_date": "2026-12-05"},
        ]


class MomentumZeroTests(_EvidenceChainFixtures):
    def test_ret20_zero_is_flat_not_negative(self):
        frame = FactorContributionTests._frame()
        close = frame["close"].copy()
        close.iloc[-1] = close.iloc[-21]
        frame["close"] = close
        result = recommend.technical_score(frame)
        self.assertIsNotNone(result)
        self.assertEqual(result["ret20"], 0.0)
        row = next(c for c in result["contributions"] if c["factor"] == "ret20")
        self.assertEqual(row["delta"], -3.0)
        self.assertIn("持平", row["label"])


class SaveRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [
            patch.object(recommend, "REC_DIR", self.dir),
            patch.object(ledger, "DB_PATH", self.dir / "ledger.db"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def _rows(self) -> list[dict]:
        return [
            {
                "code": "600519.SH", "name": "贵州茅台", "industry": "白酒",
                "composite": 82.0, "technical_score": 80.0, "technical_raw": 78.0,
                "technical_rank": 90.0, "score_before_clip": 80.0,
                "event_score": 86.0, "percentile": 92.0, "close": 1500.0,
                "ret20": 0.032, "ret60": 0.05, "rsi": 58.0, "vol_ratio": 1.3,
                "atr_pct": 0.02, "drawdown": -0.05, "trend": "多头",
                "action": "技术面强", "reason": "趋势向上",
                "contributions": [
                    {"factor": "base", "label": "基础分", "delta": 50.0,
                     "value": None, "unit": "", "detail": "起始 50 分"},
                    {"factor": "ma20", "label": "站上MA20", "delta": 8.0,
                     "value": 0.02, "unit": "ratio", "detail": "收盘 / MA20"},
                ],
                "stats": {"n": 2}, "confidence": 0.8,
            }
        ]

    def _evidence(self) -> dict:
        return {
            "600519.SH": [
                {"kind": "announcement", "pub_time": "2026-11-27T09:00:00",
                 "title": "回购公告", "source": "交易所", "url": "",
                 "content": ""}
            ]
        }

    def test_json_and_ledger_keep_evidence_ids_and_items_detail(self):
        csv_path, json_path = recommend._save(
            self._rows(), "A股", {}, self._evidence(),
            "2026-11-27", "2026-11-30", "rec_roundtrip_001",
            "2026-11-27T18:00:00", diagnostics={"candidate_n": 3},
        )
        self.assertTrue(Path(csv_path).exists())
        payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
        evidence_id = payload["evidence"]["600519.SH"][0]["evidence_id"]
        self.assertTrue(evidence_id.startswith("ev_"))
        self.assertEqual(
            payload["items_detail"]["600519.SH"]["evidence_ids"], [evidence_id]
        )

        run = ledger.get_run("rec_roundtrip_001")
        self.assertIsNotNone(run)
        self.assertEqual(run["evidence"][0]["evidence_id"], evidence_id)
        self.assertEqual(
            run["items_detail"]["600519.SH"]["factor_contributions"][1]["factor"],
            "ma20",
        )
        self.assertEqual(run["tracking"], [])

        detail = recommend.recommendation_detail(run, "600519.SH")
        self.assertEqual(detail["evidence"][0]["evidence_id"], evidence_id)
        self.assertEqual(detail["data_as_of"], "2026-11-27")


class UIEvidenceChainTests(unittest.TestCase):
    def test_context_outputs_extend_without_breaking_six_tuple(self):
        from astock import recommend_ui

        payload = {
            "run_id": "rec_ui",
            "table": pd.DataFrame([{"代码": "600519.SH"}, {"代码": "000001.SZ"}]),
            "status_text": "ok", "report": "", "evidence_md": "", "files": None,
            "batch_md": "",
        }
        result = use_cases.RunResult(
            run_id="rec_ui", status="success", payload=payload, diagnostics={}
        )
        outputs = recommend_ui._context_outputs(result)
        self.assertEqual(len(outputs), 8)
        self.assertEqual(outputs[0], "ok")
        self.assertEqual(outputs[6], "rec_ui")
        self.assertEqual(outputs[7]["choices"], ["600519.SH", "000001.SZ"])
        self.assertEqual(recommend_ui._outputs(result), outputs[:6])


if __name__ == "__main__":
    unittest.main()
