"""模型网关与工具调用协议测试（ADR-014）。

全部离线：HTTP 适配器通过 ``httpx.MockTransport`` 注入，规则兜底不访问网络。
"""

from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from typing import Any

import httpx

from astock.agent.gateway import (
    DEFAULT_USAGE_PATH,
    PROBE_TOOL_NAME,
    ChatGateway,
    ChatProtocolError,
    ChatResult,
    DeepSeekAdapter,
    GatewayConfigError,
    GatewaySettings,
    OpenAICompatibleAdapter,
    RuleFallback,
    UsageLogger,
    build_gateway,
    create_agent_model,
    to_json_protocol_messages,
    to_openai_messages,
)
from astock.agent.registry import Permission, ToolRegistry, ToolSpec
from astock.agent.runtime import AgentRuntime
from astock.agent.session import AgentSession
from astock.agent.types import ModelReply, ToolCall, ToolResult
from astock.config import BASE_DIR

TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lookup_price",
        "description": "查询股票最新价格",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
}


def _completion(
    *,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int | None = None,
    model: str = "stub-model",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": (
                total_tokens
                if total_tokens is not None
                else prompt_tokens + completion_tokens
            ),
        },
    }


def _result(
    content: str = "",
    *,
    tool_calls: tuple[ToolCall, ...] = (),
    total_tokens: int = 0,
    provider: str = "stub",
    model: str = "stub-1",
    finish_reason: str = "stop",
) -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=tool_calls,
        total_tokens=total_tokens,
        provider=provider,
        model=model,
        finish_reason=finish_reason,
    )


class StubModel:
    provider = "stub"
    model = "stub-1"

    def __init__(
        self,
        replies: list[ChatResult] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> ChatResult:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": list(tools),
                "temperature": temperature,
            }
        )
        if self.error is not None:
            raise self.error
        if not self.replies:
            raise AssertionError("StubModel 没有剩余回复")
        return self.replies.pop(0)


class AdapterTests(unittest.TestCase):
    def test_deepseek_adapter_parses_tool_calls_and_usage(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=_completion(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup_price",
                                "arguments": '{"code": "600519"}',
                            },
                        }
                    ],
                    prompt_tokens=11,
                    completion_tokens=7,
                    total_tokens=18,
                ),
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = DeepSeekAdapter(api_key="sk-test", client=client)
        result = adapter.complete(
            [{"role": "user", "content": "查询 600519"}], [TOOL_SCHEMA]
        )

        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.model, "stub-model")
        self.assertEqual(result.total_tokens, 18)
        self.assertEqual(result.tool_call, ToolCall("lookup_price", {"code": "600519"}))
        self.assertEqual(result.protocol, "tools")

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer sk-test")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["tools"], [TOOL_SCHEMA])
        self.assertEqual(payload["tool_choice"], "auto")

    def test_openai_compatible_adapter_accepts_custom_base_and_no_key(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_completion(content="本地回答"))

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:11434/v1",
            model="qwen2.5",
            client=client,
        )
        result = adapter.complete([{"role": "user", "content": "hi"}], [])

        self.assertEqual(result.content, "本地回答")
        self.assertEqual(
            str(requests[0].url),
            "http://localhost:11434/v1/chat/completions",
        )
        self.assertNotIn("authorization", requests[0].headers)

    def test_adapter_normalizes_agent_session_tool_messages(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=_completion(content="ok"))

        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_call": {"name": "lookup_price", "arguments": {"code": "1"}},
            },
            {
                "role": "tool",
                "name": "lookup_price",
                "content": '{"ok": true, "text": "x"}',
            },
        ]
        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = DeepSeekAdapter(api_key="sk-test", client=client)
        adapter.complete(messages, [])

        payload = json.loads(requests[0].content)
        assistant = payload["messages"][2]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        self.assertEqual(
            json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
            {"code": "1"},
        )
        tool_message = payload["messages"][3]
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertNotIn("name", tool_message)

    def test_to_openai_messages_keeps_existing_tool_call_ids(self) -> None:
        wire = to_openai_messages(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "existing",
                            "type": "function",
                            "function": {"name": "x", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "content": "result"},
            ]
        )
        self.assertEqual(wire[0]["tool_calls"][0]["id"], "existing")
        self.assertEqual(wire[1]["tool_call_id"], "existing")

    def test_malformed_tool_arguments_raise_protocol_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_completion(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup_price",
                                "arguments": "{not json",
                            },
                        }
                    ]
                ),
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = DeepSeekAdapter(api_key="sk-test", client=client)
        with self.assertRaises(ChatProtocolError):
            adapter.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

    def test_deepseek_adapter_requires_key(self) -> None:
        with self.assertRaises(GatewayConfigError):
            DeepSeekAdapter(api_key="")


class RoutingTests(unittest.TestCase):
    def test_native_function_calling_path(self) -> None:
        stub = StubModel(
            [
                _result(
                    tool_calls=(ToolCall("lookup_price", {"code": "1"}),),
                    total_tokens=10,
                    finish_reason="tool_calls",
                )
            ]
        )
        logger = UsageLogger()
        gateway = ChatGateway(stub, usage_logger=logger)

        result = gateway.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

        self.assertEqual(stub.calls[0]["tools"], [TOOL_SCHEMA])
        self.assertEqual(result.tool_call, ToolCall("lookup_price", {"code": "1"}))
        self.assertEqual(result.protocol, "tools")
        record = logger.records[-1]
        self.assertEqual(record.provider, "stub")
        self.assertEqual(record.model, "stub-1")
        self.assertEqual(record.protocol, "tools")
        self.assertEqual(record.total_tokens, 10)
        self.assertTrue(record.ok)

    def test_json_protocol_used_when_function_calling_unsupported(self) -> None:
        stub = StubModel(
            [
                _result(
                    content='{"tool": "lookup_price", "args": {"code": "1"}}',
                    total_tokens=8,
                )
            ]
        )
        gateway = ChatGateway(stub, function_calling=False)

        result = gateway.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

        self.assertEqual(result.protocol, "json")
        self.assertEqual(result.tool_call, ToolCall("lookup_price", {"code": "1"}))
        self.assertEqual(stub.calls[0]["tools"], [])
        self.assertIn("JSON", stub.calls[0]["messages"][0]["content"])
        self.assertEqual(len(stub.calls), 1)

    def test_json_protocol_wire_history_has_no_native_tool_fields(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    200,
                    json=_completion(
                        content='{"tool": "lookup_price", "args": {"code": "600519"}}'
                    ),
                )
            return httpx.Response(
                200,
                json=_completion(content='{"answer": "600519 最新价 1500 元。"}'),
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = OpenAICompatibleAdapter(
            base_url="http://localhost:11434/v1",
            model="qwen2.5",
            client=client,
        )
        logger = UsageLogger()
        gateway = ChatGateway(adapter, function_calling=False, usage_logger=logger)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "查询 600519"},
        ]

        first = gateway.complete(messages, [TOOL_SCHEMA])
        self.assertEqual(first.tool_call, ToolCall("lookup_price", {"code": "600519"}))
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_call": {
                        "name": "lookup_price",
                        "arguments": {"code": "600519"},
                    },
                },
                {
                    "role": "tool",
                    "name": "lookup_price",
                    "content": '{"ok": true, "text": "最新价 1500 元"}',
                },
            ]
        )
        result = gateway.complete(messages, [TOOL_SCHEMA])

        self.assertEqual(result.content, "600519 最新价 1500 元。")
        self.assertEqual(result.protocol, "json")
        self.assertFalse(result.degraded)
        self.assertEqual(len(requests), 2)
        for request in requests:
            payload = json.loads(request.content)
            self.assertNotIn("tools", payload)
            for message in payload["messages"]:
                self.assertNotIn("tool_calls", message)
                self.assertNotIn("tool_call_id", message)
                self.assertNotEqual(message["role"], "tool")
        second = json.loads(requests[1].content)
        contents = [message["content"] for message in second["messages"]]
        self.assertTrue(any("调用工具 lookup_price" in text for text in contents))
        self.assertTrue(any("工具 lookup_price 返回" in text for text in contents))
        self.assertTrue(logger.records[-1].ok)

    def test_json_protocol_converts_tool_history_to_plain_text(self) -> None:
        converted = to_json_protocol_messages(
            [
                {
                    "role": "assistant",
                    "content": "先查价",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "lookup_price",
                                "arguments": '{"code": "600519"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "lookup_price",
                    "tool_call_id": "call_1",
                    "content": "1500",
                },
            ]
        )
        self.assertEqual(converted[0]["role"], "assistant")
        self.assertIn("先查价", converted[0]["content"])
        self.assertIn("调用工具 lookup_price", converted[0]["content"])
        self.assertNotIn("tool_calls", converted[0])
        self.assertEqual(converted[1]["role"], "user")
        self.assertIn("工具 lookup_price 返回：1500", converted[1]["content"])
        self.assertNotIn("tool_call_id", converted[1])

    def test_multiple_tool_calls_record_dropped_count(self) -> None:
        stub = StubModel(
            [
                _result(
                    tool_calls=(
                        ToolCall("lookup_price", {"code": "1"}),
                        ToolCall("lookup_price", {"code": "2"}),
                    ),
                    total_tokens=5,
                )
            ]
        )
        logger = UsageLogger()
        gateway = ChatGateway(stub, usage_logger=logger)

        result = gateway.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

        self.assertEqual(result.tool_call, ToolCall("lookup_price", {"code": "1"}))
        record = logger.records[-1]
        self.assertEqual(record.dropped_tool_calls, 1)
        self.assertEqual(record.to_dict()["dropped_tool_calls"], 1)
        self.assertIn("2 个工具调用", gateway.status_note())

    def test_json_protocol_retries_once_after_invalid_output(self) -> None:
        stub = StubModel(
            [
                _result(content="不是 JSON", total_tokens=5),
                _result(
                    content='```json\n{"answer": "最终回答"}\n```',
                    total_tokens=7,
                ),
            ]
        )
        logger = UsageLogger()
        gateway = ChatGateway(stub, function_calling=False, usage_logger=logger)

        result = gateway.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(result.content, "最终回答")
        self.assertEqual(result.protocol, "json")
        self.assertEqual(result.tool_calls, ())
        self.assertIn("无法解析", stub.calls[1]["messages"][-1]["content"])
        self.assertFalse(logger.records[0].ok)
        self.assertTrue(logger.records[1].ok)

    def test_json_protocol_twice_invalid_falls_back_without_crash(self) -> None:
        stub = StubModel(
            [
                _result(content="bad", total_tokens=3),
                _result(content="still bad", total_tokens=4),
            ]
        )
        logger = UsageLogger()
        gateway = ChatGateway(stub, function_calling=False, usage_logger=logger)

        result = gateway.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(result.provider, "rule")
        self.assertEqual(result.protocol, "rule")
        self.assertTrue(result.degraded)
        self.assertIn("规则兜底", result.content)
        self.assertFalse(logger.records[0].ok)
        self.assertFalse(logger.records[1].ok)
        self.assertTrue(logger.records[2].ok)
        self.assertIn("降级", gateway.status_note())

    def test_primary_failure_uses_rule_fallback(self) -> None:
        stub = StubModel(error=RuntimeError("boom"))
        logger = UsageLogger()
        gateway = ChatGateway(stub, usage_logger=logger)

        result = gateway.complete([{"role": "user", "content": "查询行情"}], [])

        self.assertEqual(result.provider, "rule")
        self.assertIn("规则兜底", result.content)
        self.assertIn("查询行情", result.content)
        self.assertEqual(len(logger.records), 2)
        self.assertFalse(logger.records[0].ok)
        self.assertIn("boom", logger.records[0].error or "")
        self.assertTrue(logger.records[1].ok)
        self.assertIn("boom", gateway.status_note())

    def test_probe_failure_marks_degraded_and_provides_prompt(self) -> None:
        stub = StubModel([_result(content="我会直接回答", total_tokens=5)])
        logger = UsageLogger()
        gateway = ChatGateway(stub, usage_logger=logger)

        status = gateway.probe()

        self.assertFalse(status.function_calling)
        self.assertTrue(status.degraded)
        self.assertFalse(status.probe_passed)
        self.assertIn("降级", status.message)
        self.assertIn("JSON", status.message)
        self.assertIn("降级", gateway.status_note())
        self.assertEqual(logger.records[-1].protocol, "probe")
        self.assertEqual(stub.calls[0]["tools"][0]["function"]["name"], PROBE_TOOL_NAME)

    def test_probe_success_keeps_tools_protocol(self) -> None:
        stub = StubModel(
            [
                _result(
                    tool_calls=(ToolCall(PROBE_TOOL_NAME, {"ping": "ok"}),),
                    total_tokens=6,
                    finish_reason="tool_calls",
                ),
                _result(
                    tool_calls=(ToolCall("lookup_price", {"code": "1"}),),
                    total_tokens=9,
                    finish_reason="tool_calls",
                ),
            ]
        )
        gateway = ChatGateway(stub)

        status = gateway.probe()
        result = gateway.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

        self.assertTrue(status.probe_passed)
        self.assertFalse(status.degraded)
        self.assertTrue(status.function_calling)
        self.assertEqual(stub.calls[0]["tools"][0]["function"]["name"], PROBE_TOOL_NAME)
        self.assertEqual(stub.calls[1]["tools"], [TOOL_SCHEMA])
        self.assertEqual(result.tool_call, ToolCall("lookup_price", {"code": "1"}))

    def test_probe_is_cached_unless_forced(self) -> None:
        stub = StubModel(
            [
                _result(content="no tool", total_tokens=1),
            ]
        )
        gateway = ChatGateway(stub)
        gateway.probe()
        gateway.probe()
        self.assertEqual(len(stub.calls), 1)

    def test_usage_logger_writes_jsonl(self) -> None:
        tmp_root = Path(__file__).resolve().parents[1] / ".tmp-tests"
        tmp_root.mkdir(parents=True, exist_ok=True)
        path = tmp_root / f"usage-{uuid.uuid4().hex}.jsonl"
        self.addCleanup(path.unlink, missing_ok=True)
        logger = UsageLogger(path)
        stub = StubModel(
            [
                _result(
                    tool_calls=(ToolCall("lookup_price", {"code": "1"}),),
                    total_tokens=12,
                )
            ]
        )
        gateway = ChatGateway(stub, usage_logger=logger)
        gateway.complete([{"role": "user", "content": "q"}], [TOOL_SCHEMA])

        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["provider"], "stub")
        self.assertEqual(data["model"], "stub-1")
        self.assertEqual(data["total_tokens"], 12)
        self.assertTrue(data["ok"])
        self.assertEqual(logger.summary()["calls"], 1)


class ConfigTests(unittest.TestCase):
    def test_from_env_reads_all_agent_keys(self) -> None:
        settings = GatewaySettings.from_env(
            {
                "AGENT_PROVIDER": "openai_compatible",
                "AGENT_MODEL": "qwen2.5",
                "AGENT_BASE_URL": "http://localhost:11434/v1",
                "AGENT_API_KEY": "local-key",
                "AGENT_MAX_STEPS": "5",
                "AGENT_TOKEN_BUDGET": "1234",
                "AGENT_TIMEOUT_SECONDS": "12.5",
                "AGENT_USAGE_LOG": "data/agent/usage.jsonl",
                "DEEPSEEK_API_KEY": "deepseek-key",
            }
        )
        self.assertEqual(settings.provider, "openai_compatible")
        self.assertEqual(settings.model, "qwen2.5")
        self.assertEqual(settings.base_url, "http://localhost:11434/v1")
        self.assertEqual(settings.api_key, "local-key")
        self.assertEqual(settings.max_steps, 5)
        self.assertEqual(settings.token_budget, 1234)
        self.assertEqual(settings.timeout_seconds, 12.5)
        self.assertEqual(
            settings.usage_path, BASE_DIR / "data" / "agent" / "usage.jsonl"
        )

    def test_from_env_falls_back_to_deepseek_key(self) -> None:
        settings = GatewaySettings.from_env({"DEEPSEEK_API_KEY": "dk"})
        self.assertEqual(settings.provider, "deepseek")
        self.assertEqual(settings.api_key, "dk")
        self.assertEqual(settings.max_steps, 12)
        self.assertEqual(settings.token_budget, 64000)

    def test_from_env_uses_default_usage_path(self) -> None:
        settings = GatewaySettings.from_env({})
        self.assertEqual(settings.usage_path, DEFAULT_USAGE_PATH)

    def test_from_env_rejects_invalid_integer(self) -> None:
        with self.assertRaises(ValueError):
            GatewaySettings.from_env({"AGENT_MAX_STEPS": "abc"})

    def test_unknown_provider_raises(self) -> None:
        with self.assertRaises(GatewayConfigError):
            build_gateway(GatewaySettings(provider="unknown"))

    def test_openai_compatible_requires_base_url_and_model(self) -> None:
        with self.assertRaises(GatewayConfigError):
            build_gateway(GatewaySettings(provider="openai_compatible", model="qwen"))
        with self.assertRaises(GatewayConfigError):
            build_gateway(
                GatewaySettings(
                    provider="openai_compatible",
                    base_url="http://localhost:11434/v1",
                )
            )

    def test_no_key_builds_rule_fallback(self) -> None:
        gateway = build_gateway(GatewaySettings(provider="deepseek", api_key=""))
        self.assertEqual(gateway.provider, "rule")
        self.assertTrue(gateway.degraded)
        self.assertIn("API_KEY", gateway.status_note())
        result = gateway.complete([{"role": "user", "content": "q"}], [])
        self.assertIn("规则兜底", result.content)

    def test_create_agent_model_uses_rule_without_key(self) -> None:
        model = create_agent_model(
            GatewaySettings(provider="deepseek", api_key=""), probe=True
        )
        self.assertEqual(model.provider, "rule")
        self.assertIn("规则兜底", model.status_note())
        self.assertIn("AGENT_API_KEY", model.status_note())


class RuleFallbackTests(unittest.TestCase):
    def test_summarizes_tool_observation(self) -> None:
        messages = [
            {"role": "user", "content": "查询 600519"},
            {
                "role": "tool",
                "name": "lookup_price",
                "content": json.dumps(
                    {
                        "tool": "lookup_price",
                        "ok": True,
                        "text": "最新价 1500 元",
                        "data_as_of": "2026-11-09",
                        "source": "tushare",
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        result = RuleFallback().complete(messages, [])
        self.assertEqual(result.provider, "rule")
        self.assertIn("lookup_price", result.content)
        self.assertIn("1500", result.content)
        self.assertIn("2026-11-09", result.content)
        self.assertIn("tushare", result.content)

    def test_summarizes_tool_error(self) -> None:
        messages = [
            {
                "role": "tool",
                "name": "lookup_price",
                "content": '{"ok": false, "error": "没有行情数据"}',
            }
        ]
        result = RuleFallback().complete(messages, [])
        self.assertIn("执行失败", result.content)
        self.assertIn("没有行情数据", result.content)

    def test_without_observation_mentions_configuration(self) -> None:
        result = RuleFallback().complete(
            [{"role": "user", "content": "今天大盘怎么样"}], []
        )
        self.assertIn("规则兜底", result.content)
        self.assertIn("AGENT_API_KEY", result.content)
        self.assertIn("今天大盘怎么样", result.content)


class RuntimeIntegrationTests(unittest.TestCase):
    def test_agent_model_adapter_drives_runtime(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="lookup_price",
                description="查询股票最新价格",
                parameters={
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
                handler=lambda code: ToolResult(
                    text=f"{code} 最新价 1500 元",
                    data_as_of="2026-11-09",
                    source="test",
                ),
                permissions=[Permission.MARKET_READ],
            )
        )
        stub = StubModel(
            [
                _result(
                    tool_calls=(ToolCall("lookup_price", {"code": "600519"}),),
                    total_tokens=6,
                    finish_reason="tool_calls",
                ),
                _result(content="600519 最新价 1500 元。", total_tokens=5),
            ]
        )
        gateway = ChatGateway(stub)
        adapter = gateway.as_agent_model()
        runtime = AgentRuntime(
            adapter,
            registry=registry,
            session=AgentSession(),
            max_steps=3,
            auto_save=False,
        )

        outcome = runtime.run("查询 600519 最新价")

        self.assertEqual(outcome.answer, "600519 最新价 1500 元。")
        self.assertEqual(len(outcome.steps), 1)
        self.assertEqual(outcome.steps[0].tool, "lookup_price")
        self.assertIn("1500", outcome.steps[0].result.text or "")
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(
            stub.calls[1]["messages"][-2]["tool_call"]["name"], "lookup_price"
        )
        self.assertEqual(stub.calls[1]["messages"][-1]["role"], "tool")

    def test_agent_model_adapter_returns_model_reply(self) -> None:
        gateway = ChatGateway(
            StubModel(
                [
                    _result(
                        tool_calls=(ToolCall("lookup_price", {"code": "1"}),),
                        total_tokens=4,
                    )
                ]
            )
        )
        reply = gateway.as_agent_model().complete(
            messages=[{"role": "user", "content": "q"}], tools=[TOOL_SCHEMA]
        )
        self.assertIsInstance(reply, ModelReply)
        self.assertEqual(reply.tool_call, ToolCall("lookup_price", {"code": "1"}))
        self.assertEqual(reply.tokens, 4)


if __name__ == "__main__":
    unittest.main()
