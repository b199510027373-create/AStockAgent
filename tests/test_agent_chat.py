"""AI 对话 Tab 的离线测试（ADR-013 / ADR-016 验收）。

覆盖：附件 1–5 个解析状态、超限拒绝、流式切片、工具步骤时间线、图表内嵌
（iframe srcdoc + spec/来源）、引用收集与按文件名/工作表定位、协作式中断保留
已产出内容，以及 Gradio 装配的冒烟检查（不启动服务器）。
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
    attachment_note,
    attachment_summary,
    collect_citations,
    collect_figures,
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
from astock.agent.types import StepRecord


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
                    "warnings": ["PNG 导出失败"],
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
        self.assertIn("PNG 导出失败", markup)
        self.assertIn("数据截至 2026-11-18", markup)

    def test_missing_time_points_write_unknown(self) -> None:
        result = ToolResult(figure="<html></html>", source="market://x")
        figure = collect_figures([step_with(result, tool="kline")])[0]
        self.assertIn("数据截至 时点未知", figure.caption)
        self.assertIn("数据时间 时点未知", figure.caption)
        self.assertIn("本轮回答还没有图表", render_figures_html([]))


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
