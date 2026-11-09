"""进程内 Agent 运行时：plan → act → observe → reflect（ADR-013）。

运行时只做编排与护栏，不包含任何行情/研究逻辑：

- 工具来自 :class:`~astock.agent.registry.ToolRegistry` 白名单（契约 AGENT-01）；
- 每次模型回复要么是工具调用，要么是最终回答；
- 工具参数校验失败回传模型重试一次；同一 ``(tool, args)`` 连续出现达到
  ``spin_limit`` 次即停止打转；
- 步数上限 12、token 预算 64k、120s 超时、用户中断都会返回带部分结果的中文
  回答，而不是抛异常；
- ``reflect`` 优先使用模型自带的 ``reflect(step=, result=)``，否则使用默认
  中文摘要。会话可在运行结束时原子落盘。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from astock.agent.registry import (
    ToolArgumentError,
    ToolNotFoundError,
    ToolRegistry,
)
from astock.agent.session import AgentSession
from astock.agent.types import (
    MAX_TEXT_CHARS,
    AgentRunResult,
    ModelReply,
    StepRecord,
    StopReason,
    ToolCall,
    ToolResult,
)

__all__ = [
    "AgentModel",
    "AgentRuntime",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_BUDGET",
    "SYSTEM_PROMPT",
]

DEFAULT_MAX_STEPS = 12
DEFAULT_TOKEN_BUDGET = 64_000
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_VALIDATION_RETRIES = 1
DEFAULT_SPIN_LIMIT = 2

SYSTEM_PROMPT = (
    "你是 A 股研究助手。只能调用提供的工具获取数据；回答中的数字、表格和图形"
    "必须来自工具结果，并注明数据截至日期与来源。不能编造行情、财务指标或新闻。"
    "工具报错时先判断是参数问题还是数据源问题：参数问题按提示修正后重试一次，"
    "不要用相同参数反复调用。信息不足且无法通过工具补齐时向用户说明还缺什么。"
)


@runtime_checkable
class AgentModel(Protocol):
    """运行时依赖的最小模型接口（FakeModel 与真实模型适配器都实现它）。"""

    def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        ...


Clock = Callable[[], float]


class AgentRuntime:
    """编排模型与工具，执行带护栏的多步循环。"""

    def __init__(
        self,
        model: AgentModel,
        registry: ToolRegistry | None = None,
        session: AgentSession | None = None,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Clock | None = None,
        validation_retries: int = DEFAULT_VALIDATION_RETRIES,
        spin_limit: int = DEFAULT_SPIN_LIMIT,
        auto_save: bool = True,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须 >= 1")
        if token_budget < 1:
            raise ValueError("token_budget 必须 >= 1")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds 不能为负（0 表示不限制）")
        if validation_retries < 0:
            raise ValueError("validation_retries 不能为负")
        if spin_limit < 2:
            raise ValueError("spin_limit 必须 >= 2")
        self.model = model
        self.registry = registry if registry is not None else ToolRegistry()
        self.session = session if session is not None else AgentSession()
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.timeout_seconds = timeout_seconds
        self.validation_retries = validation_retries
        self.spin_limit = spin_limit
        self.auto_save = auto_save
        self._clock: Clock = clock or time.monotonic
        self._interrupt = threading.Event()
        self._cancel_event: threading.Event | None = None

    # ---- 控制 -------------------------------------------------------------

    def request_interrupt(self) -> None:
        """请求中断当前/下一次运行；已执行步骤会保留在结果里。"""
        self._interrupt.set()

    def run(
        self, question: str = "", *, cancel_event: threading.Event | None = None
    ) -> AgentRunResult:
        self._interrupt.clear()
        self._cancel_event = cancel_event
        if question:
            self.session.add_user(question)

        started = self._clock()
        steps: list[StepRecord] = []
        tokens_used = 0
        answer = ""
        error: str | None = None
        stop: StopReason | None = None
        save_error: str | None = None

        tools = self.registry.schemas()
        last_signature: str | None = None
        repeats = 0
        validation_failures: dict[str, int] = {}

        try:
            for index in range(self.max_steps):
                guard = self._guard(started, tokens_used)
                if guard is not None:
                    stop = guard
                    answer = self._partial_answer(stop, steps)
                    break

                step_started = self._clock()
                messages = self._build_messages()
                try:
                    reply = self.model.complete(messages=messages, tools=tools)
                    if not isinstance(reply, ModelReply):
                        raise TypeError(
                            f"model.complete 必须返回 ModelReply，实际 {type(reply).__name__}"
                        )
                except Exception as exc:  # noqa: BLE001 - 模型适配器可能抛任意异常
                    stop = StopReason.MODEL_ERROR
                    error = f"{type(exc).__name__}: {exc}"
                    answer = self._partial_answer(stop, steps, error)
                    break

                tokens_used += max(reply.tokens, 0)

                if reply.tool_call is None:
                    answer = reply.content or "（模型没有给出回答）"
                    self.session.add_assistant(answer)
                    stop = StopReason.COMPLETED
                    break

                call = reply.tool_call
                self.session.add_assistant(
                    reply.content,
                    tool_call={"name": call.name, "arguments": dict(call.arguments)},
                )

                if tokens_used >= self.token_budget:
                    result = ToolResult(
                        error="token 预算已用尽，未执行工具",
                        source=f"tool:{call.name}",
                    )
                    observation = self._format_observation(call.name, result)
                    tokens_used += _estimate_tokens(observation)
                    steps.append(
                        StepRecord(
                            index=index,
                            plan=reply.content,
                            tool=call.name,
                            arguments=dict(call.arguments),
                            result=result,
                            observation=observation,
                            reflection="预算已用尽，未执行工具。",
                            tokens=reply.tokens,
                            elapsed=max(self._clock() - step_started, 0.0),
                        )
                    )
                    self.session.add_tool(call.name, observation)
                    stop = StopReason.TOKEN_BUDGET
                    answer = self._partial_answer(stop, steps)
                    break

                try:
                    self.registry.tool(call.name)
                except ToolNotFoundError as exc:
                    result = ToolResult(
                        error=str(exc),
                        source=f"tool:{call.name}",
                        metadata={"available_tools": list(exc.available)},
                    )
                    observation = self._format_observation(call.name, result)
                    tokens_used += _estimate_tokens(observation)
                    steps.append(
                        StepRecord(
                            index=index,
                            plan=reply.content,
                            tool=call.name,
                            arguments=dict(call.arguments),
                            result=result,
                            observation=observation,
                            reflection=self._reflect(index, result),
                            tokens=reply.tokens,
                            elapsed=max(self._clock() - step_started, 0.0),
                        )
                    )
                    self.session.add_tool(call.name, observation)
                    continue

                try:
                    prepared = self.registry.validate(call.name, call.arguments)
                except ToolArgumentError as exc:
                    signature = call.signature()
                    attempt = validation_failures.get(signature, 0) + 1
                    validation_failures[signature] = attempt
                    result = ToolResult(
                        error=str(exc),
                        source=f"tool:{call.name}",
                        metadata={"validation_errors": list(exc.errors)},
                    )
                    observation = self._format_observation(call.name, result)
                    tokens_used += _estimate_tokens(observation)
                    steps.append(
                        StepRecord(
                            index=index,
                            plan=reply.content,
                            tool=call.name,
                            arguments=dict(call.arguments),
                            result=result,
                            observation=observation,
                            reflection=self._reflect(index, result),
                            tokens=reply.tokens,
                            elapsed=max(self._clock() - step_started, 0.0),
                        )
                    )
                    self.session.add_tool(call.name, observation)
                    if attempt > self.validation_retries:
                        stop = StopReason.INVALID_ARGUMENTS
                        error = str(exc)
                        answer = self._partial_answer(stop, steps, error)
                        break
                    continue

                signature = call.signature()
                if signature == last_signature:
                    repeats += 1
                else:
                    last_signature = signature
                    repeats = 1

                if repeats >= self.spin_limit:
                    result = ToolResult(
                        error=f"检测到重复调用 {call.name}，为避免打转已停止",
                        source=f"tool:{call.name}",
                    )
                    observation = self._format_observation(call.name, result)
                    tokens_used += _estimate_tokens(observation)
                    steps.append(
                        StepRecord(
                            index=index,
                            plan=reply.content,
                            tool=call.name,
                            arguments=dict(call.arguments),
                            result=result,
                            observation=observation,
                            reflection="参数与上一次相同，已触发打转保护。",
                            tokens=reply.tokens,
                            elapsed=max(self._clock() - step_started, 0.0),
                        )
                    )
                    self.session.add_tool(call.name, observation)
                    stop = StopReason.SPINNING
                    answer = self._partial_answer(stop, steps)
                    break

                result = self.registry.invoke(call.name, prepared)
                observation = self._format_observation(call.name, result)
                tokens_used += _estimate_tokens(observation)
                reflection = self._reflect(index, result)
                self.session.add_tool(call.name, observation)
                steps.append(
                    StepRecord(
                        index=index,
                        plan=reply.content,
                        tool=call.name,
                        arguments=dict(call.arguments),
                        result=result,
                        observation=observation,
                        reflection=reflection,
                        tokens=reply.tokens,
                        elapsed=max(self._clock() - step_started, 0.0),
                    )
                )

                guard = self._guard(started, tokens_used)
                if guard is not None:
                    stop = guard
                    answer = self._partial_answer(stop, steps)
                    break
            else:
                stop = StopReason.MAX_STEPS
                answer = self._partial_answer(stop, steps)
        finally:
            if self.auto_save and self.session.path is not None:
                try:
                    self.session.save()
                except OSError as exc:
                    save_error = f"{type(exc).__name__}: {exc}"

        if stop is None:
            stop = StopReason.MAX_STEPS
            answer = self._partial_answer(stop, steps)
        if save_error is not None:
            answer = f"{answer}\n（会话保存失败：{save_error}）"
            if error is None:
                error = save_error

        return AgentRunResult(
            answer=answer,
            stop_reason=stop,
            steps=steps,
            tokens_used=tokens_used,
            elapsed_seconds=max(self._clock() - started, 0.0),
            session_id=self.session.session_id,
            error=error,
        )

    # ---- 内部 -------------------------------------------------------------

    def _guard(self, started: float, tokens_used: int) -> StopReason | None:
        if self._interrupt.is_set():
            return StopReason.INTERRUPTED
        if self._cancel_event is not None and self._cancel_event.is_set():
            return StopReason.INTERRUPTED
        if (
            self.timeout_seconds > 0
            and self._clock() - started >= self.timeout_seconds
        ):
            return StopReason.TIMEOUT
        if tokens_used >= self.token_budget:
            return StopReason.TOKEN_BUDGET
        return None

    def _build_messages(self) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            *self.session.recent_messages(),
        ]

    def _format_observation(self, tool_name: str, result: ToolResult) -> str:
        payload: dict[str, Any] = {"tool": tool_name, "ok": result.ok}
        if result.error:
            payload["error"] = result.error
        if result.text:
            text = result.text
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + "…（已截断）"
            payload["text"] = text
        if result.data_as_of:
            payload["data_as_of"] = result.data_as_of
        if result.data_time:
            payload["data_time"] = result.data_time
        if result.source:
            payload["source"] = result.source
        if result.table is not None:
            rows = result.table
            table: dict[str, Any] = {
                "rows": len(rows),
                "columns": list(rows[0]) if rows else [],
                "preview": rows[:3],
            }
            total_rows = result.metadata.get("total_rows")
            if isinstance(total_rows, int):
                table["total_rows"] = total_rows
            if result.truncated:
                table["truncated"] = True
            payload["table"] = table
        if result.figure:
            payload["figure"] = result.figure
        if result.metadata:
            payload["metadata"] = result.metadata
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def _reflect(self, index: int, result: ToolResult) -> str:
        reflect = getattr(self.model, "reflect", None)
        if callable(reflect):
            try:
                text = reflect(step=index, result=result)
                if isinstance(text, str) and text.strip():
                    return text
            except Exception:  # noqa: BLE001 - 反思失败不应打断主循环
                pass
        if result.ok:
            return "工具执行成功，继续判断是否已有足够信息。"
        return f"工具执行失败：{result.error}；检查参数或改用其他工具。"

    def _partial_answer(
        self,
        stop: StopReason,
        steps: list[StepRecord],
        error: str | None = None,
    ) -> str:
        labels = {
            StopReason.MAX_STEPS: "达到步数上限",
            StopReason.TOKEN_BUDGET: "token 预算已用尽",
            StopReason.TIMEOUT: "运行超时",
            StopReason.INTERRUPTED: "用户中断",
            StopReason.SPINNING: "检测到重复调用（打转保护）",
            StopReason.INVALID_ARGUMENTS: "工具参数多次校验失败",
            StopReason.MODEL_ERROR: "模型调用失败",
        }
        reason = labels.get(stop, stop.value)
        lines = [f"未能完整完成本轮任务：{reason}。"]

        successes = [
            step for step in steps if step.result.ok and step.result.error is None
        ][-3:]
        if successes:
            lines.append("已获得的部分结果：")
            for step in successes:
                lines.append(f"- {step.tool}: {_summarize_result(step.result)}")

        if error:
            lines.append(f"最后错误：{error}")
        else:
            failures = [step for step in steps if not step.result.ok]
            if failures:
                lines.append(f"最后错误：{failures[-1].result.error}")

        if not steps:
            lines.append("尚未执行任何工具。")
        return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _summarize_result(result: ToolResult) -> str:
    if result.text:
        text = " ".join(result.text.split())
        if len(text) > 120:
            text = text[:120] + "…"
        suffix = f"（数据截至 {result.data_as_of}）" if result.data_as_of else ""
        return text + suffix
    if result.table is not None:
        return f"表格 {len(result.table)} 行"
    if result.figure:
        return f"图表 {result.figure}"
    return "无文本结果"
