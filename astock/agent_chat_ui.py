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

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import gradio as gr

from astock.agent.chat import (
    AgentChatController,
    Attachment,
    Citation,
    attachment_summary,
    collect_citations,
    collect_figures,
    ingest_attachments,
    render_citations_html,
    render_figures_html,
    render_steps_html,
)
from astock.agent.types import AgentRunResult, StepRecord

__all__ = ["build_agent_chat_tab"]

#: Chatbot 初始值（Gradio 6 的 messages 格式；键名固定为 role/content）。
_EMPTY_HISTORY: list[Any] = []

_FILE_TYPES = [".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md"]

_EMPTY_FIGURES = render_figures_html([])
_EMPTY_CITATIONS = render_citations_html([])


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


def _normalise_history(value: Any) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = item.get("content")
        history.append({"role": role, "content": str(content or "")})
    return history


def build_agent_chat_tab(
    *,
    controller: AgentChatController | None = None,
    context_provider: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """装配 AI 对话 Tab，返回关键组件（供 app.py 与其他 Tab 联动/测试）。"""
    controller = controller or AgentChatController(context_provider=context_provider)

    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(
                value=list(_EMPTY_HISTORY),
                label="对话",
                height=520,
                render_markdown=True,
            )
            figures_html = gr.HTML(value=_EMPTY_FIGURES)
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
            yield chat, "", gr.skip(), gr.skip(), gr.skip(), "请先输入问题。"
            return
        chat.append({"role": "user", "content": text})
        chat.append({"role": "assistant", "content": ""})
        steps: list[StepRecord] = []
        figures: list[Any] = []
        citations: list[Citation] = []
        yield (
            chat,
            "",
            render_steps_html([], pending="正在分析问题…"),
            render_figures_html([]),
            render_citations_html([]),
            "正在分析问题…",
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
                        if result.answer:
                            chat[-1]["content"] = result.answer
                    timeline = render_steps_html(steps)
                    status_text = event.text
                else:  # pragma: no cover - 控制器只产出上述四类
                    continue
                yield (
                    chat,
                    "",
                    timeline,
                    render_figures_html(figures),
                    render_citations_html(citations),
                    status_text,
                )
        except Exception as exc:  # noqa: BLE001 - 页面不能因为一轮对话而白屏
            yield (
                chat,
                "",
                render_steps_html(steps),
                render_figures_html(figures),
                render_citations_html(citations),
                f"⚠️ 本轮出错：{type(exc).__name__}: {exc}",
            )

    outputs = [chatbot, message, steps_html, figures_html, citations_html, status]

    send_event = send.click(respond, [message, chatbot, attachments_state, uploader], outputs)
    submit_event = message.submit(
        respond, [message, chatbot, attachments_state, uploader], outputs
    )
    stop.click(
        lambda: controller.interrupt(),
        None,
        [status],
        queue=False,
        cancels=[send_event, submit_event],
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
        "respond": respond,
        "handle_uploads": handle_uploads,
    }
