"""确定性 Gradio 夹具：装配真实 ``build_agent_chat_tab``，供浏览器冒烟驱动。

为什么单独做一个 app 而不起正式入口 ``app.py``（ADR-001 本地单用户）：

- 不碰真实模型/网络：用 :class:`FakeModel` 回放固定的工具调用与回答，断言才可复现；
- 数据根目录全部落在临时工作目录（``--workdir`` / ``AGENT_SMOKE_WORKDIR``），
  解析产物、审计库、Gradio 临时文件都不写进仓库；
- 两条场景：

  ``interactive``（默认）：3 步 ``read_file`` + 图表 + 两条引用 + 可中断的长回答；
  ``degraded``：不给模型工厂，页面必须仍能打开并显示「模型未就绪」横幅。

本文件只装配产品代码，不实现业务逻辑（契约 ARCH-01）。
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import gradio as gr

from astock.agent import (
    AgentRuntime,
    AgentSession,
    FakeModel,
    Permission,
    ToolCall,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from astock.agent.chat import AgentChatController
from astock.agent_chat_ui import build_agent_chat_tab

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
DEFAULT_WORKDIR = HERE / ".workdir"

DATA_AS_OF = "2025-12-31"
DATA_TIME = "2026-03-20"
CHART_TITLE = "营收趋势"
CHART_MARKER = "smoke-chart"
FIGURE_CAPTION = f"{CHART_TITLE}｜数据截至 {DATA_AS_OF}｜数据时间 {DATA_TIME}"
FIRST_CITATION = "《年报.pdf》第 2 页"
SECOND_CITATION = "《利润表.xlsx》工作表「利润表」C2"

FIGURE_HTML = (
    "<html><body style='margin:0'>"
    f"<div id='{CHART_MARKER}' style='font:14px sans-serif;padding:12px'>"
    "图表已渲染 · 600519.SH 收盘价"
    "</div></body></html>"
)

ANSWER_UNIT = (
    "营收 100 亿元，同比 +8%（数据截至 2025-12-31，数据时间 2026-03-20，"
    "见《年报.pdf》第 2 页与《利润表.xlsx》工作表「利润表」）。"
    "毛利率保持稳定，经营现金流同比改善。"
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"q": {"type": "string", "minLength": 1}},
    "required": ["q"],
    "additionalProperties": False,
}


def make_registry(delay: float) -> ToolRegistry:
    """固定工具：每次返回同一份「文件摘要 + 图表 + 引用」，步骤数可预期。"""
    registry = ToolRegistry()

    def read_file(*, q: str) -> ToolResult:
        time.sleep(delay)
        return ToolResult(
            text=f"文件摘要（{q}）：营收 100 亿元",
            figure=FIGURE_HTML,
            data_as_of=DATA_AS_OF,
            data_time=DATA_TIME,
            source="年报.pdf 第 2 页",
            metadata={
                "citations": [FIRST_CITATION, SECOND_CITATION],
                "chart": {
                    "title": CHART_TITLE,
                    "spec": {
                        "version": 1,
                        "kind": "bar",
                        "data": {"values": [100]},
                        "encode": {"x": "name", "y": "value"},
                        "transforms": [],
                        "layers": [],
                        "annotations": [],
                        "theme": "light",
                    },
                    "data_as_of": DATA_AS_OF,
                    "data_time": DATA_TIME,
                    "source": "年报.pdf 第 2 页",
                    "artifact_id": "art-smoke",
                    "artifact_path": "data/agent/charts/art-smoke.html",
                },
            },
        )

    registry.register(
        ToolSpec(
            name="read_file",
            description="读取附件并汇总",
            parameters=SCHEMA,
            handler=read_file,
            permissions=(Permission.FILE_READ,),
        )
    )
    return registry


def make_script(answer_repeat: int) -> list[Any]:
    """三轮脚本：3 次工具调用 + 一段长回答，重复多轮供「中断后再问」使用。"""
    answer = ANSWER_UNIT * max(1, answer_repeat)
    return (
        [
            ToolCall(name="read_file", arguments={"q": "营收"}),
            ToolCall(name="read_file", arguments={"q": "净利"}),
            ToolCall(name="read_file", arguments={"q": "现金流"}),
            answer,
        ]
        * 40
    )


class ThrottledController:
    """给事件流按片加延时的装饰器：让「正在输出回答」在浏览器里停留数秒。

    只影响夹具的投递节奏（模拟慢网络），不改变产品代码的任何行为。
    """

    def __init__(self, inner: AgentChatController, *, per_chunk: float) -> None:
        self._inner = inner
        self._per_chunk = per_chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def ask(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
        for event in self._inner.ask(*args, **kwargs):
            if event.kind in {"answer", "step"}:
                time.sleep(self._per_chunk)
            yield event


def _unavailable_model() -> Any:
    """降级场景专用：让模型构造必然失败，横幅才能稳定显示「模型未就绪」。"""
    raise RuntimeError("smoke 夹具：降级场景未配置模型")


def build_controller(workdir: Path, *, delay: float, chunk_delay: float,
                     chunk_chars: int, answer_repeat: int) -> Any:
    files_root = workdir / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    # files.py 的 ``_files_root(None)`` 在调用时读这个变量，所以运行期设置即可。
    os.environ["AGENT_FILES_DIR"] = str(files_root)
    model = FakeModel(make_script(answer_repeat))
    controller = AgentChatController(
        model_factory=lambda: model,
        runtime_factory=lambda m: AgentRuntime(
            m,
            registry=make_registry(delay),
            session=AgentSession(),
            db_path=workdir / "smoke.db",
        ),
        root=files_root,
        db_path=workdir / "smoke.db",
        chunk_chars=chunk_chars,
    )
    if chunk_delay > 0:
        return ThrottledController(controller, per_chunk=chunk_delay)
    return controller


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 会话 UI 冒烟夹具")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENT_SMOKE_PORT", "7866")),
        help="监听端口（默认取 AGENT_SMOKE_PORT，否则 7866）",
    )
    parser.add_argument(
        "--scenario",
        choices=("interactive", "degraded"),
        default=os.environ.get("AGENT_SMOKE_SCENARIO", "interactive"),
        help="interactive=完整夹具；degraded=无模型，验证降级横幅",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path(os.environ.get("AGENT_SMOKE_WORKDIR", str(DEFAULT_WORKDIR))),
        help="解析产物/审计库/临时文件根目录（默认 AGENT_SMOKE_WORKDIR 或 ./.workdir）",
    )
    parser.add_argument("--step-delay", type=float, default=0.2, help="每次工具调用耗时")
    parser.add_argument(
        "--chunk-delay", type=float, default=0.02, help="每个流式片段额外延时（0 关闭）"
    )
    parser.add_argument("--chunk-chars", type=int, default=8, help="回答切片粒度")
    parser.add_argument("--answer-repeat", type=int, default=12, help="长回答重复次数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    workdir: Path = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    # Gradio 把上传/缓存写进 GRADIO_TEMP_DIR；指到工作目录，避免污染系统临时目录与仓库。
    os.environ.setdefault("GRADIO_TEMP_DIR", str(workdir / "gradio"))
    host = "127.0.0.1"
    url = f"http://{host}:{args.port}"

    if args.scenario == "degraded":
        # 不能只靠「没传 model_factory」：如果本机环境有可用 key，控制器会自动就绪。
        controller: Any = AgentChatController(model_factory=_unavailable_model)
    else:
        controller = build_controller(
            workdir,
            delay=args.step_delay,
            chunk_delay=args.chunk_delay,
            chunk_chars=args.chunk_chars,
            answer_repeat=args.answer_repeat,
        )

    with gr.Blocks(title=f"agent-chat-smoke-{args.scenario}") as demo:
        build_agent_chat_tab(controller=controller)
    demo.queue().launch(
        server_name=host,
        server_port=args.port,
        prevent_thread_lock=True,
        quiet=True,
    )
    print(
        f"READY url={url} scenario={args.scenario} workdir={workdir} "
        f"fixtures={FIXTURES}",
        flush=True,
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        demo.close()


if __name__ == "__main__":
    main()
