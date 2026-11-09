"""工具注册表：白名单、JSON Schema 校验与权限标签（ADR-013 / ADR-017）。

设计要点：

- 只有注册过的工具可被调用（契约 ``AGENT-01``），未知工具在运行时变成一条
  可回传模型的错误 observation，而不是抛异常中断整轮；
- 参数按 JSON Schema 的常用子集校验（``type/required/properties/enum/
  additionalProperties/minimum/maximum/minLength/maxLength/items``），失败抛
  :class:`ToolArgumentError`，由 runtime 回传给模型重试一次；
- 权限标签只能是 :class:`Permission` 中的只读/计算/渲染类目，注册即拒绝
  ``shell:exec`` 之类的任意字符串（契约 ``AGENT-03``：Agent 对推荐只读）；
- 工具异常与畸形返回值统一转成 ``ToolResult(error=...)``；表格超过上限时截断，
  完整行交给可选的 ``artifact_writer`` 落盘；未提供 writer 时在 ``metadata``
  标记 ``artifact_missing=True`` 与 ``dropped_rows``，避免静默丢弃（ADR-017）。
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from astock.agent.types import MAX_TABLE_ROWS, ToolResult

__all__ = [
    "Permission",
    "SAFE_PERMISSIONS",
    "ToolArgumentError",
    "ToolNotFoundError",
    "ToolRegistrationError",
    "ToolSpec",
    "ToolRegistry",
    "apply_defaults",
    "validate_tool_arguments",
]


class ToolRegistrationError(ValueError):
    """工具定义非法（重名、schema 不合法、未知权限标签等）。"""


class ToolNotFoundError(LookupError):
    """调用了未注册的工具。"""

    def __init__(self, name: str, available: Iterable[str] = ()) -> None:
        self.name = name
        self.available = tuple(available)
        hint = f"；可用工具：{', '.join(self.available)}" if self.available else ""
        super().__init__(f"未知工具 {name!r}{hint}")


class ToolArgumentError(ValueError):
    """工具参数未通过 JSON Schema 校验。"""

    def __init__(self, tool: str, errors: Iterable[str]) -> None:
        self.tool = tool
        self.errors = list(errors)
        detail = "；".join(self.errors) if self.errors else "参数不合法"
        super().__init__(f"工具 {tool} 参数校验失败：{detail}")


class Permission(str, Enum):
    """允许 Agent 使用的工具权限（只读 / 计算 / 渲染 / 追问）。"""

    MARKET_READ = "market:read"
    INDICATOR_COMPUTE = "indicator:compute"
    CHART_RENDER = "chart:render"
    FILE_READ = "file:read"
    RESEARCH_READ = "research:read"
    LEDGER_READ = "ledger:read"
    USER_ASK = "user:ask"


SAFE_PERMISSIONS = frozenset(Permission)


def _permission_values() -> str:
    return ", ".join(p.value for p in Permission)


def _normalize_permissions(values: Iterable[Permission | str]) -> frozenset[Permission]:
    if isinstance(values, (str, bytes)):
        raise ToolRegistrationError("permissions 必须是标签序列，不能是单个字符串")
    result: set[Permission] = set()
    for value in values:
        try:
            result.add(value if isinstance(value, Permission) else Permission(str(value)))
        except ValueError as exc:
            raise ToolRegistrationError(
                f"未知权限标签 {value!r}；只允许：{_permission_values()}"
            ) from exc
    if not result:
        raise ToolRegistrationError("工具至少需要一个权限标签")
    return frozenset(result)


_ALLOWED_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _check_schema(schema: Mapping[str, Any], path: str = "$") -> None:
    """在注册时检查 schema 结构，避免运行期才发现拼写错误。"""
    if not isinstance(schema, Mapping):
        raise ToolRegistrationError(f"{path}: schema 必须是 object")
    schema_type = schema.get("type")
    if schema_type is not None:
        allowed = [schema_type] if isinstance(schema_type, str) else schema_type
        if not isinstance(allowed, list) or any(t not in _ALLOWED_TYPES for t in allowed):
            raise ToolRegistrationError(f"{path}: 非法 type {schema_type!r}")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise ToolRegistrationError(f"{path}.properties 必须是 object")
        for name, sub in properties.items():
            _check_schema(sub, f"{path}.properties.{name}")
    items = schema.get("items")
    if items is not None:
        _check_schema(items, f"{path}.items")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, Sequence)
        or isinstance(required, (str, bytes))
        or any(not isinstance(name, str) for name in required)
    ):
        raise ToolRegistrationError(f"{path}.required 必须是字符串数组")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise ToolRegistrationError(f"{path}.additionalProperties 只支持布尔值")
    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, Sequence) or isinstance(enum, (str, bytes))
    ):
        raise ToolRegistrationError(f"{path}.enum 必须是数组")
    if "minimum" in schema and not isinstance(schema["minimum"], (int, float)):
        raise ToolRegistrationError(f"{path}.minimum 必须是数字")
    if "maximum" in schema and not isinstance(schema["maximum"], (int, float)):
        raise ToolRegistrationError(f"{path}.maximum 必须是数字")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (
            not isinstance(schema[key], int)
            or isinstance(schema[key], bool)
            or schema[key] < 0
        ):
            raise ToolRegistrationError(f"{path}.{key} 必须是非负整数")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def validate_tool_arguments(schema: Mapping[str, Any], value: Any) -> list[str]:
    """返回全部校验错误；空列表表示通过。错误路径使用 ``$`` 根记法。"""
    errors: list[str] = []
    _validate(schema, value, "$", errors)
    return errors


def _validate(schema: Mapping[str, Any], value: Any, path: str, errors: list[str]) -> None:
    expected = schema.get("type")
    if expected is not None:
        candidates = [expected] if isinstance(expected, str) else list(expected)
        if not any(_matches_type(value, item) for item in candidates):
            errors.append(f"{path}: 期望 {'/'.join(candidates)}，实际 {_type_name(value)}")
            return

    enum = schema.get("enum")
    if enum is not None and not any(value == item for item in enum):
        errors.append(f"{path}: 取值 {value!r} 不在枚举 {list(enum)!r} 中")

    if isinstance(value, Mapping):
        required = schema.get("required") or []
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name}: 缺少必填参数")
        properties = schema.get("properties") or {}
        if isinstance(properties, Mapping):
            for name, sub in properties.items():
                if name in value:
                    _validate(sub, value[name], f"{path}.{name}", errors)
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name}: 不允许的额外参数")

    if isinstance(value, (list, tuple)):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: 数组长度 {len(value)} < {min_items}")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path}: 数组长度 {len(value)} > {max_items}")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate(items, item, f"{path}[{index}]", errors)

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path}: 长度 {len(value)} < {min_length}")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path}: 长度 {len(value)} > {max_length}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: {value} < 最小值 {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: {value} > 最大值 {maximum}")


def apply_defaults(schema: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    """补齐顶层 ``properties[*].default``（不递归，保持行为可预期）。"""
    result = dict(value)
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, sub in properties.items():
            if name not in result and isinstance(sub, Mapping) and "default" in sub:
                result[name] = copy.deepcopy(sub["default"])
    return result


@dataclass(frozen=True)
class ToolSpec:
    """一个白名单工具：名称、JSON Schema、处理函数与权限标签。"""

    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: Callable[..., Any] = field(repr=False, compare=False)
    permissions: frozenset[Permission] | Sequence[Permission | str] = field(
        default_factory=frozenset
    )

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", self.name
        ):
            raise ToolRegistrationError(
                f"工具名 {self.name!r} 非法：只允许小写字母/数字/下划线，且以字母开头"
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise ToolRegistrationError(f"工具 {self.name} 缺少 description")
        if not isinstance(self.parameters, Mapping):
            raise ToolRegistrationError(f"工具 {self.name} 的 parameters 必须是 object")
        if self.parameters.get("type") != "object":
            raise ToolRegistrationError(f"工具 {self.name} 的 parameters.type 必须是 object")
        _check_schema(self.parameters, path=f"$.{self.name}")
        if not callable(self.handler):
            raise ToolRegistrationError(f"工具 {self.name} 的 handler 不可调用")
        object.__setattr__(self, "parameters", copy.deepcopy(dict(self.parameters)))
        object.__setattr__(self, "permissions", _normalize_permissions(self.permissions))

    @property
    def permission_names(self) -> tuple[str, ...]:
        permissions = _normalize_permissions(self.permissions)
        return tuple(sorted(p.value for p in permissions))

    def json_schema(self) -> dict[str, Any]:
        """OpenAI-compatible function calling 描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(dict(self.parameters)),
            },
        }


ArtifactWriter = Callable[[str, dict[str, Any], list[dict[str, Any]]], str]


class ToolRegistry:
    """工具白名单与调用入口。

    表格超过 ``table_row_limit`` 时只保留前 N 行；传入 ``artifact_writer`` 时
    完整行先交给 writer 落盘，未传入时在结果 ``metadata`` 标记
    ``artifact_missing=True``。
    """

    def __init__(
        self,
        *,
        allowed_permissions: Iterable[Permission | str] | None = None,
        table_row_limit: int = MAX_TABLE_ROWS,
        artifact_writer: ArtifactWriter | None = None,
    ) -> None:
        if table_row_limit < 1:
            raise ValueError("table_row_limit 必须 >= 1")
        self._tools: dict[str, ToolSpec] = {}
        self._allowed = (
            frozenset(SAFE_PERMISSIONS)
            if allowed_permissions is None
            else _normalize_permissions(allowed_permissions)
        )
        self.table_row_limit = table_row_limit
        self.artifact_writer = artifact_writer

    @property
    def allowed_permissions(self) -> frozenset[Permission]:
        return self._allowed

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ToolRegistrationError(f"工具 {spec.name!r} 已注册")
        permissions = _normalize_permissions(spec.permissions)
        denied = sorted(p.value for p in permissions - self._allowed)
        if denied:
            raise ToolRegistrationError(
                f"工具 {spec.name!r} 的权限 {denied} 不在注册表允许范围内"
            )
        self._tools[spec.name] = spec
        return spec

    def tool(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name, self._tools.keys()) from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.json_schema() for spec in self._tools.values()]

    def validate(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """校验并补齐默认参数；失败抛 :class:`ToolArgumentError`。"""
        spec = self.tool(name)
        if not isinstance(arguments, Mapping):
            raise ToolArgumentError(name, ["$: 参数必须是 object"])
        prepared = apply_defaults(spec.parameters, arguments)
        errors = validate_tool_arguments(spec.parameters, prepared)
        if errors:
            raise ToolArgumentError(name, errors)
        return prepared

    def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        """校验参数并调用工具；工具异常转成 ``ToolResult(error=...)``。"""
        prepared = self.validate(name, arguments)
        spec = self.tool(name)
        try:
            raw = spec.handler(**prepared)
        except Exception as exc:  # noqa: BLE001 - 工具失败必须回传给模型
            return ToolResult(
                error=f"{type(exc).__name__}: {exc}",
                source=f"tool:{name}",
            )

        try:
            result = ToolResult.coerce(raw)
        except Exception as exc:  # noqa: BLE001 - 工具返回结构非法也必须回传模型
            return ToolResult(
                error=f"工具返回结构非法：{type(exc).__name__}: {exc}",
                source=f"tool:{name}",
            )
        if result.table is not None and len(result.table) > self.table_row_limit:
            full_rows = [dict(row) for row in result.table]
            artifact_id: str | None = None
            artifact_error: str | None = None
            if self.artifact_writer is not None:
                try:
                    artifact_id = self.artifact_writer(name, prepared, full_rows)
                except Exception as exc:  # noqa: BLE001 - 截断结果仍可返回
                    artifact_error = f"{type(exc).__name__}: {exc}"
            result = result.trim_table(self.table_row_limit)
            metadata = dict(result.metadata)
            if self.artifact_writer is None:
                metadata["artifact_missing"] = True
            if artifact_id:
                metadata["table_artifact_id"] = artifact_id
            if artifact_error:
                metadata["artifact_error"] = artifact_error
            result = ToolResult(
                text=result.text,
                table=result.table,
                figure=result.figure,
                error=result.error,
                data_as_of=result.data_as_of,
                data_time=result.data_time,
                source=result.source,
                truncated=result.truncated,
                metadata=metadata,
            )
        return result
