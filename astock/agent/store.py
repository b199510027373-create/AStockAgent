"""``agent_*`` 三表与 artifact 内容寻址落盘（《接口冻结清单》§5，ADR-017）。

本模块承担 §5 冻结 DDL 的**唯一实现点**：``AGENT_SCHEMA`` 与冻结清单逐字一致，
建表幂等，与既有五表同库（``data/astock.db``）。artifact 路径布局同样冻结：

    data/agent/artifacts/<digest[:2]>/<digest>/<filename>

当前实现范围 = 图表管线需要的**最小面**（plan-20261107-005）：

- ``connect()`` / ``ensure_schema()``：建表与索引；
- ``put_content()``：按 sha256 内容寻址写文件，同 digest 复用目录；
- ``record_artifact()``：插入 ``agent_artifacts`` 行（``artifact_id`` 由 digest+filename 派生，
  同一份内容重复渲染只保留一行，可凭 ``artifact_id`` 原样复现）；
- ``get_artifact()`` / ``find_artifacts_by_digest()``：读回记录与相对路径；
- ``ensure_run()``：为 ``agent_artifacts.run_id`` 的 NOT NULL 外键提供最小 run 行。

``agent_runs``/``agent_steps`` 的完整写入语义、留存清理（GC 不删审计行）由
plan-20261107-008 扩展；扩展时**不得改表结构**（冻结清单 §8 治理流程）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from astock.config import BASE_DIR

__all__ = [
    "AGENT_SCHEMA",
    "ARTIFACT_ID_RE",
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_DB_PATH",
    "STEP_KINDS",
    "ArtifactRecord",
    "artifact_id_for",
    "artifact_rel_path",
    "connect",
    "ensure_run",
    "find_artifacts_by_digest",
    "get_artifact",
    "put_content",
    "record_artifact",
    "record_step",
    "record_tool_step",
    "sha256_bytes",
    "utc_now",
]

#: 与冻结清单 §5 逐字一致的 DDL（``CREATE TABLE/INDEX IF NOT EXISTS``，建表幂等）。
AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs(
  run_id           TEXT PRIMARY KEY,
  session_id       TEXT NOT NULL,
  provider         TEXT NOT NULL,
  model            TEXT NOT NULL,
  temperature      REAL,
  seed             INTEGER,
  code_version     TEXT,
  status           TEXT NOT NULL CHECK(status IN ('running','success','partial','failed')),
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  steps            INTEGER NOT NULL DEFAULT 0,
  prompt_tokens    INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  error            TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps(
  run_id        TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  step          INTEGER NOT NULL,
  kind          TEXT NOT NULL CHECK(kind IN ('tool','model','final')),
  tool          TEXT,
  args_json     TEXT NOT NULL DEFAULT '{}',
  result_digest TEXT,
  latency_ms    INTEGER,
  created_at    TEXT NOT NULL,
  PRIMARY KEY(run_id, step)
);

CREATE TABLE IF NOT EXISTS agent_artifacts(
  artifact_id TEXT PRIMARY KEY,                 -- art_<16 hex>
  run_id      TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  step        INTEGER,
  kind        TEXT NOT NULL CHECK(kind IN ('figure','table','file','report')),
  path        TEXT NOT NULL,                    -- 相对 data/agent/ 的 POSIX 路径
  digest      TEXT NOT NULL,                    -- 文件内容 sha256
  meta_json   TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_session    ON agent_runs(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run       ON agent_steps(run_id, step);
CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run   ON agent_artifacts(run_id, step);
CREATE INDEX IF NOT EXISTS idx_agent_artifacts_digest ON agent_artifacts(digest);
"""

ARTIFACT_KINDS = ("figure", "table", "file", "report")
ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{16}$")

DEFAULT_DB_PATH = Path(os.getenv("ASTOCK_DB_PATH") or (BASE_DIR / "data" / "astock.db"))
DEFAULT_ARTIFACT_ROOT = Path(
    os.getenv("AGENT_ARTIFACT_DIR") or (BASE_DIR / "data" / "agent")
)


def utc_now() -> str:
    """审计时间戳统一用带时区的 UTC ISO 串。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def artifact_id_for(digest: str, filename: str) -> str:
    """``art_<16 hex>``：由内容 digest + 文件名派生，同一内容稳定复现同一 id。"""
    seed = f"{digest}\x00{filename}".encode("utf-8")
    return "art_" + hashlib.sha256(seed).hexdigest()[:16]


def artifact_rel_path(digest: str, filename: str) -> str:
    """相对 ``data/agent/`` 的 POSIX 路径（冻结布局，§5）。"""
    return f"artifacts/{digest[:2]}/{digest}/{filename}"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开审计库并确保三表存在（幂等）；``:memory:`` 可用于测试。"""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(AGENT_SCHEMA)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(AGENT_SCHEMA)


def ensure_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    session_id: str = "chart",
    provider: str = "local",
    model: str = "chart_render",
    temperature: float | None = None,
    seed: int | None = None,
    code_version: str | None = None,
    status: str = "success",
    started_at: str | None = None,
) -> str:
    """确保 ``agent_runs`` 有这一行（存在则不动），返回 run_id。

    渲染管线在独立调用（没有 Agent 循环）时也需要落 artifact；这里只补齐外键所需
    的最小 run 行，完整生命周期由 plan-20261107-008 接管。

    ``status="running"`` 的 run 不写 ``finished_at``（运行中不能被当成已结束）。
    """
    now = utc_now()
    started = started_at or now
    finished = None if status == "running" else now
    conn.execute(
        """
        INSERT INTO agent_runs(
            run_id, session_id, provider, model, temperature, seed, code_version,
            status, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO NOTHING
        """,
        (
            run_id,
            session_id,
            provider,
            model,
            temperature,
            seed,
            code_version,
            status,
            started,
            finished,
        ),
    )
    conn.commit()
    return run_id


def put_content(
    payload: bytes | str,
    filename: str,
    *,
    root: str | Path | None = None,
) -> tuple[str, str, Path]:
    """内容寻址写文件，返回 ``(digest, 相对路径, 绝对路径)``；同 digest 复用目录。"""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    digest = sha256_bytes(data)
    rel = artifact_rel_path(digest, filename)
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    absolute = base / rel
    absolute.parent.mkdir(parents=True, exist_ok=True)
    if not absolute.exists():
        absolute.write_bytes(data)
    return digest, rel, absolute


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    step: int | None
    kind: str
    path: str
    digest: str
    meta: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "step": self.step,
            "kind": self.kind,
            "path": self.path,
            "digest": self.digest,
            "meta": dict(self.meta),
            "created_at": self.created_at,
        }


def record_artifact(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    path: str,
    digest: str,
    meta: Mapping[str, Any] | None = None,
    step: int | None = None,
    created_at: str | None = None,
) -> str:
    """插入 ``agent_artifacts``（同 id 只插一行，meta 以最新一次写入为准），返回 ``artifact_id``。"""
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"kind 必须是 {ARTIFACT_KINDS} 之一，实际 {kind!r}")
    filename = Path(path).name
    artifact_id = artifact_id_for(digest, filename)
    payload = json.dumps(dict(meta or {}), ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        INSERT INTO agent_artifacts(
            artifact_id, run_id, step, kind, path, digest, meta_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(artifact_id) DO UPDATE SET meta_json = excluded.meta_json
        """,
        (artifact_id, run_id, step, kind, path, digest, payload, created_at or utc_now()),
    )
    conn.commit()
    return artifact_id


STEP_KINDS = ("tool", "model", "final")


def record_step(
    conn: sqlite3.Connection,
    run_id: str,
    step: int,
    kind: str,
    *,
    tool: str | None = None,
    args: Mapping[str, Any] | list[Any] | None = None,
    result_digest: str | None = None,
    latency_ms: int | None = None,
    created_at: str | None = None,
) -> None:
    """写 ``agent_steps``（同 ``(run_id, step)`` 幂等），审计流最小落点（§5）。"""
    if kind not in STEP_KINDS:
        raise ValueError(f"kind 必须是 {STEP_KINDS} 之一，实际 {kind!r}")
    payload = json.dumps(args if args is not None else {}, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        INSERT INTO agent_steps(
            run_id, step, kind, tool, args_json, result_digest, latency_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, step) DO NOTHING
        """,
        (
            run_id,
            int(step),
            kind,
            tool,
            payload,
            result_digest,
            latency_ms,
            created_at or utc_now(),
        ),
    )
    conn.commit()


def record_tool_step(
    conn: sqlite3.Connection,
    run_id: str,
    step: int,
    tool: str,
    args: Mapping[str, Any] | list[Any] | None = None,
    *,
    result_digest: str | None = None,
    latency_ms: int | None = None,
    created_at: str | None = None,
) -> None:
    """工具调用落一行 ``kind='tool'`` 的 step（参数 JSON 原样留痕）。"""
    record_step(
        conn,
        run_id,
        step,
        "tool",
        tool=tool,
        args=args,
        result_digest=result_digest,
        latency_ms=latency_ms,
        created_at=created_at,
    )


def _row_to_record(row: sqlite3.Row | None) -> ArtifactRecord | None:
    if row is None:
        return None
    try:
        meta = json.loads(row["meta_json"])
    except (TypeError, ValueError):
        meta = {}
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        run_id=row["run_id"],
        step=row["step"],
        kind=row["kind"],
        path=row["path"],
        digest=row["digest"],
        meta=meta if isinstance(meta, dict) else {},
        created_at=row["created_at"],
    )


def get_artifact(
    conn: sqlite3.Connection, artifact_id: str
) -> ArtifactRecord | None:
    row = conn.execute(
        "SELECT * FROM agent_artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    return _row_to_record(row)


def find_artifacts_by_digest(
    conn: sqlite3.Connection, digest: str
) -> list[ArtifactRecord]:
    rows = conn.execute(
        "SELECT * FROM agent_artifacts WHERE digest = ? ORDER BY created_at", (digest,)
    ).fetchall()
    return [record for record in (_row_to_record(row) for row in rows) if record]


def prune_artifacts(
    conn: sqlite3.Connection,
    *,
    before: str,
    keep_digests: Sequence[str] = (),
    root: str | Path | None = None,
    delete_files: bool = True,
) -> dict[str, int]:
    """删除 ``created_at < before`` 的 artifact 行，并按需清理不再被引用的文件。

    参数
    ----
    before:
        ISO 时间下界（与 ``created_at`` 同格式，UTC ISO 字符串）。
    keep_digests:
        命中这些 digest 的行一律保留（例如最近一次回答引用的图）。
    root:
        artifact 根目录，默认 :data:`DEFAULT_ARTIFACT_ROOT`。
    delete_files:
        ``True`` 时删除没有其他行引用的文件与空目录；仅删行时传 ``False``。

    返回 ``{"rows": 删除行数, "files": 删除文件数, "pruned_digests": 目录清理数}``。
    只用 ``Path.unlink``/``Path.rmdir``（架构门禁禁止 ``shutil.rmtree``）。
    """
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    keep = {digest for digest in keep_digests if digest}
    rows = conn.execute(
        "SELECT artifact_id, digest FROM agent_artifacts WHERE created_at < ? ORDER BY created_at",
        (before,),
    ).fetchall()
    targets = [row for row in rows if row["digest"] not in keep]
    removed_rows = 0
    removed_files = 0
    pruned_dirs = 0
    stale_digests: set[str] = set()
    for row in targets:
        conn.execute(
            "DELETE FROM agent_artifacts WHERE artifact_id = ?", (row["artifact_id"],)
        )
        removed_rows += 1
        stale_digests.add(row["digest"])
    if delete_files:
        for digest in stale_digests:
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_artifacts WHERE digest = ?", (digest,)
            ).fetchone()["n"]
            if remaining:
                continue
            directory = base / "artifacts" / digest[:2] / digest
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.is_file():
                    entry.unlink()
                    removed_files += 1
            try:
                directory.rmdir()
                pruned_dirs += 1
            except OSError:
                # 目录非空或已被占用：保留目录，不影响行删除结果。
                pass
        referenced = {
            row["digest"] for row in conn.execute("SELECT DISTINCT digest FROM agent_artifacts")
        }
        artifacts_root = base / "artifacts"
        if artifacts_root.is_dir():
            # 二次渲染/降级升级会留下没有 DB 行的 digest 目录（例如 PNG），一并清掉。
            for shard in sorted(artifacts_root.iterdir()):
                if not shard.is_dir():
                    continue
                for directory in sorted(shard.iterdir()):
                    if not directory.is_dir() or directory.name in referenced:
                        continue
                    for entry in sorted(directory.iterdir()):
                        if entry.is_file():
                            entry.unlink()
                            removed_files += 1
                    try:
                        directory.rmdir()
                        pruned_dirs += 1
                    except OSError:
                        pass
                try:
                    shard.rmdir()
                except OSError:
                    pass
    conn.commit()
    return {"rows": removed_rows, "files": removed_files, "pruned_digests": pruned_dirs}
