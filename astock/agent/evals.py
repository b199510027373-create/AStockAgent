"""Agent 离线评测集与回归（ADR-013 / ADR-017）。

评测不访问网络、不读取真实行情库：

- 任务集是 JSON（默认 ``.devloop/evals/agent_eval_tasks.json``）；
- 每条任务声明 FakeModel 的脚本、固定工具结果与最终回答；
- 工具注册表复用生产 ``ToolSpec`` 的 schema/权限，只把 handler 换成固定桩，
  因此参数校验、默认值、权限白名单与真实运行时完全一致；
- 指标包括工具选择准确率、任务成功率、平均步数、平均时延与 P95 时延；
- 报告可写入 JSON，并与 baseline 逐任务、逐指标比较。

运行方式::

    python -m astock.agent.evals \
        --tasks .devloop/evals/agent_eval_tasks.json \
        --output .devloop/evals/agent_eval_latest.json \
        --baseline .devloop/evals/agent_eval_baseline.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

from astock.agent.fake import FakeModel
from astock.agent.registry import ToolRegistry, ToolSpec
from astock.agent.runtime import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TOKEN_BUDGET,
    AgentRuntime,
)
from astock.agent.types import StopReason, ToolCall, ToolResult

__all__ = [
    "CATEGORY_LABELS",
    "EvalReport",
    "EvalTask",
    "EvalTaskResult",
    "StubCall",
    "compare_reports",
    "load_report",
    "load_tasks",
    "run_evaluation",
    "run_task",
    "validate_dataset",
    "write_report",
]

SCHEMA_VERSION = 1
DEFAULT_TASKS_PATH = ".devloop/evals/agent_eval_tasks.json"
DEFAULT_OUTPUT_PATH = ".devloop/evals/agent_eval_latest.json"
DEFAULT_BASELINE_PATH = ".devloop/evals/agent_eval_baseline.json"

CATEGORY_LABELS = {
    "pdf_qa": "PDF 研报问答",
    "table_stats": "Excel 表格统计图",
    "minute_kline": "指定股票 + 指标的分钟 K 线",
    "refusal": "边界拒答",
}
MARKET_TOOLS = frozenset({"quote", "kline", "indicator"})
FILE_CITING_TOOLS = frozenset({"file_search", "file_read", "table_schema", "table_query"})


@dataclass(frozen=True)
class StubCall:
    """一条固定工具调用：参数用于校验，结果是回放给运行时的 observation。"""

    name: str
    arguments: Mapping[str, Any]
    result: ToolResult


@dataclass(frozen=True)
class StepExpectation:
    """对某一步工具结果的额外断言（成功/失败、元数据、错误文本）。"""

    index: int
    ok: bool = True
    error_contains: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    require_citation: bool = True


@dataclass(frozen=True)
class EvalTask:
    """一条离线评测任务。"""

    id: str
    category: str
    question: str
    calls: tuple[StubCall, ...]
    final_answer: str
    expect_tools: tuple[str, ...]
    answer_contains: tuple[str, ...] = ()
    answer_not_contains: tuple[str, ...] = ()
    stop_reason: str = StopReason.COMPLETED.value
    steps: tuple[StepExpectation, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EvalTask:
        calls = tuple(
            StubCall(
                name=str(item["tool"]),
                arguments=dict(item.get("arguments") or {}),
                result=_tool_result(item["result"]),
            )
            for item in data.get("calls") or ()
        )
        expect = data.get("expect") or {}
        expect_tools = tuple(
            str(name)
            for name in (expect.get("tools") or [call.name for call in calls])
        )
        return cls(
            id=str(data["id"]),
            category=str(data["category"]),
            question=str(data["question"]),
            calls=calls,
            final_answer=str(data["final_answer"]),
            expect_tools=expect_tools,
            answer_contains=_str_tuple(expect.get("answer_contains")),
            answer_not_contains=_str_tuple(expect.get("answer_not_contains")),
            stop_reason=str(expect.get("stop_reason") or StopReason.COMPLETED.value),
            steps=tuple(
                StepExpectation(
                    index=int(item["index"]),
                    ok=bool(item.get("ok", True)),
                    error_contains=(
                        str(item["error_contains"])
                        if item.get("error_contains") is not None
                        else None
                    ),
                    metadata=dict(item.get("metadata") or {}),
                    require_citation=bool(item.get("require_citation", True)),
                )
                for item in expect.get("steps") or ()
            ),
        )


@dataclass
class EvalTaskResult:
    """一条任务的执行结果，保留足够的证据供 diff / 排障。"""

    id: str
    category: str
    passed: bool
    failures: list[str]
    expected_tools: list[str]
    actual_tools: list[str]
    expected_tool_calls: int
    tool_matches: int
    answer: str
    stop_reason: str
    steps: int
    tokens_used: int
    latency_ms: float
    step_evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "passed": self.passed,
            "failures": list(self.failures),
            "expected_tools": list(self.expected_tools),
            "actual_tools": list(self.actual_tools),
            "expected_tool_calls": self.expected_tool_calls,
            "tool_matches": self.tool_matches,
            "answer": self.answer,
            "stop_reason": self.stop_reason,
            "steps": self.steps,
            "tokens_used": self.tokens_used,
            "latency_ms": round(self.latency_ms, 3),
            "step_evidence": list(self.step_evidence),
        }


@dataclass
class EvalReport:
    """一次完整评测的可序列化报告。"""

    summary: dict[str, Any]
    categories: dict[str, dict[str, Any]]
    tasks: list[EvalTaskResult]
    tasks_file: str = ""
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "tasks_file": self.tasks_file,
            "summary": dict(self.summary),
            "categories": {key: dict(value) for key, value in self.categories.items()},
            "tasks": [task.to_dict() for task in self.tasks],
        }


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _tool_result(data: Mapping[str, Any]) -> ToolResult:
    table = data.get("table")
    if table is not None and not isinstance(table, list):
        raise ValueError("工具结果 table 必须是数组")
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("工具结果 metadata 必须是 object")
    return ToolResult(
        text=str(data.get("text") or ""),
        table=[dict(row) for row in table] if table else None,
        figure=str(data["figure"]) if data.get("figure") is not None else None,
        error=str(data["error"]) if data.get("error") is not None else None,
        data_as_of=(
            str(data["data_as_of"]) if data.get("data_as_of") is not None else None
        ),
        data_time=str(data["data_time"]) if data.get("data_time") is not None else None,
        source=str(data["source"]) if data.get("source") is not None else None,
        truncated=bool(data.get("truncated", False)),
        metadata=dict(metadata or {}),
    )


def load_tasks(path: str | Path = DEFAULT_TASKS_PATH) -> tuple[EvalTask, ...]:
    """从 JSON 读取任务集；兼容直接是数组或 ``{"tasks": [...]}``。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_tasks = payload.get("tasks") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_tasks, list):
        raise TypeError("评测集必须是数组或包含 tasks 数组的对象")
    return tuple(EvalTask.from_mapping(item) for item in raw_tasks)


def validate_dataset(
    tasks: Sequence[EvalTask],
    *,
    expected_total: int = 20,
    per_category: int = 5,
) -> None:
    """校验任务集的覆盖、结构与工具调用参数；失败抛 ``ValueError``。"""
    issues: list[str] = []
    if len(tasks) != expected_total:
        issues.append(f"任务数应为 {expected_total}，实际 {len(tasks)}")
    counts = Counter(task.category for task in tasks)
    for category in CATEGORY_LABELS:
        if counts.get(category, 0) != per_category:
            issues.append(
                f"类别 {category} 应有 {per_category} 条，实际 {counts.get(category, 0)}"
            )
    unknown = sorted(set(counts) - set(CATEGORY_LABELS))
    if unknown:
        issues.append(f"未知类别：{', '.join(unknown)}")
    seen: set[str] = set()
    catalog = _catalog()
    for task in tasks:
        if not task.id or task.id in seen:
            issues.append(f"任务 id 为空或重复：{task.id!r}")
        seen.add(task.id)
        if not task.question.strip():
            issues.append(f"{task.id}: question 不能为空")
        if not task.final_answer.strip():
            issues.append(f"{task.id}: final_answer 不能为空")
        if not task.calls:
            issues.append(f"{task.id}: 至少需要一条工具调用")
        if task.stop_reason not in {reason.value for reason in StopReason}:
            issues.append(f"{task.id}: 未知 stop_reason {task.stop_reason!r}")
        names = tuple(call.name for call in task.calls)
        if task.expect_tools != names:
            issues.append(f"{task.id}: expect.tools 与 calls 的工具名不一致")
        step_indexes = [step.index for step in task.steps]
        if len(step_indexes) != len(set(step_indexes)):
            issues.append(f"{task.id}: steps 断言 index 重复")
        for step in task.steps:
            if step.index < 0 or step.index >= len(task.calls):
                issues.append(f"{task.id}: steps 断言 index {step.index} 越界")
        for call in task.calls:
            if call.name not in catalog.names():
                issues.append(f"{task.id}: 未知工具 {call.name!r}")
                continue
            try:
                catalog.validate(call.name, call.arguments)
            except Exception as exc:  # noqa: BLE001 - 数据集错误统一报告
                issues.append(f"{task.id}: {call.name} 参数不合法：{exc}")
    if issues:
        raise ValueError("评测集校验失败：\n- " + "\n- ".join(issues))


@lru_cache(maxsize=1)
def _catalog() -> ToolRegistry:
    """生产工具白名单的 schema 目录；handler 不会被调用。"""
    from astock.agent.tools.file_tools import build_file_registry
    from astock.agent.tools.market_tools import register_market_tools

    return register_market_tools(build_file_registry())


def _signature(arguments: Mapping[str, Any]) -> str:
    return json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True, default=str)


class _StubToolset:
    """按 ``(工具名, 规范化参数)`` 回放固定结果；未配置的调用显式报错。"""

    def __init__(self, calls: Sequence[StubCall]) -> None:
        self._queues: dict[tuple[str, str], deque[ToolResult]] = defaultdict(deque)
        self._expected: list[tuple[str, dict[str, Any]]] = []
        for call in calls:
            key = (call.name, _signature(call.arguments))
            self._queues[key].append(call.result)
            self._expected.append((call.name, dict(call.arguments)))

    def handle(self, name: str, **arguments: Any) -> ToolResult:
        key = (name, _signature(arguments))
        queue = self._queues.get(key)
        if not queue:
            return ToolResult(
                error=f"stub 未配置调用：{name} {arguments}",
                source=f"tool:{name}",
            )
        return queue.popleft()

    @property
    def leftovers(self) -> list[str]:
        remaining: list[str] = []
        for name, arguments in self._expected:
            queue = self._queues.get((name, _signature(arguments)))
            if queue:
                remaining.append(f"{name} {arguments}")
        return remaining


def _build_stub_registry(stub: _StubToolset) -> ToolRegistry:
    registry = ToolRegistry(table_row_limit=200)
    for spec in _catalog().specs():
        registry.register(
            ToolSpec(
                name=spec.name,
                description=spec.description,
                parameters=spec.parameters,
                handler=partial(stub.handle, spec.name),
                permissions=spec.permissions,
            )
        )
    return registry


def _failure_result(task: EvalTask, message: str, started: float) -> EvalTaskResult:
    return EvalTaskResult(
        id=task.id,
        category=task.category,
        passed=False,
        failures=[message],
        expected_tools=list(task.expect_tools),
        actual_tools=[],
        expected_tool_calls=len(task.calls),
        tool_matches=0,
        answer="",
        stop_reason="",
        steps=0,
        tokens_used=0,
        latency_ms=(time.perf_counter() - started) * 1000,
    )


def run_task(task: EvalTask, *, catalog: ToolRegistry | None = None) -> EvalTaskResult:
    """用 FakeModel + 固定工具桩执行一条任务。"""
    catalog = catalog or _catalog()
    started = time.perf_counter()
    try:
        calls = tuple(
            StubCall(
                name=call.name,
                arguments=catalog.validate(call.name, call.arguments),
                result=call.result,
            )
            for call in task.calls
        )
        stub = _StubToolset(calls)
        model = FakeModel(
            [
                *(ToolCall(call.name, call.arguments) for call in calls),
                task.final_answer,
            ]
        )
        runtime = AgentRuntime(
            model,
            _build_stub_registry(stub),
            max_steps=DEFAULT_MAX_STEPS,
            token_budget=DEFAULT_TOKEN_BUDGET,
            timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
            auto_save=False,
        )
        outcome = runtime.run(task.question)
    except Exception as exc:  # noqa: BLE001 - 单条任务失败不能中断整批评测
        return _failure_result(task, f"评测执行异常：{type(exc).__name__}: {exc}", started)

    failures = _check_outcome(task, outcome, model, stub)
    actual_tools = [step.tool for step in outcome.steps]
    expected_calls = len(task.expect_tools)
    matches = sum(
        1
        for expected, actual in zip(task.expect_tools, actual_tools)
        if expected == actual
    )
    evidence = [
        {
            "index": step.index,
            "tool": step.tool,
            "ok": step.result.ok,
            "error": step.result.error,
            "metadata": dict(step.result.metadata or {}),
        }
        for step in outcome.steps
    ]
    return EvalTaskResult(
        id=task.id,
        category=task.category,
        passed=not failures,
        failures=failures,
        expected_tools=list(task.expect_tools),
        actual_tools=actual_tools,
        expected_tool_calls=expected_calls,
        tool_matches=matches,
        answer=outcome.answer,
        stop_reason=outcome.stop_reason.value,
        steps=len(outcome.steps),
        tokens_used=outcome.tokens_used,
        latency_ms=outcome.elapsed_seconds * 1000,
        step_evidence=evidence,
    )


def _check_outcome(
    task: EvalTask,
    outcome: Any,
    model: FakeModel,
    stub: _StubToolset,
) -> list[str]:
    failures: list[str] = []
    actual_tools = [step.tool for step in outcome.steps]
    expected_tools = list(task.expect_tools)
    if outcome.stop_reason.value != task.stop_reason:
        failures.append(
            f"stop_reason 期望 {task.stop_reason}，实际 {outcome.stop_reason.value}"
        )
    if actual_tools != expected_tools:
        failures.append(f"工具序列不匹配：期望 {expected_tools}，实际 {actual_tools}")
    expected_model_calls = len(task.calls) + 1
    if model.call_count != expected_model_calls:
        failures.append(
            f"模型调用次数期望 {expected_model_calls}，实际 {model.call_count}"
        )
    for text in task.answer_contains:
        if text not in outcome.answer:
            failures.append(f"最终回答缺少 {text!r}")
    for text in task.answer_not_contains:
        if text in outcome.answer:
            failures.append(f"最终回答不应包含 {text!r}")
    leftovers = stub.leftovers
    if leftovers:
        failures.append(f"有 {len(leftovers)} 条固定工具结果未被消费：{leftovers}")

    by_index = {step.index: step for step in task.steps}
    for index, step in enumerate(outcome.steps):
        expected = by_index.get(index)
        if expected is None:
            if step.result.error:
                failures.append(
                    f"第 {index} 步 {step.tool} 意外报错：{step.result.error}"
                )
            continue
        if step.result.ok != expected.ok:
            failures.append(
                f"第 {index} 步 {step.tool} ok 期望 {expected.ok}，实际 {step.result.ok}"
            )
        if expected.error_contains and expected.error_contains not in (
            step.result.error or ""
        ):
            failures.append(
                f"第 {index} 步错误文本缺少 {expected.error_contains!r}"
            )
        for key, value in expected.metadata.items():
            if step.result.metadata.get(key) != value:
                failures.append(
                    f"第 {index} 步 metadata[{key!r}] 期望 {value!r}，"
                    f"实际 {step.result.metadata.get(key)!r}"
                )
        if (
            expected.require_citation
            and step.tool in FILE_CITING_TOOLS
            and step.result.ok
        ):
            citations = _result_citations(step.result)
            if not citations:
                failures.append(f"第 {index} 步 {step.tool} 没有返回可核对引用")
        if (
            step.tool in MARKET_TOOLS
            and step.result.ok
            and (not step.result.data_as_of or not step.result.data_time)
        ):
            failures.append(f"第 {index} 步 {step.tool} 缺少 data_as_of/data_time")
    return failures


def _result_citations(result: ToolResult) -> list[str]:
    metadata = result.metadata or {}
    citations = list(metadata.get("citations") or ())
    if metadata.get("citation"):
        citations.append(str(metadata["citation"]))
    for row in result.table or ():
        if row.get("citation"):
            citations.append(str(row["citation"]))
    return [str(item) for item in citations if str(item).strip()]


def _summarize(results: Sequence[EvalTaskResult]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    expected_calls = sum(result.expected_tool_calls for result in results)
    tool_matches = sum(result.tool_matches for result in results)
    sequence_matches = sum(
        1 for result in results if result.actual_tools == result.expected_tools
    )
    steps = [result.steps for result in results]
    latencies = [result.latency_ms for result in results]
    ordered = sorted(latencies)
    p95 = (
        ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
        if ordered
        else 0.0
    )
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "task_success_rate": round(passed / total, 6) if total else 0.0,
        "tool_selection_accuracy": (
            round(tool_matches / expected_calls, 6) if expected_calls else 0.0
        ),
        "tool_sequence_accuracy": round(sequence_matches / total, 6) if total else 0.0,
        "avg_steps": round(statistics.fmean(steps), 3) if steps else 0.0,
        "avg_latency_ms": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        "p95_latency_ms": round(p95, 3) if latencies else 0.0,
    }


def run_evaluation(
    tasks: Sequence[EvalTask], *, tasks_file: str = ""
) -> EvalReport:
    """执行整批任务并汇总指标。"""
    results = [run_task(task) for task in tasks]
    grouped: dict[str, list[EvalTaskResult]] = defaultdict(list)
    for result in results:
        grouped[result.category].append(result)
    categories = {
        category: _summarize(items) for category, items in grouped.items()
    }
    return EvalReport(
        summary=_summarize(results),
        categories=categories,
        tasks=results,
        tasks_file=tasks_file,
    )


def write_report(report: EvalReport, path: str | Path = DEFAULT_OUTPUT_PATH) -> Path:
    """原子式写入 UTF-8 JSON 报告（先写临时文件再替换）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(target)
    return target


def load_report(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("报告必须是 JSON object")
    return payload


def compare_reports(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """比较两份报告；时延只展示、不作为回归门槛。"""
    current_summary = current.get("summary") or {}
    baseline_summary = baseline.get("summary") or {}
    metric_rules = (
        ("task_success_rate", "higher"),
        ("tool_selection_accuracy", "higher"),
        ("tool_sequence_accuracy", "higher"),
        ("avg_steps", "lower"),
    )
    metrics: dict[str, dict[str, Any]] = {}
    metric_ok = True
    for name, direction in metric_rules:
        baseline_value = float(baseline_summary.get(name, 0.0))
        current_value = float(current_summary.get(name, 0.0))
        delta = current_value - baseline_value
        ok = delta >= -1e-9 if direction == "higher" else delta <= 1e-9
        metric_ok = metric_ok and ok
        metrics[name] = {
            "baseline": baseline_value,
            "current": current_value,
            "delta": round(delta, 6),
            "direction": direction,
            "ok": ok,
        }
    latency_before = float(baseline_summary.get("avg_latency_ms", 0.0))
    latency_after = float(current_summary.get("avg_latency_ms", 0.0))
    metrics["avg_latency_ms"] = {
        "baseline": latency_before,
        "current": latency_after,
        "delta": round(latency_after - latency_before, 3),
        "direction": "informational",
        "ok": True,
    }

    baseline_tasks = {
        str(task.get("id")): task for task in baseline.get("tasks") or ()
    }
    current_tasks = {
        str(task.get("id")): task for task in current.get("tasks") or ()
    }
    regressions: list[str] = []
    improvements: list[str] = []
    for task_id, baseline_task in baseline_tasks.items():
        current_task = current_tasks.get(task_id)
        if current_task is None or (
            baseline_task.get("passed") and not current_task.get("passed")
        ):
            regressions.append(task_id)
        elif not baseline_task.get("passed") and current_task.get("passed"):
            improvements.append(task_id)
    new_tasks = sorted(set(current_tasks) - set(baseline_tasks))
    current_failed = int(current_summary.get("failed", 0) or 0)
    ok = metric_ok and not regressions and current_failed == 0
    return {
        "ok": ok,
        "metrics": metrics,
        "regressions": sorted(regressions),
        "improvements": sorted(improvements),
        "new_tasks": new_tasks,
        "current_failed": current_failed,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 Agent 离线评测集")
    parser.add_argument("--tasks", default=DEFAULT_TASKS_PATH)
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--baseline", default=DEFAULT_BASELINE_PATH)
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="只跑评测并写报告，不与 baseline 比较",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        tasks = load_tasks(args.tasks)
        validate_dataset(tasks)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"评测集不可用：{exc}")
        return 2
    report = run_evaluation(tasks, tasks_file=str(args.tasks))
    target = write_report(report, args.output)
    summary = report.summary
    print(
        f"评测完成：{summary['passed']}/{summary['total']} 通过，"
        f"工具选择准确率 {summary['tool_selection_accuracy']:.2%}，"
        f"平均步数 {summary['avg_steps']:.2f}，"
        f"平均时延 {summary['avg_latency_ms']:.2f}ms；报告：{target}"
    )
    if args.no_baseline:
        return 0 if summary["failed"] == 0 else 1
    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"未找到 baseline：{baseline_path}（本次只写报告）")
        return 0 if summary["failed"] == 0 else 1
    comparison = compare_reports(report.to_dict(), load_report(baseline_path))
    print(
        "baseline 比较："
        + ("通过" if comparison["ok"] else "存在回归")
        + f"；regressions={comparison['regressions']}"
        + f"；improvements={comparison['improvements']}"
    )
    if not comparison["ok"]:
        return 1
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
