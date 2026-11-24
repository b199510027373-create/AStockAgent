"""AI 对话 Tab 的离线测试（ADR-013 / ADR-016 验收）。

覆盖：附件 1–5 个解析状态、超限拒绝、流式切片、工具步骤时间线、图表内嵌
（iframe srcdoc + spec/来源）、引用收集与按文件名/工作表定位、协作式中断保留
已产出内容，以及 Gradio 装配的冒烟检查（不启动服务器）。
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock
from urllib import parse as urlparse
from urllib import request as urlrequest

from astock.agent import (
    AgentRuntime,
    AgentSession,
    FakeModel,
    Permission,
    StopReason,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from astock.agent.chat import (
    _CITATION_SNIPPET_CHARS,
    AgentChatController,
    Attachment,
    ChatEvent,
    Citation,
    Figure,
    attachment_note,
    attachment_summary,
    collect_citations,
    collect_figures,
    export_figures,
    figure_embed_html,
    ingest_attachments,
    iter_answer_chunks,
    parse_citation,
    render_citations_html,
    render_figures_html,
    render_steps_html,
    resolve_citation,
    stop_reason_label,
)
from astock.agent.tools.citations import citation_for_sheet
from astock.agent.types import AgentRunResult, StepRecord


@contextmanager
def _tempdir() -> Iterator[Path]:
    """默认权限的临时目录（沙箱里 0o700 目录不可读，见 test_agent_files）。"""
    base = Path(tempfile.gettempdir()) / f"astock-chat-{uuid.uuid4().hex}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"code": {"type": "string", "minLength": 6}},
    "required": ["code"],
    "additionalProperties": False,
}


def price_registry(delay: float = 0.0) -> ToolRegistry:
    registry = ToolRegistry()

    def handler(*, code: str) -> ToolResult:
        if delay:
            time.sleep(delay)
        return ToolResult(
            text=f"{code} 收盘价 1680.00 元",
            data_as_of="2026-11-18",
            data_time="2026-11-18 15:00",
            source="fake://price",
        )

    registry.register(
        ToolSpec(
            name="lookup_price",
            description="查询收盘价",
            parameters=SCHEMA,
            handler=handler,
            permissions=(Permission.MARKET_READ,),
        )
    )
    return registry


def make_controller(model: Any, *, delay: float = 0.0, **kwargs: Any) -> AgentChatController:
    return AgentChatController(
        model_factory=lambda: model,
        runtime_factory=lambda m: AgentRuntime(
            m, registry=price_registry(delay), session=AgentSession(), auto_save=False
        ),
        chunk_chars=12,
        **kwargs,
    )


class BlockingRuntime:
    """测试用运行时：run 阻塞到 release，用来制造并发与预运行中断窗口。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.runs = 0
        self.interrupts = 0

    def run(
        self,
        question: str = "",
        *,
        cancel_event: Any = None,
        on_step: Any = None,
    ) -> AgentRunResult:
        self.runs += 1
        self.started.set()
        self.release.wait(5)
        return AgentRunResult(
            answer="好的",
            stop_reason=StopReason.COMPLETED,
            steps=[],
            tokens_used=0,
            elapsed_seconds=0.0,
            session_id="s-blocking",
            error=None,
        )

    def request_interrupt(self) -> None:
        self.interrupts += 1

    def request_interrupt_if_running(self) -> bool:
        self.request_interrupt()
        return True


def step_with(
    result: ToolResult,
    *,
    tool: str = "lookup_price",
    index: int = 0,
    arguments: dict[str, Any] | None = None,
) -> StepRecord:
    return StepRecord(
        index=index,
        plan="",
        tool=tool,
        arguments=arguments or {"code": "600519.SH"},
        result=result,
        observation="",
        reflection=result.text or result.error or "",
        tokens=0,
        elapsed=0.12,
    )


class StreamChunkTests(unittest.TestCase):
    def test_chunks_reassemble_and_respect_boundaries(self) -> None:
        text = "第一段结论。\n第二段有数据：营收 10 亿元；净利润 2 亿元。\n第三段。"
        chunks = list(iter_answer_chunks(text, chunk_chars=16))
        self.assertEqual("".join(chunks), text)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 16)

    def test_empty_text_yields_nothing(self) -> None:
        self.assertEqual(list(iter_answer_chunks("")), [])

    def test_invalid_chunk_size(self) -> None:
        with self.assertRaises(ValueError):
            list(iter_answer_chunks("abc", chunk_chars=0))


class AttachmentTests(unittest.TestCase):
    def test_ingest_csv_and_txt_shows_status(self) -> None:
        with _tempdir() as tmp:
            root = Path(tmp) / "files"
            csv_path = Path(tmp) / "营收.csv"
            csv_path.write_text("项目,金额\n营收,100\n净利润,20\n", encoding="utf-8")
            txt_path = Path(tmp) / "notes.txt"
            txt_path.write_text("这是会议纪要。", encoding="utf-8")

            attachments = ingest_attachments([csv_path, txt_path], root=root, index=False)
            self.assertEqual([item.ok for item in attachments], [True, True])
            summary = attachment_summary(attachments)
            self.assertIn("附件解析：2/2 成功", summary)
            self.assertIn("✅ 营收.csv", summary)
            note = attachment_note(attachments)
            self.assertIn("营收.csv", note)
            self.assertIn("不可信数据", note)
            self.assertIn("未在文件中找到", note)

    def test_more_than_five_files_rejected_with_reason(self) -> None:
        with _tempdir() as tmp:
            root = Path(tmp) / "files"
            paths = []
            for index in range(6):
                path = Path(tmp) / f"f{index}.txt"
                path.write_text(f"第 {index} 份内容", encoding="utf-8")
                paths.append(path)
            attachments = ingest_attachments(paths, root=root, index=False)
            self.assertEqual(len(attachments), 6)
            self.assertEqual(sum(1 for item in attachments if item.ok), 5)
            self.assertFalse(attachments[-1].ok)
            self.assertIn("单轮最多", "；".join(attachments[-1].warnings))
            self.assertIn("5/6", attachment_summary(attachments))

    def test_attachment_summary_empty(self) -> None:
        summary = attachment_summary([])
        self.assertIn("未选择", summary)
        self.assertIn("5 个文件", summary)
        self.assertEqual(attachment_note([]), "")

    def test_unsupported_file_gives_status_not_exception(self) -> None:
        with _tempdir() as tmp:
            root = Path(tmp) / "files"
            path = Path(tmp) / "image.png"
            path.write_bytes(b"\x89PNG\r\n")
            attachments = ingest_attachments([path], root=root, index=False)
            self.assertEqual(len(attachments), 1)
            self.assertFalse(attachments[0].ok)
            self.assertTrue(attachments[0].warnings)


class TimelineTests(unittest.TestCase):
    def test_steps_render_tool_arguments_and_tags(self) -> None:
        step = step_with(
            ToolResult(
                text="收盘价 1680.00 元",
                data_as_of="2026-11-18",
                data_time="2026-11-18 15:00",
                source="fake://price",
                table=[{"close": 1680.0}],
                figure="<html>fig</html>",
            ),
            arguments={"code": "<600519.SH>"},
        )
        markup = render_steps_html([step], pending="继续分析…")
        self.assertIn('data-status="ok"', markup)
        self.assertIn("lookup_price", markup)
        self.assertIn("&lt;600519.SH&gt;", markup)  # 参数必须转义
        self.assertIn("截至 2026-11-18", markup)
        self.assertIn("数据时间 2026-11-18 15:00", markup)
        self.assertIn("📊 图表", markup)
        self.assertIn("📋 表格", markup)
        self.assertIn("继续分析…", markup)
        self.assertIn("0.12s", markup)
        self.assertIn("收盘价 1680.00 元", markup)

    def test_steps_show_tool_output_beside_reflection(self) -> None:
        """真实运行里 reflection 是通用提示，工具结果必须单独可见（AC2）。"""

        step = StepRecord(
            index=0,
            plan="",
            tool="quote",
            arguments={"ts_code": "600519.SH"},
            result=ToolResult(
                text="600519.SH 最新价 1,485.75 元（数据截至 2026-11-20）",
                data_as_of="2026-11-20",
                source="market:quote:daily",
                figure="<html>SHOULD-NOT-INLINE</html>",
            ),
            observation="",
            reflection="工具执行成功，继续判断是否已有足够信息。",
            tokens=0,
            elapsed=0.5,
        )
        markup = render_steps_html([step])
        self.assertIn("工具执行成功", markup)
        self.assertIn("最新价 1,485.75 元", markup)
        self.assertIn('class="step-output"', markup)
        self.assertNotIn("SHOULD-NOT-INLINE", markup)

    def test_step_output_is_escaped_and_clipped(self) -> None:
        step = StepRecord(
            index=0,
            plan="",
            tool="file_read",
            arguments={},
            result=ToolResult(text="<script>alert(1)</script>" + "x" * 600),
            observation="",
            reflection="成功",
            tokens=0,
            elapsed=0.1,
        )
        markup = render_steps_html([step])
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)
        self.assertLessEqual(len(markup), 1200)

    def test_failed_step_marked_error(self) -> None:
        step = step_with(ToolResult(error="参数非法"), tool="file_search")
        markup = render_steps_html([step])
        self.assertIn('data-status="error"', markup)
        self.assertIn("file_search", markup)

    def test_empty_timeline_placeholder(self) -> None:
        self.assertIn("尚未调用工具", render_steps_html([]))


class FigureTests(unittest.TestCase):
    def _figure_step(self) -> StepRecord:
        html_doc = '<html><body><div id="chart"></div><script>1</script></body></html>'
        result = ToolResult(
            text="K 线已生成",
            figure=html_doc,
            data_as_of="2026-11-18",
            data_time="2026-11-18 15:00",
            source="market://kline",
            metadata={
                "artifact_id": "art_123",
                "artifact_path": "data/agent/artifacts/art_123.html",
                "chart": {
                    "title": "600519.SH 日线",
                    "spec": {"version": 1, "kind": "line", "data": {"source": "market"}},
                    "warnings": ["指标窗口不足"],
                },
            },
        )
        return step_with(result, tool="kline")

    def test_collect_figures_reads_spec_and_time_points(self) -> None:
        figures = collect_figures([self._figure_step()])
        self.assertEqual(len(figures), 1)
        figure = figures[0]
        self.assertEqual(figure.title, "600519.SH 日线")
        self.assertEqual(figure.artifact_id, "art_123")
        self.assertEqual(figure.data_as_of, "2026-11-18")
        self.assertIn("数据截至 2026-11-18", figure.caption)
        self.assertIn("数据时间 2026-11-18 15:00", figure.caption)

    def test_embed_uses_iframe_srcdoc_and_details(self) -> None:
        figure = collect_figures([self._figure_step()])[0]
        markup = figure_embed_html(figure)
        self.assertIn("<iframe", markup)
        self.assertIn("srcdoc=", markup)
        self.assertIn("&lt;html&gt;", markup)  # 文档被转义进属性
        self.assertIn("查看图表 spec / 来源", markup)
        self.assertIn("&quot;version&quot;: 1", markup)
        self.assertIn("market://kline", markup)
        self.assertIn("指标窗口不足", markup)
        self.assertIn("数据截至 2026-11-18", markup)
        self.assertIn("PNG：不适用（该工具不导出 PNG）", markup)
        self.assertNotIn("PNG：未生成", markup)

    def test_missing_time_points_write_unknown(self) -> None:
        result = ToolResult(figure="<html></html>", source="market://x")
        figure = collect_figures([step_with(result, tool="kline")])[0]
        self.assertIn("数据截至 时点未知", figure.caption)
        self.assertIn("数据时间 时点未知", figure.caption)
        self.assertIn("本轮回答还没有图表", render_figures_html([]))

    def _table_chart_step(self, png_path: str = "") -> StepRecord:
        """table_chart 的 metadata 形状：chart_spec / png_path，无 ``chart`` 包装。"""
        result = ToolResult(
            text="已生成柱状图",
            figure='<html><body><div id="plotly-table"></div></body></html>',
            source="file:月度销售.xlsx",
            data_as_of="2026-03-31",
            data_time="2026-03-31 00:00",
            metadata={
                "title": "月度销售",
                "chart_spec": {
                    "version": 1,
                    "kind": "bar",
                    "data": {"source": "inline"},
                },
                "artifact_id": "art_table",
                "artifact_path": "data/agent/artifacts/art_table.html",
                "png_path": png_path,
                "png_mode": "auto",
                "warnings": ["PNG 导出失败：Chrome 不可用"],
            },
        )
        return step_with(result, tool="table_chart")

    def test_collect_figures_reads_table_chart_metadata(self) -> None:
        figure = collect_figures([self._table_chart_step("/tmp/art_table.png")])[0]
        self.assertEqual(figure.tool, "table_chart")
        self.assertEqual(figure.title, "月度销售")
        self.assertEqual(figure.spec["kind"], "bar")
        self.assertEqual(figure.png_path, "/tmp/art_table.png")
        self.assertTrue(figure.png_requested)
        self.assertEqual(figure.png_mode, "auto")
        self.assertEqual(figure.data_as_of, "2026-03-31")
        self.assertIn("PNG 导出失败", figure.warnings[0])

    def test_export_figures_does_not_report_png_degradation_for_kline(self) -> None:
        """K 线固定不导出 PNG，不能把设计选择写成 kaleido/Chrome 降级。"""
        with _tempdir() as tmp:
            figure = collect_figures([self._figure_step()])[0]
            self.assertFalse(figure.png_requested)
            paths, notes = export_figures([figure], out_dir=Path(tmp) / "exports")
            self.assertEqual(len(paths), 1)
            self.assertEqual(notes, [])

    def test_export_figures_prunes_stale_files(self) -> None:
        with _tempdir() as tmp:
            exports = Path(tmp) / "exports"
            exports.mkdir()
            stale = exports / "stale.html"
            stale.write_text("old", encoding="utf-8")
            old = time.time() - 8 * 86400
            os.utime(stale, (old, old))
            figure = collect_figures([self._table_chart_step()])[0]
            paths, _ = export_figures([figure], out_dir=exports)
            self.assertFalse(stale.exists())
            self.assertIn(str(exports / "art_table.html"), paths)

    def test_export_figures_writes_html_and_reuses_png(self) -> None:
        with _tempdir() as tmp:
            png = Path(tmp) / "art_table.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            figure = collect_figures([self._table_chart_step(str(png))])[0]
            paths, notes = export_figures([figure], out_dir=Path(tmp) / "exports")
            self.assertEqual(notes, [])
            html_path = Path(tmp) / "exports" / "art_table.html"
            self.assertIn(str(html_path), paths)
            png_export = Path(tmp) / "exports" / "art_table.png"
            self.assertIn(str(png_export), paths)
            self.assertEqual(png_export.read_bytes(), png.read_bytes())
            self.assertIn('id="plotly-table"', html_path.read_text(encoding="utf-8"))

    def test_export_figures_reports_missing_png(self) -> None:
        with _tempdir() as tmp:
            figure = collect_figures([self._table_chart_step("")])[0]
            paths, notes = export_figures([figure], out_dir=Path(tmp) / "exports")
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].endswith("art_table.html"))
            self.assertIn("未生成 PNG", notes[0])

    def test_embed_shows_png_availability(self) -> None:
        with _tempdir() as tmp:
            png = Path(tmp) / "art_table.png"
            png.write_bytes(b"\x89PNG")
            markup = figure_embed_html(
                collect_figures([self._table_chart_step(str(png))])[0]
            )
            self.assertIn("PNG：art_table.png", markup)
        missing = figure_embed_html(collect_figures([self._table_chart_step()])[0])
        self.assertIn("PNG：未生成", missing)


class CitationTests(unittest.TestCase):
    def test_parse_citation_round_trip(self) -> None:
        parsed = parse_citation("《营收.xlsx》工作表「利润表」A2:D2")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["name"], "营收.xlsx")
        self.assertEqual(parsed["sheet"], "利润表")
        self.assertEqual(parsed["cell"], "A2:D2")
        self.assertIsNone(parsed["page"])
        pdf = parse_citation("《年报.pdf》第 12 页")
        self.assertEqual(pdf["page"], 12)
        self.assertIsNone(parse_citation("《只是一本书》"))

    def test_collect_and_resolve_against_ingested_csv(self) -> None:
        with _tempdir() as tmp:
            root = Path(tmp) / "files"
            path = Path(tmp) / "营收.csv"
            path.write_text("项目,金额\n营收,100\n净利润,20\n", encoding="utf-8")
            ingested = ingest_attachments([path], root=root, index=False)[0]
            self.assertTrue(ingested.ok)
            from astock.agent import files as file_store

            meta = file_store.load_meta(ingested.file_id or "", root=root)
            sheet = meta["sheets"][0]
            label = citation_for_sheet(meta, sheet)

            result = ToolResult(
                text="营收 100",
                source="file:营收.csv",
                metadata={"file_id": meta["file_id"], "citations": [label]},
            )
            citations = collect_citations([step_with(result, tool="file_table")], answer=f"见 {label}")
            self.assertEqual(len(citations), 1)
            self.assertEqual(citations[0].name, "营收.csv")
            self.assertIsNotNone(citations[0].sheet)

            detail = resolve_citation(citations[0], root=root)
            self.assertTrue(detail.found)
            self.assertIn("营收", detail.snippet)
            self.assertLessEqual(len(detail.snippet), _CITATION_SNIPPET_CHARS + 1)
            self.assertTrue(Path(detail.source_path).is_file())

            markup = render_citations_html(citations, root=root)
            self.assertIn('id="cite-1"', markup)
            self.assertIn('href="#cite-1"', markup)
            self.assertIn("营收.csv", markup)

    def test_unresolved_citation_is_explained(self) -> None:
        citation = Citation(label="《不存在.pdf》第 3 页", name="不存在.pdf", page=3)
        detail = resolve_citation(citation)
        self.assertFalse(detail.found)
        self.assertIn("未找到同名文件", detail.note)
        self.assertIn("本轮没有文件引用", render_citations_html([]))

    def test_stop_reason_labels(self) -> None:
        self.assertIn("已完成", stop_reason_label(StopReason.COMPLETED))
        self.assertIn("中断", stop_reason_label(StopReason.INTERRUPTED))
        self.assertIn("something_else", stop_reason_label("something_else"))

    def test_page_mismatch_falls_back_to_first_chunk(self) -> None:
        """引用页码不命中时退回首段，并明确说明未精确命中（评审 P2）。"""
        with _tempdir() as tmp:
            root = Path(tmp) / "files"
            path = Path(tmp) / "年报.csv"
            path.write_text("项目,金额\n营收,100\n", encoding="utf-8")
            ingested = ingest_attachments([path], root=root, index=False)[0]
            self.assertTrue(ingested.ok)
            citation = Citation(
                label="《年报.csv》第 999 页",
                name="年报.csv",
                page=999,
                file_id=ingested.file_id,
            )
            detail = resolve_citation(citation, root=root)
            self.assertTrue(detail.found)
            self.assertIn("项目", detail.snippet)
            self.assertIn("未在提取内容中精确命中", detail.note)

    def test_sheet_level_match_with_cell_does_not_claim_cell_precision(self) -> None:
        """只匹配到工作表时不能说成行/单元格级定位（评审 iss-mwmtta2g）。"""
        with _tempdir() as tmp:
            root = Path(tmp) / "files"
            path = Path(tmp) / "利润表.csv"
            path.write_text("项目,金额\n营收,100\n", encoding="utf-8")
            ingested = ingest_attachments([path], root=root, index=False)[0]
            self.assertTrue(ingested.ok)
            from astock.agent import files as file_store

            meta = file_store.load_meta(ingested.file_id or "", root=root)
            first_sheet = meta["sheets"][0]
            sheet_name = (
                first_sheet["name"] if isinstance(first_sheet, dict) else str(first_sheet)
            )
            citation = Citation(
                label=f"《利润表.csv》工作表「{sheet_name}」B2",
                name="利润表.csv",
                sheet=sheet_name,
                cell="B2",
                file_id=ingested.file_id,
            )
            detail = resolve_citation(citation, root=root)
            self.assertTrue(detail.found)
            self.assertIn("工作表级定位", detail.note)
            self.assertIn("B2", detail.note)

    def test_more_than_limit_truncates_with_hint(self) -> None:
        citations = [
            Citation(label=f"《f{index}.pdf》第 {index + 1} 页", name=f"f{index}.pdf", page=index + 1)
            for index in range(21)
        ]
        markup = render_citations_html(citations)
        self.assertIn("还有 1 条引用未展示", markup)
        self.assertEqual(markup.count('class="astock-cite-card"'), 20)
        self.assertIn("引用（21）", markup)

    def test_memo_reuses_resolved_details(self) -> None:
        citations = [Citation(label="《年报.pdf》第 3 页", name="年报.pdf", page=3)]
        memo: dict[tuple[Any, ...], Any] = {}
        from astock.agent import chat as chat_module

        calls = 0
        real = chat_module.resolve_citation

        def counting(citation: Citation, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return real(citation, **kwargs)

        with mock.patch.object(chat_module, "resolve_citation", counting):
            render_citations_html(citations, memo=memo)
            render_citations_html(citations, memo=memo)
        self.assertEqual(calls, 1)


class ControllerTests(unittest.TestCase):
    def test_streams_steps_then_answer_and_finishes(self) -> None:
        model = FakeModel(
            [
                ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                "收盘价 1680.00 元（数据截至 2026-11-18）。",
            ]
        )
        controller = make_controller(model)
        events = list(controller.ask("600519.SH 收盘价？"))
        kinds = [event.kind for event in events]
        self.assertEqual(kinds[0], "status")
        self.assertIn("step", kinds)
        self.assertIn("answer", kinds)
        self.assertEqual(kinds[-1], "done")

        steps = [event.step for event in events if event.kind == "step" and event.step]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].tool, "lookup_price")
        answer = "".join(event.text for event in events if event.kind == "answer")
        self.assertIn("1680.00", answer)
        done = events[-1]
        self.assertIsNotNone(done.result)
        self.assertIs(done.result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(done.result.steps[0].tool, "lookup_price")

    def test_empty_question_finishes_without_runtime(self) -> None:
        controller = make_controller(FakeModel([]))
        events = list(controller.ask("   "))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "done")
        self.assertIn("请先输入问题", events[0].text)

    def test_interrupt_keeps_produced_steps_and_partial_answer(self) -> None:
        script = [
            ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
            ToolCall(name="lookup_price", arguments={"code": "000001.SZ"}),
            ToolCall(name="lookup_price", arguments={"code": "300750.SZ"}),
            "不会走到这里",
        ]
        controller = make_controller(FakeModel(script), delay=0.5)
        events: list[ChatEvent] = []
        for event in controller.ask("这三只股票怎么样？"):
            events.append(event)
            if event.kind == "step":
                self.assertIn("中断", controller.interrupt())
        self.assertTrue(any(event.kind == "step" for event in events))
        done = events[-1]
        self.assertEqual(done.kind, "done")
        self.assertIsNotNone(done.result)
        self.assertIs(done.result.stop_reason, StopReason.INTERRUPTED)
        self.assertIn("保留", done.text)
        answer = "".join(event.text for event in events if event.kind == "answer")
        self.assertIn("中断", answer)
        # 已产出的步骤仍在事件流里（页面时间线不清空），且没有继续跑完脚本。
        step_events = [event for event in events if event.kind == "step"]
        self.assertGreaterEqual(len(step_events), 1)
        self.assertLess(len(step_events), 3)
        self.assertEqual(
            [event.step for event in step_events], list(done.result.steps)
        )

    def test_attachment_note_is_injected_into_question(self) -> None:
        seen: list[list[dict[str, Any]]] = []

        class RecordingModel(FakeModel):
            def complete(self, *, messages, tools):  # type: ignore[override]
                seen.append([dict(item) for item in messages])
                return super().complete(messages=messages, tools=tools)

        controller = make_controller(RecordingModel(["好的"]))
        attachment = Attachment(
            name="年报.pdf",
            status="ok",
            ok=True,
            file_id="f_" + "0" * 16,
            pages=12,
        )
        list(controller.ask("营收如何？", attachments=[attachment]))
        self.assertTrue(seen)
        user_messages = [item for item in seen[0] if item.get("role") == "user"]
        self.assertTrue(any("年报.pdf" in str(item.get("content")) for item in user_messages))

    def test_model_factory_failure_degrades_instead_of_crashing(self) -> None:
        controller = AgentChatController(
            model_factory=lambda: (_ for _ in ()).throw(RuntimeError("no key")),
        )
        events = list(controller.ask("你好"))
        self.assertEqual(events[0].kind, "status")
        self.assertEqual(events[-1].kind, "done")
        self.assertIn("模型不可用", events[-1].text)
        self.assertIn("模型未就绪", controller.model_banner())

    def test_busy_round_is_rejected_and_lock_released(self) -> None:
        """并发第二次提问直接拒绝，且第一轮结束后锁释放（评审 P2）。"""
        runtime = BlockingRuntime()
        controller = AgentChatController(
            model_factory=lambda: FakeModel(["好的"]),
            runtime_factory=lambda model: runtime,
        )
        first: list[ChatEvent] = []
        worker = threading.Thread(target=lambda: first.extend(controller.ask("第一问")))
        worker.start()
        self.assertTrue(runtime.started.wait(5))
        second = list(controller.ask("第二问"))
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].kind, "done")
        self.assertIn("上一轮仍在生成", second[0].text)
        runtime.release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        third = list(controller.ask("第三问"))
        self.assertEqual(third[-1].kind, "done")
        self.assertEqual(runtime.runs, 2)

    def test_done_event_releases_lock_before_generator_is_drained(self) -> None:
        """UI 收到 done 后不再 next() 时，busy 锁也必须释放（浏览器断开场景）。"""
        controller = make_controller(
            FakeModel(
                [
                    ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                    "收盘价 1680.00 元。",
                ]
            )
        )
        stream = controller.ask("第一问")
        for event in stream:
            if event.kind == "done":
                # 模拟 Gradio：拿到 done 后停止消费，不再耗尽生成器。
                break
        second = list(controller.ask("第二问"))
        self.assertFalse(
            any("上一轮仍在生成" in event.text for event in second),
            [event.text for event in second],
        )
        self.assertIs(second[-1].result.stop_reason, StopReason.COMPLETED)

    def test_interrupt_before_runtime_starts_skips_run(self) -> None:
        """模型/运行时准备阶段的中断也必须生效（评审 iss-mwmtt3ab）。"""
        runtime = BlockingRuntime()
        holder: dict[str, AgentChatController] = {}

        def factory(model: Any) -> BlockingRuntime:
            holder["controller"].interrupt()
            return runtime

        controller = AgentChatController(
            model_factory=lambda: FakeModel(["不该被调用"]),
            runtime_factory=factory,
        )
        holder["controller"] = controller
        events = list(controller.ask("有新闻吗？"))
        self.assertEqual(runtime.runs, 0)
        self.assertIn("中断", events[-1].text)
        # 锁已释放，可以立刻再问。
        runtime.release.set()
        again = list(controller.ask("再问一次"))
        self.assertEqual(again[-1].kind, "done")
        self.assertEqual(runtime.runs, 1)

    def test_interrupt_after_finished_run_does_not_poison_next_question(self) -> None:
        """run 已返回后点「中断」不能给缓存 runtime 留下中断标记（iss-mwmvlhso）。"""
        model = FakeModel(
            [
                ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                "第一次回答：1680.00 元。",
                ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                "第二次回答：1680.00 元。",
            ]
        )
        controller = make_controller(model)
        first = list(controller.ask("第一问"))
        self.assertIs(first[-1].result.stop_reason, StopReason.COMPLETED)

        # 空闲时点「中断」：明确告知没有在生成，且不留下任何运行时中断标记。
        self.assertIn("没有正在生成", controller.interrupt())

        second = list(controller.ask("第二问"))
        done = second[-1]
        self.assertIsNotNone(done.result)
        self.assertIs(done.result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(done.result.steps), 1)
        self.assertNotIn("已中断", done.text)
        answer = "".join(event.text for event in second if event.kind == "answer")
        self.assertIn("1680.00", answer)

    def test_stop_output_during_streaming_keeps_text_and_next_question_completes(
        self,
    ) -> None:
        """回答流式输出阶段点「停止输出」：保留已产出内容，下一问仍正常（AC5）。"""
        model = FakeModel(
            [
                ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                "回答内容" * 20 + " 1680.00 元。",
                ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                "第二问 1680.00 元。",
            ]
        )
        controller = make_controller(model)
        stream = controller.ask("第一问")
        events: list[ChatEvent] = []
        stopped = ""
        for event in stream:
            events.append(event)
            if event.kind == "answer":
                stopped = controller.interrupt()
                break
        events.extend(stream)

        self.assertIn("中断输出", stopped)
        done = events[-1]
        self.assertEqual(done.kind, "done")
        self.assertIn("停止输出", done.text)
        self.assertTrue(done.stopped)
        # 已产出的回答分片保留，不因停止输出而清空。
        answer = "".join(event.text for event in events if event.kind == "answer")
        self.assertTrue(answer)

        second = list(controller.ask("第二问"))
        self.assertIs(second[-1].result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(second[-1].result.steps), 1)

    def test_interrupt_while_runtime_ready_does_not_poison_next_question(self) -> None:
        """runtime 已缓存的预运行阶段中断，同样不能污染下一问（路径 B）。"""
        model = FakeModel(
            [
                ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                "预热回答：1680.00 元。",
                ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                "第三问回答：1680.00 元。",
            ]
        )
        calls = {"n": 0}
        holder: dict[str, AgentChatController] = {}

        def context_provider(question: str) -> str:
            calls["n"] += 1
            if calls["n"] == 2:
                holder["controller"].interrupt()
            return ""

        controller = make_controller(model, context_provider=context_provider)
        holder["controller"] = controller

        warm = list(controller.ask("预热"))
        self.assertIs(warm[-1].result.stop_reason, StopReason.COMPLETED)
        # runtime 已缓存；在上下文检索阶段点中断：本轮 0 步、以中断收尾。
        aborted = list(controller.ask("第二问"))
        self.assertIn("中断", aborted[-1].text)
        # 下一问必须恢复正常：不能被上一轮留下的 runtime 中断标记污染。
        third = list(controller.ask("第三问"))
        self.assertIs(third[-1].result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(third[-1].result.steps), 1)
        self.assertIn("1680.00", "".join(e.text for e in third if e.kind == "answer"))

    def test_run_and_step_records_are_persisted(self) -> None:
        """生产入口传了审计库时，run 与每个步骤都要落库（评审 P1-1）。"""
        with _tempdir() as tmp:
            db = Path(tmp) / "audit.db"
            model = FakeModel(
                [
                    ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                    "收盘价 1680.00 元。",
                ]
            )
            controller = AgentChatController(
                model_factory=lambda: model,
                runtime_factory=lambda m: AgentRuntime(
                    m,
                    registry=price_registry(),
                    session=AgentSession(),
                    db_path=db,
                ),
            )
            events = list(controller.ask("600519.SH 收盘价？"))
            done = events[-1]
            self.assertIsNotNone(done.result)
            with sqlite3.connect(db) as conn:
                runs = conn.execute("SELECT status FROM agent_runs").fetchall()
                rows = conn.execute(
                    "SELECT kind, tool FROM agent_steps ORDER BY step"
                ).fetchall()
            self.assertEqual(len(runs), 1)
            self.assertIn(runs[0][0], {"success", "partial"})
            self.assertEqual(len(rows), len(done.result.steps) + 1)
            self.assertEqual(rows[-1][0], "final")


class UiBuilderTests(unittest.TestCase):
    def test_tab_builds_with_upload_streaming_and_stop(self) -> None:
        import gradio as gr

        from astock.agent_chat_ui import build_agent_chat_tab

        controller = make_controller(
            FakeModel(
                [
                    ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                    "收盘价 1680.00 元。",
                ]
            )
        )
        with gr.Blocks():
            components = build_agent_chat_tab(controller=controller)
        self.assertEqual(components["uploader"].file_count, "multiple")
        self.assertIn("附件", components["uploader"].label)
        self.assertIsNotNone(components["stop"])
        self.assertIn("中断", components["stop"].value)

        # 上传 → 解析状态（不启动服务器，直接调接线函数）。
        with _tempdir() as tmp:
            path = Path(tmp) / "notes.txt"
            path.write_text("上海机场 2026 年旅客吞吐量回升。", encoding="utf-8")
            attachments, markdown = components["handle_uploads"]([str(path)])
            self.assertEqual(len(attachments), 1)
            self.assertTrue(attachments[0].ok)
            self.assertIn("✅ notes.txt", markdown)

        # 发送 → 多帧增量输出，最后一帧含最终回答与步骤时间线。
        frames = list(components["respond"]("600519.SH 收盘价？", [], [], []))
        self.assertGreaterEqual(len(frames), 3)
        self.assertIn("正在分析", frames[0][5])
        final = frames[-1]
        self.assertEqual(final[0][-1]["role"], "assistant")
        self.assertIn("1680.00", final[0][-1]["content"])
        self.assertIn("lookup_price", final[2])
        self.assertIn("已完成", final[5])

    def test_stop_output_keeps_partial_answer_in_chat(self) -> None:
        """流式阶段点「停止输出」后，UI 不得再用完整回答覆盖已产出片段。"""
        import gradio as gr

        from astock.agent_chat_ui import build_agent_chat_tab

        full = "回答内容" * 20 + " 1680.00 元。"
        controller = make_controller(
            FakeModel(
                [
                    ToolCall(name="lookup_price", arguments={"code": "600519.SH"}),
                    full,
                ]
            )
        )
        with gr.Blocks():
            components = build_agent_chat_tab(controller=controller)

        frames = []
        interrupted = False
        for frame in components["respond"]("600519.SH 收盘价？", [], [], []):
            frames.append(frame)
            if not interrupted and "正在输出回答" in frame[5]:
                self.assertIn("中断输出", controller.interrupt())
                interrupted = True

        self.assertTrue(interrupted)
        final = frames[-1]
        self.assertIn("停止输出", final[5])
        shown = final[0][-1]["content"]
        self.assertTrue(shown)
        self.assertLess(len(shown), len(full))

    def test_db_path_reaches_controller(self) -> None:
        import gradio as gr

        from astock.agent_chat_ui import build_agent_chat_tab

        with _tempdir() as tmp:
            db = Path(tmp) / "ui.db"
            with gr.Blocks():
                components = build_agent_chat_tab(db_path=db)
        self.assertEqual(components["controller"].db_path, db)

    def test_table_chart_figure_and_exports_reach_ui(self) -> None:
        """table_chart 的 figure/spec 必须显示，HTML/PNG 必须出现在下载区。"""
        import gradio as gr

        from astock.agent_chat_ui import build_agent_chat_tab

        with _tempdir() as tmp:
            png = Path(tmp) / "art_ui_table.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            export_root = Path(tmp) / "exports"
            result = ToolResult(
                text="已生成柱状图",
                figure=(
                    '<html><body><div id="plotly-ui-table">'
                    "月度销售</div></body></html>"
                ),
                source="file:月度销售.xlsx",
                data_as_of="2026-03-31",
                data_time="2026-03-31 00:00",
                metadata={
                    "title": "月度销售",
                    "chart_spec": {"version": 1, "kind": "bar"},
                    "artifact_id": "art_ui_table",
                    "artifact_path": "artifacts/art_ui_table.html",
                    "png_path": str(png),
                    "warnings": [],
                },
            )
            step = step_with(result, tool="table_chart")

            class TableController:
                def model_banner(self) -> str:
                    return "离线桩"

                def ask(self, text: str, attachments: Any = (), files: Any = ()):
                    yield ChatEvent(kind="step", step=step, text="")
                    yield ChatEvent(kind="answer", text="已生成图表。")
                    yield ChatEvent(
                        kind="done",
                        text="已完成。",
                        result=AgentRunResult(
                            answer="已生成图表。",
                            stop_reason=StopReason.COMPLETED,
                            steps=[step],
                            tokens_used=1,
                            elapsed_seconds=0.1,
                            session_id="s",
                        ),
                    )

                def interrupt(self) -> str:
                    return "已中断"

                def new_session(self) -> None:
                    return None

            with mock.patch.dict(
                os.environ, {"ASTOCK_AGENT_EXPORT_DIR": str(export_root)}
            ):
                with gr.Blocks():
                    components = build_agent_chat_tab(controller=TableController())
                frames = list(components["respond"]("画月度销售", [], [], []))
            self.assertTrue((export_root / "art_ui_table.html").is_file())
            self.assertTrue((export_root / "art_ui_table.png").is_file())
        final = frames[-1]
        self.assertIn("plotly-ui-table", final[3])
        self.assertIn("查看图表 spec / 来源", final[3])
        self.assertIn("PNG：art_ui_table.png", final[3])
        self.assertEqual(len(final), 7)
        exported = final[6]
        self.assertTrue(any(str(item).endswith("art_ui_table.html") for item in exported))
        self.assertTrue(any(str(item).endswith("art_ui_table.png") for item in exported))
        self.assertEqual(components["export_files"].file_count, "multiple")

    def test_missing_png_status_does_not_double_parentheses(self) -> None:
        """PNG 缺失说明只带一对括号，不能被状态栏再包一层。"""
        import gradio as gr

        from astock.agent_chat_ui import build_agent_chat_tab

        step = FigureTests()._table_chart_step("")

        class MissingPngController:
            def model_banner(self) -> str:
                return "离线桩"

            def ask(self, text: str, attachments: Any = (), files: Any = ()):
                yield ChatEvent(kind="step", step=step, text="")
                yield ChatEvent(kind="answer", text="已生成图表。")
                yield ChatEvent(
                    kind="done",
                    text="已完成。",
                    result=AgentRunResult(
                        answer="已生成图表。",
                        stop_reason=StopReason.COMPLETED,
                        steps=[step],
                        tokens_used=1,
                        elapsed_seconds=0.1,
                        session_id="s",
                    ),
                )

            def interrupt(self) -> str:
                return "已中断"

            def new_session(self) -> None:
                return None

        with _tempdir() as tmp:
            export_root = Path(tmp) / "exports"
            with mock.patch.dict(
                os.environ, {"ASTOCK_AGENT_EXPORT_DIR": str(export_root)}
            ):
                with gr.Blocks():
                    components = build_agent_chat_tab(controller=MissingPngController())
                final = list(components["respond"]("画月度销售", [], [], []))[-1]

        self.assertIn("未生成 PNG", final[5])
        self.assertEqual(final[5].count("（"), 1)
        self.assertEqual(final[5].count("）"), 1)

    def test_kline_without_png_is_not_reported_as_export_degradation(self) -> None:
        """K 线不请求 PNG；状态栏/图注不能误报 kaleido/Chrome 降级。"""
        import gradio as gr

        from astock.agent_chat_ui import build_agent_chat_tab

        with _tempdir() as tmp:
            export_root = Path(tmp) / "exports"
            result = ToolResult(
                text="K 线已生成",
                figure='<html><body><div id="plotly-kline">K</div></body></html>',
                source="market://kline",
                data_as_of="2026-11-18",
                data_time="2026-11-18 15:00",
                metadata={
                    "artifact_id": "art_kline",
                    "artifact_path": "artifacts/art_kline.html",
                    "chart": {
                        "title": "600519.SH 日线",
                        "spec": {"version": 1, "kind": "line"},
                    },
                },
            )
            step = step_with(result, tool="kline")

            class KlineController:
                def model_banner(self) -> str:
                    return "离线桩"

                def ask(self, text: str, attachments: Any = (), files: Any = ()):
                    yield ChatEvent(kind="step", step=step, text="")
                    yield ChatEvent(kind="answer", text="K 线已生成。")
                    yield ChatEvent(
                        kind="done",
                        text="已完成。",
                        result=AgentRunResult(
                            answer="K 线已生成。",
                            stop_reason=StopReason.COMPLETED,
                            steps=[step],
                            tokens_used=1,
                            elapsed_seconds=0.1,
                            session_id="s",
                        ),
                    )

                def interrupt(self) -> str:
                    return "已中断"

                def new_session(self) -> None:
                    return None

            with mock.patch.dict(
                os.environ, {"ASTOCK_AGENT_EXPORT_DIR": str(export_root)}
            ):
                with gr.Blocks():
                    components = build_agent_chat_tab(controller=KlineController())
                final = list(components["respond"]("画 K 线", [], [], []))[-1]

        self.assertIn("PNG：不适用（该工具不导出 PNG）", final[3])
        self.assertNotIn("PNG：未生成", final[3])
        self.assertNotIn("kaleido", final[5])
        self.assertNotIn("Chrome", final[5])
        self.assertNotIn("未生成 PNG", final[5])
        self.assertTrue(any(str(item).endswith("art_kline.html") for item in final[6]))
        self.assertFalse(any(str(item).endswith(".png") for item in final[6]))

    def test_export_dir_is_configured_for_gradio_downloads(self) -> None:
        """导出目录可配置，且必须落在 Gradio allowed_paths 内，否则下载 403。"""
        from gradio import utils as gradio_utils

        from astock.agent_chat_ui import (
            agent_export_allowed_paths,
            agent_export_dir,
        )

        with _tempdir() as tmp:
            export_root = Path(tmp) / "exports"
            with mock.patch.dict(
                os.environ, {"ASTOCK_AGENT_EXPORT_DIR": str(export_root)}
            ):
                self.assertEqual(agent_export_dir(), export_root)
                allowed_paths = agent_export_allowed_paths()
                self.assertEqual(allowed_paths, [str(export_root)])
                allowed, reason = gradio_utils.is_allowed_file(
                    export_root / "art_table.html",
                    blocked_paths=[],
                    allowed_paths=allowed_paths,
                    created_paths=[],
                )
        self.assertTrue(allowed)
        self.assertEqual(reason, "allowed")
        # app.py 的 launch() 在 __main__ 下执行，单测无法启动服务；用静态断言锁住接线。
        app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("allowed_paths=agent_export_allowed_paths()", app_source)

    def test_gradio_serves_exported_html_and_png(self) -> None:
        """启动真实 Gradio 服务，验证导出文件可通过 /file= 下载（AC-2 运行证据）。"""
        import gradio as gr

        from astock.agent_chat_ui import (
            agent_export_allowed_paths,
            agent_export_dir,
        )

        with _tempdir() as tmp:
            png = Path(tmp) / "art_dl.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            export_root = Path(tmp) / "exports"
            figure = Figure(
                step=1,
                tool="table_chart",
                html='<html><body><div id="plotly-download">下载</div></body></html>',
                artifact_id="art_dl",
                png_path=str(png),
                png_requested=True,
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            with mock.patch.dict(
                os.environ, {"ASTOCK_AGENT_EXPORT_DIR": str(export_root)}
            ):
                paths, notes = export_figures([figure], out_dir=agent_export_dir())
                self.assertEqual(notes, [])
                demo = gr.Blocks()
                with demo:
                    gr.File(value=paths, file_count="multiple", interactive=False)
                demo.launch(
                    server_name="127.0.0.1",
                    server_port=port,
                    prevent_thread_lock=True,
                    allowed_paths=agent_export_allowed_paths(),
                    quiet=True,
                )
                self.addCleanup(demo.close)
                self.assertEqual(len(paths), 2)
                for path in paths:
                    url = f"{demo.local_api_url}file={urlparse.quote(path)}"
                    with urlrequest.urlopen(url, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                        content_type = response.headers.get_content_type()
                        if path.endswith(".html"):
                            self.assertEqual(content_type, "text/html")
                            self.assertIn("plotly-download", response.read().decode())
                        else:
                            self.assertEqual(content_type, "image/png")
                            self.assertTrue(response.read().startswith(b"\x89PNG"))

    def test_structured_history_is_not_stringified_as_repr(self) -> None:
        """Gradio 6 回传的 text block 列表必须还原成纯文本，不能显示成 dict 的 repr。"""
        import gradio as gr

        from astock.agent_chat_ui import build_agent_chat_tab

        controller = make_controller(FakeModel(["补充回答"]))
        with gr.Blocks():
            components = build_agent_chat_tab(controller=controller)
        history = [
            {"role": "user", "content": [{"text": "上一问", "type": "text"}]},
            {"role": "assistant", "content": [{"text": "上一答", "type": "text"}]},
        ]
        frames = list(components["respond"]("追问", history, [], []))
        rendered = frames[0][0]
        self.assertEqual(rendered[0], {"role": "user", "content": "上一问"})
        self.assertEqual(rendered[1], {"role": "assistant", "content": "上一答"})
        self.assertEqual(rendered[2], {"role": "user", "content": "追问"})

    def test_citation_html_rendered_once_per_round(self) -> None:
        """引用面板只在引用集合变化时重算，不随回答分片反复读盘（评审 P2）。"""
        import gradio as gr

        import astock.agent_chat_ui as chat_ui

        with _tempdir() as tmp:
            root = Path(tmp) / "files"
            path = Path(tmp) / "年报.csv"
            path.write_text("项目,金额\n营收,100\n", encoding="utf-8")
            registry = ToolRegistry()

            def handler(**_kwargs: Any) -> ToolResult:
                return ToolResult(
                    text="营收 100",
                    metadata={"citations": ["《年报.csv》工作表「Sheet1」"]},
                )

            registry.register(
                ToolSpec(
                    name="read_file",
                    description="读取文件",
                    parameters=SCHEMA,
                    handler=handler,
                    permissions=(Permission.FILE_READ,),
                )
            )
            model = FakeModel(
                [
                    ToolCall(name="read_file", arguments={"code": "600519.SH"}),
                    "营收 100（见《年报.csv》工作表「Sheet1」）。",
                ]
            )
            controller = AgentChatController(
                model_factory=lambda: model,
                runtime_factory=lambda m: AgentRuntime(
                    m, registry=registry, session=AgentSession(), auto_save=False
                ),
                root=root,
                chunk_chars=8,
            )
            real = chat_ui.render_citations_html
            counts: list[int] = []

            def counting(citations: Any, **kwargs: Any) -> str:
                counts.append(len(citations))
                return real(citations, **kwargs)

            with gr.Blocks():
                components = chat_ui.build_agent_chat_tab(controller=controller)
            with mock.patch.object(chat_ui, "render_citations_html", counting):
                frames = list(
                    components["respond"]("营收如何？", [], [], [str(path)])
                )
        self.assertEqual([count for count in counts if count], [1])
        self.assertIn("cite-1", frames[-1][4])
        self.assertIn("年报.csv", frames[-1][4])
