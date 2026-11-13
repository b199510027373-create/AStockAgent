"""agent store 用例（冻结清单 §5）。

只覆盖本任务用到的能力：DDL 与冻结清单逐字一致、artifact 内容寻址去重、
``agent_artifacts`` 记录读写与按 digest 反查。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astock.agent import store
from astock.agent.registry import ToolRegistry, ToolSpec

ROOT = Path(__file__).resolve().parents[1]
FREEZE_DOC = ROOT / "docs" / "AStockAgent_interface_freeze_v1.md"


def _freeze_text() -> str:
    return FREEZE_DOC.read_text(encoding="utf-8")


def test_schema_matches_frozen_ddl_verbatim() -> None:
    freeze = _freeze_text()
    for statement in store.AGENT_SCHEMA.split(";"):
        body = statement.strip()
        if not body.upper().startswith("CREATE"):
            continue
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            assert line in freeze, f"DDL 行未出现在冻结清单：{line}"


def test_connect_is_idempotent_and_creates_indexes(tmp_path: Path) -> None:
    db_path = str(tmp_path / "agent.db")
    conn = store.connect(db_path)
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"agent_runs", "agent_steps", "agent_artifacts"} <= tables
    assert {
        "idx_agent_runs_session",
        "idx_agent_steps_run",
        "idx_agent_artifacts_run",
        "idx_agent_artifacts_digest",
    } <= indexes

    again = store.connect(db_path)
    again.close()


def test_put_content_is_content_addressed(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    digest, rel_path, absolute = store.put_content(b"payload", "chart.html", root=root)
    assert digest == store.sha256_bytes(b"payload")
    assert rel_path == f"artifacts/{digest[:2]}/{digest}/chart.html"
    assert absolute == root / rel_path

    again = store.put_content(b"payload", "chart.html", root=root)
    assert again == (digest, rel_path, absolute)


def test_record_and_read_artifact(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        store.ensure_run(conn, "run_1", session_id="s1")
        digest, rel_path, _ = store.put_content("{}", "chart.html", root=tmp_path / "agent")
        artifact_id = store.record_artifact(
            conn,
            run_id="run_1",
            kind="figure",
            path=rel_path,
            digest=digest,
            meta={"spec": {"version": 1}},
        )
        assert store.ARTIFACT_ID_RE.match(artifact_id)
        record = store.get_artifact(conn, artifact_id)
        assert record is not None
        assert record.kind == "figure"
        assert record.path == rel_path
        assert record.meta == {"spec": {"version": 1}}
        assert [item.artifact_id for item in store.find_artifacts_by_digest(conn, digest)] == [
            artifact_id
        ]
        # 重复写入同一 artifact_id 不报错也不重复
        assert (
            store.record_artifact(
                conn, run_id="run_1", kind="figure", path=rel_path, digest=digest
            )
            == artifact_id
        )
        assert store.get_artifact(conn, "art_ffffffffffffffff") is None
    finally:
        conn.close()


def test_artifact_digest_dedupes(tmp_path: Path) -> None:
    """同一份内容重复落库只留一行，两次返回同一 artifact_id（§5 约定）。"""
    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        store.ensure_run(conn, "run_a")
        store.ensure_run(conn, "run_b")
        digest, rel_path, _ = store.put_content("same-bytes", "chart.html", root=tmp_path / "agent")
        first = store.record_artifact(
            conn, run_id="run_a", kind="figure", path=rel_path, digest=digest
        )
        second = store.record_artifact(
            conn, run_id="run_b", kind="figure", path=rel_path, digest=digest
        )
        assert first == second
        rows = conn.execute("SELECT * FROM agent_artifacts").fetchall()
        assert len(rows) == 1
        assert rows[0]["artifact_id"] == first
        assert len(store.find_artifacts_by_digest(conn, digest)) == 1
    finally:
        conn.close()


def test_tool_call_writes_step_row(tmp_path: Path) -> None:
    """工具调用留痕：调用结果 + 参数写入 agent_steps（kind='tool'）。"""

    def handler(*, code: str) -> list[dict]:
        return [{"ts_code": code, "close": 10.0}]

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="quote",
            description="查询最新收盘价",
            parameters={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
                "additionalProperties": False,
            },
            handler=handler,
            permissions=["market:read"],
        )
    )
    result = registry.invoke("quote", {"code": "000001.SZ"})
    assert result.error is None

    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        store.ensure_run(conn, "run_1", session_id="s1")
        digest = store.sha256_bytes(result.to_json().encode("utf-8"))
        store.record_tool_step(
            conn, "run_1", 1, "quote", {"code": "000001.SZ"}, result_digest=digest, latency_ms=12
        )
        # 重试同一步不产生第二行（幂等）
        store.record_tool_step(conn, "run_1", 1, "quote", {"code": "000001.SZ"})
        row = conn.execute("SELECT * FROM agent_steps").fetchone()
        assert row["kind"] == "tool"
        assert row["tool"] == "quote"
        assert json.loads(row["args_json"]) == {"code": "000001.SZ"}
        assert row["result_digest"] == digest
        assert row["latency_ms"] == 12
    finally:
        conn.close()


def test_record_step_rejects_unknown_kind(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        store.ensure_run(conn, "run_1")
        with pytest.raises(ValueError):
            store.record_step(conn, "run_1", 1, "thinking")
    finally:
        conn.close()



    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        store.ensure_run(conn, "run_1")
        with pytest.raises(ValueError):
            store.record_artifact(
                conn, run_id="run_1", kind="video", path="x", digest="0" * 64
            )
    finally:
        conn.close()


def test_ensure_run_keeps_first_record(tmp_path: Path) -> None:
    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        store.ensure_run(conn, "run_1", session_id="s1", model="chart_render")
        store.ensure_run(conn, "run_1", session_id="s1", model="other")
        rows = conn.execute("SELECT * FROM agent_runs").fetchall()
        assert len(rows) == 1
        assert rows[0]["model"] == "chart_render"
        assert rows[0]["status"] == "success"
        assert rows[0]["steps"] == 0
    finally:
        conn.close()
