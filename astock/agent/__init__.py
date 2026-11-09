"""进程内 Agent 骨架（ADR-013）。

公共入口：

- :class:`AgentRuntime`：plan→act→observe→reflect 多步循环与护栏；
- :class:`ToolRegistry` / :class:`ToolSpec`：白名单工具、JSON Schema、权限标签；
- :class:`AgentSession` / :class:`WorkingMemory`：最近 20 轮与工作记忆；
- :class:`FakeModel`：离线测试模型；
- :class:`ToolResult` / :class:`AgentRunResult` 等共享类型。
"""

from astock.agent.fake import FakeModel
from astock.agent.registry import (
    Permission,
    SAFE_PERMISSIONS,
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
    "AgentModel",
    "AgentRunResult",
    "AgentRuntime",
    "AgentSession",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_BUDGET",
    "FakeModel",
    "MAX_TABLE_ROWS",
    "MAX_TEXT_CHARS",
    "ModelReply",
    "Permission",
    "SAFE_PERMISSIONS",
    "SYSTEM_PROMPT",
    "StepRecord",
    "StopReason",
    "ToolArgumentError",
    "ToolCall",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "WorkingMemory",
    "apply_defaults",
    "validate_tool_arguments",
]
