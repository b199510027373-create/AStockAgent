"""agent store v2 用例（ADR-017、规格 §4/§7/§8、plan-20261107-008）。

覆盖内容 / 版本 / 引用三表、不可变触发器、内容寻址去重、link 幂等、
删 run 保 artifact、指标排除 render、GC 只删无引用文件与 v1 迁移。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.agent import store

ROOT = Path(__file__).resolve().parents[1]
SPEC_DOC = ROOT / "docs" / "AStockAgent_chart_audit_v2_change_spec.md"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _norm_sql(statement: str) -> list[str]:
    sql = statement.strip().rstrip(";").lower()
    sql = re.sub(r"\s+if\s+not\s+exists\b", "", sql)
    sql = sql.replace("agent_artifact_links", "agent_run_artifacts")
    sql = sql.replace("(", " ( ").replace(")", " ) ").replace(",", " , ")
    return sql.split()


def _spec_statements() -> list[list[str]]:
    text = SPEC_DOC.read_text(encoding="utf-8")
    marker = "### 4.2 DDL"
    start = text.index(marker)
    fence = text.index("```sql", start) + len("```sql")
    end = text.index("```", fence)
    return [
        _norm_sql(part)
        for part in text[fence:end].split(";")
        if part.strip().lower().startswith("create")
    ]


def _schema_statements() -> list[list[str]]:
    return [
        _norm_sql(statement)
        for statement in store._SCHEMA_STATEMENTS
        if statement.strip().lower().startswith("create")
    ]


def _key(tokens: list[str]) -> tuple[str, str]:
    kind = tokens[1]
    if kind == "view":
        return kind, tokens[3]
    if kind == "index":
        return kind, tokens[2]
    # CREATE TABLE <name>
    return kind, tokens[2]


def _connect(tmp_path: Path) -> sqlite3.Connection:
    return store.connect(str(tmp_path / "agent.db"))


def _record(
    conn: sqlite3.Connection,
    root: Path,
    payload: bytes,
    *,
    filename: str = "chart.html",
    kind: str = "chart",
    env: str = "env1",
    meta: dict | None = None,
    run_id: str | None = None,
    step: int | None = None,
    source: str = "render",
    reason: str = "render",
    actor: str = "system",
) -> tuple[str, int]:
    digest, rel, _ = store.put_content(payload, filename, root=root)
    return store.record_artifact(
        conn,
        kind=kind,
        path=rel,
        content_digest=digest,
        env_fingerprint=env,
        meta=meta if meta is not None else {"spec": {"kind": "line"}},
        run_id=run_id,
        step=step,
        source=source,
        reason=reason,
        actor=actor,
    )


V1_SCHEMA = """
CREATE TABLE agent_runs(
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  temperature REAL,
  seed INTEGER,
  code_version TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  steps INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE agent_steps(
  run_id TEXT NOT NULL,
  step INTEGER NOT NULL,
  kind TEXT NOT NULL,
  tool TEXT,
  args_json TEXT NOT NULL DEFAULT '{}',
  result_digest TEXT,
  latency_ms INTEGER,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, step)
);
CREATE TABLE agent_artifacts(
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  step INTEGER,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  digest TEXT NOT NULL,
  meta_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _make_v1_db(path: Path, root: Path) -> None:
    payload = b"<html>v1-chart</html>"
    digest, rel, absolute = store.put_content(payload, "chart.html", root=root)
    absolute.parent.mkdir(parents=True, exist_ok=True)
    meta = {"spec": {"kind": "line"}, "code_version": "astock-old"}
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO agent_runs(run_id, session_id, provider, model, status, started_at, "
            "finished_at, steps, prompt_tokens, completion_tokens) VALUES "
            "('run_agent', 's1', 'fake', 'fake', 'success', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:01+00:00', 2, 10, 5)"
        )
        conn.execute(
            "INSERT INTO agent_runs(run_id, session_id, provider, model, status, started_at, "
            "finished_at) VALUES ('run_chart_art123', 'chart', 'local', 'chart_render', "
            "'success', '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:01+00:00')"
        )
        conn.execute(
            "INSERT INTO agent_steps(run_id, step, kind, tool, args_json, created_at) "
            "VALUES ('run_agent', 0, 'tool', 'file_read', '{}', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO agent_artifacts(artifact_id, run_id, step, kind, path, digest, "
            "meta_json, created_at) VALUES ('art_legacy01', 'run_chart_art123', 0, 'figure', "
            "?, ?, ?, '2026-01-02T00:00:00+00:00')",
            (rel, digest, store.canonical_json(meta)),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DDL / 连接
# ---------------------------------------------------------------------------


def test_schema_matches_frozen_v2_ddl() -> None:
    expected = {_key(tokens): tokens for tokens in _spec_statements()}
    actual = {_key(tokens): tokens for tokens in _schema_statements()}
    for name in ("agent_artifacts", "agent_artifact_versions", "agent_run_artifacts"):
        assert ("table", name) in actual, f"缺少冻结表 {name}"
    for key, tokens in expected.items():
        if key[0] == "view":
            continue
        assert key in actual, f"冻结清单里的 {key} 未实现"
        assert actual[key] == tokens, f"{key} 与冻结清单不一致：{actual[key]}"


def test_connect_is_idempotent_and_creates_v2_objects(tmp_path: Path) -> None:
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
        triggers = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        views = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            ).fetchall()
        }
        assert store.schema_state(conn) == "v2"
    finally:
        conn.close()
    assert {
        "agent_runs",
        "agent_steps",
        "agent_artifacts",
        "agent_artifact_versions",
        "agent_run_artifacts",
        "corrections",
    } <= tables
    assert {
        "idx_agent_runs_session",
        "idx_agent_runs_kind",
        "idx_agent_steps_run",
        "ix_agent_artifacts_digest",
        "ix_agent_artifacts_created",
        "ix_artifact_links_run",
        "ix_artifact_links_artifact",
    } <= indexes
    assert {
        "trg_agent_artifacts_no_update",
        "trg_agent_artifact_versions_no_update",
        "trg_corrections_no_update",
        "trg_corrections_no_delete",
    } <= triggers
    assert "agent_artifact_links" in views

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


def test_put_content_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        store.put_content(b"x", "../evil.html", root=tmp_path)
    with pytest.raises(ValueError):
        store.put_content(b"x", "a/b.html", root=tmp_path)


# ---------------------------------------------------------------------------
# 内容 / 版本 / 引用
# ---------------------------------------------------------------------------


def test_direct_render_writes_link_without_run(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        artifact_id, version = _record(conn, root, b"<html>render</html>")
        assert version == 1
        artifact = conn.execute("SELECT * FROM agent_artifacts").fetchone()
        assert "run_id" not in dict(artifact)
        assert artifact["artifact_id"] == artifact_id
        assert artifact["content_digest"].startswith("sha256:")
        assert artifact["env_fingerprint"] == "env1"
        link = conn.execute("SELECT * FROM agent_run_artifacts").fetchone()
        assert link["run_id"] is None
        assert link["source"] == "render"
        assert link["artifact_version"] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_artifact_links").fetchone()[0] == 1
    finally:
        conn.close()


def test_artifact_digest_dedupes(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        store.ensure_run(conn, "run_agent", session_id="s1", status="running")
        render_id, render_version = _record(conn, root, b"same-bytes")
        agent_id, agent_version = _record(
            conn,
            root,
            b"same-bytes",
            run_id="run_agent",
            step=3,
            source="agent",
            reason="agent_render",
            actor="agent",
        )
        assert (render_id, render_version) == (agent_id, agent_version)
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_artifact_versions").fetchone()[0] == 1
        links = conn.execute(
            "SELECT source, run_id, step FROM agent_run_artifacts ORDER BY link_id"
        ).fetchall()
        assert [(row["source"], row["run_id"], row["step"]) for row in links] == [
            ("render", None, None),
            ("agent", "run_agent", 3),
        ]
    finally:
        conn.close()


def test_artifact_identity_ignores_filename_but_includes_env(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        first = store.put_content(b"same-bytes", "a.html", root=root)
        second = store.put_content(b"same-bytes", "b.html", root=root)
        meta = {"spec": {"kind": "line"}}
        id_a, _ = store.record_artifact(
            conn,
            kind="chart",
            path=first[1],
            content_digest=first[0],
            env_fingerprint="env1",
            meta=meta,
        )
        id_b, _ = store.record_artifact(
            conn,
            kind="chart",
            path=second[1],
            content_digest=second[0],
            env_fingerprint="env1",
            meta=meta,
        )
        # 同内容不同文件名：同一 artifact 行，保留首个路径。
        assert id_a == id_b
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        stored = store.get_artifact(conn, id_a)
        assert stored is not None and stored.path == first[1]
        # 同内容不同来源：靠 link 保留归属（run/step 不同 → 两条 link）。
        store.ensure_run(conn, "run_agent", session_id="s1", status="running")
        third = store.put_content(b"same-bytes", "c.html", root=root)
        store.record_artifact(
            conn,
            kind="chart",
            path=third[1],
            content_digest=third[0],
            env_fingerprint="env1",
            meta=meta,
            run_id="run_agent",
            step=4,
            source="agent",
        )
        assert conn.execute("SELECT COUNT(*) FROM agent_run_artifacts").fetchone()[0] == 2

        env2 = store.put_content(b"same-bytes", "a.html", root=root)
        id_env2, _ = store.record_artifact(
            conn,
            kind="chart",
            path=env2[1],
            content_digest=env2[0],
            env_fingerprint="env2",
            meta=meta,
        )
        # 环境变化：不同 artifact，二者都仍可按 id 读回（P1-1）。
        assert id_env2 != id_a
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 2
        assert store.get_artifact(conn, id_a) is not None
        assert store.get_artifact(conn, id_env2) is not None
    finally:
        conn.close()


def test_new_meta_appends_version_and_link_is_idempotent(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        artifact_id, first = _record(conn, root, b"v", meta={"degraded": True})
        artifact_id, second = _record(conn, root, b"v", meta={"degraded": False})
        artifact_id, again = _record(conn, root, b"v", meta={"degraded": False})
        assert (first, second, again) == (1, 2, 2)
        assert conn.execute("SELECT COUNT(*) FROM agent_artifact_versions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM agent_run_artifacts").fetchone()[0] == 2
        latest = store.get_artifact(conn, artifact_id)
        assert latest is not None and latest.version == 2
        assert latest.meta == {"degraded": False}
    finally:
        conn.close()


def test_artifact_identity_conflicts_are_rejected(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        artifact_id, _ = _record(conn, root, b"one")
        other = store.put_content(b"two", "chart.html", root=root)
        with pytest.raises(store.ArtifactIdentityConflict):
            store.record_artifact(
                conn,
                artifact_id=artifact_id,
                kind="chart",
                path=other[1],
                content_digest=other[0],
                env_fingerprint="env1",
                meta={},
            )
        with pytest.raises(store.ArtifactIdentityConflict):
            store.record_artifact(
                conn,
                artifact_id=artifact_id,
                kind="chart",
                path=store.artifact_rel_path(store.sha256_bytes(b"one"), "chart.html"),
                content_digest=store.sha256_bytes(b"one"),
                env_fingerprint="env2",
                meta={},
            )
    finally:
        conn.close()


def test_artifacts_and_versions_are_immutable(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        _artifact_id, _ = _record(conn, root, b"x")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE agent_artifacts SET path = 'artifacts/x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE agent_artifact_versions SET actor = 'x'")
    finally:
        conn.rollback()
        conn.close()


def test_correct_artifact_appends_version_and_correction_rows(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        artifact_id, _ = _record(
            conn, root, b"x", meta={"data_as_of": "2026-01-01", "degraded": True}
        )
        with pytest.raises(ValueError):
            store.correct_artifact(conn, artifact_id, {"degraded": False}, reason="", actor="a")
        with pytest.raises(ValueError):
            store.correct_artifact(conn, artifact_id, {"degraded": False}, reason="r", actor="")
        with pytest.raises(ValueError):
            store.correct_artifact(conn, artifact_id, {"spec": {}}, reason="r", actor="a")
        version = store.correct_artifact(
            conn,
            artifact_id,
            {"degraded": False, "data_as_of": "2026-02-02"},
            reason="数据源修订",
            actor="analyst",
        )
        assert version == 2
        original = store.get_artifact(conn, artifact_id, version=1)
        assert original is not None
        assert original.meta["degraded"] is True
        corrected = store.get_artifact(conn, artifact_id, version=2)
        assert corrected is not None
        assert corrected.meta["degraded"] is False
        assert corrected.meta["data_as_of"] == "2026-02-02"
        corrections = conn.execute(
            "SELECT field, old_value, new_value, reason, actor FROM corrections ORDER BY correction_id"
        ).fetchall()
        assert [row["field"] for row in corrections] == ["degraded", "data_as_of"]
        assert corrections[0]["old_value"] == "true"
        assert corrections[0]["new_value"] == "false"
        assert corrections[0]["actor"] == "analyst"
    finally:
        conn.close()


def test_load_artifact_verifies_bytes_and_replays(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        snapshot = {"fields": ["x"], "rows": [{"x": 1}], "row_count": 1}
        payload = store.canonical_json(snapshot).encode("utf-8")
        data_digest, data_blob, _ = store.put_content(payload, "data.json", root=root)
        data_digest = store.normalize_content_digest(data_digest)
        artifact_id, _ = _record(
            conn,
            root,
            b"payload",
            meta={"data_digest": data_digest, "data_blob": data_blob},
        )
        loaded = store.load_artifact(conn, artifact_id, root=root)
        assert loaded["content"] == b"payload"
        assert loaded["version"] == 1
        recomputed = store.load_artifact(conn, artifact_id, mode="recomputed", root=root)
        assert recomputed["data"] == snapshot
        assert recomputed["data_digest"] == data_digest

        absolute = root / store.get_artifact(conn, artifact_id).path  # type: ignore[union-attr]
        absolute.write_bytes(b"tampered")
        with pytest.raises(store.ArtifactDigestMismatch):
            store.load_artifact(conn, artifact_id, root=root)
        absolute.unlink()
        with pytest.raises(store.ArtifactFileMissing):
            store.load_artifact(conn, artifact_id, root=root)
    finally:
        conn.close()


def test_recomputed_without_data_digest_is_unavailable(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        artifact_id, _ = _record(conn, root, b"payload", meta={"spec": {}})
        with pytest.raises(store.ArtifactReplayUnavailable):
            store.load_artifact(conn, artifact_id, mode="recomputed")
    finally:
        conn.close()


def test_deleting_run_keeps_artifacts_and_only_cascades_links(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        store.ensure_run(conn, "run_agent", session_id="s1", status="running")
        store.record_step(conn, "run_agent", 0, "tool", tool="file_read")
        artifact_id, version = _record(
            conn,
            root,
            b"kept",
            run_id="run_agent",
            step=0,
            source="agent",
            reason="agent_render",
            actor="agent",
        )
        conn.execute("DELETE FROM agent_runs WHERE run_id = 'run_agent'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_artifact_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_run_artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0] == 0
        record = store.get_artifact(conn, artifact_id, version=version)
        assert record is not None and record.meta["spec"] == {"kind": "line"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run / step / 指标
# ---------------------------------------------------------------------------


def test_run_identity_and_finish_are_enforced(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        store.ensure_run(conn, "run_1", session_id="s1", status="running")
        store.ensure_run(conn, "run_1", session_id="s1", status="running")
        with pytest.raises(store.RunIdentityConflict):
            store.ensure_run(conn, "run_1", session_id="other", status="running")
        with pytest.raises(store.RunIdentityConflict):
            store.ensure_run(conn, "run_1", session_id="s1", run_kind="render")
        with pytest.raises(ValueError):
            store.finish_run(conn, "run_1", status="running")
        store.record_step(conn, "run_1", 0, "tool", tool="file_read", args={"path": "a"})
        store.finish_run(conn, "run_1", status="success", token_usage=42, step_count=1)
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = 'run_1'").fetchone()
        assert row["status"] == "success"
        assert row["run_kind"] == "agent"
        assert row["finished_at"] is not None
        assert row["completion_tokens"] == 42
        assert row["steps"] == 1
    finally:
        conn.close()


def test_ensure_run_backfills_provenance_and_finish_keeps_failed(
    tmp_path: Path,
) -> None:
    conn = _connect(tmp_path)
    try:
        store.ensure_run(conn, "run_1", status="running")
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = 'run_1'").fetchone()
        assert row["temperature"] is None
        assert row["seed"] is None
        assert row["code_version"] is None
        # 后续调用补齐空列，但不覆盖已有非空值（P1-2）。
        store.ensure_run(
            conn,
            "run_1",
            status="running",
            temperature=0.2,
            seed=7,
            code_version="astock-test",
        )
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = 'run_1'").fetchone()
        assert row["temperature"] == pytest.approx(0.2)
        assert row["seed"] == 7
        assert row["code_version"] == "astock-test"

        store.fail_run(conn, "run_1", error="PNG 导出失败")
        store.finish_run(conn, "run_1", status="success", token_usage=9)
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = 'run_1'").fetchone()
        # 硬失败不被收尾降级，error 不被清空（P1-4）。
        assert row["status"] == "failed"
        assert row["error"] == "PNG 导出失败"
        assert row["finished_at"] is not None
        assert row["completion_tokens"] == 9
    finally:
        conn.close()


def test_finish_run_documents_total_tokens_in_completion(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        store.ensure_run(conn, "run_1", status="running")
        store.finish_run(
            conn, "run_1", status="success", token_usage=120, prompt_tokens=0
        )
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = 'run_1'").fetchone()
        # 网关只汇总整轮总 token：未拆分时总数记 completion，prompt 保持 0，
        # 成本口径 = prompt_tokens + completion_tokens（P2-4）。
        assert row["completion_tokens"] == 120
        assert row["prompt_tokens"] == 0
        store.ensure_run(conn, "run_2", status="running")
        store.finish_run(
            conn, "run_2", status="success", token_usage=80, prompt_tokens=20
        )
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = 'run_2'").fetchone()
        assert (row["prompt_tokens"], row["completion_tokens"]) == (20, 80)
    finally:
        conn.close()


def test_tool_call_writes_step_row(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        store.ensure_run(conn, "run_1", status="running")
        store.record_tool_step(
            conn,
            "run_1",
            2,
            "file_read",
            {"file": "a.pdf", "page": 1},
            result_digest="sha256:" + "0" * 64,
            result_preview="不可信文件正文" * 100,
            latency_ms=17,
        )
        row = conn.execute("SELECT * FROM agent_steps WHERE run_id = 'run_1'").fetchone()
        payload = json.loads(row["args_json"])
        assert payload["arguments"] == {"file": "a.pdf", "page": 1}
        assert payload["result_preview"].startswith("不可信文件正文")
        assert len(payload["result_preview"]) == 500
        assert row["result_digest"] == "sha256:" + "0" * 64
        assert row["latency_ms"] == 17
        with pytest.raises(ValueError):
            store.record_step(conn, "run_1", 3, "bogus")
    finally:
        conn.close()


def test_agent_metrics_exclude_render_runs(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    try:
        store.ensure_run(conn, "run_agent", status="running", run_kind="agent")
        store.finish_run(conn, "run_agent", status="success", token_usage=100, step_count=3)
        store.ensure_run(conn, "run_chart_1", status="running", run_kind="render")
        store.finish_run(conn, "run_chart_1", status="success", token_usage=500, step_count=9)
        store.ensure_run(conn, "run_failed", status="running", run_kind="agent")
        store.finish_run(conn, "run_failed", status="failed", token_usage=1000, step_count=2)
        metrics = store.agent_metrics(conn)
        assert metrics == {"runs": 1, "steps": 3, "tokens": 100}
        assert store.agent_metrics(conn, include_render=True)["runs"] == 2
    finally:
        conn.close()


def test_prune_runs_only_removes_render_and_legacy(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat(timespec="seconds")
    try:
        store.ensure_run(conn, "run_agent", status="success", started_at=old)
        store.ensure_run(conn, "run_chart_x", status="success", run_kind="render", started_at=old)
        store.ensure_run(conn, "run_legacy", status="success", run_kind="legacy", started_at=old)
        _record(
            conn,
            root,
            b"kept",
            run_id="run_agent",
            source="agent",
            reason="agent_render",
            actor="agent",
        )
        removed = store.prune_runs(conn, older_than_days=90)
        assert removed == 2
        remaining = {row["run_id"] for row in conn.execute("SELECT run_id FROM agent_runs")}
        assert remaining == {"run_agent"}
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_run_artifacts").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------


def _old_mtime(path: Path, days: int = 200) -> None:
    stamp = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


def test_prune_orphan_blobs_is_dry_run_by_default(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        _record(conn, root, b"referenced")
        protected = next((root / "artifacts").rglob("*.html"))
        orphan = root / "artifacts" / "zz" / ("f" * 64) / "orphan.html"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")
        _old_mtime(protected)
        _old_mtime(orphan)

        report = store.prune_orphan_blobs(conn, older_than_days=90, root=root)
        assert report["dry_run"] is True
        assert report["candidates"] == [orphan.relative_to(root).as_posix()]
        assert orphan.is_file() and protected.is_file()
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1

        report = store.prune_orphan_blobs(
            conn, older_than_days=90, root=root, dry_run=False
        )
        assert report["deleted"] == 1
        assert not orphan.exists()
        assert protected.is_file()
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_artifacts_keeps_referenced_files_and_rows(tmp_path: Path) -> None:
    conn = _connect(tmp_path)
    root = tmp_path / "agent"
    try:
        artifact_id, _ = _record(conn, root, b"referenced")
        referenced = root / store.get_artifact(conn, artifact_id).path  # type: ignore[union-attr]
        orphan = root / "artifacts" / "zz" / ("e" * 64) / "orphan.html"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"orphan")
        _old_mtime(referenced)
        _old_mtime(orphan)
        result = store.prune_artifacts(conn, before=store.utc_now(), root=root)
        assert result["rows"] == 0
        assert result["files"] == 1
        assert referenced.is_file()
        assert not orphan.exists()
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_artifact_versions").fetchone()[0] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# v1 → v2 迁移
# ---------------------------------------------------------------------------


def test_migrate_v1_fixture_to_v2(tmp_path: Path) -> None:
    db_path = tmp_path / "astock.db"
    root = tmp_path / "agent"
    _make_v1_db(db_path, root)

    conn = store.connect(db_path)
    try:
        assert store.schema_state(conn) == "v2"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        runs = {
            row["run_id"]: row["run_kind"]
            for row in conn.execute("SELECT run_id, run_kind FROM agent_runs")
        }
        assert runs == {"run_agent": "agent", "run_chart_art123": "render"}
        artifact = conn.execute("SELECT * FROM agent_artifacts").fetchone()
        assert artifact["artifact_id"] == "art_legacy01"
        assert artifact["kind"] == "chart"
        assert artifact["env_fingerprint"] == "legacy"
        assert artifact["content_digest"].startswith("sha256:")
        version = conn.execute("SELECT * FROM agent_artifact_versions").fetchone()
        assert version["version"] == 1
        assert version["reason"] == "migration"
        assert version["actor"] == "migration"
        assert json.loads(version["meta_json"])["schema_version"] == "legacy"
        link = conn.execute("SELECT * FROM agent_run_artifacts").fetchone()
        assert link["source"] == "legacy"
        assert link["run_id"] == "run_chart_art123"
        assert link["artifact_version"] == 1
        assert conn.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0] == 1
        legacy_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_v1'"
            )
        }
        assert legacy_tables == set()
        assert store.migrate_v1_to_v2(conn, db_path=db_path)["status"] == "already_v2"
    finally:
        conn.close()

    backups = list(tmp_path.glob("astock.db.bak-*"))
    assert len(backups) == 1
    backup = sqlite3.connect(str(backups[0]))
    try:
        assert backup.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        assert "run_kind" not in {
            row[1] for row in backup.execute("PRAGMA table_info(agent_runs)")
        }
    finally:
        backup.close()


def test_migrate_empty_database_creates_v2(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = store.connect(db_path)
    try:
        assert store.schema_state(conn) == "v2"
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 0
    finally:
        conn.close()


def test_ensure_schema_backs_up_using_the_connection_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "custom-location.db"
    _make_v1_db(db_path, tmp_path / "agent")
    # 若调用方连接的是自定义库，备份必须挨着它，而不是 DEFAULT_DB_PATH（P2-2）。
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", tmp_path / "wrong.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        store.ensure_schema(conn)
        assert store.schema_state(conn) == "v2"
    finally:
        conn.close()
    assert list(tmp_path.glob("custom-location.db.bak-*"))
    assert not (tmp_path / "wrong.db.bak-1").exists()


def test_backup_aborts_when_wal_checkpoint_is_busy(tmp_path: Path) -> None:
    class _BusyConnection:
        def execute(self, statement: str):
            class _Cursor:
                def fetchone(self):
                    return (1, 0, 0)

            return _Cursor()

        def backup(self, destination):  # pragma: no cover - 不应被调用
            raise AssertionError("checkpoint 未完成时不得备份")

    target = tmp_path / "astock.db"
    target.write_bytes(b"stub")
    with pytest.raises(RuntimeError, match="wal_checkpoint"):
        store._backup_database(_BusyConnection(), target)  # type: ignore[arg-type]
