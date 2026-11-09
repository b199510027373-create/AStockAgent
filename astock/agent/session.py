"""会话状态：最近 N 轮对话 + 工作记忆（ADR-013）。

工作记忆是模型跨轮次复用的结构化上下文（代码、日期区间、指标、文件、
最近图表等），以 JSON 落盘，便于 CLI/GUI 重启后继续。对话只保留最近
``max_turns`` 个用户轮次，避免上下文无限增长。
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["AgentSession", "WorkingMemory", "DEFAULT_MAX_TURNS"]

DEFAULT_MAX_TURNS = 20
_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


@dataclass
class WorkingMemory:
    """跨轮次复用的研究上下文。"""

    codes: list[str] = field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    indicators: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    last_chart: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "codes": list(self.codes),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "indicators": list(self.indicators),
            "files": list(self.files),
            "last_chart": self.last_chart,
            "extras": copy.deepcopy(self.extras),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> WorkingMemory:
        if not isinstance(raw, Mapping):
            return cls()
        extras = raw.get("extras")
        return cls(
            codes=_as_list(raw.get("codes")),
            start_date=_as_optional_str(raw.get("start_date")),
            end_date=_as_optional_str(raw.get("end_date")),
            indicators=_as_list(raw.get("indicators")),
            files=_as_list(raw.get("files")),
            last_chart=_as_optional_str(raw.get("last_chart")),
            extras=dict(extras) if isinstance(extras, Mapping) else {},
        )

    def update(self, **changes: Any) -> None:
        """更新已知字段；未知字段并入 ``extras``。"""
        known = {item.name for item in fields(self)}
        for key, value in changes.items():
            if key in known and key != "extras":
                setattr(self, key, value)
            else:
                self.extras[key] = value


class AgentSession:
    """一次会话的消息历史、工作记忆与可选的落盘路径。"""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        working_memory: WorkingMemory | None = None,
        path: str | os.PathLike[str] | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns 必须 >= 1")
        self.session_id = session_id or uuid.uuid4().hex
        self.max_turns = max_turns
        self.working_memory = working_memory or WorkingMemory()
        self.path = Path(path) if path else None
        self.created_at = _utc_now()
        self.history: list[dict[str, Any]] = []

    # ---- 消息 -------------------------------------------------------------

    def add_message(self, role: str, content: str = "", **extra: Any) -> dict[str, Any]:
        if role not in _MESSAGE_ROLES:
            raise ValueError(f"未知消息角色 {role!r}")
        message: dict[str, Any] = {"role": role, "content": str(content)}
        for key, value in extra.items():
            if value is not None:
                message[key] = value
        self.history.append(message)
        self._trim()
        return message

    def add_user(self, content: str) -> dict[str, Any]:
        return self.add_message("user", content)

    def add_assistant(
        self, content: str = "", tool_call: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        extra = {"tool_call": dict(tool_call)} if tool_call is not None else {}
        return self.add_message("assistant", content, **extra)

    def add_tool(self, name: str, content: str, **extra: Any) -> dict[str, Any]:
        return self.add_message("tool", content, name=name, **extra)

    def recent_messages(self, limit: int | None = None) -> list[dict[str, Any]]:
        self._trim()
        if limit is None:
            messages = self.history
        elif limit <= 0:
            messages = []
        else:
            messages = self.history[-limit:]
        return copy.deepcopy(messages)

    def _trim(self) -> None:
        user_positions = [
            index for index, message in enumerate(self.history) if message["role"] == "user"
        ]
        if len(user_positions) > self.max_turns:
            keep_from = user_positions[-self.max_turns]
            del self.history[:keep_from]
        elif not user_positions and len(self.history) > self.max_turns * 4:
            del self.history[: len(self.history) - self.max_turns * 4]

    # ---- 持久化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "max_turns": self.max_turns,
            "working_memory": self.working_memory.to_dict(),
            "history": copy.deepcopy(self.history),
        }

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("未指定会话文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        self.path = target
        return target

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        *,
        max_turns: int | None = None,
    ) -> AgentSession:
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError(f"会话文件 {target} 格式错误：顶层必须是 object")
        stored_turns = data.get("max_turns")
        if max_turns is None:
            max_turns = stored_turns if isinstance(stored_turns, int) else DEFAULT_MAX_TURNS
        session = cls(
            session_id=str(data.get("session_id") or uuid.uuid4().hex),
            max_turns=max_turns,
            working_memory=WorkingMemory.from_dict(data.get("working_memory")),
            path=target,
        )
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            session.created_at = created_at
        history = data.get("history")
        if isinstance(history, Iterable) and not isinstance(history, (str, bytes, Mapping)):
            for item in history:
                if isinstance(item, Mapping) and item.get("role") in _MESSAGE_ROLES:
                    session.history.append(copy.deepcopy(dict(item)))
        session._trim()
        return session
