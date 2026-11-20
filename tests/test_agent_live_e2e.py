"""真实模型端到端验证（plan-20261119-002 · AC1–AC6）。

这不是单元测试：它用**真实** DeepSeek / OpenAI-compatible 网关跑完整三轮会话，
覆盖流式分片、工具步骤、引用定位、内嵌图、生成中停止、停止后下一问、以及
首字延迟 / 总耗时 / token 统计。

安全与成本：
- 默认**跳过**。只有同时满足「配置了 API key」和 ``AGENT_LIVE_E2E=1`` 才真正
  请求模型，日常 ``pytest`` 不会消耗额度；没有 key 时 pytest.skip 并说明
  「未运行、不能视为已验证」。
- 密钥只从进程环境变量读取（``.env`` 由 ``astock.config`` 载入，且已 gitignore）；
  测试不打印密钥、不把密钥写进报告。
- 报告写入 ``.tmp-tests/live-e2e/<时间戳>/report.json``（``.tmp-tests/`` 已
  gitignore），包含每轮的首字延迟、总耗时、token、引用定位与图表信息。

运行方式（PowerShell）::

    $env:AGENT_LIVE_E2E = "1"
    .venv\\Scripts\\python.exe -m pytest tests\\test_agent_live_e2e.py -m live -s

模型配置沿用生产入口的环境变量：``AGENT_PROVIDER``（deepseek / openai_compatible）、
``AGENT_MODEL``、``AGENT_BASE_URL``、``AGENT_API_KEY``；DeepSeek 下回退
``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE``，兼容网关下回退 ``OPENAI_API_KEY``。
可选 ``AGENT_PRICE_INPUT_PER_M`` / ``AGENT_PRICE_OUTPUT_PER_M``（每百万 token
价格）与 ``AGENT_PRICE_CURRENCY``，配置后报告里附带费用估算。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import astock.config  # noqa: F401 - 导入即加载 .env（与生产入口一致）
from astock.agent import files as file_store
from astock.agent.chat import (
    AgentChatController,
    Attachment,
    collect_citations,
    collect_figures,
    render_citations_html,
    render_figures_html,
    render_steps_html,
    resolve_citation,
)
from astock.agent.gateway import (
    AgentModelAdapter,
    GatewaySettings,
    UsageLogger,
    build_gateway,
)
from astock.agent.runtime import AgentRuntime
from astock.agent.session import AgentSession
from astock.agent.tools.file_tools import build_file_registry
from astock.agent.tools.market_tools import register_market_tools
from astock.agent.types import AgentRunResult, StepRecord, StopReason

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PDF = REPO_ROOT / "tests" / "smoke" / "agent_ui" / "fixtures" / "年报.pdf"
DEFAULT_WORKDIR = REPO_ROOT / ".tmp-tests" / "live-e2e"
CHUNK_CHARS = 40


class LiveSkip(RuntimeError):
    """真实模型用例缺少运行条件（key / opt-in / 网关配置）。"""


@dataclass(frozen=True)
class LiveConfig:
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float = 120.0
    max_steps: int = 8
    token_budget: int = 64_000


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def live_config(
    env: dict[str, str] | None = None, *, require_opt_in: bool = True
) -> LiveConfig:
    """解析真实模型配置；不满足条件时抛 :class:`LiveSkip`（绝不静默降级）。"""

    source = dict(os.environ if env is None else env)
    if require_opt_in and not _enabled(source.get("AGENT_LIVE_E2E")):
        hint = ""
        if source.get("AGENT_API_KEY") or source.get("DEEPSEEK_API_KEY"):
            hint = "（已检测到 API key，但真实模型 E2E 默认不跑）"
        raise LiveSkip(
            "未启用真实模型 E2E：请设置 AGENT_LIVE_E2E=1 再运行"
            f"{hint}。本次未请求模型，不能视为已验证。"
        )

    provider = (source.get("AGENT_PROVIDER") or "deepseek").strip().lower()
    model = (source.get("AGENT_MODEL") or "").strip()
    base_url = (source.get("AGENT_BASE_URL") or "").strip()

    if provider == "deepseek":
        api_key = (
            source.get("AGENT_API_KEY") or source.get("DEEPSEEK_API_KEY") or ""
        ).strip()
        model = model or "deepseek-chat"
        base_url = base_url or (source.get("DEEPSEEK_BASE") or "").strip()
    elif provider == "openai_compatible":
        api_key = (
            source.get("AGENT_API_KEY") or source.get("OPENAI_API_KEY") or ""
        ).strip()
        if not base_url or not model:
            raise LiveSkip(
                "AGENT_PROVIDER=openai_compatible 时必须配置 AGENT_BASE_URL 与 "
                "AGENT_MODEL；本次未请求模型，不能视为已验证。"
            )
    else:
        raise LiveSkip(
            f"AGENT_PROVIDER={provider!r} 不是真实模型（可选 deepseek / "
            "openai_compatible）；本次未请求模型，不能视为已验证。"
        )

    if not api_key:
        names = (
            "AGENT_API_KEY/DEEPSEEK_API_KEY"
            if provider == "deepseek"
            else "AGENT_API_KEY/OPENAI_API_KEY"
        )
        raise LiveSkip(
            f"未发现 API key（{names}）：真实模型 E2E 未运行，不能视为已验证。"
            "请在环境变量或本地 .env 中配置后重试。"
        )

    def _number(name: str, default: float) -> float:
        raw = (source.get(name) or "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError as exc:
            raise LiveSkip(f"{name}={raw!r} 不是数字") from exc

    return LiveConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=_number("AGENT_TIMEOUT_SECONDS", 120.0),
        max_steps=int(_number("AGENT_MAX_STEPS", 8)),
        token_budget=int(_number("AGENT_TOKEN_BUDGET", 64_000)),
    )


# ---------------------------------------------------------------------------
# 一轮对话：事件计时 + 渲染产物
# ---------------------------------------------------------------------------


@dataclass
class RoundRecord:
    name: str
    question: str
    events: int = 0
    answer_chunks: list[str] = field(default_factory=list)
    ttft_seconds: float | None = None
    elapsed_seconds: float = 0.0
    stopped: bool = False
    done_text: str = ""
    interrupt_text: str = ""
    result: AgentRunResult | None = None
    steps: list[StepRecord] = field(default_factory=list)
    citations: list[Any] = field(default_factory=list)
    figures: list[Any] = field(default_factory=list)
    usage: list[Any] = field(default_factory=list)
    steps_html: str = ""
    figures_html: str = ""
    citations_html: str = ""

    @property
    def answer(self) -> str:
        return "".join(self.answer_chunks)

    @property
    def tokens(self) -> dict[str, int]:
        return {
            "calls": len(self.usage),
            "prompt_tokens": sum(int(item.prompt_tokens or 0) for item in self.usage),
            "completion_tokens": sum(
                int(item.completion_tokens or 0) for item in self.usage
            ),
            "total_tokens": sum(int(item.total_tokens or 0) for item in self.usage),
        }


def _ask(
    controller: AgentChatController,
    usage: UsageLogger,
    *,
    name: str,
    question: str,
    files_root: str | Path,
    attachments: list[Attachment] | None = None,
    interrupt_after_first_chunk: bool = False,
) -> RoundRecord:
    """执行一轮并记录首字延迟、总耗时、步骤、引用与图表。"""

    record = RoundRecord(name=name, question=question)
    before = len(usage.records)
    started = time.perf_counter()
    stream = controller.ask(question, attachments=list(attachments or ()))
    for event in stream:
        record.events += 1
        if event.kind == "step" and event.step is not None:
            record.steps.append(event.step)
        elif event.kind == "answer":
            if record.ttft_seconds is None:
                record.ttft_seconds = time.perf_counter() - started
            record.answer_chunks.append(event.text)
            if interrupt_after_first_chunk and not record.interrupt_text:
                record.interrupt_text = controller.interrupt()
        elif event.kind == "done":
            record.done_text = event.text
            record.stopped = event.stopped
            record.result = event.result
    record.elapsed_seconds = time.perf_counter() - started
    record.usage = list(usage.records[before:])

    steps = list(record.steps)
    if record.result is not None:
        steps = list(record.result.steps)
        record.steps = steps
    record.steps_html = render_steps_html(steps)
    record.citations = collect_citations(
        steps, record.result.answer if record.result is not None else record.answer
    )
    record.figures = collect_figures(steps)
    record.figures_html = render_figures_html(record.figures)
    record.citations_html = render_citations_html(
        record.citations, root=files_root
    )
    return record


@dataclass
class LiveRun:
    config: LiveConfig
    probe: Any
    controller: AgentChatController
    usage: UsageLogger
    files_root: Path
    run_dir: Path
    rounds: list[RoundRecord]
    report_path: Path


def _stop_reason_value(result: AgentRunResult | None) -> str | None:
    reason = getattr(result, "stop_reason", None)
    return getattr(reason, "value", None) if reason is not None else None


def _cost(
    tokens: dict[str, int], env: dict[str, str] | None = None
) -> dict[str, Any] | None:
    source = os.environ if env is None else env
    raw_in = (source.get("AGENT_PRICE_INPUT_PER_M") or "").strip()
    raw_out = (source.get("AGENT_PRICE_OUTPUT_PER_M") or "").strip()
    if not raw_in or not raw_out:
        return None
    try:
        price_in = float(raw_in)
        price_out = float(raw_out)
    except ValueError:
        return None
    estimated = (
        tokens["prompt_tokens"] / 1_000_000 * price_in
        + tokens["completion_tokens"] / 1_000_000 * price_out
    )
    return {
        "input_per_m": price_in,
        "output_per_m": price_out,
        "currency": (source.get("AGENT_PRICE_CURRENCY") or "CNY").strip(),
        "estimated": round(estimated, 6),
    }


def _round_report(record: RoundRecord, files_root: Path) -> dict[str, Any]:
    citations = []
    for citation in record.citations:
        detail = resolve_citation(citation, root=files_root)
        citations.append(
            {
                "label": citation.label,
                "page": citation.page,
                "found": detail.found,
                "note": detail.note,
                "snippet": detail.snippet[:200],
                "source_path": detail.source_path,
            }
        )
    figures = [
        {
            "tool": figure.tool,
            "caption": figure.caption,
            "html_chars": len(figure.html),
            "embedded_chars": len(record.figures_html),
            "warnings": list(figure.warnings),
        }
        for figure in record.figures
    ]
    return {
        "name": record.name,
        "question": record.question,
        "answer": record.answer[:1500],
        "answer_chars": len(record.answer),
        "chunks": len(record.answer_chunks),
        "ttft_ms": (
            None if record.ttft_seconds is None else round(record.ttft_seconds * 1000, 1)
        ),
        "elapsed_ms": round(record.elapsed_seconds * 1000, 1),
        "stopped": record.stopped,
        "done_text": record.done_text,
        "stop_reason": _stop_reason_value(record.result),
        "model_error": getattr(record.result, "error", None),
        "interrupt_text": record.interrupt_text,
        "steps": [
            {
                "tool": step.tool,
                "arguments": dict(step.arguments),
                "error": step.result.error,
                "observation": (step.observation or "")[:200],
                "elapsed_ms": round(step.elapsed * 1000, 1),
            }
            for step in record.steps
        ],
        "usage": record.tokens,
        "cost": _cost(record.tokens),
        "citations": citations,
        "figures": figures,
    }


def _write_report(run: LiveRun) -> Path:
    totals = {
        key: sum(record.tokens[key] for record in run.rounds)
        for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
    }
    report = {
        "task": "plan-20261119-002",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "provider": run.config.provider,
        "model": run.config.model,
        "probe": run.probe.to_dict() if hasattr(run.probe, "to_dict") else {},
        "totals": {**totals, "cost": _cost(totals)},
        "rounds": [_round_report(record, run.files_root) for record in run.rounds],
    }
    run.report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return run.report_path


def _run_live(config: LiveConfig) -> LiveRun:
    """真实三轮会话：quote 流式回答 → 文件检索并中途停止 → 下一问 + K 线图。"""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path(
        os.environ.get("AGENT_LIVE_E2E_WORKDIR") or DEFAULT_WORKDIR
    ) / stamp
    files_root = run_dir / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "index.db"

    settings = GatewaySettings(
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        max_steps=config.max_steps,
        token_budget=config.token_budget,
        timeout_seconds=config.timeout_seconds,
        usage_path=None,
    )
    usage = UsageLogger(None)
    gateway = build_gateway(settings, probe=True, usage_logger=usage)
    probe = gateway.probe()
    model = AgentModelAdapter(gateway)

    registry = register_market_tools(
        build_file_registry(root=files_root, db_path=db_path)
    )
    session = AgentSession(path=run_dir / "session.json")
    controller = AgentChatController(
        model_factory=lambda: model,
        runtime_factory=lambda bound: AgentRuntime(
            bound,
            registry=registry,
            session=session,
            max_steps=config.max_steps,
            token_budget=config.token_budget,
            timeout_seconds=config.timeout_seconds,
        ),
        session=session,
        session_path=run_dir / "session.json",
        root=files_root,
        chunk_chars=CHUNK_CHARS,
    )

    ingest = file_store.ingest_files(
        [str(FIXTURE_PDF)], root=files_root, db_path=db_path, index=True
    )
    attachments = [Attachment.from_result(item) for item in ingest]
    assert attachments and all(item.ok for item in attachments), (
        "测试夹具 PDF 导入失败：" + repr([(a.name, a.status, a.warnings) for a in attachments])
    )

    rounds = [
        _ask(
            controller,
            usage,
            name="round1_quote_stream",
            question=(
                "请先调用 quote 工具查询 600519.SH 的最新价，然后用三句话向用户解释"
                "这个价格，必须提到工具返回的最新价和数据截至时间。"
            ),
            files_root=files_root,
        ),
        _ask(
            controller,
            usage,
            name="round2_file_search_interrupt",
            question=(
                "我上传了《年报.pdf》。请调用 file_search 工具检索 Revenue，然后用至少"
                "四句话分别说明每一页的营收数字，并在每句话末尾写成《年报.pdf》第 N 页。"
            ),
            files_root=files_root,
            attachments=attachments,
            interrupt_after_first_chunk=True,
        ),
        _ask(
            controller,
            usage,
            name="round3_next_question_kline",
            question=(
                "换一个新话题：请调用 kline 工具查询 600519.SH 最近 60 个交易日的日线"
                "（period 必须是 day，chart 必须是 true），然后用两句话说明近期走势。"
            ),
            files_root=files_root,
        ),
    ]

    run = LiveRun(
        config=config,
        probe=probe,
        controller=controller,
        usage=usage,
        files_root=files_root,
        run_dir=run_dir,
        rounds=rounds,
        report_path=run_dir / "report.json",
    )
    _write_report(run)
    return run


@pytest.fixture(scope="module")
def live_run() -> LiveRun:
    try:
        config = live_config()
    except LiveSkip as exc:
        pytest.skip(str(exc))
    return _run_live(config)


# ---------------------------------------------------------------------------
# 跳过语义：没有 key / 没有 opt-in 时必须明确 skip，不能假装已验证
# ---------------------------------------------------------------------------


def test_live_config_requires_explicit_opt_in() -> None:
    with pytest.raises(LiveSkip) as info:
        live_config({"DEEPSEEK_API_KEY": "unit-test-key"})
    message = str(info.value)
    assert "AGENT_LIVE_E2E=1" in message
    assert "不能视为已验证" in message


def test_live_config_skips_without_api_key() -> None:
    with pytest.raises(LiveSkip) as info:
        live_config({"AGENT_LIVE_E2E": "1"})
    message = str(info.value)
    assert "未发现 API key" in message
    assert "DEEPSEEK_API_KEY" in message
    assert "不能视为已验证" in message


def test_live_config_skips_incomplete_compatible_gateway() -> None:
    with pytest.raises(LiveSkip) as info:
        live_config({"AGENT_LIVE_E2E": "1", "AGENT_PROVIDER": "openai_compatible"})
    message = str(info.value)
    assert "AGENT_BASE_URL" in message and "AGENT_MODEL" in message


# ---------------------------------------------------------------------------
# 失败时的明确降级提示（本地桩，不花额度；与真实轮次互补）
# ---------------------------------------------------------------------------


class _BoomModel:
    provider = "boom"
    model = "boom-model"

    def complete(self, *, messages: list[dict[str, Any]], tools: list[Any]) -> Any:
        raise RuntimeError("网关不可用（测试桩）")

    def status_note(self) -> str:
        return "模型能力降级：网关不可用（测试桩）"


@pytest.fixture
def workdir() -> Path:
    """工作区内的临时目录（pytest 的 tmp_path 落在受沙箱限制的系统 temp）。"""

    path = DEFAULT_WORKDIR / f"unit-{os.getpid()}-{time.strftime('%H%M%S')}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_model_failure_surfaces_clear_degradation(workdir: Path) -> None:
    controller = AgentChatController(
        model_factory=lambda: _BoomModel(),
        session_path=workdir / "session.json",
        root=workdir / "files",
    )
    events = list(controller.ask("模型挂掉时用户看到什么？"))
    done = events[-1]
    assert done.kind == "done"
    assert done.result is not None
    assert done.result.stop_reason is StopReason.MODEL_ERROR
    assert "模型调用失败" in done.text
    assert "已保留此前产出的内容" in done.text
    assert "RuntimeError" in done.text and "网关不可用（测试桩）" in done.text
    # busy 锁必须释放：失败后还能继续提问，而不是永远「上一轮仍在生成」。
    retry = list(controller.ask("再试一次"))
    assert retry[-1].kind == "done"
    assert "上一轮仍在生成" not in retry[-1].text


# ---------------------------------------------------------------------------
# 真实模型三轮验收
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.timeout(900)
def test_round1_real_answer_streams_with_tool_step(live_run: LiveRun) -> None:
    """AC1/AC2/AC5：真实模型流式分片可见，quote 步骤与结果出现在时间线，token 有记录。"""

    record = live_run.rounds[0]
    assert live_run.probe.degraded is False, (
        f"模型能力探测失败：{getattr(live_run.probe, 'reason', None)}"
    )
    assert live_run.probe.function_calling is True
    assert record.result is not None
    assert record.result.stop_reason is StopReason.COMPLETED, record.done_text
    assert record.stopped is False
    assert record.answer.strip()
    assert len(record.answer_chunks) >= 2, (
        f"没有看到流式分片（chunks={len(record.answer_chunks)}）：{record.answer!r}"
    )
    assert record.ttft_seconds is not None
    assert 0 <= record.ttft_seconds <= record.elapsed_seconds

    quote_steps = [step for step in record.result.steps if step.tool == "quote"]
    assert quote_steps, f"本轮没有调用 quote：{[s.tool for s in record.result.steps]}"
    assert any(step.result.error is None for step in quote_steps)
    assert "quote" in record.steps_html
    assert "最新价" in record.steps_html, "工具结果没有出现在时间线里"
    assert record.tokens["prompt_tokens"] > 0
    assert record.tokens["completion_tokens"] > 0
    assert record.tokens["calls"] >= 1


@pytest.mark.live
@pytest.mark.timeout(900)
def test_round2_stop_keeps_partial_and_citations_resolve(live_run: LiveRun) -> None:
    """AC1/AC3/AC4：回答中途停止保留分片，引用可定位，停止不覆盖已产出内容。"""

    record = live_run.rounds[1]
    assert record.result is not None
    assert record.stopped is True
    assert "停止输出" in record.done_text
    assert "中断输出" in record.interrupt_text
    assert record.answer.strip(), "停止后已产出的分片被清空了"
    assert record.result.answer.startswith(record.answer), (
        "流式分片不是完整回答的前缀："
        f"partial={record.answer[:80]!r} full={record.result.answer[:80]!r}"
    )
    assert len(record.answer) < len(record.result.answer), (
        "停止发生在回答输出完之后，无法验证“不覆盖已停止分片”"
    )
    # UI 收到 stopped=True 时保留流式片段；这里直接验证停止标记与片段一致性。
    assert record.answer != record.result.answer

    file_steps = [step for step in record.result.steps if step.tool == "file_search"]
    assert file_steps, f"本轮没有调用 file_search：{[s.tool for s in record.result.steps]}"
    assert any(step.result.error is None for step in file_steps)
    # 真实模型第一次常把文件名当 file_id；工具必须给出可执行的下一步提示。
    for step in file_steps:
        if step.result.error:
            assert "file_list" in step.result.error, step.result.error
            assert 'data-status="error"' in record.steps_html
    # 工具结果正文（而不只是模型转述）要出现在时间线里。
    assert "Revenue" in record.steps_html, "file_search 的结果正文没有出现在时间线里"
    assert record.citations, "file_search 没有产生可定位的引用"
    details = [resolve_citation(item, root=live_run.files_root) for item in record.citations]
    assert all(detail.found for detail in details), [
        (detail.citation.label, detail.note) for detail in details if not detail.found
    ]
    assert any("Revenue" in detail.snippet for detail in details), (
        "引用没有定位到 PDF 正文：" + repr([d.snippet[:80] for d in details])
    )
    assert "《年报.pdf》" in record.citations_html
    assert "astock-cite-cards" in record.citations_html


@pytest.mark.live
@pytest.mark.timeout(900)
def test_round3_next_question_clean_and_chart_renders(live_run: LiveRun) -> None:
    """AC4/AC3：停止后下一问不受污染，K 线图内嵌可渲染，会话里没有中断标记。"""

    record = live_run.rounds[2]
    assert record.result is not None
    assert record.result.stop_reason is StopReason.COMPLETED, record.done_text
    assert record.stopped is False
    assert record.answer.strip()
    assert "⏹" not in record.answer
    assert "已停止输出" not in record.answer

    kline_steps = [step for step in record.result.steps if step.tool == "kline"]
    assert kline_steps, f"本轮没有调用 kline：{[s.tool for s in record.result.steps]}"
    assert any(step.arguments.get("chart") is True for step in kline_steps), (
        "模型没有按提示传 chart=true：" + repr([s.arguments for s in kline_steps])
    )
    assert any(step.result.figure for step in kline_steps)
    assert record.figures, "kline 步骤没有产出可内嵌的图表"
    assert "<iframe" in record.figures_html
    assert 'sandbox="allow-scripts"' in record.figures_html
    assert "astock-figure-frame" in record.figures_html
    assert "kline" in record.steps_html

    history = live_run.controller.session.history
    answers = [
        str(message.get("content") or "")
        for message in history
        if message.get("role") == "assistant"
    ]
    assert not any("⏹" in text or "已停止输出" in text for text in answers), (
        "中断状态写进了会话历史，污染下一问上下文"
    )
