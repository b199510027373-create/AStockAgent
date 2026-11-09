"""供应商无关的模型网关与工具调用协议（ADR-014）。

业务代码只依赖 :class:`ChatModel` / :class:`ChatGateway`，不直接 import
任何模型供应商 SDK。网关负责：

- 统一 OpenAI ``tools`` / ``tool_calls`` 解析；
- 对不支持原生 function calling 的模型退化为 JSON 工具协议，失败重试一次；
- 无 key、请求失败或协议解析失败时使用 :class:`RuleFallback`，保证流程不崩；
- 每次调用（含探针与兜底）记录 provider/model/token 用量；
- 启动时可用 :meth:`ChatGateway.probe` 探测能力，失败标记 ``degraded`` 并给出中文提示。

配置全部来自 ``.env`` / 环境变量，见 :class:`GatewaySettings`。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self, runtime_checkable

import httpx

from astock.agent.types import ModelReply, ToolCall
from astock.config import BASE_DIR

DEFAULT_PROVIDER = "deepseek"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAX_STEPS = 12
DEFAULT_TOKEN_BUDGET = 64_000
DEFAULT_TIMEOUT_SECONDS = 60.0
PROBE_TOOL_NAME = "gateway_probe"
PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PROBE_TOOL_NAME,
        "description": "模型网关能力探测：原样回显 ping 参数。",
        "parameters": {
            "type": "object",
            "properties": {
                "ping": {"type": "string", "description": "固定为 ok"},
            },
            "required": ["ping"],
        },
    },
}

JSON_PROTOCOL_INSTRUCTION = (
    "当前模型不支持原生 function calling，请使用 JSON 工具协议。\n"
    "只输出一个 JSON 对象，不要输出 Markdown 代码块、解释或额外文字。\n"
    '需要调用工具时输出：{"tool": "工具名", "args": {...}}\n'
    '已经可以回答时输出：{"answer": "最终回答"}\n'
    "可用工具（JSON Schema）："
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


class GatewayError(RuntimeError):
    """模型网关的基类异常。"""


class GatewayConfigError(GatewayError, ValueError):
    """配置缺失或不合法。"""


class ChatProtocolError(GatewayError):
    """模型返回内容不符合约定的工具调用协议。"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _truncate(text: Any, limit: int = 300) -> str:
    value = str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def _env_value(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key) or "").strip()


def _env_int(env: Mapping[str, str], key: str, default: int, minimum: int = 1) -> int:
    raw = _env_value(env, key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是整数，当前为 {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{key} 必须大于等于 {minimum}，当前为 {value}")
    return value


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = _env_value(env, key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是数字，当前为 {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{key} 必须大于 0，当前为 {value}")
    return value


@dataclass(frozen=True)
class UsageRecord:
    """一次模型调用的用量记录。"""

    provider: str
    model: str
    protocol: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    finish_reason: str = ""
    elapsed_ms: float = 0.0
    ok: bool = True
    error: str | None = None
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "protocol": self.protocol,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "finish_reason": self.finish_reason,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "ok": self.ok,
            "error": self.error,
            "created_at": self.created_at,
        }


class UsageLogger:
    """线程安全的内存用量账本，可选追加写入 JSONL。

    文件写入失败不会打断模型调用，错误记录在 ``last_write_error``。
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        sink: Callable[[UsageRecord], None] | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.sink = sink
        self._records: list[UsageRecord] = []
        self._lock = threading.Lock()
        self.last_write_error: str | None = None

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def record(self, record: UsageRecord) -> None:
        with self._lock:
            self._records.append(record)
            if self.path is not None:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
                        )
                    self.last_write_error = None
                except OSError as exc:  # 日志失败不应影响主流程
                    self.last_write_error = f"{type(exc).__name__}: {exc}"
        if self.sink is not None:
            try:
                self.sink(record)
            except Exception:  # noqa: BLE001, S110 - 外部回调失败不应影响主流程
                pass

    def summary(self) -> dict[str, int]:
        records = self.records
        return {
            "calls": len(records),
            "ok": sum(1 for record in records if record.ok),
            "failed": sum(1 for record in records if not record.ok),
            "prompt_tokens": sum(record.prompt_tokens for record in records),
            "completion_tokens": sum(record.completion_tokens for record in records),
            "total_tokens": sum(record.total_tokens for record in records),
        }


@dataclass(frozen=True)
class ChatResult:
    """统一的一次模型回复。"""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    provider: str = ""
    protocol: str = "tools"
    degraded: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            object.__setattr__(self, "content", str(self.content))
        if not isinstance(self.tool_calls, tuple):
            object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        for call in self.tool_calls:
            if not isinstance(call, ToolCall):
                raise TypeError(
                    f"tool_calls 只能包含 ToolCall，收到 {type(call).__name__}"
                )
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数，收到 {value!r}")
        if self.total_tokens == 0:
            computed = self.prompt_tokens + self.completion_tokens
            if computed:
                object.__setattr__(self, "total_tokens", computed)
        if not isinstance(self.raw, Mapping):
            object.__setattr__(self, "raw", {})

    @property
    def tool_call(self) -> ToolCall | None:
        return self.tool_calls[0] if self.tool_calls else None

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@runtime_checkable
class ChatModel(Protocol):
    """供应商无关的聊天模型接口。"""

    provider: str
    model: str

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> ChatResult: ...


@dataclass(frozen=True)
class CapabilityStatus:
    """启动能力探测结果，供 UI 提示。"""

    provider: str
    model: str
    function_calling: bool
    degraded: bool
    reason: str | None = None
    probe_passed: bool | None = None

    @property
    def message(self) -> str:
        if self.degraded:
            return f"模型能力降级：{self.reason or '未知原因'}"
        if self.function_calling:
            return "模型能力正常：支持 function calling"
        return "模型能力正常：使用 JSON 工具协议"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "function_calling": self.function_calling,
            "degraded": self.degraded,
            "reason": self.reason,
            "probe_passed": self.probe_passed,
            "message": self.message,
        }


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, Mapping):
                text = part.get("text")
                if text is not None:
                    parts.append(str(text))
        return "".join(parts)
    return str(content)


def _join_chat_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise GatewayConfigError("AGENT_BASE_URL 不能为空")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def to_openai_messages(
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把 AgentSession 内部消息转换成 OpenAI Chat Completions 线格式。

    ``AgentSession`` 用 ``tool_call`` 记录助手发起的调用，用 ``name`` 标记
    工具结果；OpenAI 协议要求 ``assistant.tool_calls`` 与 ``tool.tool_call_id``
    成对出现。网关在发请求前统一补齐 id，避免运行时与供应商协议耦合。
    """

    wire: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    counter = 0
    for raw in messages:
        if not isinstance(raw, Mapping):
            raise ChatProtocolError(f"消息必须是 Mapping，收到 {type(raw).__name__}")
        role = str(raw.get("role") or "")
        content = raw.get("content")
        content_text = "" if content is None else str(content)

        if role == "assistant":
            calls_raw = raw.get("tool_calls")
            single_call = raw.get("tool_call")
            calls: list[dict[str, Any]] = []
            if isinstance(calls_raw, list) and calls_raw:
                for item in calls_raw:
                    if not isinstance(item, Mapping):
                        continue
                    function = item.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    name = str(function.get("name") or "").strip()
                    if not name:
                        continue
                    arguments = function.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(
                            arguments, ensure_ascii=False, default=str
                        )
                    counter += 1
                    call_id = str(item.get("id") or f"call_{counter}")
                    calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    )
                    pending_ids.append(call_id)
            elif isinstance(single_call, Mapping):
                name = str(single_call.get("name") or "").strip()
                if not name:
                    raise ChatProtocolError("assistant.tool_call 缺少工具名")
                arguments = single_call.get("arguments", {})
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, default=str)
                counter += 1
                call_id = f"call_{counter}"
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                )
                pending_ids = [call_id]

            if calls:
                wire.append(
                    {
                        "role": "assistant",
                        "content": content_text,
                        "tool_calls": calls,
                    }
                )
            else:
                wire.append({"role": "assistant", "content": content_text})
                pending_ids = []
            continue

        if role == "tool":
            raw_call_id = raw.get("tool_call_id")
            if not raw_call_id:
                raw_call_id = pending_ids[0] if pending_ids else "call_1"
            wire.append(
                {
                    "role": "tool",
                    "content": content_text,
                    "tool_call_id": str(raw_call_id),
                }
            )
            if pending_ids:
                pending_ids.pop(0)
            continue

        wire.append({"role": role, "content": content_text})
    return wire


class OpenAICompatibleAdapter:
    """基于 OpenAI Chat Completions 协议的适配器（DeepSeek/Ollama/vLLM）。

    直接使用 httpx 而不是供应商 SDK，确保业务代码与供应商实现解耦。
    """

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not str(base_url or "").strip():
            raise GatewayConfigError("AGENT_BASE_URL 不能为空")
        if not str(model or "").strip():
            raise GatewayConfigError("AGENT_MODEL 不能为空")
        self.base_url = str(base_url).strip().rstrip("/")
        self.model = str(model).strip()
        self.api_key = str(api_key or "").strip()
        self.timeout = float(timeout)
        self.provider = getattr(type(self), "provider", "openai_compatible")
        self._client = client or httpx.Client(timeout=self.timeout)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "astock-agent/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        temperature: float,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "temperature": float(temperature),
        }
        tool_list = list(tools or [])
        if tool_list:
            payload["tools"] = tool_list
            payload["tool_choice"] = "auto"
        return payload

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> ChatResult:
        url = _join_chat_url(self.base_url)
        payload = self._payload(messages, tools, temperature)
        try:
            response = self._client.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ChatProtocolError(
                f"{self.provider} 请求失败：{type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise ChatProtocolError(
                f"{self.provider} HTTP {response.status_code}: "
                f"{_truncate(response.text, 500)}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ChatProtocolError(
                f"{self.provider} 返回非 JSON 响应：{_truncate(response.text, 500)}"
            ) from exc
        return self._parse_response(data)

    def _parse_response(self, data: Any) -> ChatResult:
        if not isinstance(data, Mapping):
            raise ChatProtocolError("模型响应顶层不是 JSON 对象")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ChatProtocolError("模型响应缺少 choices")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ChatProtocolError("模型响应 choices[0] 不是对象")
        message = choice.get("message")
        if not isinstance(message, Mapping):
            message = {}
        content = _content_text(message.get("content", choice.get("text")))

        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ChatProtocolError("message.tool_calls 不是数组")
        for item in raw_calls:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ChatProtocolError("tool_calls 缺少合法 function.name")
            arguments = function.get("arguments", "{}")
            if arguments is None:
                arguments = {}
            if isinstance(arguments, str):
                stripped = arguments.strip()
                if not stripped:
                    arguments = {}
                else:
                    try:
                        arguments = json.loads(stripped)
                    except json.JSONDecodeError as exc:
                        raise ChatProtocolError(
                            f"工具 {name} 的参数不是合法 JSON："
                            f"{_truncate(arguments, 300)}"
                        ) from exc
            if not isinstance(arguments, Mapping):
                raise ChatProtocolError(f"工具 {name} 的参数必须是 JSON 对象")
            tool_calls.append(ToolCall(name=name.strip(), arguments=arguments))

        usage = data.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}
        prompt_tokens = _as_int(usage.get("prompt_tokens"))
        completion_tokens = _as_int(usage.get("completion_tokens"))
        total_tokens = _as_int(
            usage.get("total_tokens"), prompt_tokens + completion_tokens
        )
        finish_reason = str(
            choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
        )
        return ChatResult(
            content=content,
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            model=str(data.get("model") or self.model),
            provider=self.provider,
            protocol="tools",
            raw=data,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class DeepSeekAdapter(OpenAICompatibleAdapter):
    """DeepSeek 适配器（OpenAI 兼容协议）。"""

    provider = "deepseek"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not str(api_key or "").strip():
            raise GatewayConfigError("DeepSeek 需要 AGENT_API_KEY 或 DEEPSEEK_API_KEY")
        super().__init__(
            base_url=base_url or DEFAULT_DEEPSEEK_BASE_URL,
            model=model or DEFAULT_DEEPSEEK_MODEL,
            api_key=api_key,
            timeout=timeout,
            client=client,
        )


def _collect_tool_observations(
    messages: Sequence[Mapping[str, Any]],
) -> list[tuple[str, Any]]:
    observations: list[tuple[str, Any]] = []
    try:
        reversed_messages = list(reversed(list(messages)))
    except TypeError:
        return observations
    for message in reversed_messages:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        name = str(message.get("name") or "tool")
        content = message.get("content")
        data: Any = content
        if isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith(("{", "[")):
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError:
                    data = content
        observations.append((name, data))
        if len(observations) >= 3:
            break
    observations.reverse()
    return observations


def _last_user_content(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(list(messages)):
        if isinstance(message, Mapping) and message.get("role") == "user":
            return str(message.get("content") or "").strip()
    return ""


class RuleFallback:
    """无 key / 全部失败时的规则化答复，保证 Agent 流程不崩。"""

    provider = "rule"
    model = "rule-fallback"

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> ChatResult:
        try:
            content = self._compose(messages)
        except Exception as exc:  # noqa: BLE001 - 兜底实现自身也必须安全
            content = f"规则兜底不可用：{type(exc).__name__}: {exc}"
        return ChatResult(
            content=content,
            finish_reason="stop",
            model=self.model,
            provider=self.provider,
            protocol="rule",
        )

    def _compose(self, messages: Sequence[Mapping[str, Any]]) -> str:
        observations = _collect_tool_observations(messages)
        if not observations:
            parts = [
                (
                    "当前没有可用的大模型（未配置 API Key 或调用失败），已启用规则兜底，"
                    "无法生成新的分析结论。"
                )
            ]
            question = _last_user_content(messages)
            if question:
                parts.append(f"收到的问题：{_truncate(question, 200)}")
            parts.append(
                "请检查 AGENT_PROVIDER / AGENT_API_KEY / AGENT_BASE_URL 配置后重试。"
            )
            return "\n".join(parts)

        parts = ["规则兜底：以下内容根据最近一次工具返回整理，未调用大模型。"]
        for name, data in observations[-2:]:
            if not isinstance(data, Mapping):
                parts.append(f"- {name}: {_truncate(data, 400)}")
                continue
            error = data.get("error")
            if error:
                parts.append(f"- {name} 执行失败：{_truncate(error, 400)}")
                continue
            text = data.get("text") or data.get("content")
            if text:
                parts.append(f"- {name}: {_truncate(text, 800)}")
            else:
                parts.append(f"- {name}: （无文本结果）")
            if data.get("data_as_of"):
                parts.append(f"  数据截至：{data['data_as_of']}")
            if data.get("source"):
                parts.append(f"  来源：{data['source']}")
            table = data.get("table")
            if isinstance(table, Mapping):
                rows = table.get("rows")
                if isinstance(rows, int):
                    parts.append(f"  表格行数：{rows}")
        return "\n".join(parts)


@dataclass(frozen=True)
class _JsonPayload:
    tool_call: ToolCall | None = None
    answer: str = ""


def _tool_payload(name: Any, arguments: Any) -> _JsonPayload:
    if not isinstance(name, str) or not name.strip():
        raise ChatProtocolError("JSON 工具调用缺少 tool/name")
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if not stripped:
            arguments = {}
        else:
            try:
                arguments = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ChatProtocolError(
                    f"工具 {name} 的 args 不是合法 JSON：{_truncate(arguments, 300)}"
                ) from exc
    if not isinstance(arguments, Mapping):
        raise ChatProtocolError(f"工具 {name} 的 args 必须是 JSON 对象")
    return _JsonPayload(tool_call=ToolCall(name=name.strip(), arguments=arguments))


def _parse_json_payload(text: str) -> _JsonPayload:
    raw = (text or "").strip()
    if not raw:
        raise ChatProtocolError("模型返回空内容，无法解析 JSON 工具协议")

    candidates: list[str] = [raw]
    fenced = _FENCE_RE.search(raw)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])

    data: Any = None
    last_error: Exception | None = None
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            last_error = exc
    if data is None:
        raise ChatProtocolError(
            f"模型输出不是合法 JSON：{_truncate(last_error or raw, 300)}"
        )
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, Mapping):
        raise ChatProtocolError("模型输出的 JSON 顶层必须是对象")

    calls = data.get("tool_calls")
    if isinstance(calls, list) and calls and isinstance(calls[0], Mapping):
        function = calls[0].get("function")
        if isinstance(function, Mapping):
            return _tool_payload(function.get("name"), function.get("arguments", "{}"))

    tool_name = data.get("tool") or data.get("name") or data.get("tool_name")
    if tool_name:
        arguments = data.get("args", data.get("arguments", data.get("parameters", {})))
        return _tool_payload(tool_name, arguments)

    for key in ("answer", "final", "final_answer", "content", "text", "response"):
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            continue
        answer = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str)
        )
        if answer.strip():
            return _JsonPayload(answer=answer)
    raise ChatProtocolError("JSON 中既没有 tool 也没有非空 answer")


def _with_json_instruction(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    try:
        schema = json.dumps(list(tools), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        schema = "[]"
    instruction = f"{JSON_PROTOCOL_INSTRUCTION}\n{schema}"
    result: list[dict[str, Any]] = [dict(message) for message in messages]
    if result and result[0].get("role") == "system":
        first = result[0]
        first["content"] = f"{first.get('content') or ''}\n\n{instruction}"
    else:
        result.insert(0, {"role": "system", "content": instruction})
    return result


class ChatGateway:
    """路由原生 tool_calls / JSON 协议 / 规则兜底，并记录每次调用。"""

    def __init__(
        self,
        primary: ChatModel,
        *,
        fallback: ChatModel | None = None,
        function_calling: bool | None = None,
        usage_logger: UsageLogger | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback or RuleFallback()
        if function_calling is None:
            function_calling = getattr(primary, "supports_function_calling", True)
        self.function_calling = bool(function_calling) and primary.provider != "rule"
        self.usage = usage_logger or UsageLogger()
        self.probe_passed: bool | None = None
        self.last_error: str | None = None
        self._probe_degraded = primary.provider == "rule"
        self._probe_reason = (
            "未配置可用大模型，使用规则兜底" if primary.provider == "rule" else None
        )
        self.degraded = self._probe_degraded
        self.degraded_reason = self._probe_reason

    @property
    def provider(self) -> str:
        return self.primary.provider

    @property
    def model(self) -> str:
        return self.primary.model

    # ---- 对外入口 ---------------------------------------------------------

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> ChatResult:
        message_list = list(messages)
        tool_list = list(tools or [])
        if self.primary.provider == "rule":
            return self._complete_rule(message_list, tool_list, temperature)
        if tool_list and not self.function_calling:
            return self._complete_json(message_list, tool_list, temperature)
        return self._complete_native(message_list, tool_list, temperature)

    def probe(self, *, force: bool = False) -> CapabilityStatus:
        """轻量能力探测：请求模型返回一个合法 tool_call。"""

        if self.probe_passed is not None and not force:
            return self._status()
        if self.primary.provider == "rule":
            self.probe_passed = False
            self.function_calling = False
            self._probe_degraded = True
            self._probe_reason = (
                self.degraded_reason or "未配置可用大模型，使用规则兜底"
            )
            self.degraded = True
            self.degraded_reason = self._probe_reason
            return self._status()

        messages = [
            {
                "role": "user",
                "content": (
                    "这是一次能力探测。请调用 gateway_probe 工具，参数 ping 为 ok；"
                    "不要直接回答。"
                ),
            }
        ]
        started = time.perf_counter()
        passed = False
        reason: str | None = None
        try:
            result = self.primary.complete(messages, [PROBE_TOOL], temperature=0.0)
        except Exception as exc:  # noqa: BLE001 - 探测失败不应打断启动
            elapsed = (time.perf_counter() - started) * 1000
            self._record_failure(
                self.primary.provider, self.primary.model, "probe", elapsed, exc
            )
            reason = f"能力探测请求失败：{type(exc).__name__}: {_truncate(exc, 200)}"
        else:
            elapsed = (time.perf_counter() - started) * 1000
            self._record(result, "probe", elapsed)
            passed = any(call.name == PROBE_TOOL_NAME for call in result.tool_calls)
            if not passed:
                reason = "能力探测未返回合法 tool_call，已退化为 JSON 工具协议"

        self.probe_passed = passed
        self.function_calling = passed
        self._probe_degraded = not passed
        self._probe_reason = reason
        self.degraded = not passed
        self.degraded_reason = reason
        return self._status()

    def status_note(self) -> str:
        if self.primary.provider == "rule":
            reason = self.degraded_reason or "未配置可用大模型"
            return f"规则兜底模式：{reason}，仅返回工具结果摘要。"
        parts: list[str] = []
        if self.degraded:
            parts.append(f"模型能力降级：{self.degraded_reason or '未知原因'}")
        if self.function_calling:
            parts.append("function calling 可用")
        else:
            parts.append("使用 JSON 工具协议")
        if self.last_error:
            parts.append(f"最近错误：{self.last_error}")
        return "；".join(parts)

    def mark_degraded(self, reason: str) -> None:
        self._probe_degraded = True
        self._probe_reason = reason
        self.degraded = True
        self.degraded_reason = reason

    def as_agent_model(self) -> AgentModelAdapter:
        return AgentModelAdapter(self)

    def close(self) -> None:
        seen: set[int] = set()
        for model in (self.primary, self.fallback):
            if id(model) in seen:
                continue
            seen.add(id(model))
            close = getattr(model, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- 路由实现 ---------------------------------------------------------

    def _complete_rule(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        temperature: float,
    ) -> ChatResult:
        started = time.perf_counter()
        try:
            result = self.primary.complete(messages, tools, temperature=temperature)
        except Exception as exc:  # noqa: BLE001 - 规则兜底自身异常也不能崩
            elapsed = (time.perf_counter() - started) * 1000
            self._record_failure("rule", "rule-fallback", "rule", elapsed, exc)
            return ChatResult(
                content="规则兜底不可用，请检查配置。",
                finish_reason="error",
                model="rule-fallback",
                provider="rule",
                protocol="rule",
                degraded=True,
            )
        elapsed = (time.perf_counter() - started) * 1000
        self._record(result, "rule", elapsed)
        return replace(result, protocol="rule", degraded=True)

    def _complete_native(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        temperature: float,
    ) -> ChatResult:
        protocol = "tools" if tools else "text"
        try:
            result, elapsed = self._invoke(messages, tools, temperature, protocol)
        except ChatProtocolError as exc:
            return self._fallback(messages, tools, exc)
        self._record(result, protocol, elapsed)
        self.last_error = None
        self.degraded = self._probe_degraded
        self.degraded_reason = self._probe_reason
        return result

    def _complete_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        temperature: float,
    ) -> ChatResult:
        first_messages = _with_json_instruction(messages, tools)
        try:
            first, first_elapsed = self._invoke(first_messages, [], temperature, "json")
        except ChatProtocolError as exc:
            return self._fallback(messages, tools, exc)

        try:
            payload = _parse_json_payload(first.content)
        except ChatProtocolError as first_error:
            self._record(first, "json", first_elapsed, ok=False, error=str(first_error))
            correction = [
                *messages,
                {"role": "assistant", "content": first.content},
                {
                    "role": "user",
                    "content": (
                        f"上一次输出无法解析为 JSON 工具协议：{first_error}。"
                        "请只输出一个合法 JSON 对象，不要输出其他文字。"
                    ),
                },
            ]
            retry_messages = _with_json_instruction(correction, tools)
            try:
                second, second_elapsed = self._invoke(
                    retry_messages, [], temperature, "json"
                )
            except ChatProtocolError as exc:
                return self._fallback(messages, tools, exc)
            try:
                payload = _parse_json_payload(second.content)
            except ChatProtocolError as second_error:
                self._record(
                    second, "json", second_elapsed, ok=False, error=str(second_error)
                )
                return self._fallback(messages, tools, second_error)
            self._record(second, "json", second_elapsed)
            self.last_error = None
            return replace(
                second,
                content=payload.answer,
                tool_calls=(payload.tool_call,) if payload.tool_call else (),
                finish_reason="tool_calls" if payload.tool_call else "stop",
                protocol="json",
                degraded=self._probe_degraded,
            )

        self._record(first, "json", first_elapsed)
        self.last_error = None
        return replace(
            first,
            content=payload.answer,
            tool_calls=(payload.tool_call,) if payload.tool_call else (),
            finish_reason="tool_calls" if payload.tool_call else "stop",
            protocol="json",
            degraded=self._probe_degraded,
        )

    def _invoke(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        temperature: float,
        protocol: str,
    ) -> tuple[ChatResult, float]:
        started = time.perf_counter()
        try:
            result = self.primary.complete(messages, tools, temperature=temperature)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            self._record_failure(
                self.primary.provider, self.primary.model, protocol, elapsed, exc
            )
            raise ChatProtocolError(
                f"模型调用失败：{type(exc).__name__}: {_truncate(exc, 300)}"
            ) from exc
        elapsed = (time.perf_counter() - started) * 1000
        if not isinstance(result, ChatResult):
            error = ChatProtocolError(
                f"模型返回类型错误：{type(result).__name__}，应为 ChatResult"
            )
            self._record_failure(
                self.primary.provider, self.primary.model, protocol, elapsed, error
            )
            raise error
        return result, elapsed

    def _fallback(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        cause: Exception,
    ) -> ChatResult:
        error_text = f"{type(cause).__name__}: {cause}"
        self.last_error = error_text
        self.degraded = True
        self.degraded_reason = (
            f"最近一次模型调用失败，已使用规则兜底：{_truncate(cause, 200)}"
        )
        started = time.perf_counter()
        try:
            result = self.fallback.complete(messages, tools, temperature=0.2)
        except Exception as exc:  # noqa: BLE001 - 最后一道安全网
            elapsed = (time.perf_counter() - started) * 1000
            provider = getattr(self.fallback, "provider", "rule")
            model = getattr(self.fallback, "model", "fallback")
            self._record_failure(provider, model, "rule", elapsed, exc)
            return ChatResult(
                content=f"模型与规则兜底均不可用：{_truncate(exc, 300)}",
                finish_reason="error",
                model=str(model),
                provider=str(provider),
                protocol="rule",
                degraded=True,
            )
        elapsed = (time.perf_counter() - started) * 1000
        self._record(result, "rule", elapsed)
        content = result.content
        if isinstance(cause, ChatProtocolError) and "JSON" in str(cause):
            content = f"模型输出无法解析为工具协议，已启用规则兜底。\n{content}"
        return replace(result, content=content, degraded=True)

    # ---- 用量与状态 -------------------------------------------------------

    def _record(
        self,
        result: ChatResult,
        protocol: str,
        elapsed_ms: float,
        *,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        self.usage.record(
            UsageRecord(
                provider=result.provider or self.primary.provider,
                model=result.model or self.primary.model,
                protocol=protocol,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                finish_reason=result.finish_reason,
                elapsed_ms=elapsed_ms,
                ok=ok,
                error=error,
            )
        )

    def _record_failure(
        self,
        provider: str,
        model: str,
        protocol: str,
        elapsed_ms: float,
        exc: Exception,
    ) -> None:
        self.usage.record(
            UsageRecord(
                provider=provider,
                model=model,
                protocol=protocol,
                finish_reason="error",
                elapsed_ms=elapsed_ms,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        )

    def _status(self) -> CapabilityStatus:
        return CapabilityStatus(
            provider=self.primary.provider,
            model=self.primary.model,
            function_calling=self.function_calling,
            degraded=self.degraded,
            reason=self.degraded_reason,
            probe_passed=self.probe_passed,
        )


class AgentModelAdapter:
    """把 :class:`ChatGateway` 适配成 :class:`AgentRuntime` 需要的 ModelReply 接口。"""

    def __init__(self, gateway: ChatGateway) -> None:
        self.gateway = gateway

    @property
    def provider(self) -> str:
        return self.gateway.provider

    @property
    def model(self) -> str:
        return self.gateway.model

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> ModelReply:
        result = self.gateway.complete(messages, tools)
        return ModelReply(
            content=result.content,
            tool_call=result.tool_call,
            tokens=result.total_tokens,
        )

    def status_note(self) -> str:
        return self.gateway.status_note()

    def close(self) -> None:
        self.gateway.close()


@dataclass(frozen=True)
class GatewaySettings:
    """模型网关配置，全部来自 ``.env`` / 环境变量。"""

    provider: str = DEFAULT_PROVIDER
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    max_steps: int = DEFAULT_MAX_STEPS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    usage_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", str(self.provider or "").strip().lower())
        for name in ("model", "base_url", "api_key"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        if not self.provider:
            raise ValueError("AGENT_PROVIDER 不能为空")
        if self.max_steps < 1:
            raise ValueError(f"AGENT_MAX_STEPS 必须大于等于 1，当前为 {self.max_steps}")
        if self.token_budget < 1:
            raise ValueError(
                f"AGENT_TOKEN_BUDGET 必须大于等于 1，当前为 {self.token_budget}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"AGENT_TIMEOUT_SECONDS 必须大于 0，当前为 {self.timeout_seconds}"
            )
        if self.usage_path is not None:
            object.__setattr__(self, "usage_path", Path(self.usage_path))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewaySettings:
        source: Mapping[str, str] = os.environ if env is None else env
        provider = _env_value(source, "AGENT_PROVIDER") or DEFAULT_PROVIDER
        model = _env_value(source, "AGENT_MODEL")
        base_url = _env_value(source, "AGENT_BASE_URL")
        api_key = _env_value(source, "AGENT_API_KEY") or _env_value(
            source, "DEEPSEEK_API_KEY"
        )
        max_steps = _env_int(source, "AGENT_MAX_STEPS", DEFAULT_MAX_STEPS)
        token_budget = _env_int(source, "AGENT_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET)
        timeout_seconds = _env_float(
            source, "AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        )
        usage_raw = _env_value(source, "AGENT_USAGE_LOG")
        usage_path = Path(usage_raw) if usage_raw else None
        if usage_path is not None and not usage_path.is_absolute():
            usage_path = BASE_DIR / usage_path
        return cls(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_steps=max_steps,
            token_budget=token_budget,
            timeout_seconds=timeout_seconds,
            usage_path=usage_path,
        )


def build_gateway(
    settings: GatewaySettings | None = None,
    *,
    probe: bool = False,
    usage_logger: UsageLogger | None = None,
    http_client: httpx.Client | None = None,
) -> ChatGateway:
    """按配置构建网关；业务代码只需调用本函数或 :func:`create_agent_model`。"""

    settings = settings or GatewaySettings.from_env()
    logger = usage_logger or UsageLogger(settings.usage_path)
    provider = settings.provider

    if provider == "rule":
        gateway = ChatGateway(
            RuleFallback(),
            fallback=RuleFallback(),
            function_calling=False,
            usage_logger=logger,
        )
        if probe:
            gateway.probe()
        return gateway

    if provider == "deepseek":
        adapter: ChatModel
        if not settings.api_key:
            gateway = ChatGateway(
                RuleFallback(),
                fallback=RuleFallback(),
                function_calling=False,
                usage_logger=logger,
            )
            gateway.mark_degraded(
                "未配置 AGENT_API_KEY/DEEPSEEK_API_KEY，已使用规则兜底"
            )
            if probe:
                gateway.probe()
            return gateway
        adapter = DeepSeekAdapter(
            api_key=settings.api_key,
            model=settings.model or DEFAULT_DEEPSEEK_MODEL,
            base_url=settings.base_url or DEFAULT_DEEPSEEK_BASE_URL,
            timeout=settings.timeout_seconds,
            client=http_client,
        )
        gateway = ChatGateway(adapter, usage_logger=logger, function_calling=True)
        if probe:
            gateway.probe()
        return gateway

    if provider == "openai_compatible":
        if not settings.base_url:
            raise GatewayConfigError(
                "AGENT_PROVIDER=openai_compatible 时必须配置 AGENT_BASE_URL"
            )
        if not settings.model:
            raise GatewayConfigError(
                "AGENT_PROVIDER=openai_compatible 时必须配置 AGENT_MODEL"
            )
        adapter = OpenAICompatibleAdapter(
            base_url=settings.base_url,
            model=settings.model,
            api_key=settings.api_key,
            timeout=settings.timeout_seconds,
            client=http_client,
        )
        gateway = ChatGateway(adapter, usage_logger=logger, function_calling=True)
        if probe:
            gateway.probe()
        return gateway

    raise GatewayConfigError(
        f"未知的 AGENT_PROVIDER {settings.provider!r}，"
        "可选 value: deepseek / openai_compatible / rule"
    )


def create_agent_model(
    settings: GatewaySettings | None = None,
    *,
    probe: bool = True,
    usage_logger: UsageLogger | None = None,
    http_client: httpx.Client | None = None,
) -> AgentModelAdapter:
    """业务侧唯一入口：读 .env、构建网关并（默认）做启动能力探测。"""

    gateway = build_gateway(
        settings,
        probe=probe,
        usage_logger=usage_logger,
        http_client=http_client,
    )
    return AgentModelAdapter(gateway)


__all__ = [
    "DEFAULT_DEEPSEEK_BASE_URL",
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_PROVIDER",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_BUDGET",
    "JSON_PROTOCOL_INSTRUCTION",
    "PROBE_TOOL",
    "PROBE_TOOL_NAME",
    "AgentModelAdapter",
    "CapabilityStatus",
    "ChatGateway",
    "ChatModel",
    "ChatProtocolError",
    "ChatResult",
    "DeepSeekAdapter",
    "GatewayConfigError",
    "GatewayError",
    "GatewaySettings",
    "OpenAICompatibleAdapter",
    "RuleFallback",
    "UsageLogger",
    "UsageRecord",
    "build_gateway",
    "create_agent_model",
    "to_openai_messages",
]
