"""Agent 离线评测集与回归（plan-20261107-009）。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from astock.agent.evals import (
    CATEGORY_LABELS,
    compare_reports,
    load_report,
    load_tasks,
    main,
    run_evaluation,
    run_task,
    validate_dataset,
    write_report,
)
from astock.agent.types import ToolResult

TASKS_PATH = (
    Path(__file__).resolve().parents[1] / ".devloop" / "evals" / "agent_eval_tasks.json"
)


def _tasks():
    return load_tasks(TASKS_PATH)


def _report_path() -> Path:
    """在系统临时目录预留一个文件路径，避免依赖 pytest 的 tmp_path 目录扫描。"""
    handle, name = tempfile.mkstemp(prefix="agent-eval-", suffix=".json")
    os.close(handle)
    path = Path(name)
    path.unlink()
    return path


def test_dataset_covers_twenty_tasks_and_four_categories():
    tasks = _tasks()
    validate_dataset(tasks)

    assert len(tasks) == 20
    assert {task.category for task in tasks} == set(CATEGORY_LABELS)
    assert len({task.id for task in tasks}) == 20
    for category in CATEGORY_LABELS:
        assert sum(task.category == category for task in tasks) == 5


def test_dataset_validation_rejects_broken_coverage():
    tasks = list(_tasks())
    broken = [
        replace(task, category="not_a_category") if task.id == "pdf-001" else task
        for task in tasks
    ]

    with pytest.raises(ValueError, match="pdf_qa"):
        validate_dataset(broken)


def test_offline_evaluation_passes_and_records_required_metrics():
    tasks = _tasks()
    report = run_evaluation(tasks, tasks_file=str(TASKS_PATH))

    assert report.summary["total"] == 20
    assert report.summary["passed"] == 20
    assert report.summary["failed"] == 0
    assert report.summary["task_success_rate"] == 1.0
    assert report.summary["tool_selection_accuracy"] == 1.0
    assert report.summary["tool_sequence_accuracy"] == 1.0
    assert report.summary["avg_steps"] > 0
    assert report.summary["avg_latency_ms"] >= 0
    assert report.summary["p95_latency_ms"] >= 0
    assert set(report.categories) == set(CATEGORY_LABELS)
    assert all(result.passed for result in report.tasks)
    assert all(result.actual_tools == result.expected_tools for result in report.tasks)
    assert all(result.step_evidence for result in report.tasks)


def test_report_round_trip_and_baseline_comparison():
    report = run_evaluation(_tasks(), tasks_file=str(TASKS_PATH))
    path = _report_path()
    try:
        write_report(report, path)
        loaded = load_report(path)

        assert loaded["summary"]["passed"] == 20
        assert len(loaded["tasks"]) == 20
        identical = compare_reports(loaded, loaded)
        assert identical["ok"] is True
        assert identical["regressions"] == []
        assert identical["metrics"]["avg_latency_ms"]["direction"] == "informational"

        regressed = json.loads(json.dumps(loaded))
        regressed["summary"]["failed"] = 1
        regressed["summary"]["task_success_rate"] = 0.95
        regressed["tasks"][0]["passed"] = False
        comparison = compare_reports(regressed, loaded)
        assert comparison["ok"] is False
        assert loaded["tasks"][0]["id"] in comparison["regressions"]
    finally:
        path.unlink(missing_ok=True)


def test_missing_file_citation_is_reported_as_failure():
    task = next(task for task in _tasks() if task.id == "pdf-001")
    no_citation = replace(
        task.calls[0],
        result=ToolResult(
            text="Revenue 1250000",
            source="file:pdf_text_tables.pdf",
            metadata={"page": 1},
        ),
    )
    broken = replace(task, calls=(no_citation, *task.calls[1:]))

    result = run_task(broken)

    assert result.passed is False
    assert any("没有返回可核对引用" in failure for failure in result.failures)


def test_cli_writes_report_without_baseline():
    output = _report_path()
    try:
        exit_code = main(
            [
                "--tasks",
                str(TASKS_PATH),
                "--output",
                str(output),
                "--no-baseline",
            ]
        )

        assert exit_code == 0
        assert load_report(output)["summary"]["passed"] == 20
    finally:
        output.unlink(missing_ok=True)
