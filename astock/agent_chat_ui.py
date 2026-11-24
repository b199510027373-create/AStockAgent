"""「AI 对话」Tab 的 Gradio 装配（ADR-013 / ADR-016）。

只做组件与事件连线，业务逻辑都在 :mod:`astock.agent.chat`（契约 ARCH-01：
``*_ui`` 只装配不实现指标/回测/统计）。

交互要点：

- 上传 1–5 个文件，上传即解析并显示状态；超限/扫描件/加密文件按行给出原因；
- 发送后先显示「正在分析」，每个工具步骤完成后时间线增量刷新；
- 最终回答按标点切片增量写入 Chatbot（模型层仍是一次性返回，见 chat.py 说明）；
- 图表以 ``iframe srcdoc`` 内嵌在回答区，附带可展开的 ChartSpec/来源；
- 引用以「《文件名》页码/工作表」列出，点击跳转到带正文摘录的详情卡；
- 中断按钮取消发送事件并调用运行时协作式中断，已产出内容留在页面上。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import gradio as gr

from astock.agent.chat import (
    AgentChatController,
    Attachment,
    Citation,
    Figure,
    attachment_summary,
    collect_citations,
    collect_figures,
    export_figures,
    ingest_attachments,
    render_citations_html,
    render_figures_html,
    render_steps_html,
)
from astock.agent.types import AgentRunResult, StepRecord

__all__ = [
    "AGENT_EXPORT_DIR",
    "agent_export_allowed_paths",
    "agent_export_dir",
    "build_agent_chat_tab",
]

#: Chatbot 初始值（Gradio 6 的 messages 格式；键名固定为 role/content）。
_EMPTY_HISTORY: list[Any] = []

_FILE_TYPES = [".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md"]

_EMPTY_FIGURES = render_figures_html([])
_EMPTY_CITATIONS = render_citations_html([])
#: 图表导出目录：只放本轮可下载的 HTML/PNG；同一 artifact 重绘时覆盖同名文件。
#: 可用 ``ASTOCK_AGENT_EXPORT_DIR`` 覆盖（测试/部署隔离），默认在系统临时目录。
_DEFAULT_EXPORT_DIR = Path(tempfile.gettempdir()) / "astock-agent-exports"
AGENT_EXPORT_DIR = _DEFAULT_EXPORT_DIR


def agent_export_dir() -> Path:
    """返回当前图表导出目录；环境变量在每次调用时生效，便于测试隔离。"""
    override = os.environ.get("ASTOCK_AGENT_EXPORT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_EXPORT_DIR


def agent_export_allowed_paths() -> list[str]:
    """返回启动 Gradio 时必须加入 ``allowed_paths`` 的导出目录。

    Gradio 的 ``/file=`` 端点只服务上传目录、缓存目录和显式允许的路径；
    不配置时浏览器下载导出文件会得到 403。
    """
    return [str(agent_export_dir())]


def _paths(value: Any) -> list[str]:
    """兼容 ``gr.File`` 返回 str 路径 / FileData 对象 / None。"""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    paths: list[str] = []
    for item in items:
        if isinstance(item, (str, Path)):
            paths.append(str(item))
            continue
        path = getattr(item, "name", None) or getattr(item, "path", None)
        if path:
            paths.append(str(path))
    return paths


def _content_text(content: Any) -> str:
    """Gradio 6 回传的 content 可能是 str，也可能是 text block 列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text:
                    parts.append(str(text))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content or "")


def _normalise_history(value: Any) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        history.append({"role": role, "content": _content_text(item.get("content"))})
    return history


def build_agent_chat_tab(
    *,
    controller: AgentChatController | None = None,
    context_provider: Callable[[str], str] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """装配 AI 对话 Tab，返回关键组件（供 app.py 与其他 Tab 联动/测试）。

    ``db_path`` 透传到 ``AgentChatController``：生产入口必须传
    ``astock.agent.store.DEFAULT_DB_PATH``，否则本轮的 run / 工具步骤不会落
    ``agent_runs`` / ``agent_steps``（AGENT-02 / ADR-017）。
    """
    controller = controller or AgentChatController(
        context_provider=context_provider, db_path=db_path
    )

    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(
                value=list(_EMPTY_HISTORY),
                label="对话",
                height=520,
                render_markdown=True,
            )
            figures_html = gr.HTML(value=_EMPTY_FIGURES)
            export_files = gr.File(
                label="图表导出（HTML / PNG，可多选下载）",
                file_count="multiple",
                interactive=False,
            )
            with gr.Row():
                message = gr.Textbox(
                    label="问题",
                    placeholder="如：上传的财报里营收增速如何？顺便画一下 600519.SH 的 K 线",
                    scale=4,
                    lines=2,
                )
                send = gr.Button("发送", variant="primary", scale=1)
                stop = gr.Button("⏹ 中断", variant="stop", scale=1)
            status = gr.Markdown(value="就绪。")
            clear = gr.Button("清空对话")

        with gr.Column(scale=2):
            model_status = gr.Markdown(value=controller.model_banner())
            uploader = gr.File(
                label="附件（1–5 个，单文件 ≤ 20 MiB）",
                file_count="multiple",
                file_types=_FILE_TYPES,
                type="filepath",
            )
            upload_status = gr.Markdown(value=attachment_summary([]))
            steps_html = gr.HTML(value=render_steps_html([]))
            citations_html = gr.HTML(value=_EMPTY_CITATIONS)

    attachments_state = gr.State(value=[])

    def handle_uploads(files: Any) -> tuple[list[Attachment], str]:
        paths = _paths(files)
        if not paths:
            return [], attachment_summary([])
        attachments = ingest_attachments(paths)
        return attachments, attachment_summary(attachments)

    def respond(
        message: Any,
        history: Any,
        attachments: Sequence[Attachment] | None,
        files: Any,
    ) -> Iterator[tuple[Any, ...]]:
        chat = _normalise_history(history)
        text = str(message or "").strip()
        if not text:
            yield (
                chat,
                "",
                gr.skip(),
                gr.skip(),
                gr.skip(),
                "请先输入问题。",
                gr.skip(),
            )
            return
        chat.append({"role": "user", "content": text})
        chat.append({"role": "assistant", "content": ""})
        steps: list[StepRecord] = []
        figures: list[Figure] = []
        exports_list: list[str] = []
        citations: list[Citation] = []
        # 引用定位要读盘/扫目录，只在引用集合变化时重算（评审 iss-mwmtt39d）。
        cite_key: tuple[tuple[Any, ...], ...] | None = None
        cite_html = _EMPTY_CITATIONS
        cite_memo: dict[tuple[Any, ...], Any] = {}

        def citations_block() -> str:
            nonlocal cite_key, cite_html
            key = tuple(
                (item.label, item.page, item.sheet, item.file_id) for item in citations
            )
            if key != cite_key:
                cite_key = key
                cite_html = render_citations_html(citations, memo=cite_memo)
            return cite_html

        yield (
            chat,
            "",
            render_steps_html([], pending="正在分析问题…"),
            render_figures_html([]),
            citations_block(),
            "正在分析问题…",
            [],
        )
        try:
            for event in controller.ask(
                text, attachments=list(attachments or ()), files=_paths(files)
            ):
                if event.kind == "step" and event.step is not None:
                    steps.append(event.step)
                    figures = collect_figures(steps)
                    citations = collect_citations(steps, "")
                    timeline = render_steps_html(steps, pending="继续分析…")
                    status_text = f"已调用 {event.step.tool}，继续分析…"
                elif event.kind == "answer":
                    chat[-1]["content"] += event.text
                    timeline = render_steps_html(steps, pending="正在输出回答…")
                    status_text = "正在输出回答…"
                elif event.kind == "status":
                    timeline = render_steps_html(steps, pending=event.text)
                    status_text = event.text
                elif event.kind == "done":
                    result: AgentRunResult | None = event.result
                    if result is not None:
                        figures = collect_figures(result.steps)
                        citations = collect_citations(result.steps, result.answer)
                        if result.answer and not event.stopped:
                            # 流式阶段点过「停止输出」时，界面保留已产出的片段，
                            # 不用完整回答覆盖，避免与状态栏文案矛盾（iss-mwn7nzfl）。
                            chat[-1]["content"] = result.answer
                    timeline = render_steps_html(steps)
                    status_text = event.text
                    # 收尾时把本轮图表落成可下载文件；PNG 缺失只记降级说明，
                    # 不让整轮回答失败（接口冻结 v2 §3.5 / ADR-016）。
                    exports_list, export_notes = export_figures(
                        figures, out_dir=agent_export_dir()
                    )
                    if export_notes:
                        note = "；".join(export_notes)
                        status_text = (
                            f"{status_text}；{note}" if status_text else note
                        )
                else:  # pragma: no cover - 控制器只产出上述四类
                    continue
                yield (
                    chat,
                    "",
                    timeline,
                    render_figures_html(figures),
                    citations_block(),
                    status_text,
                    exports_list,
                )
        except Exception as exc:  # noqa: BLE001 - 页面不能因为一轮对话而白屏
            yield (
                chat,
                "",
                render_steps_html(steps),
                render_figures_html(figures),
                citations_block(),
                f"⚠️ 本轮出错：{type(exc).__name__}: {exc}",
                exports_list,
            )

    outputs = [
        chatbot,
        message,
        steps_html,
        figures_html,
        citations_html,
        status,
        export_files,
    ]

    send.click(respond, [message, chatbot, attachments_state, uploader], outputs)
    message.submit(respond, [message, chatbot, attachments_state, uploader], outputs)
    stop.click(
        lambda: controller.interrupt(),
        None,
        [status],
        # 不走 cancels：取消生成器会丢掉「已产出内容保留」的半截回答，
        # 也会让 runtime 的协作式中断来不及回填 partial answer。
        queue=False,
    )
    uploader.change(handle_uploads, [uploader], [attachments_state, upload_status])
    clear.click(
        lambda: (
            [],
            "",
            render_steps_html([]),
            render_figures_html([]),
            render_citations_html([]),
            "已清空对话与上下文。",
            [],
        ),
        None,
        outputs,
        queue=False,
    ).then(lambda: controller.new_session(), None, None, queue=False)

    return {
        "controller": controller,
        "chatbot": chatbot,
        "message": message,
        "send": send,
        "stop": stop,
        "status": status,
        "clear": clear,
        "model_status": model_status,
        "uploader": uploader,
        "upload_status": upload_status,
        "attachments_state": attachments_state,
        "steps_html": steps_html,
        "figures_html": figures_html,
        "citations_html": citations_html,
        "export_files": export_files,
        "respond": respond,
        "handle_uploads": handle_uploads,
    }
