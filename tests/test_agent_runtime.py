"""Agent 运行时骨架的离线测试（ADR-013 验收）。

覆盖：正常 plan→act→observe→reflect、步数上限、token 预算、超时、中断、
打转保护、参数校验重试、工具报错、未知工具；以及工具注册表、会话裁剪与
JSON 落盘。
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from astock.agent import (
    AgentRuntime,
    AgentSession,
    FakeModel,
    Permission,
    StopReason,
    ToolArgumentError,
    ToolCall,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    WorkingMemory,
)
from astock.agent.runtime import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TOKEN_BUDGET,
    SYSTEM_PROMPT,
)
from astock.agent.types import MAX_TABLE_ROWS, StepRecord
from astock.agent.types import ToolResult as ResultAlias

PRICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"code": {"type": "string", "minLength": 6}},
    "required": ["code"],
    "additionalProperties": False,
}


def price_spec(
    handler: Any,
    *,
    name: str = "lookup_price",
    permissions: tuple[Permission, ...] = (Permission.MARKET_READ,),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="查询指定代码的最新收盘价",
        parameters=PRICE_SCHEMA,
        handler=handler,
        permissions=permissions,
    )


def ok_price(code: str) -> ToolResult:
    return ToolResult(
        text=f"{code} 收盘价 1680.00 元",
        data_as_of="2026-11-07",
        source="fake://price",
    )


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RuntimeLoopTests(unittest.TestCase):
    def test_normal_plan_act_observe_completed(self) -> None:
        calls: list[str] = []

        def handler(*, code: str) -> ToolResult:
            calls.append(code)
            return ok_price(code)

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel(
            [ToolCall("lookup_price", {"code": "600519"}), "贵州茅台收盘价 1680.00 元。"]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("茅台现在什么价？")

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertFalse(result.partial)
        self.assertEqual(calls, ["600519"])
        self.assertEqual(len(result.steps), 1)
        self.assertIn("1680.00", result.steps[0].observation)
        self.assertIn("2026-11-07", result.steps[0].observation)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(
            model.calls[0]["tools"][0]["function"]["name"], "lookup_price"
        )
        tool_messages = [
            message
            for message in model.calls[1]["messages"]
            if message["role"] == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["name"], "lookup_price")
        self.assertEqual(
            [message["role"] for message in runtime.session.history],
            ["user", "assistant", "tool", "assistant"],
        )

    def test_max_steps_returns_partial_result(self) -> None:
        def handler(*, code: str) -> ToolResult:
            return ok_price(code)

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel(
            [
                ToolCall("lookup_price", {"code": "600519"}),
                ToolCall("lookup_price", {"code": "000001"}),
                ToolCall("lookup_price", {"code": "300750"}),
            ]
        )
        runtime = AgentRuntime(model, registry, max_steps=2)

        result = runtime.run("比较三只股票")

        self.assertEqual(result.stop_reason, StopReason.MAX_STEPS)
        self.assertTrue(result.partial)
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(model.call_count, 2)
        self.assertIn("步数上限", result.answer)
        self.assertIn("已获得的部分结果", result.answer)

    def test_token_budget_stops_before_next_tool_execution(self) -> None:
        calls: list[str] = []

        def handler(*, code: str) -> ToolResult:
            calls.append(code)
            return ok_price(code)

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel(
            [
                ToolCall("lookup_price", {"code": "600519"}),
                ToolCall("lookup_price", {"code": "000001"}),
            ],
            tokens_per_reply=60_000,
        )
        runtime = AgentRuntime(model, registry, token_budget=100_000)

        result = runtime.run("查两只股票")

        self.assertEqual(result.stop_reason, StopReason.TOKEN_BUDGET)
        self.assertEqual(calls, ["600519"])
        self.assertEqual(len(result.steps), 2)
        self.assertFalse(result.steps[1].result.ok)
        self.assertIn("token 预算", result.answer)
        self.assertGreaterEqual(result.tokens_used, 100_000)

    def test_timeout_keeps_executed_steps(self) -> None:
        clock = FakeClock()

        def handler(*, code: str) -> ToolResult:
            clock.advance(200)
            return ok_price(code)

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel([ToolCall("lookup_price", {"code": "600519"}), "不应到达"])
        runtime = AgentRuntime(model, registry, clock=clock, timeout_seconds=120)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.TIMEOUT)
        self.assertEqual(len(result.steps), 1)
        self.assertIn("超时", result.answer)
        self.assertIn("1680.00", result.answer)
        self.assertAlmostEqual(result.elapsed_seconds, 200.0)

    def test_user_interrupt_returns_partial_result(self) -> None:
        holder: dict[str, AgentRuntime] = {}

        def handler(*, code: str) -> ToolResult:
            holder["runtime"].request_interrupt()
            return ok_price(code)

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel([ToolCall("lookup_price", {"code": "600519"}), "不应到达"])
        runtime = AgentRuntime(model, registry)
        holder["runtime"] = runtime

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.INTERRUPTED)
        self.assertEqual(len(result.steps), 1)
        self.assertIn("中断", result.answer)
        self.assertEqual(model.call_count, 1)

    def test_interrupt_requested_before_run_stops_at_first_guard(self) -> None:
        model = FakeModel(["不应到达"])
        runtime = AgentRuntime(model, ToolRegistry())
        runtime.request_interrupt()

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.INTERRUPTED)
        self.assertEqual(model.call_count, 0)
        self.assertEqual(result.steps, [])
        self.assertIn("中断", result.answer)

        # 中断标记被本次运行消费，下一次运行应恢复正常。
        follow_up = runtime.run("再查一次")
        self.assertEqual(follow_up.stop_reason, StopReason.COMPLETED)
        self.assertEqual(model.call_count, 1)

    def test_interrupt_if_running_does_not_leak_after_run(self) -> None:
        model = FakeModel(["完成"])
        runtime = AgentRuntime(model, ToolRegistry())

        first = runtime.run("第一问")
        self.assertEqual(first.stop_reason, StopReason.COMPLETED)
        # run 已结束时请求中断应返回 False，且不留下标记给下一问。
        self.assertFalse(runtime.request_interrupt_if_running())

        follow_up = runtime.run("第二问")
        self.assertEqual(follow_up.stop_reason, StopReason.COMPLETED)
        self.assertEqual(model.call_count, 2)

    def test_pre_set_cancel_event_stops_at_first_guard(self) -> None:
        model = FakeModel(["不应到达"])
        cancel_event = threading.Event()
        cancel_event.set()
        runtime = AgentRuntime(model, ToolRegistry())

        result = runtime.run("查价格", cancel_event=cancel_event)

        self.assertEqual(result.stop_reason, StopReason.INTERRUPTED)
        self.assertEqual(model.call_count, 0)
        self.assertEqual(result.steps, [])

    def test_identical_call_twice_triggers_spin_guard(self) -> None:
        calls: list[str] = []

        def handler(*, code: str) -> ToolResult:
            calls.append(code)
            return ok_price(code)

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel(
            [
                ToolCall("lookup_price", {"code": "600519"}),
                ToolCall("lookup_price", {"code": "600519"}),
                "不应到达",
            ]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.SPINNING)
        self.assertEqual(calls, ["600519"])
        self.assertEqual(len(result.steps), 2)
        self.assertIn("打转", result.answer)
        self.assertIn("重复", result.steps[1].result.error or "")

    def test_invalid_arguments_retried_once_then_succeeds(self) -> None:
        calls: list[str] = []

        def handler(*, code: str) -> ToolResult:
            calls.append(code)
            return ok_price(code)

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel(
            [
                ToolCall("lookup_price", {}),
                ToolCall("lookup_price", {"code": "600519"}),
                "重试后成功。",
            ]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(calls, ["600519"])
        self.assertEqual(len(result.steps), 2)
        self.assertFalse(result.steps[0].result.ok)
        second_messages = model.calls[1]["messages"]
        self.assertTrue(
            any(
                "缺少必填参数" in message.get("content", "")
                for message in second_messages
                if message["role"] == "tool"
            )
        )

    def test_invalid_arguments_twice_stops(self) -> None:
        registry = ToolRegistry()
        registry.register(price_spec(lambda *, code: ok_price(code)))
        model = FakeModel(
            [
                ToolCall("lookup_price", {}),
                ToolCall("lookup_price", {}),
                "不应到达",
            ]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.INVALID_ARGUMENTS)
        self.assertEqual(len(result.steps), 2)
        self.assertIn("参数", result.answer)
        self.assertTrue(all(not step.result.ok for step in result.steps))

    def test_tool_error_is_observed_and_model_can_recover(self) -> None:
        def handler(*, code: str) -> ToolResult:
            raise RuntimeError("数据源不可用")

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel(
            [ToolCall("lookup_price", {"code": "600519"}), "数据源暂时不可用，请稍后再试。"]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(result.steps), 1)
        self.assertIn("RuntimeError", result.steps[0].result.error or "")
        self.assertIn("数据源不可用", result.steps[0].observation)
        self.assertIn("稍后再试", result.answer)

    def test_tool_error_then_retry_with_different_arguments(self) -> None:
        seen: list[str] = []

        def handler(*, code: str) -> ToolResult:
            seen.append(code)
            if code == "600519":
                raise RuntimeError("该代码无行情")
            return ToolResult(text=f"{code} 收盘 10.00", source="tool:lookup_price")

        registry = ToolRegistry()
        registry.register(price_spec(handler))
        model = FakeModel(
            [
                ToolCall("lookup_price", {"code": "600519"}),
                ToolCall("lookup_price", {"code": "000001"}),
                "600519 无行情，000001 收盘 10.00。",
            ]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(seen, ["600519", "000001"])
        self.assertEqual(len(result.steps), 2)
        self.assertFalse(result.steps[0].result.ok)
        self.assertTrue(result.steps[1].result.ok)
        self.assertIn("10.00", result.answer)

    def test_malformed_table_return_becomes_error_result(self) -> None:
        registry = ToolRegistry()
        registry.register(price_spec(lambda *, code: {"table": "bad"}))
        model = FakeModel(
            [ToolCall("lookup_price", {"code": "600519"}), "工具返回结构有误，已记录。"]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(result.steps), 1)
        self.assertIn("工具返回结构非法", result.steps[0].result.error or "")
        self.assertIn("ToolResult.table", result.steps[0].result.error or "")
        tool_messages = [
            message
            for message in model.calls[1]["messages"]
            if message["role"] == "tool"
        ]
        self.assertTrue(
            any("工具返回结构非法" in message["content"] for message in tool_messages)
        )

    def test_malformed_text_return_becomes_error_result(self) -> None:
        registry = ToolRegistry()
        registry.register(price_spec(lambda *, code: {"text": 123}))
        model = FakeModel(
            [ToolCall("lookup_price", {"code": "600519"}), "工具返回结构有误，已记录。"]
        )
        runtime = AgentRuntime(model, registry)

        result = runtime.run("查价格")

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(result.steps), 1)
        self.assertIn("工具返回结构非法", result.steps[0].result.error or "")
        self.assertIn("ToolResult.text", result.steps[0].result.error or "")
        self.assertIn("工具返回结构非法", result.steps[0].observation)

    def test_unknown_tool_is_reported_without_crashing(self) -> None:
        model = FakeModel(
            [ToolCall("delete_everything", {}), "我没有这个工具，改用可用工具。"]
        )
        runtime = AgentRuntime(model, ToolRegistry())

        result = runtime.run("试试未知工具")

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(result.steps), 1)
        self.assertIn("未知工具", result.steps[0].result.error or "")
        self.assertIn("未知工具", result.steps[0].observation)

    def test_identical_unknown_tool_twice_triggers_spin_guard(self) -> None:
        model = FakeModel(
            [
                ToolCall("ghost_tool", {"query": "x"}),
                ToolCall("ghost_tool", {"query": "x"}),
                "不应到达",
            ]
        )
        runtime = AgentRuntime(model, ToolRegistry())

        result = runtime.run("调用不存在的工具")

        self.assertEqual(result.stop_reason, StopReason.SPINNING)
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(model.call_count, 2)
        self.assertIn("重复", result.steps[1].result.error or "")
        self.assertIn("打转", result.answer)

    def test_model_exception_becomes_model_error(self) -> None:
        class BrokenModel:
            def complete(self, *, messages: list[dict[str, Any]], tools: list[Any]):
                raise RuntimeError("connection reset")

        runtime = AgentRuntime(BrokenModel(), ToolRegistry())  # type: ignore[arg-type]

        result = runtime.run("你好")

        self.assertEqual(result.stop_reason, StopReason.MODEL_ERROR)
        self.assertIn("connection reset", result.answer)
        self.assertIn("connection reset", result.error or "")

    def test_model_timeout_error_becomes_model_error(self) -> None:
        class TimeoutModel:
            def complete(self, *, messages: list[dict[str, Any]], tools: list[Any]):
                raise TimeoutError("gateway read timeout")

        runtime = AgentRuntime(TimeoutModel(), ToolRegistry())  # type: ignore[arg-type]

        result = runtime.run("你好")

        self.assertEqual(result.stop_reason, StopReason.MODEL_ERROR)
        self.assertIn("TimeoutError", result.answer)
        self.assertIn("gateway read timeout", result.answer)

    def test_default_guard_constants(self) -> None:
        self.assertEqual(DEFAULT_MAX_STEPS, 12)
        self.assertEqual(DEFAULT_TOKEN_BUDGET, 64_000)
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 120.0)

    def test_system_prompt_marks_tool_content_untrusted(self) -> None:
        self.assertIn("不可信", SYSTEM_PROMPT)
        self.assertIn("不得执行", SYSTEM_PROMPT)

    def test_on_step_observes_each_step_in_order(self) -> None:
        registry = ToolRegistry()
        registry.register(price_spec(ok_price))
        model = FakeModel(
            [
                ToolCall("lookup_price", {"code": "600519"}),
                ToolCall("lookup_price", {"code": "000001"}),
                "两家公司收盘价已列出。",
            ]
        )
        runtime = AgentRuntime(model, registry)
        seen: list[StepRecord] = []

        result = runtime.run("对比一下", on_step=seen.append)

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual([step.tool for step in seen], ["lookup_price", "lookup_price"])
        self.assertEqual(seen, result.steps)
        self.assertEqual(
            [step.arguments["code"] for step in seen], ["600519", "000001"]
        )

    def test_on_step_failure_does_not_break_run(self) -> None:
        registry = ToolRegistry()
        registry.register(price_spec(ok_price))
        model = FakeModel(
            [ToolCall("lookup_price", {"code": "600519"}), "贵州茅台收盘价 1680.00 元。"]
        )
        runtime = AgentRuntime(model, registry)

        def broken_hook(step: StepRecord) -> None:
            raise RuntimeError("界面渲染失败")

        result = runtime.run("茅台现在什么价？", on_step=broken_hook)

        self.assertEqual(result.stop_reason, StopReason.COMPLETED)
        self.assertEqual(len(result.steps), 1)
        self.assertIn("1680.00", result.answer)


class RegistryTests(unittest.TestCase):
    def test_permissions_and_schema_exposed(self) -> None:
        registry = ToolRegistry()
        spec = registry.register(price_spec(ok_price))
        self.assertEqual(spec.permission_names, ("market:read",))
        schema = registry.schemas()[0]
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["parameters"]["required"], ["code"])
        with self.assertRaises(ToolRegistrationError):
            ToolSpec(
                name="shell",
                description="任意命令",
                parameters={"type": "object"},
                handler=lambda: None,
                permissions=("shell:exec",),
            )
        with self.assertRaises(ToolRegistrationError):
            ToolSpec(
                name="Bad-Name",
                description="非法名称",
                parameters={"type": "object"},
                handler=lambda: None,
                permissions=(Permission.FILE_READ,),
            )

    def test_allowed_permissions_enforced(self) -> None:
        registry = ToolRegistry(allowed_permissions=[Permission.MARKET_READ])
        with self.assertRaises(ToolRegistrationError):
            registry.register(
                ToolSpec(
                    name="render_chart",
                    description="渲染图表",
                    parameters={"type": "object"},
                    handler=lambda: None,
                    permissions=(Permission.CHART_RENDER,),
                )
            )
        duplicate = price_spec(ok_price)
        registry.register(duplicate)
        with self.assertRaises(ToolRegistrationError):
            registry.register(duplicate)

    def test_schema_validation_and_defaults(self) -> None:
        received: dict[str, Any] = {}

        def handler(*, code: str, limit: int = 10) -> ToolResult:
            received.update({"code": code, "limit": limit})
            return ToolResult(text="ok")

        schema = {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "default": 10},
            },
            "required": ["code"],
            "additionalProperties": False,
        }
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="lookup",
                description="查询",
                parameters=schema,
                handler=handler,
                permissions=(Permission.MARKET_READ,),
            )
        )

        self.assertEqual(registry.validate("lookup", {"code": "600519"})["limit"], 10)
        registry.invoke("lookup", {"code": "600519"})
        self.assertEqual(received, {"code": "600519", "limit": 10})

        with self.assertRaises(ToolArgumentError) as ctx:
            registry.validate("lookup", {"code": 123, "extra": 1})
        message = str(ctx.exception)
        self.assertIn("期望 string", message)
        self.assertIn("不允许的额外参数", message)
        self.assertEqual(ctx.exception.tool, "lookup")

        with self.assertRaises(ToolNotFoundError) as missing:
            registry.tool("nope")
        self.assertEqual(missing.exception.available, ("lookup",))

    def test_long_table_is_trimmed_and_archived(self) -> None:
        rows = [{"code": f"{index:06d}", "close": index} for index in range(250)]
        archived: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []

        def writer(name: str, arguments: dict[str, Any], full_rows: list[dict[str, Any]]) -> str:
            archived.append((name, arguments, full_rows))
            return "artifact-1"

        registry = ToolRegistry(artifact_writer=writer)
        registry.register(
            ToolSpec(
                name="dump_prices",
                description="导出价格",
                parameters={"type": "object"},
                handler=lambda: rows,
                permissions=(Permission.MARKET_READ,),
            )
        )

        result = registry.invoke("dump_prices", {})

        self.assertEqual(len(result.table or []), MAX_TABLE_ROWS)
        self.assertTrue(result.truncated)
        self.assertEqual(result.metadata["total_rows"], 250)
        self.assertEqual(result.metadata["dropped_rows"], 50)
        self.assertEqual(result.metadata["table_artifact_id"], "artifact-1")
        self.assertEqual(len(archived), 1)
        self.assertEqual(len(archived[0][2]), 250)

    def test_artifact_writer_failure_still_returns_trimmed_table(self) -> None:
        rows = [{"code": str(index)} for index in range(201)]

        def writer(name: str, arguments: dict[str, Any], full_rows: list[dict[str, Any]]) -> str:
            raise OSError("disk full")

        registry = ToolRegistry(artifact_writer=writer)
        registry.register(
            ToolSpec(
                name="dump_prices",
                description="导出价格",
                parameters={"type": "object"},
                handler=lambda: rows,
                permissions=(Permission.MARKET_READ,),
            )
        )

        result = registry.invoke("dump_prices", {})

        self.assertEqual(len(result.table or []), 200)
        self.assertTrue(result.truncated)
        self.assertIn("disk full", result.metadata["artifact_error"])

    def test_long_table_without_writer_marks_artifact_missing(self) -> None:
        rows = [{"code": str(index)} for index in range(5)]
        registry = ToolRegistry(table_row_limit=3)
        registry.register(
            ToolSpec(
                name="dump_prices",
                description="导出价格",
                parameters={"type": "object"},
                handler=lambda: rows,
                permissions=(Permission.MARKET_READ,),
            )
        )

        result = registry.invoke("dump_prices", {})

        self.assertEqual(len(result.table or []), 3)
        self.assertTrue(result.truncated)
        self.assertEqual(result.metadata["total_rows"], 5)
        self.assertEqual(result.metadata["dropped_rows"], 2)
        self.assertIs(result.metadata["artifact_missing"], True)
        self.assertNotIn("table_artifact_id", result.metadata)


class SessionTests(unittest.TestCase):
    def test_recent_twenty_turns_are_kept(self) -> None:
        session = AgentSession(max_turns=20)
        for index in range(25):
            session.add_user(f"问题 {index}")
            session.add_assistant(f"回答 {index}")

        recent = session.recent_messages()

        self.assertEqual(len(recent), 40)
        self.assertEqual(recent[0]["content"], "问题 5")
        self.assertEqual(recent[-1]["content"], "回答 24")

    def test_working_memory_and_json_roundtrip(self) -> None:
        target = Path(__file__).parent / f"_agent_session_{uuid.uuid4().hex}.json"
        try:
            session = AgentSession(
                "session-test",
                max_turns=5,
                working_memory=WorkingMemory(codes=["600519"]),
                path=target,
            )
            session.working_memory.update(
                start_date="2026-01-01", end_date="2026-11-07", note="自定义"
            )
            session.add_user("问题")
            session.add_assistant("回答", tool_call={"name": "lookup_price", "arguments": {}})
            session.save()

            self.assertTrue(target.exists())
            self.assertFalse(target.with_name(target.name + ".tmp").exists())

            loaded = AgentSession.load(target)
            self.assertEqual(loaded.session_id, "session-test")
            self.assertEqual(loaded.working_memory.codes, ["600519"])
            self.assertEqual(loaded.working_memory.start_date, "2026-01-01")
            self.assertEqual(loaded.working_memory.extras["note"], "自定义")
            self.assertEqual(loaded.history, session.history)
            self.assertEqual(loaded.path, target)
        finally:
            target.unlink(missing_ok=True)
            target.with_name(target.name + ".tmp").unlink(missing_ok=True)

    def test_load_rejects_non_object_payload_with_type_error(self) -> None:
        """顶层不是 JSON object 时必须抛 TypeError（公共 API 语义，plan-20261103-012）。"""
        target = Path(__file__).parent / f"_agent_session_bad_{uuid.uuid4().hex}.json"
        try:
            target.write_text('["not", "an", "object"]', encoding="utf-8")

            with self.assertRaises(TypeError) as ctx:
                AgentSession.load(target)

            self.assertIn("顶层必须是 object", str(ctx.exception))
        finally:
            target.unlink(missing_ok=True)

    def test_recent_messages_limit_zero_returns_empty(self) -> None:
        session = AgentSession()
        session.add_user("问题")
        session.add_assistant("回答")

        self.assertEqual(session.recent_messages(limit=0), [])
        self.assertEqual(session.recent_messages(limit=-1), [])
        self.assertEqual(len(session.recent_messages(limit=1)), 1)

    def test_save_without_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            AgentSession().save()

    def test_runtime_reports_session_save_failure(self) -> None:
        blocker = Path(__file__).parent / f"_agent_blocked_{uuid.uuid4().hex}.tmp"
        blocker.write_text("not a directory", encoding="utf-8")
        target = blocker / "session.json"
        try:
            runtime = AgentRuntime(
                FakeModel(["完成"]), session=AgentSession(path=target)
            )

            result = runtime.run("问题")

            self.assertEqual(result.stop_reason, StopReason.COMPLETED)
            self.assertIn("会话保存失败", result.answer)
            self.assertIsNotNone(result.error)
        finally:
            blocker.unlink(missing_ok=True)

    def test_corrupt_or_missing_working_memory_is_tolerated(self) -> None:
        memory = WorkingMemory.from_dict({"codes": None, "extras": "bad"})
        self.assertEqual(memory.codes, [])
        self.assertEqual(memory.extras, {})


class FileCitationTests(unittest.TestCase):
    """P1：文件工具结果进入最终回答前必须过引用收口（AGENT-04）。"""

    def _runtime(
        self, result: ToolResult, model: FakeModel, *, tool: str = "file_search"
    ) -> AgentRuntime:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name=tool,
                description="文件工具测试桩",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "minLength": 1}},
                    "additionalProperties": False,
                },
                handler=lambda **_: result,
                permissions=(Permission.FILE_READ,),
            )
        )
        return AgentRuntime(model, registry)

    def test_file_tool_answer_without_citation_becomes_not_found(self) -> None:
        result = ToolResult(
            text="《年报.pdf》第 2 页\n营业收入同比增长 10%。",
            source="file:年报.pdf",
            metadata={"citations": ["《年报.pdf》第 2 页"]},
        )
        model = FakeModel(
            [ToolCall("file_search", {"query": "营收"}), "营业收入同比增长 10%。"]
        )
        runtime = self._runtime(result, model)

        run = runtime.run("年报里营收增长多少？")

        self.assertEqual(run.stop_reason, StopReason.COMPLETED)
        self.assertEqual(run.answer, "未在文件中找到")

    def test_file_tool_answer_with_grounded_citation_is_kept(self) -> None:
        result = ToolResult(
            text="《年报.pdf》第 2 页\n营业收入同比增长 10%。",
            source="file:年报.pdf",
            metadata={"citations": ["《年报.pdf》第 2 页"]},
        )
        answer = "营业收入同比增长 10%（《年报.pdf》第 2 页）。"
        model = FakeModel([ToolCall("file_search", {"query": "营收"}), answer])
        runtime = self._runtime(result, model)

        run = runtime.run("年报里营收增长多少？")

        self.assertEqual(run.stop_reason, StopReason.COMPLETED)
        self.assertEqual(run.answer, answer)

    def test_file_list_only_answer_is_not_rewritten(self) -> None:
        """file_list 只给清单、不产生定位引用，不能被收口成「未在文件中找到」。"""
        result = ToolResult(
            text="共 2 个文件：\n- 《年报.pdf》\n- 《财务.xlsx》",
            source="files:local",
            table=[{"name": "年报.pdf"}, {"name": "财务.xlsx"}],
            metadata={"files": 2, "returned": 2},
        )
        answer = "共 2 个文件：《年报.pdf》、《财务.xlsx》。"
        model = FakeModel([ToolCall("file_list", {}), answer])
        runtime = self._runtime(result, model, tool="file_list")

        run = runtime.run("我上传了哪些文件？")

        self.assertEqual(run.stop_reason, StopReason.COMPLETED)
        self.assertEqual(run.answer, answer)

    def test_document_body_citation_is_not_trusted(self) -> None:
        """上传文件正文里的《…》片段是 untrusted data，不能进入引用白名单。"""
        result = ToolResult(
            text="《虚构.pdf》第 9 页\n营业收入同比增长 10%。",
            source="file:公告.txt",
            metadata={"citations": ["《公告.txt》第 1 页"]},
        )
        answer = "营业收入同比增长 10%（《虚构.pdf》第 9 页）。"
        model = FakeModel([ToolCall("file_read", {"query": "营收"}), answer])
        runtime = self._runtime(result, model, tool="file_read")

        run = runtime.run("公告里营收多少？")

        self.assertEqual(run.stop_reason, StopReason.COMPLETED)
        self.assertNotIn("虚构", run.answer)
        self.assertEqual(run.answer, "引用：《公告.txt》第 1 页")

    def test_zero_tool_citation_is_downgraded(self) -> None:
        """没有工具结果作白名单时，回答自带的《…》引用不得原样落库。"""
        answer = "营业收入同比增长 10%（《年报.pdf》第 2 页）。"
        model = FakeModel([answer])
        runtime = AgentRuntime(model, ToolRegistry())

        run = runtime.run("年报里营收多少？")

        self.assertEqual(run.stop_reason, StopReason.COMPLETED)
        self.assertEqual(run.answer, "未在文件中找到")


class TypeTests(unittest.TestCase):
    def test_dataframe_result_is_coerced_to_table(self) -> None:
        import pandas as pd

        result = ToolResult.coerce(
            pd.DataFrame([{"code": "600519", "close": 1680.0}])
        )

        self.assertEqual(result.table, [{"code": "600519", "close": 1680.0}])
        self.assertEqual(result.metadata["columns"], ["code", "close"])

    def test_tool_result_trim_and_signature(self) -> None:
        result = ResultAlias(table=[{"i": index} for index in range(205)])
        trimmed = result.trim_table()
        self.assertEqual(len(trimmed.table or []), 200)
        self.assertTrue(trimmed.truncated)
        self.assertEqual(trimmed.metadata["dropped_rows"], 5)

        first = ToolCall("lookup", {"a": 1, "b": 2})
        second = ToolCall("lookup", {"b": 2, "a": 1})
        self.assertEqual(first.signature(), second.signature())


def test_runtime_audits_run_and_steps_into_store(tmp_path: Path) -> None:
    import json

    from astock.agent import store

    registry = ToolRegistry()
    registry.register(price_spec(ok_price))
    model = FakeModel(
        [ToolCall("lookup_price", {"code": "600519"}), "贵州茅台收盘价 1680.00 元。"]
    )
    runtime = AgentRuntime(model, registry, db_path=tmp_path / "astock.db")

    result = runtime.run("茅台现在什么价？")

    assert result.stop_reason == StopReason.COMPLETED
    conn = store.connect(tmp_path / "astock.db")
    try:
        runs = conn.execute(
            "SELECT run_kind, status, steps, completion_tokens, finished_at "
            "FROM agent_runs"
        ).fetchall()
        assert len(runs) == 1
        run_kind, status, steps, tokens, finished_at = runs[0]
        assert run_kind == "agent"
        assert status == "success"
        assert steps == 2
        assert tokens == result.tokens_used
        assert finished_at

        rows = conn.execute(
            "SELECT step, kind, tool, args_json, result_digest, latency_ms "
            "FROM agent_steps ORDER BY step"
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [(0, "tool"), (1, "final")]
        tool_args = json.loads(rows[0][3])
        assert tool_args["arguments"] == {"code": "600519"}
        assert "1680.00" in tool_args["result_preview"]
        assert rows[0][2] == "lookup_price"
        assert rows[0][4]
        assert rows[0][5] is not None and rows[0][5] >= 0
    finally:
        conn.close()


def test_runtime_audits_invalid_arguments_as_steps(tmp_path: Path) -> None:
    from astock.agent import store

    registry = ToolRegistry()
    registry.register(price_spec(ok_price))
    model = FakeModel(
        [ToolCall("lookup_price", {}), ToolCall("lookup_price", {}), "不应到达"]
    )
    runtime = AgentRuntime(model, registry, db_path=tmp_path / "astock.db")

    result = runtime.run("查价格")

    assert result.stop_reason == StopReason.INVALID_ARGUMENTS
    conn = store.connect(tmp_path / "astock.db")
    try:
        rows = conn.execute(
            "SELECT step, kind, tool, args_json FROM agent_steps ORDER BY step"
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (0, "tool"),
            (1, "tool"),
            (2, "final"),
        ]
        tool_rows = [row for row in rows if row[1] == "tool"]
        assert all(row[2] == "lookup_price" for row in tool_rows)
        assert all(row[3] for row in tool_rows)
        status, = conn.execute("SELECT status FROM agent_runs").fetchone()
        assert status == "partial"
    finally:
        conn.close()


def test_tool_payload_is_wrapped_as_untrusted_data(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(price_spec(ok_price))
    model = FakeModel(
        [ToolCall("lookup_price", {"code": "600519"}), "完成"]
    )
    runtime = AgentRuntime(model, registry, db_path=tmp_path / "astock.db")

    runtime.run("查价格")

    tool_messages = [
        message for message in model.calls[1]["messages"] if message["role"] == "tool"
    ]
    assert len(tool_messages) == 1
    content = tool_messages[0]["content"]
    assert content.startswith("<untrusted_data>")
    assert content.rstrip().endswith("</untrusted_data>")
    assert "1680.00" in content


def test_runtime_records_model_provenance_in_run(tmp_path: Path) -> None:
    from astock.agent import store

    registry = ToolRegistry()
    registry.register(price_spec(ok_price))
    model = FakeModel([ToolCall("lookup_price", {"code": "600519"}), "完成"])
    runtime = AgentRuntime(
        model,
        registry,
        db_path=tmp_path / "astock.db",
        temperature=0.2,
        seed=11,
        code_version="astock-test",
    )

    runtime.run("查价格")

    conn = store.connect(tmp_path / "astock.db")
    try:
        row = conn.execute(
            "SELECT temperature, seed, code_version, prompt_tokens, completion_tokens "
            "FROM agent_runs"
        ).fetchone()
        assert abs(row["temperature"] - 0.2) < 1e-9
        assert row["seed"] == 11
        assert row["code_version"] == "astock-test"
        # 整轮总 token 记 completion，prompt 保持 0（P2-4 语义）。
        assert row["prompt_tokens"] == 0
        assert row["completion_tokens"] >= 0
    finally:
        conn.close()


def test_step_failure_still_finishes_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astock.agent import store

    def broken_step(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_step", broken_step)
    registry = ToolRegistry()
    registry.register(price_spec(ok_price))
    model = FakeModel([ToolCall("lookup_price", {"code": "600519"}), "完成"])
    runtime = AgentRuntime(model, registry, db_path=tmp_path / "astock.db")

    result = runtime.run("查价格")

    assert result.stop_reason == StopReason.COMPLETED
    conn = store.connect(tmp_path / "astock.db")
    try:
        row = conn.execute("SELECT status, finished_at, error FROM agent_runs").fetchone()
        assert row["status"] == "failed"
        assert row["finished_at"]
        assert "record_step" in (row["error"] or "")
    finally:
        conn.close()


def test_finish_run_failure_falls_back_to_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astock.agent import store

    def broken_finish(*args: Any, **kwargs: Any) -> str:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "finish_run", broken_finish)
    registry = ToolRegistry()
    registry.register(price_spec(ok_price))
    model = FakeModel([ToolCall("lookup_price", {"code": "600519"}), "完成"])
    runtime = AgentRuntime(model, registry, db_path=tmp_path / "astock.db")

    runtime.run("查价格")

    conn = store.connect(tmp_path / "astock.db")
    try:
        row = conn.execute("SELECT status, finished_at, error FROM agent_runs").fetchone()
        assert row["status"] == "failed"
        assert row["finished_at"]
        assert "finish_run failed" in (row["error"] or "")
    finally:
        conn.close()


def test_failed_run_is_not_downgraded_by_runtime_finish(tmp_path: Path) -> None:
    from astock.agent import store

    db_path = tmp_path / "astock.db"

    def explode(*, code: str) -> ToolResult:
        del code
        conn = store.connect(db_path)
        try:
            run_id = conn.execute("SELECT run_id FROM agent_runs").fetchone()[0]
            store.fail_run(conn, run_id, error="PNG 导出失败（png_mode=require）")
        finally:
            conn.close()
        raise RuntimeError("PNG 导出失败（png_mode=require）")

    registry = ToolRegistry()
    registry.register(price_spec(explode))
    model = FakeModel([ToolCall("lookup_price", {"code": "600519"}), "失败后继续"])
    runtime = AgentRuntime(model, registry, db_path=db_path)

    runtime.run("画图")

    conn = store.connect(db_path)
    try:
        row = conn.execute("SELECT status, error FROM agent_runs").fetchone()
        assert row["status"] == "failed"
        assert "PNG 导出失败" in (row["error"] or "")
    finally:
        conn.close()


def test_runtime_observation_keeps_chart_html_out_of_context() -> None:
    """图表 HTML 只以 artifact 句柄进 observation，避免撑爆模型上下文。"""
    import json

    runtime = AgentRuntime(FakeModel(["ok"]), ToolRegistry())
    big = "<html>" + "x" * 5000 + "</html>"
    result = ToolResult(
        text="图已生成",
        figure=big,
        data_as_of="2024-01-04",
        data_time="2024-01-04 15:00",
        metadata={
            "chart": {
                "artifact_id": "art_0123456789abcdef",
                "artifact_path": "artifacts/kline.html",
            }
        },
    )

    observation = runtime._format_observation("kline", result)
    payload = json.loads(observation)
    assert payload["figure"] == {
        "chars": len(big),
        "artifact_id": "art_0123456789abcdef",
        "artifact_path": "artifacts/kline.html",
    }
    assert big not in observation
    assert payload["data_time"] == "2024-01-04 15:00"

    # 小图（例如内联 SVG 片段）仍原样透传。
    small = ToolResult(figure="<svg/>")
    assert json.loads(runtime._format_observation("kline", small))["figure"] == "<svg/>"


def test_summarize_result_keeps_chart_html_out_of_partial_answer() -> None:
    """部分答案的图表摘要与 observation 同口径：超长 HTML 只留 artifact 句柄。"""
    from astock.agent.runtime import _summarize_result

    big = "<html>" + "x" * 5000 + "</html>"
    result = ToolResult(
        figure=big,
        metadata={
            "chart": {
                "artifact_id": "art_0123456789abcdef",
                "artifact_path": "artifacts/kline.html",
            }
        },
    )
    summary = _summarize_result(result)
    assert "art_0123456789abcdef" in summary
    assert str(len(big)) in summary
    assert "<html>" not in summary

    # 短图（内联 SVG 等）保持原样，避免把正常小图也抽象掉。
    assert _summarize_result(ToolResult(figure="<svg/>")) == "图表 <svg/>"


def test_runtime_passes_run_context_so_chart_artifact_links_real_run(
    tmp_path: Path,
) -> None:
    """P1：runtime→tool 的 run 上下文让图表 link 挂到真实 run（source="agent"）。

    ``chart_service`` 只在 ``ToolContext`` 存在时记录 ``source="agent"`` + ``run_id``；
    旧实现里工具拿不到 run_id，图表永远落成 ``source="render"``、link.run_id=NULL。
    """
    from astock.agent import store
    from astock.agent.tools import market_tools

    code = "600519.SH"
    daily = pd.DataFrame(
        {
            "ts_code": code,
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "pre_close": [99.5, 100.5, 101.5],
            "change": [1.0, 1.0, 1.0],
            "pct_chg": [1.0, 1.0, 1.0],
            "vol": [1000.0, 1000.0, 1000.0],
            "amount": [5000.0, 5000.0, 5000.0],
        }
    )
    calendar = pd.DataFrame(
        {"cal_date": pd.bdate_range("2024-01-01", "2024-03-31"), "is_open": 1}
    )
    from astock import data as market

    deps = market_tools.MarketDeps(
        daily=lambda ts_code, *, start, end, adj: daily.copy(),
        index_daily=lambda ts_code, *, start, end: daily.copy(),
        minute=lambda ts_code, *, freq, days, adj: daily.copy(),
        resample=market.resample_ohlcv,
        bar_time=market.latest_bar_time,
        trade_cal=lambda: calendar,
        clock=lambda: pd.Timestamp("2024-01-04 16:30"),
    )
    db_path = str(tmp_path / "astock.db")
    artifact_root = str(tmp_path / "agent")
    registry = market_tools.build_market_registry(
        deps=deps, db_path=db_path, artifact_root=artifact_root
    )
    model = FakeModel(
        [
            ToolCall("kline", {"ts_code": code, "chart": True, "limit": 3}),
            "2024-01-04 收盘 102.50 元。",
        ]
    )
    runtime = AgentRuntime(model, registry, db_path=db_path)

    result = runtime.run("画个 K 线")

    assert result.stop_reason == StopReason.COMPLETED
    chart_meta = result.steps[0].result.metadata["chart"]
    conn = store.connect(db_path)
    try:
        run = conn.execute(
            "SELECT run_id, status FROM agent_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        links = conn.execute(
            "SELECT run_id, step, source FROM agent_run_artifacts "
            "WHERE artifact_id = ?",
            (chart_meta["artifact_id"],),
        ).fetchall()
    finally:
        conn.close()

    assert run is not None
    assert (run["status"], len(links)) == ("success", 1)
    assert (links[0]["run_id"], links[0]["step"], links[0]["source"]) == (
        run["run_id"],
        0,
        "agent",
    )


if __name__ == "__main__":
    unittest.main()
