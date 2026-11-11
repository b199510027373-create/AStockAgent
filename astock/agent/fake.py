"""离线 FakeModel：按脚本回放工具调用与最终回答（ADR-013 验收用）。

脚本元素可以是：

- :class:`ModelReply`：原样返回；
- :class:`ToolCall`：包装成 ``ModelReply(tool_call=...)``；
- ``str``：包装成最终回答；
- ``Mapping``：允许 ``{"content": ..., "tokens": ..., "tool_call": {...}}``。

脚本用尽后返回 ``default_answer``。所有调用都记录在 ``calls`` 中，供测试断言
运行时确实把工具 observation 回传给了模型。
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from astock.agent.types import ModelReply, ToolCall, ToolResult

__all__ = ["FakeModel"]


class FakeModel:
    """确定性的离线模型，不访问网络。"""

    def __init__(
        self,
        script: Iterable[ModelReply | ToolCall | str | Mapping[str, Any]] = (),
        *,
        default_answer: str = "（FakeModel 脚本已用尽）",
        tokens_per_reply: int = 0,
    ) -> None:
        if tokens_per_reply < 0:
            raise ValueError("tokens_per_reply 不能为负")
        self.script = list(script)
        self.default_answer = default_answer
        self.tokens_per_reply = tokens_per_reply
        self.calls: list[dict[str, Any]] = []
        self.reflect_calls: list[tuple[int, ToolResult]] = []
        self._cursor = 0

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
            }
        )
        if self._cursor < len(self.script):
            item = self.script[self._cursor]
            self._cursor += 1
        else:
            item = self.default_answer
        reply = _to_reply(item)
        if reply.tokens == 0 and self.tokens_per_reply:
            reply = replace(reply, tokens=self.tokens_per_reply)
        return reply

    def reflect(self, *, step: int, result: ToolResult) -> str:
        self.reflect_calls.append((step, result))
        if result.ok:
            return "已读取工具结果，信息可用于回答。"
        return f"工具报错（{result.error}），需要调整参数或改用其他工具。"


def _to_reply(item: ModelReply | ToolCall | str | Mapping[str, Any]) -> ModelReply:
    if isinstance(item, ModelReply):
        return item
    if isinstance(item, ToolCall):
        return ModelReply(tool_call=item)
    if isinstance(item, str):
        return ModelReply(content=item)
    if isinstance(item, Mapping):
        tool_call = item.get("tool_call")
        if isinstance(tool_call, Mapping):
            tool_call = ToolCall(
                name=str(tool_call.get("name", "")),
                arguments=tool_call.get("arguments") or {},
            )
        elif tool_call is not None and not isinstance(tool_call, ToolCall):
            raise TypeError("FakeModel 脚本中的 tool_call 必须是 object 或 ToolCall")
        return ModelReply(
            content=str(item.get("content", "")),
            tool_call=tool_call,
            tokens=int(item.get("tokens", 0)),
        )
    raise TypeError(f"FakeModel 无法识别脚本元素：{type(item).__name__}")
