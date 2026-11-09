"""进程内 Agent 骨架（ADR-013）。

公共入口：

- :class:`AgentRuntime`：plan→act→observe→reflect 多步循环与护栏；
- :class:`ToolRegistry` / :class:`ToolSpec`：白名单工具、JSON Schema、权限标签；
- :class:`AgentSession` / :class:`WorkingMemory`：最近 20 轮与工作记忆；
- :class:`FakeModel`：离线测试模型；
- :class:`ChatGateway` / :class:`DeepSeekAdapter` / :class:`RuleFallback`：
  供应商无关模型网关（ADR-014）；
- :class:`ToolResult` / :class:`AgentRunResult` 等共享类型。
"""

from astock.agent.fake import FakeModel
from astock.agent.gateway import (
    AgentModelAdapter,
    CapabilityStatus,
    ChatGateway,
    ChatModel,
    ChatProtocolError,
    ChatResult,
    DeepSeekAdapter,
    GatewayConfigError,
    GatewayError,
    GatewaySettings,
    OpenAICompatibleAdapter,
    RuleFallback,
    UsageLogger,
    UsageRecord,
    build_gateway,
    create_agent_model,
    to_openai_messages,
)
from astock.agent.registry import (
    SAFE_PERMISSIONS,
    Permission,
    ToolArgumentError,
    ToolNotFoundError,
    ToolRegistrationError,
    ToolRegistry,
    ToolSpec,
    apply_defaults,
    validate_tool_arguments,
)
from astock.agent.runtime import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TOKEN_BUDGET,
    SYSTEM_PROMPT,
    AgentModel,
    AgentRuntime,
)
from astock.agent.session import DEFAULT_MAX_TURNS, AgentSession, WorkingMemory
from astock.agent.types import (
    MAX_TABLE_ROWS,
    MAX_TEXT_CHARS,
    AgentRunResult,
    ModelReply,
    StepRecord,
    StopReason,
    ToolCall,
    ToolResult,
)

__all__ = [
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_BUDGET",
    "MAX_TABLE_ROWS",
    "MAX_TEXT_CHARS",
    "SAFE_PERMISSIONS",
    "SYSTEM_PROMPT",
    "AgentModel",
    "AgentModelAdapter",
    "AgentRunResult",
    "AgentRuntime",
    "AgentSession",
    "CapabilityStatus",
    "ChatGateway",
    "ChatModel",
    "ChatProtocolError",
    "ChatResult",
    "DeepSeekAdapter",
    "FakeModel",
    "GatewayConfigError",
    "GatewayError",
    "GatewaySettings",
    "ModelReply",
    "OpenAICompatibleAdapter",
    "Permission",
    "RuleFallback",
    "StepRecord",
    "StopReason",
    "ToolArgumentError",
    "ToolCall",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "UsageLogger",
    "UsageRecord",
    "WorkingMemory",
    "apply_defaults",
    "build_gateway",
    "create_agent_model",
    "to_openai_messages",
    "validate_tool_arguments",
]
