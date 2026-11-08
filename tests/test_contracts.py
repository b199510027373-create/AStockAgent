"""契约层（astock/contracts.py）单元测试 + 三个入口的接入测试。

覆盖验收标准：四类缺失/类型错误（BarFrame 缺列、BarFrame 类型错误、EventFrame
缺列/空 code、Evidence 缺字段/时间不可解析、RecommendationRun schema_version 与
NaN），并验证 data/events/recommend 的关键入口在供应商列漂移时抛显式异常，
而不是静默返回空结果（评审 D18）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import astock.data as data
import astock.events as events
import astock.ledger as ledger
import astock.recommend as recommend
from astock.contracts import (
    BAR_SCHEMA,
    EVENT_SCHEMA,
    BarFrame,
    ContractError,
    EventFrame,
    validate_bars,
    validate_events,
    validate_evidence,
    validate_evidence_list,
    validate_recommendation_run,
)


def _bars_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["600519.SH"] * 3,
            "trade_date": pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"]),
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.8, 10.8, 11.8],
            "close": [10.2, 11.2, 12.2],
            "vol": [1000.0, 1100.0, 1200.0],
            "amount": [10000.0, 11000.0, 12000.0],
        }
    )


def _event_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": [""],
            "kind": ["news"],
            "pub_time": pd.to_datetime(["2025-01-05 09:00:00"]),
            "title": ["央行开展逆回购"],
            "source": ["sina"],
            "url": [""],
        }
    )


def _recommend_payload(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "run_id": "run-20250106-abc",
        "generated_at": "2025-01-06T15:30:00",
        "data_as_of": "2025-01-06",
        "tradable_from": "2025-01-07",
        "source": "live",
        "market": "A股",
        "params": {},
        "diagnostics": {},
        "table": [
            {
                "排名": 1,
                "代码": "600519.SH",
                "名称": "贵州茅台",
                "行业": "白酒",
                "综合分": 70.0,
                "技术分": 72.0,
                "事件分": 66.0,
                "收盘": 1500.0,
                "20日%": 3.2,
                "RSI": 58.0,
                "建议": "关注",
                "理由": "趋势向上",
            }
        ],
        "evidence": {
            "600519.SH": [
                {
                    "kind": "announcement",
                    "pub_time": "2025-01-05T09:00:00",
                    "title": "关于回购的公告",
                    "source": "公告",
                    "url": "http://example.com/a",
                    "content": "",
                }
            ]
        },
    }
    base.update(overrides)
    return base


class BarFrameContractTests(unittest.TestCase):
    """BarFrame：缺列与类型错误都必须带列名抛出。"""

    def test_missing_column_reports_name(self):
        df = _bars_frame().drop(columns=["ts_code"])
        with self.assertRaises(ContractError) as ctx:
            validate_bars(df, source="test")
        self.assertIn("ts_code", str(ctx.exception))
        self.assertIn("ts_code", ctx.exception.missing)

    def test_bad_trade_date_type(self):
        df = _bars_frame()
        df["trade_date"] = ["2025-01-02", "not-a-date", "2025-01-06"]
        with self.assertRaises(ContractError) as ctx:
            validate_bars(df, source="test")
        self.assertIn("trade_date", str(ctx.exception))

    def test_non_numeric_close(self):
        df = _bars_frame()
        df["close"] = ["10.2", "abc", "12.2"]
        with self.assertRaises(ContractError) as ctx:
            validate_bars(df, source="test")
        self.assertIn("close", str(ctx.exception))

    def test_require_adj_missing_factor_column(self):
        with self.assertRaises(ContractError) as ctx:
            validate_bars(_bars_frame(), require_adj=True)
        self.assertIn("adj_factor", ctx.exception.missing)

    def test_require_adj_nan_factor_rejected(self):
        df = _bars_frame()
        df["adj_factor"] = [1.0, np.nan, 4.0]
        with self.assertRaises(ContractError) as ctx:
            validate_bars(df, require_adj=True)
        self.assertIn("adj_factor", str(ctx.exception))

    def test_allow_empty_and_valid_frame_passthrough(self):
        empty = validate_bars(pd.DataFrame(), allow_empty=True)
        self.assertTrue(empty.empty)

        df = _bars_frame()
        df["open"] = ["10", "11", "12"]
        out = validate_bars(df)
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(out["trade_date"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(out["open"]))


class EventFrameContractTests(unittest.TestCase):
    """EventFrame：pub_time/title 缺失与个股事件空 code 必须显式失败。"""

    def test_missing_pub_time(self):
        df = _event_frame().drop(columns=["pub_time"])
        with self.assertRaises(ContractError) as ctx:
            validate_events(df)
        self.assertIn("pub_time", ctx.exception.missing)

    def test_blank_title_rejected(self):
        df = _event_frame()
        df["title"] = ["   "]
        with self.assertRaises(ContractError) as ctx:
            validate_events(df)
        self.assertIn("title", str(ctx.exception))

    def test_require_code_rejects_blank_code(self):
        with self.assertRaises(ContractError) as ctx:
            validate_events(_event_frame(), require_code=True)
        self.assertIn("code", str(ctx.exception))

    def test_market_news_allows_empty_code(self):
        out = validate_events(_event_frame())
        self.assertEqual(out["code"].iloc[0], "")


class EvidenceContractTests(unittest.TestCase):
    """Evidence：缺字段、时间不可解析、旧 date 键迁移。"""

    def test_missing_required_key(self):
        item = {"pub_time": "2025-01-05", "title": "t", "source": "公告", "kind": "announcement"}
        with self.assertRaises(ContractError) as ctx:
            validate_evidence(item)
        self.assertIn("url", ctx.exception.missing)

    def test_unparsable_pub_time(self):
        item = {
            "pub_time": "not-a-time",
            "title": "t",
            "source": "公告",
            "url": "",
            "kind": "announcement",
        }
        with self.assertRaises(ContractError) as ctx:
            validate_evidence(item)
        self.assertIn("pub_time", str(ctx.exception))

    def test_legacy_date_key_is_normalized(self):
        item = {"date": "2025-01-05", "title": "t", "source": "公告", "url": "", "kind": "announcement"}
        out = validate_evidence(item)
        self.assertEqual(out["pub_time"], "2025-01-05")

    def test_list_rejects_bad_item_with_index(self):
        items = [
            {"pub_time": "2025-01-05", "title": "t", "source": "公告", "url": "", "kind": "announcement"},
            {"pub_time": "2025-01-05", "title": "", "source": "公告", "url": "", "kind": "announcement"},
        ]
        with self.assertRaises(ContractError) as ctx:
            validate_evidence_list(items)
        self.assertIn("第 2 条", str(ctx.exception))


class RecommendationRunContractTests(unittest.TestCase):
    """RecommendationRun：schema_version、必填字段、表列与 NaN。"""

    def test_schema_version_must_be_2(self):
        with self.assertRaises(ContractError) as ctx:
            validate_recommendation_run(_recommend_payload(schema_version=1))
        self.assertIn("schema_version", str(ctx.exception))

    def test_missing_run_id(self):
        payload = _recommend_payload()
        payload.pop("run_id")
        with self.assertRaises(ContractError) as ctx:
            validate_recommendation_run(payload)
        self.assertIn("run_id", ctx.exception.missing)

    def test_table_row_requires_documented_columns(self):
        payload = _recommend_payload()
        payload["table"][0].pop("建议")
        with self.assertRaises(ContractError) as ctx:
            validate_recommendation_run(payload)
        self.assertIn("建议", str(ctx.exception))

    def test_nan_rejected(self):
        payload = _recommend_payload()
        payload["table"][0]["收盘"] = float("nan")
        with self.assertRaises(ContractError) as ctx:
            validate_recommendation_run(payload)
        self.assertIn("收盘", str(ctx.exception))

    def test_valid_payload_is_json_serializable(self):
        out = validate_recommendation_run(_recommend_payload())
        text = json.dumps(out, ensure_ascii=False, allow_nan=False)
        self.assertIn("600519.SH", text)


class _FakeClient:
    """按 api_name 返回固定 DataFrame 的最小客户端替身。"""

    def __init__(self, **frames):
        self._frames = frames
        self.calls: list[str] = []

    def query(self, api_name, **kwargs):
        self.calls.append(api_name)
        frame = self._frames.get(api_name)
        return frame.copy() if frame is not None else pd.DataFrame()


class DataEntryContractTests(unittest.TestCase):
    """data.py：供应商列漂移抛 ContractError，而不是空结果/静默丢列。"""

    def test_fetch_daily_missing_ts_code_raises(self):
        frame = _bars_frame().drop(columns=["ts_code"])
        client = _FakeClient(daily=frame)
        with self.assertRaises(ContractError) as ctx:
            data._fetch_daily(client, "600519.SH", "20250102", "20250106", "qfq")
        self.assertIn("ts_code", str(ctx.exception))


class EventsEntryContractTests(unittest.TestCase):
    """events.py：列漂移冒泡；公告缺 ts_code 时用请求代码补全（D18）。"""

    def test_fetch_news_missing_title_propagates(self):
        frame = pd.DataFrame(
            {"datetime": ["2025-01-05 09:00:00"], "content": ["x"]}
        )
        client = _FakeClient(news=frame)
        with (
            patch.object(events, "get_client", return_value=client),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, value: value),
            self.assertRaises(ContractError) as ctx,
        ):
            events.fetch_news(days=2)
        self.assertIn("title", str(ctx.exception))

    def test_fetch_announcements_fills_code_from_request(self):
        frame = pd.DataFrame(
            {
                "ann_date": ["20250105"],
                "name": ["贵州茅台"],
                "title": ["关于回购股份的公告"],
                "url": ["http://example.com/a"],
            }
        )
        client = _FakeClient(anns_d=frame)
        with (
            patch.object(events, "get_client", return_value=client),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, value: value),
        ):
            out = events.fetch_announcements(["600519.SH"], days=30)
        self.assertEqual(len(out), 1)
        self.assertEqual(out["code"].iloc[0], "600519.SH")

        with patch.object(
            events,
            "_meta_for",
            return_value={
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "industry": "白酒",
                "aliases": "贵州茅台",
            },
        ):
            evidence = events.evidence_for_stock(
                "600519.SH",
                "贵州茅台",
                "白酒",
                out,
                pd.DataFrame(),
                pd.DataFrame(),
                pd.DataFrame(),
                pd.DataFrame(),
            )
        self.assertEqual(len(evidence), 1)
        self.assertIn("pub_time", evidence[0])
        self.assertEqual(evidence[0]["kind"], "公告")

    def test_fetch_announcements_missing_ann_date_raises(self):
        frame = pd.DataFrame({"title": ["公告"], "ts_code": ["600519.SH"]})
        client = _FakeClient(anns_d=frame)
        with (
            patch.object(events, "get_client", return_value=client),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, value: value),
            self.assertRaises(ContractError) as ctx,
        ):
            events.fetch_announcements(["600519.SH"], days=30)
        self.assertIn("ann_date", ctx.exception.missing)


class RecommendEntryContractTests(unittest.TestCase):
    """recommend.py：_save 写盘前过 RecommendationRun，残缺证据不得落盘。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self) -> dict:
        return {
            "code": "600519.SH",
            "name": "贵州茅台",
            "industry": "白酒",
            "composite": 70.0,
            "technical_score": 72.0,
            "event_score": 66.0,
            "close": 1500.0,
            "ret20": 0.032,
            "rsi": 58.0,
            "action": "关注",
            "reason": "趋势向上",
        }

    def test_save_rejects_evidence_without_pub_time(self):
        bad_evidence = {
            "600519.SH": [{"title": "公告", "source": "公告", "url": "", "kind": "announcement"}]
        }
        with (
            patch.object(recommend, "REC_DIR", self.root),
            self.assertRaises(ContractError) as ctx,
        ):
            recommend._save(
                [self._row()],
                "A股",
                {},
                bad_evidence,
                "2025-01-06",
                "2025-01-07",
                "run-20250106-abc",
                "2025-01-06T15:30:00",
            )
        self.assertIn("pub_time", str(ctx.exception))
        self.assertEqual(list(self.root.glob("recommend_*.json")), [])

    def test_save_writes_schema_version_2(self):
        evidence = {
            "600519.SH": [
                {
                    "pub_time": pd.Timestamp("2025-01-05 09:00:00"),
                    "title": "关于回购的公告",
                    "source": "公告",
                    "url": "",
                    "kind": "announcement",
                }
            ]
        }
        with (
            patch.object(recommend, "REC_DIR", self.root),
            patch.object(recommend, "_atomic_write_text", wraps=recommend._atomic_write_text),
        ):
            _, json_path = recommend._save(
                [self._row()],
                "A股",
                {},
                evidence,
                "2025-01-06",
                "2025-01-07",
                "run-20250106-abc",
                "2025-01-06T15:30:00",
            )
        payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["evidence"]["600519.SH"][0]["pub_time"], "2025-01-05T09:00:00")


class ReviewRegressionTests(unittest.TestCase):
    """评审 rev-20261108-001 的回归：NaN、allow_empty 语义、DATA-02、日期漂移、入口冒泡。"""

    def test_nan_ts_code_rejected(self):
        df = _bars_frame()
        df["ts_code"] = [np.nan, "600519.SH", "600519.SH"]
        with self.assertRaises(ContractError) as ctx:
            validate_bars(df)
        self.assertIn("ts_code", str(ctx.exception))

    def test_allow_empty_requires_columns_on_zero_row_frame(self):
        with self.assertRaises(ContractError) as bars_ctx:
            validate_bars(pd.DataFrame({"ts_code": []}), allow_empty=True)
        self.assertIn("open", bars_ctx.exception.missing)

        with self.assertRaises(ContractError) as events_ctx:
            validate_events(pd.DataFrame({"code": []}), allow_empty=True)
        self.assertIn("kind", events_ctx.exception.missing)

        # 完全没有列的"无数据"哨兵仍然放行
        self.assertTrue(validate_bars(pd.DataFrame(), allow_empty=True).empty)
        self.assertTrue(validate_events(pd.DataFrame(), allow_empty=True).empty)

    def test_require_code_rejects_nan(self):
        df = _event_frame()
        df["code"] = [np.nan]
        with self.assertRaises(ContractError) as ctx:
            validate_events(df, require_code=True)
        self.assertIn("code", str(ctx.exception))

    def test_evidence_after_generated_at_rejected(self):
        payload = _recommend_payload()
        payload["evidence"]["600519.SH"][0]["pub_time"] = "2099-01-01T09:00:00"
        with self.assertRaises(ContractError) as ctx:
            validate_recommendation_run(payload)
        self.assertIn("DATA-02", str(ctx.exception))

    def test_validate_does_not_mutate_caller_payload(self):
        payload = _recommend_payload()
        payload["evidence"]["600519.SH"] = [
            {
                "date": "2025-01-05",
                "title": "t",
                "source": "公告",
                "url": "",
                "kind": "announcement",
            }
        ]
        out = validate_recommendation_run(payload)
        self.assertNotIn("pub_time", payload["evidence"]["600519.SH"][0])
        self.assertEqual(out["evidence"]["600519.SH"][0]["pub_time"], "2025-01-05")

    def test_announcement_date_format_drift_raises(self):
        frame = pd.DataFrame(
            {
                "ann_date": ["2026-11-06"],
                "title": ["关于回购的公告"],
                "ts_code": ["600519.SH"],
            }
        )
        client = _FakeClient(anns_d=frame)
        with (
            patch.object(events, "get_client", return_value=client),
            patch.object(events, "_cache_get", return_value=None),
            patch.object(events, "_cache_put", side_effect=lambda key, value: value),
            self.assertRaises(ContractError) as ctx,
        ):
            events.fetch_announcements(["600519.SH"], days=30)
        self.assertIn("pub_time", str(ctx.exception))

    def test_load_many_propagates_contract_error(self):
        error = ContractError("BarFrame", "缺少必需列 ['close']", missing=["close"])
        with (
            patch.object(recommend, "_thread_client", return_value=object()),
            patch.object(recommend, "load_daily", side_effect=error),
            self.assertRaises(ContractError),
        ):
            recommend.load_many(["600519.SH"], years=1)

    def test_schema_is_single_source_of_truth(self):
        """r2 P2：Schema 常量与 required 不能各写一份清单。"""
        self.assertEqual(
            BarFrame.required, tuple(c for c in BAR_SCHEMA if c != "adj_factor")
        )
        self.assertTrue(set(BarFrame.required) <= set(BAR_SCHEMA))
        self.assertIn("adj_factor", BAR_SCHEMA)
        self.assertEqual(EventFrame.required, EVENT_SCHEMA)
        self.assertTrue(set(EventFrame.required) <= set(EVENT_SCHEMA))

    def test_tz_aware_pub_time_compares_instead_of_type_error(self):
        """r2 P2：带 +08:00 的证据时间不能抛 TypeError。"""
        payload = _recommend_payload()
        payload["evidence"]["600519.SH"][0]["pub_time"] = "2025-01-05T09:00:00+08:00"
        out = validate_recommendation_run(payload)
        self.assertEqual(
            out["evidence"]["600519.SH"][0]["pub_time"], "2025-01-05T09:00:00+08:00"
        )

    def test_tz_aware_future_pub_time_raises_contract_error(self):
        payload = _recommend_payload()
        payload["evidence"]["600519.SH"][0]["pub_time"] = "2099-01-01T09:00:00+08:00"
        with self.assertRaises(ContractError) as ctx:
            validate_recommendation_run(payload)
        self.assertIn("DATA-02", str(ctx.exception))


class CitationBlockTests(unittest.TestCase):
    """r2 P1：app._citation_block 必须消费 Evidence 的 pub_time 结构。"""

    @classmethod
    def setUpClass(cls):
        try:
            import app  # noqa: PLC0415 - UI 模块导入较重，按需加载
        except Exception as exc:  # pragma: no cover - 缺 UI 依赖时跳过
            raise unittest.SkipTest(f"app 无法导入（{type(exc).__name__}: {exc}）")
        cls.app = app

    def _citations(self, item: dict) -> list[dict]:
        return validate_evidence_list([item], field="build_ai_context")

    def test_renders_pub_time_from_evidence_contract(self):
        citations = self._citations(
            {
                "kind": "news",
                "pub_time": "2025-01-05T09:00:00",
                "title": "央行开展逆回购",
                "source": "sina",
                "url": "http://example.com/n",
                "content": "",
            }
        )
        text = self.app._citation_block(citations)
        self.assertIn("2025-01-05", text)
        self.assertIn("央行开展逆回购", text)
        self.assertIn("[原文](http://example.com/n)", text)

    def test_accepts_legacy_date_key(self):
        """迁移期旧缓存可能还是 date 键，渲染层需要兼容。"""
        text = self.app._citation_block(
            [
                {
                    "kind": "announcement",
                    "date": "2025-01-06",
                    "title": "关于回购的公告",
                    "source": "公告",
                    "url": "",
                }
            ]
        )
        self.assertIn("2025-01-06", text)

    def test_missing_or_bad_time_does_not_raise(self):
        text = self.app._citation_block(
            [
                {"kind": "news", "title": "无日期", "source": "sina", "url": ""},
                {
                    "kind": "news",
                    "pub_time": "not-a-date",
                    "title": "坏日期",
                    "source": "sina",
                    "url": "",
                },
            ]
        )
        self.assertIn("无日期", text)
        self.assertIn("坏日期", text)

    def test_empty_citations(self):
        self.assertEqual(self.app._citation_block([]), "")
        self.assertEqual(self.app._citation_block(None), "")


def _synthetic_daily(n: int = 140, start: str = "2025-01-02") -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=n)
    close = pd.Series(np.linspace(10.0, 15.0, n))
    frame = pd.DataFrame(
        {
            "trade_date": dates,
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "pre_close": close.shift(1).fillna(close.iloc[0]),
            "vol": 1000.0,
            "amount": 10000.0,
        }
    )
    return data.enrich(frame)


class LlmActionContractTests(unittest.TestCase):
    """AI-01：LLM 只能补充理由，不能覆盖规则算出的「建议」。"""

    def test_llm_action_cannot_override_rule_action(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        daily = _synthetic_daily()
        last = daily["trade_date"].max()
        basic = pd.DataFrame(
            [
                {
                    "ts_code": "600519.SH",
                    "name": "贵州茅台",
                    "industry": "白酒",
                    "aliases": "茅台",
                }
            ]
        )
        cal = pd.DataFrame(
            {"cal_date": pd.bdate_range("2025-01-01", "2025-12-31"), "is_open": 1}
        )
        ai = {
            "market": "震荡",
            "items": [
                {"code": "600519.SH", "action": "回避", "reason": "AI 看空"}
            ],
        }
        with (
            patch.object(recommend, "REC_DIR", root),
            patch.object(ledger, "DB_PATH", root / "ledger.db"),
            patch.object(recommend, "parse_universe", return_value=["600519.SH"]),
            patch.object(recommend, "stock_basic", return_value=basic),
            patch.object(recommend, "load_many", return_value={"600519.SH": daily}),
            patch.object(recommend, "load_index_daily", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_announcements", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_reports", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_news", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_major_news", return_value=pd.DataFrame()),
            patch.object(recommend, "fetch_policy", return_value=pd.DataFrame()),
            patch.object(recommend, "evidence_for_stock", return_value=[]),
            patch.object(recommend, "last_completed_trade_date", return_value=last),
            patch.object(
                recommend, "next_open_date", return_value=pd.Timestamp("2025-07-11")
            ),
            patch.object(recommend, "load_trade_cal", return_value=cal),
            patch.object(recommend, "_llm_rank", return_value=ai),
        ):
            result = recommend.run_daily_recommendation(
                universe_text="600519.SH", top_n=1, candidate_n=1, use_llm=True
            )
        row = result.table.iloc[0]
        # 前提：规则给出的建议不是"技术面弱"，否则 AI 的"回避"覆盖与否无法区分。
        self.assertGreaterEqual(row["综合分"], 52)
        self.assertEqual(row["建议"], recommend._action(row["综合分"]))
        self.assertIn(row["建议"], ("技术面强", "技术面中性", "技术面弱"))
        self.assertNotEqual(row["建议"], "回避")
        self.assertEqual(row["理由"], "AI 看空")


if __name__ == "__main__":
    unittest.main()
