"""架构契约门禁：防止 ADR 状态、contract 规则落点、冻结接口与代码漂移。

对应 plan-20261110-003（架构契约评审门禁）。这里只做"文档与代码是否还对得上"的机械检查，
不判断架构好坏；架构判断在 ADR 与 `docs/AStockAgent_interface_freeze_v1.md` 里。

任何一条失败都意味着冻结接口被改而未改文档（或反之），按 ADR-012 治理附录的顺序处理：
先改 ADR → accepted → 升冻结清单版本 → 再改本文件。
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / ".devloop" / "architecture"
ADR_DIR = ARCH / "adr"
FREEZE_DOC = ROOT / "docs" / "AStockAgent_interface_freeze_v1.md"
RULE_MAP_DOC = ROOT / "docs" / "AStockAgent_contract_rule_map_v1.md"

# 本次评审门禁裁决的 8 条 ADR（ADR-012 治理附录要求不再出现"已实现但 proposed"）
GATED_ADRS = ("ADR-002", "ADR-010", "ADR-013", "ADR-014", "ADR-015", "ADR-016", "ADR-017", "ADR-018")
WITH_CONDITIONS = ("ADR-010", "ADR-015", "ADR-017", "ADR-018")
IMPLEMENTATION_FEEDBACK = ("ADR-002", "ADR-010", "ADR-013", "ADR-014")

ALLOWED_STATUS = {"accepted", "accepted-with-conditions", "superseded", "deprecated", "rejected"}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _adr_index() -> list[dict]:
    return json.loads(_read(ARCH / "adrs.json"))


def _contract() -> dict:
    return json.loads(_read(ARCH / "contract.json"))


def _adr_status_line(text: str) -> str:
    match = re.search(r"^- 状态：(\S+)$", text, re.MULTILINE)
    assert match is not None, "ADR 文件缺少「- 状态：…」行"
    return match.group(1)


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(_read(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    }


# ---------------------------------------------------------------- ADR 状态


def test_no_proposed_adr_remains() -> None:
    """ADR-012 治理附录：proposed 不得作为实现依据，工作台不得长期挂着 proposed。"""
    proposed = [entry["id"] for entry in _adr_index() if entry["status"] == "proposed"]
    assert proposed == [], f"仍为 proposed 的 ADR：{proposed}"


def test_adr_index_status_values_are_known() -> None:
    for entry in _adr_index():
        assert entry["status"] in ALLOWED_STATUS, f"{entry['id']} 状态越界：{entry['status']}"


def test_gated_adrs_have_verdict_suffix_in_summary() -> None:
    index = {entry["id"]: entry for entry in _adr_index()}
    for adr_id in GATED_ADRS:
        assert adr_id in index, f"{adr_id} 不在 adrs.json"
        assert index[adr_id]["summary"].startswith("[accepted"), (
            f"{adr_id} 摘要未标注裁决结论，工作台无法区分 accepted / accepted-with-conditions"
        )


def test_adr_markdown_status_matches_index() -> None:
    """adrs.json 是工作台数据源，ADR 文件是正文；两者状态不一致就是漂移。"""
    for entry in _adr_index():
        path = ARCH / entry["file"]
        assert path.exists(), f"{entry['id']} 的 markdown 不存在：{entry['file']}"
        assert _adr_status_line(_read(path)) == entry["status"], (
            f"{entry['id']} 状态漂移：adrs.json={entry['status']}，文件={_adr_status_line(_read(path))}"
        )


def test_conditional_adrs_declare_conditions_with_followup_plan() -> None:
    index = {entry["id"]: entry for entry in _adr_index()}
    for adr_id in WITH_CONDITIONS:
        text = _read(ARCH / index[adr_id]["file"])
        assert "accepted-with-conditions" in text, f"{adr_id} 正文未写明 accepted-with-conditions"
        assert "## 条件" in text, f"{adr_id} 缺少「## 条件」段"
        followups = re.findall(r"plan-\d{8}-\d{3}", text)
        assert followups, f"{adr_id} 的条件没有挂任何计划项"


def test_implemented_adrs_have_implementation_feedback_section() -> None:
    """已实现并通过评审的 ADR 必须写「实现回写」，偏差以文档为准而不是重写代码。"""
    index = {entry["id"]: entry for entry in _adr_index()}
    for adr_id in IMPLEMENTATION_FEEDBACK:
        text = _read(ARCH / index[adr_id]["file"])
        assert "实现回写" in text, f"{adr_id} 已实现但缺少「实现回写」段"


def test_adr_012_carries_governance_appendix() -> None:
    index = {entry["id"]: entry for entry in _adr_index()}
    text = _read(ARCH / index["ADR-012"]["file"])
    for token in ("治理附录", "proposed", "追认", "实现回写", "接口冻结"):
        assert token in text, f"ADR-012 治理附录缺少要点：{token}"
    assert re.search(r"7\s*个?自然日", text), "ADR-012 未写明先行实现 7 天内追认"
    assert "状态变更记录" in text, "ADR-012 缺少状态变更记录表"


# ---------------------------------------------------------------- 规则落点


def test_contract_has_eighteen_rules_each_with_landing_point() -> None:
    contract = _contract()
    rules = contract["rules"]
    assert len(rules) == 18, f"contract 规则数应为 18，实际 {len(rules)}"
    for rule in rules:
        assert rule["enforcement"] in {"test", "review"}, rule["id"]
        assert rule.get("scope"), f"{rule['id']} 缺少 scope"
        assert rule.get("rationale", "").startswith("落点："), f"{rule['id']} 没有落点说明"


def _landing_refs(rationale: str) -> list[tuple[str, str | None]]:
    """把「落点：」文本解析成 (测试文件, 测试函数或 None) 列表。"""
    refs: list[tuple[str, str | None]] = []
    current: str | None = None
    for token in re.split(r"[、，；（）()\[\]\s]+", rationale):
        file_match = re.search(r"(tests/[\w./-]+\.py)", token)
        if file_match:
            current = file_match.group(1)
            name_match = re.search(r"::(\w+)", token)
            refs.append((current, name_match.group(1) if name_match else None))
            continue
        name_match = re.fullmatch(r"::(\w+)", token)
        if name_match and current:
            refs.append((current, name_match.group(1)))
    return refs


def test_every_contract_rule_points_at_an_existing_landing() -> None:
    """test 类规则必须指向真实存在的测试函数；review 类至少要有评审清单条目。"""
    rule_map = _read(RULE_MAP_DOC)
    for rule in _contract()["rules"]:
        rationale = rule["rationale"]
        if rule["enforcement"] != "test":
            assert re.search(r"RC-\d{2}", rationale), f"{rule['id']} 为 review 规则但未指向 RC 清单"
            assert any(rc in rule_map for rc in re.findall(r"RC-\d{2}", rationale)), (
                f"{rule['id']} 指向的评审清单条目不在落点表里"
            )
            continue
        refs = _landing_refs(rationale)
        assert refs, f"{rule['id']} 的落点没有任何测试引用"
        for path_text, func in refs:
            path = ROOT / path_text
            if not path.exists():  # 未来项承诺的测试：必须已在落点表登记
                assert path_text in rule_map, f"{rule['id']} 引用的 {path_text} 不存在也未登记"
                continue
            if func is None:
                continue
            assert func in _test_functions(path), f"{rule['id']} 引用的 {path_text}::{func} 不存在"


def test_rule_map_lists_every_rule_and_review_checklist() -> None:
    text = _read(RULE_MAP_DOC)
    for rule in _contract()["rules"]:
        assert rule["id"] in text, f"落点表缺少规则 {rule['id']}"
    for index in range(1, 9):
        assert f"RC-{index:02d}" in text, f"落点表缺少评审清单 RC-{index:02d}"


# ---------------------------------------------------------------- 冻结接口与代码一致性


def test_module_graph_descriptions_are_not_stale() -> None:
    """模块图不写状态，但描述里也不能残留「计划新增」这类过期标记（contracts 已实现并追认）。"""
    graph = json.loads(_read(ARCH / "modules.json"))
    for module in graph["modules"]:
        assert "计划新增" not in module["description"], (
            f"模块 {module['id']} 描述仍是「计划新增」，需按 ADR 裁决更新"
        )


def test_freeze_doc_covers_six_interfaces() -> None:
    text = _read(FREEZE_DOC)
    for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."):
        assert heading in text, f"冻结清单缺少章节 {heading}"
    for token in ("ToolResult", "ToolSpec", "ChatModel.complete", "ChartSpec", "agent_runs",
                  "agent_steps", "agent_artifacts", "latest_bar_time"):
        assert token in text, f"冻结清单缺少接口要素：{token}"


def test_tool_result_shape_matches_freeze() -> None:
    from astock.agent import types

    fields = tuple(types.ToolResult.__dataclass_fields__)  # type: ignore[attr-defined]
    assert fields == (
        "text",
        "table",
        "figure",
        "error",
        "data_as_of",
        "data_time",
        "source",
        "truncated",
        "metadata",
    ), f"ToolResult 字段顺序漂移：{fields}"
    assert types.MAX_TABLE_ROWS == 200
    assert types.MAX_TEXT_CHARS == 4000
    assert len(list(types.StopReason)) == 8
    freeze = _read(FREEZE_DOC)
    assert "MAX_TABLE_ROWS" in freeze and "MAX_TEXT_CHARS" in freeze


def test_tool_spec_and_permission_labels_match_freeze() -> None:
    from astock.agent import registry

    assert {p.value for p in registry.Permission} == {
        "market:read",
        "indicator:compute",
        "chart:render",
        "file:read",
        "research:read",
        "ledger:read",
        "user:ask",
    }
    assert not [p.value for p in registry.Permission if p.value.endswith(":write")], (
        "权限里出现了 write 标签：Agent 写台账只能由 store 内部控制（ADR-017 §9）"
    )
    spec_fields = tuple(registry.ToolSpec.__dataclass_fields__)  # type: ignore[attr-defined]
    assert spec_fields == ("name", "description", "parameters", "handler", "permissions")

    signature = inspect.signature(registry.ToolRegistry.__init__)
    assert list(signature.parameters)[1:] == ["allowed_permissions", "table_row_limit", "artifact_writer"]
    assert signature.parameters["table_row_limit"].default == 200


def test_chat_model_signature_matches_freeze() -> None:
    from astock.agent import gateway

    signature = inspect.signature(gateway.ChatModel.complete)
    params = signature.parameters
    assert list(params) == ["self", "messages", "tools", "temperature"], (
        f"ChatModel.complete 形参漂移：{list(params)}（v1 冻结为 messages, tools, *, temperature）"
    )
    assert "stream" not in params, "v1 明确不做流式（ADR-014 非目标），不得预留 stream 形参"
    assert params["temperature"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["temperature"].default == 0.2

    result_fields = set(gateway.ChatResult.__dataclass_fields__)  # type: ignore[attr-defined]
    for name in ("content", "tool_calls", "finish_reason", "prompt_tokens", "completion_tokens",
                 "total_tokens", "model", "provider", "protocol", "degraded"):
        assert name in result_fields, f"ChatResult 缺少字段 {name}"


def test_agent_model_protocol_is_bridged_not_merged() -> None:
    """ADR-013 的 AgentModel 与 ADR-014 的 ChatModel 是两层，桥只有 AgentModelAdapter。"""
    from astock.agent import gateway, runtime, types

    signature = inspect.signature(runtime.AgentModel.complete)
    assert list(signature.parameters) == ["self", "messages", "tools"]
    for name in ("messages", "tools"):
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    bridge = inspect.signature(gateway.AgentModelAdapter.complete)
    # 只比形参名/种类/默认值：适配器允许把 list[dict] 放宽成 Sequence[Mapping]（同一契约）
    assert [(name, p.kind, p.default) for name, p in bridge.parameters.items()] == [
        (name, p.kind, p.default) for name, p in signature.parameters.items()
    ]
    assert list(gateway.AgentModelAdapter.complete.__annotations__)[-1] == "return"
    assert types.ModelReply.__dataclass_fields__["content"].default == ""  # type: ignore[attr-defined]


def test_runtime_defaults_match_freeze() -> None:
    from astock.agent import runtime

    assert runtime.DEFAULT_MAX_STEPS == 12
    assert runtime.DEFAULT_TOKEN_BUDGET == 64_000
    assert runtime.DEFAULT_TIMEOUT_SECONDS == 120.0
    assert runtime.DEFAULT_VALIDATION_RETRIES == 1
    assert runtime.DEFAULT_SPIN_LIMIT == 2


def test_frozen_layout_and_ddl_recorded_for_unbuilt_modules() -> None:
    """015/016/017/018 还没开工：文件布局与 DDL 只能靠冻结清单约束，这里守住清单内容。"""
    text = _read(FREEZE_DOC)
    for token in ("extracted.jsonl", "meta.json", "parquet", "FTS5", 'f_" + sha256[:16]'):
        assert token in text, f"冻结清单缺少文件布局要素：{token}"
    for token in ("CREATE TABLE IF NOT EXISTS agent_runs", "CREATE TABLE IF NOT EXISTS agent_steps",
                  "CREATE TABLE IF NOT EXISTS agent_artifacts", "digest",
                  "temperature", "seed", "code_version"):
        assert token in text, f"冻结清单缺少 agent_* DDL 要素：{token}"
    assert "running/success/partial/failed" in text


def test_chart_spec_caps_are_frozen_in_doc() -> None:
    text = _read(FREEZE_DOC)
    for token in ("`version`", "additionalProperties", "5000", "candlestick"):
        assert token in text, f"冻结清单缺少 ChartSpec 要素：{token}"


def test_data_time_gap_is_declared() -> None:
    """data_time 目前没有生产者，冻结清单与 ADR-018 必须写着这个缺口，避免被当成已实现。"""
    assert "latest_bar_time" in _read(FREEZE_DOC)
    index = {entry["id"]: entry for entry in _adr_index()}
    adr18 = _read(ARCH / index["ADR-018"]["file"])
    assert "唯一生产者" in adr18
    assert "plan-20261107-006" in adr18


# ---------------------------------------------------------------- 依赖方向 / 供应商隔离


VENDOR_SDK_ROOTS = {"openai", "anthropic", "zhipuai", "dashscope", "google"}
ALLOWED_VENDOR_FILES = {"gateway.py", "llm.py"}


def test_no_vendor_sdk_import_outside_gateway() -> None:
    """AGENT-06：业务代码不得直接 import 模型供应商 SDK。"""
    offenders: list[str] = []
    for path in (ROOT / "astock").rglob("*.py"):
        if path.name in ALLOWED_VENDOR_FILES:
            continue
        tree = ast.parse(_read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            hit = VENDOR_SDK_ROOTS & set(names)
            if hit:
                offenders.append(f"{path.relative_to(ROOT)} -> {sorted(hit)}")
    assert offenders == [], f"供应商 SDK 泄漏到网关之外：{offenders}"


def test_agent_tools_dir_has_no_shell_or_http_capability() -> None:
    """AGENT-01：v1 工具层不得出现 shell / 任意 HTTP / 删文件能力（目录尚未实现时自动跳过）。"""
    tools_dir = ROOT / "astock" / "agent" / "tools"
    if not tools_dir.exists():
        pytest.skip("astock/agent/tools 尚未实现（plan-20261107-003/005）")
    forbidden = {"subprocess", "os", "shutil", "requests", "httpx", "urllib", "socket"}
    offenders: list[str] = []
    for path in tools_dir.rglob("*.py"):
        tree = ast.parse(_read(path), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            if names & forbidden:
                offenders.append(f"{path.relative_to(ROOT)} -> {sorted(names & forbidden)}")
    assert offenders == [], f"工具层出现越权能力：{offenders}"
