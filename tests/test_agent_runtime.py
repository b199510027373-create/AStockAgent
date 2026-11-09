"""Agent 运行时骨架的离线测试（ADR-013 验收）。

覆盖：正常 plan→act→observe→reflect、步数上限、token 预算、超时、中断、
打转保护、参数校验重试、工具报错、未知工具；以及工具注册表、会话裁剪与
JSON 落盘。
"""

from __future__ import annotations

import threading
import unittest
import uuid
from pathlib import Path
from typing import Any

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
from astock.agent.types import MAX_TABLE_ROWS, ToolResult as ResultAlias


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


if __name__ == "__main__":
    unittest.main()
