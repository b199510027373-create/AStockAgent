"""Agent 运行时共享数据类型（ADR-013）。

- :class:`ToolResult`：工具统一出口，``text`` / ``table`` / ``figure`` / ``error``
  四类载荷，并携带 ``data_as_of`` / ``data_time`` / ``source`` 供最终回答引用；
- :class:`ToolCall` / :class:`ModelReply`：模型选择工具或给出最终回答；
- :class:`StepRecord` / :class:`AgentRunResult`：单轮运行的审计轨迹与部分结果。

表格超过 :data:`MAX_TABLE_ROWS` 行时只保留前 200 行并标记 ``truncated``；
完整行由 :class:`astock.agent.registry.ToolRegistry` 的 artifact writer 落盘
（ADR-016 / ADR-017），运行时只把摘要回传给模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from typing import Any, Mapping

import pandas as pd

__all__ = [
    "MAX_TABLE_ROWS",
    "MAX_TEXT_CHARS",
    "StopReason",
    "ToolCall",
    "ToolResult",
    "ModelReply",
    "StepRecord",
    "AgentRunResult",
]

#: 单次工具结果允许进入模型上下文的表格行数上限。
MAX_TABLE_ROWS = 200

#: 单段文本进入 observation 时的截断长度（完整文本仍保留在 ToolResult 中）。
MAX_TEXT_CHARS = 4000


class StopReason(str, Enum):
    """一轮 Agent 运行结束的原因。"""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    TOKEN_BUDGET = "token_budget"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
    SPINNING = "spinning"
    INVALID_ARGUMENTS = "invalid_arguments"
    MODEL_ERROR = "model_error"


@dataclass(frozen=True)
class ToolCall:
    """模型发起的一次工具调用。"""

    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ToolCall.name 必须是非空字符串")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("ToolCall.arguments 必须是 Mapping")
        object.__setattr__(self, "arguments", dict(self.arguments))

    def signature(self) -> str:
        """用于打转保护的规范化签名（参数顺序无关）。"""
        return json.dumps(
            {"name": self.name, "arguments": dict(self.arguments)},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )


@dataclass
class ToolResult:
    """工具统一返回值。

    四类载荷互不排斥（例如一段文字 + 一张图），但 ``error`` 非空时视为失败。
    行情/图表结果必须带 ``data_as_of``（以及可得的 ``data_time``），文件结论必须
    带 ``source``（文件名 + 页码/工作表），由具体工具负责填充。
    """

    text: str = ""
    table: list[dict[str, Any]] | None = None
    figure: str | None = None
    error: str | None = None
    data_as_of: str | None = None
    data_time: str | None = None
    source: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error

    def __post_init__(self) -> None:
        for field_name in ("text", "figure", "error", "data_as_of", "data_time", "source"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"ToolResult.{field_name} 必须是字符串或 None")
        if not isinstance(self.truncated, bool):
            raise TypeError("ToolResult.truncated 必须是 bool")
        if self.table is not None:
            if not isinstance(self.table, list) or any(
                not isinstance(row, Mapping) for row in self.table
            ):
                raise TypeError("ToolResult.table 必须是 list[dict]")
            self.table = [dict(row) for row in self.table]
        if self.metadata is None:
            self.metadata = {}
        if not isinstance(self.metadata, Mapping):
            raise TypeError("ToolResult.metadata 必须是 Mapping")
        self.metadata = dict(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        """稳定的 JSON 友好表示；空字段省略，便于落审计与回传模型。"""
        payload: dict[str, Any] = {"ok": self.ok}
        if self.text:
            payload["text"] = self.text
        if self.table is not None:
            payload["table"] = self.table
        if self.figure:
            payload["figure"] = self.figure
        if self.error:
            payload["error"] = self.error
        if self.data_as_of:
            payload["data_as_of"] = self.data_as_of
        if self.data_time:
            payload["data_time"] = self.data_time
        if self.source:
            payload["source"] = self.source
        if self.truncated:
            payload["truncated"] = True
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, default=str
        )

    def trim_table(self, limit: int = MAX_TABLE_ROWS) -> "ToolResult":
        """按行数截断表格并记录被舍弃的行数（完整行由 artifact writer 落盘）。"""
        if self.table is None or len(self.table) <= limit:
            return self
        metadata = dict(self.metadata)
        metadata.setdefault("total_rows", len(self.table))
        metadata["dropped_rows"] = len(self.table) - limit
        return replace(
            self,
            table=[dict(row) for row in self.table[:limit]],
            truncated=True,
            metadata=metadata,
        )

    @classmethod
    def coerce(cls, value: Any) -> "ToolResult":
        """把工具处理函数的常见返回值规范化为 ToolResult。

        支持 :class:`ToolResult`、``str``、``list[dict]``、``DataFrame``、
        字段子集 ``Mapping``；其他值退化为 ``text=repr(value)``。
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(text=value)
        if isinstance(value, pd.DataFrame):
            return cls(
                table=value.to_dict(orient="records"),
                metadata={"columns": [str(c) for c in value.columns]},
            )
        if isinstance(value, Mapping):
            known = {item.name for item in fields(cls)}
            if set(value).issubset(known):
                return cls(**dict(value))
            return cls(text=json.dumps(value, ensure_ascii=False, default=str))
        if isinstance(value, (list, tuple)):
            if all(isinstance(row, Mapping) for row in value):
                return cls(table=[dict(row) for row in value])
            return cls(text=json.dumps(value, ensure_ascii=False, default=str))
        return cls(text=str(value))


@dataclass(frozen=True)
class ModelReply:
    """模型一次回复：要么调用一个工具，要么给出最终回答。"""

    content: str = ""
    tool_call: ToolCall | None = None
    tokens: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("ModelReply.content 必须是字符串")
        if not isinstance(self.tokens, int) or isinstance(self.tokens, bool):
            raise TypeError("ModelReply.tokens 必须是整数")
        if self.tokens < 0:
            raise ValueError("ModelReply.tokens 不能为负")
        if self.tool_call is not None and not isinstance(self.tool_call, ToolCall):
            raise TypeError("ModelReply.tool_call 必须是 ToolCall 或 None")


@dataclass
class StepRecord:
    """一步 plan→act→observe→reflect 的审计轨迹。"""

    index: int
    plan: str
    tool: str
    arguments: dict[str, Any]
    result: ToolResult
    observation: str
    reflection: str
    tokens: int
    elapsed: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "plan": self.plan,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "result": self.result.to_dict(),
            "observation": self.observation,
            "reflection": self.reflection,
            "tokens": self.tokens,
            "elapsed": round(self.elapsed, 4),
        }


@dataclass
class AgentRunResult:
    """一次 ``AgentRuntime.run`` 的完整结果。"""

    answer: str
    stop_reason: StopReason
    steps: list[StepRecord]
    tokens_used: int
    elapsed_seconds: float
    session_id: str
    error: str | None = None

    @property
    def partial(self) -> bool:
        """是否因限额/中断/异常而未拿到模型的最终回答。"""
        return self.stop_reason is not StopReason.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "answer": self.answer,
            "stop_reason": self.stop_reason.value,
            "partial": self.partial,
            "steps": [step.to_dict() for step in self.steps],
            "tokens_used": self.tokens_used,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "session_id": self.session_id,
        }
        if self.error:
            payload["error"] = self.error
        return payload
