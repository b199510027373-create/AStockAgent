"""``agent_*`` 审计表 v2 与 artifact 内容寻址落盘（ADR-017、plan-20261107-008）。

v2 模型把「内容 / 版本 / 引用」拆开（chart_audit_v2_change_spec §4）：

- ``agent_artifacts``：一行一个逻辑 artifact，**没有 run_id**，不可 UPDATE；
- ``agent_artifact_versions``：meta 版本，v1 创建、修正追加 v2/v3，不可 UPDATE；
- ``agent_run_artifacts``：artifact 被谁引用（run / 直接渲染 / 导入），``run_id`` 可空；
  ``agent_artifact_links`` 是同一引用表的兼容只读视图（规格 §4.2 的旧名）；
- ``corrections``：字段级修正流水，只追加，actor/理由/时间必填。

``agent_runs`` 只记录真实 Agent 会话；直接渲染写 ``source='render', run_id=NULL``，
不再合成 ``run_chart_<artifact_id>``。删 run 只级联删 link，artifact/version 行永久保留；
文件由 :func:`prune_orphan_blobs` 按「无引用 + 保留期」清理。

布局沿用冻结约定：``data/agent/artifacts/<digest[:2]>/<digest>/<filename>``。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from astock.config import BASE_DIR

__all__ = [
    "AGENT_SCHEMA",
    "ARTIFACT_ID_RE",
    "ARTIFACT_KINDS",
    "CORRECTION_FIELDS",
    "DEFAULT_ARTIFACT_ROOT",
    "DEFAULT_DB_PATH",
    "LEGACY_KIND_MAP",
    "PRUNE_RUN_KINDS",
    "RUN_KINDS",
    "RUN_STATUSES",
    "STEP_KINDS",
    "ArtifactDigestMismatch",
    "ArtifactFileMissing",
    "ArtifactIdentityConflict",
    "ArtifactRecord",
    "ArtifactReplayUnavailable",
    "RunIdentityConflict",
    "agent_metrics",
    "artifact_id_for",
    "artifact_rel_path",
    "canonical_json",
    "compute_env_fingerprint",
    "connect",
    "correct_artifact",
    "ensure_run",
    "ensure_schema",
    "fail_run",
    "find_artifacts_by_digest",
    "finish_run",
    "get_artifact",
    "load_artifact",
    "meta_digest",
    "migrate_v1_to_v2",
    "normalize_content_digest",
    "prune_artifacts",
    "prune_orphan_blobs",
    "prune_runs",
    "put_content",
    "record_artifact",
    "record_step",
    "record_tool_step",
    "schema_state",
    "sha256_bytes",
    "utc_now",
]

ARTIFACT_KINDS = ("chart", "table", "file", "image")
LEGACY_KIND_MAP = {"figure": "chart", "report": "file"}
ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{16}$")
RUN_KINDS = ("agent", "render", "legacy")
RUN_STATUSES = ("running", "success", "partial", "failed")
STEP_KINDS = ("tool", "model", "final")
CORRECTION_FIELDS = (
    "source",
    "data_as_of",
    "data_time",
    "view_as_of",
    "view_time",
    "session",
    "degraded",
    "warnings",
)
#: 可被保留期清理的 run 类型：真实 Agent 会话（``agent``）永不删除（ADR-017 C3）。
PRUNE_RUN_KINDS = ("render", "legacy")

# 渲染器 PNG 导出降级时的告警前缀；记录新版本时靠它识别「已有 PNG 不应被降级覆盖」。
PNG_DEGRADED_WARNING_PREFIX = "PNG 导出降级"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

DEFAULT_DB_PATH = Path(os.getenv("ASTOCK_DB_PATH") or (BASE_DIR / "data" / "astock.db"))
DEFAULT_ARTIFACT_ROOT = Path(
    os.getenv("AGENT_ARTIFACT_DIR") or (BASE_DIR / "data" / "agent")
)

# ---------------------------------------------------------------------------
# 冻结 DDL（schema_state / 迁移脚本共用同一份语句）
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS agent_runs(
  run_id           TEXT PRIMARY KEY,
  session_id       TEXT NOT NULL,
  provider         TEXT NOT NULL,
  model            TEXT NOT NULL,
  temperature      REAL,
  seed             INTEGER,
  code_version     TEXT,
  status           TEXT NOT NULL CHECK(status IN ('running','success','partial','failed')),
  run_kind         TEXT NOT NULL DEFAULT 'agent'
                   CHECK(run_kind IN ('agent','render','legacy')),
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  steps            INTEGER NOT NULL DEFAULT 0,
  prompt_tokens    INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  error            TEXT
)
""",
    """
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
)
""",
    """
CREATE TABLE IF NOT EXISTS agent_artifacts(
  artifact_id     TEXT PRIMARY KEY,
  kind            TEXT NOT NULL CHECK(kind IN ('chart','table','file','image')),
  path            TEXT NOT NULL,
  content_digest  TEXT NOT NULL,
  env_fingerprint TEXT NOT NULL,
  created_at      TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS agent_artifact_versions(
  artifact_id  TEXT NOT NULL
               REFERENCES agent_artifacts(artifact_id) ON DELETE RESTRICT,
  version      INTEGER NOT NULL CHECK(version >= 1),
  meta_json    TEXT NOT NULL,
  meta_digest  TEXT NOT NULL,
  reason       TEXT NOT NULL,
  actor        TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY(artifact_id, version)
)
""",
    """
CREATE TABLE IF NOT EXISTS agent_run_artifacts(
  link_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id      TEXT NOT NULL
                   REFERENCES agent_artifacts(artifact_id) ON DELETE RESTRICT,
  artifact_version INTEGER NOT NULL,
  run_id           TEXT
                   REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  step             INTEGER,
  source           TEXT NOT NULL
                   CHECK(source IN ('agent','render','import','legacy')),
  session_id       TEXT,
  created_at       TEXT NOT NULL,
  FOREIGN KEY (artifact_id, artifact_version)
      REFERENCES agent_artifact_versions(artifact_id, version)
      ON DELETE RESTRICT
)
""",
    """
CREATE TABLE IF NOT EXISTS corrections(
  correction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id      TEXT NOT NULL
                   REFERENCES agent_artifacts(artifact_id) ON DELETE RESTRICT,
  artifact_version INTEGER NOT NULL,
  field            TEXT NOT NULL,
  old_value        TEXT,
  new_value        TEXT,
  reason           TEXT NOT NULL,
  actor            TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  FOREIGN KEY (artifact_id, artifact_version)
      REFERENCES agent_artifact_versions(artifact_id, version)
      ON DELETE RESTRICT
)
""",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_kind ON agent_runs(run_kind, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, step)",
    "CREATE INDEX IF NOT EXISTS ix_agent_artifacts_digest ON agent_artifacts(content_digest)",
    "CREATE INDEX IF NOT EXISTS ix_agent_artifacts_created ON agent_artifacts(created_at)",
    "CREATE INDEX IF NOT EXISTS ix_artifact_links_run ON agent_run_artifacts(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_artifact_links_artifact ON agent_run_artifacts(artifact_id)",
    "CREATE INDEX IF NOT EXISTS ix_corrections_artifact ON corrections(artifact_id, artifact_version)",
    """
CREATE VIEW IF NOT EXISTS agent_artifact_links AS
SELECT link_id, artifact_id, artifact_version, run_id, step, source, session_id, created_at
FROM agent_run_artifacts
""",
    """
CREATE TRIGGER IF NOT EXISTS trg_agent_artifacts_no_update
BEFORE UPDATE ON agent_artifacts
BEGIN
    SELECT RAISE(ABORT, 'agent_artifacts is immutable');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS trg_agent_artifact_versions_no_update
BEFORE UPDATE ON agent_artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'agent_artifact_versions is append-only');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS trg_corrections_no_update
BEFORE UPDATE ON corrections
BEGIN
    SELECT RAISE(ABORT, 'corrections is append-only');
END
""",
    """
CREATE TRIGGER IF NOT EXISTS trg_corrections_no_delete
BEFORE DELETE ON corrections
BEGIN
    SELECT RAISE(ABORT, 'corrections is append-only');
END
""",
)

AGENT_SCHEMA = ";\n\n".join(statement.strip() for statement in _SCHEMA_STATEMENTS) + ";\n"


class ArtifactIdentityConflict(RuntimeError):
    """同一 ``artifact_id`` 出现不同内容/环境身份（内容寻址被破坏）。"""


class ArtifactFileMissing(FileNotFoundError):
    """artifact 行存在但文件缺失；不降级、不返回空内容。"""


class ArtifactDigestMismatch(RuntimeError):
    """文件内容与 ``content_digest`` 不一致（被篡改或写错路径）。"""


class ArtifactReplayUnavailable(RuntimeError):
    """``recomputed`` 回放缺少数据快照，无法重算。"""


class RunIdentityConflict(RuntimeError):
    """同一 ``run_id`` 的 ``run_kind``/``session_id`` 与既有行冲突。"""


def utc_now() -> str:
    """审计时间戳统一用带时区的 UTC ISO 串。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: Any) -> str:
    """canonical JSON：键排序、无多余空格（digest 稳定性的前提）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_content_digest(value: str) -> str:
    """接受裸 sha256 或 ``sha256:`` 前缀，统一返回 ``sha256:<hex>``。"""
    text = str(value or "").strip().removeprefix("sha256:")
    if not _DIGEST_RE.match(text):
        raise ValueError(f"content_digest 必须是 sha256 hex，实际 {value!r}")
    return f"sha256:{text}"


def meta_digest(meta: Mapping[str, Any]) -> str:
    return "sha256:" + sha256_bytes(canonical_json(dict(meta)).encode("utf-8"))


def compute_env_fingerprint(
    *,
    plotly_version: str = "",
    python_version: str | None = None,
    code_version: str = "",
) -> str:
    """环境指纹：``plotly`` / Python / 代码版本三件套，取 sha256 前 16 位。"""
    py = python_version or platform.python_version()
    raw = f"plotly={plotly_version}\npython={py}\ncode={code_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def artifact_id_for(
    digest: str,
    filename: str | None = None,
    *,
    env_fingerprint: str = "",
    canonical_spec: str = "",
) -> str:
    """``art_<16 hex>``：按冻结规格 §4.4 由 spec + 数据 digest + 环境派生。

    文件名不参与逻辑身份（只决定落盘路径）：同内容换文件名仍指向同一 artifact
    行；环境变化（plotly/python/code_version）则生成新 artifact 而不是撞身份。
    ``filename`` 仅为兼容旧调用保留，不再进入种子。
    """
    del filename  # 身份与文件名无关（见规格 §4.4）
    data_digest = normalize_content_digest(digest)
    seed = f"{canonical_spec}\n{data_digest}\n{env_fingerprint}".encode()
    return "art_" + hashlib.sha256(seed).hexdigest()[:16]


def artifact_rel_path(digest: str, filename: str) -> str:
    """相对 ``data/agent/`` 的 POSIX 路径（冻结布局）。"""
    bare = digest.split(":", 1)[-1]
    return f"artifacts/{bare[:2]}/{bare}/{filename}"


def _safe_filename(filename: str) -> str:
    """文件名白名单：只允许单段名称，禁止绝对路径与 ``..`` 穿越。"""
    text = str(filename or "")
    if not text or text in (".", "..") or "/" in text or "\\" in text:
        raise ValueError(f"filename 必须是单段文件名，实际 {filename!r}")
    if Path(text).name != text:
        raise ValueError(f"filename 必须是单段文件名，实际 {filename!r}")
    return text


def _validate_rel_path(path: str) -> str:
    """artifact 路径白名单：POSIX 相对路径，必须位于 ``artifacts/`` 下。"""
    text = str(path or "")
    if not text or "\\" in text:
        raise ValueError(f"artifact path 必须是 POSIX 相对路径，实际 {path!r}")
    posix = PurePosixPath(text)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        raise ValueError(f"artifact path 不得包含绝对路径/穿越片段，实际 {path!r}")
    if not posix.parts or posix.parts[0] != "artifacts":
        raise ValueError(f"artifact path 必须位于 artifacts/ 下，实际 {path!r}")
    return posix.as_posix()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def schema_state(conn: sqlite3.Connection) -> str:
    """返回 ``empty`` / ``v1`` / ``v2`` / ``unknown``。"""
    if not _table_exists(conn, "agent_runs") or not _table_exists(conn, "agent_artifacts"):
        return "empty"
    columns = _table_columns(conn, "agent_artifacts")
    if "content_digest" in columns and _table_exists(conn, "agent_artifact_versions"):
        return "v2"
    if "run_id" in columns or "digest" in columns:
        return "v1"
    return "unknown"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开审计库并确保 v2 表存在；旧 v1 库会就地迁移（先备份）。"""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    is_memory = str(path) == ":memory:"
    if not is_memory:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    state = schema_state(conn)
    if state == "v1":
        migrate_v1_to_v2(conn, db_path=None if is_memory else path)
    elif state == "unknown":
        raise RuntimeError("agent_* 表结构无法识别：既不是 v1 也不是 v2")
    conn.executescript(AGENT_SCHEMA)
    conn.commit()
    return conn


def _connection_db_path(conn: sqlite3.Connection) -> Path | None:
    """从 ``PRAGMA database_list`` 取 main 库文件路径；内存库返回 None。"""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.DatabaseError:
        return None
    for row in rows:
        name, file = row[1], row[2]
        if name == "main" and file:
            return Path(file)
    return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    state = schema_state(conn)
    if state == "v1":
        # 自定义连接没有 db_path 参数：从连接本身取，避免备份 DEFAULT_DB_PATH（P2-2）。
        migrate_v1_to_v2(conn, db_path=_connection_db_path(conn))
    elif state == "unknown":
        raise RuntimeError("agent_* 表结构无法识别：既不是 v1 也不是 v2")
    conn.executescript(AGENT_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# 真实 Agent run
# ---------------------------------------------------------------------------


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
    status: str = "running",
    run_kind: str = "agent",
    started_at: str | None = None,
) -> str:
    """确保 ``agent_runs`` 有这一行；已存在时不覆盖，但校验身份一致。

    v2 起 ``agent_runs`` 只服务真实 Agent（``run_kind='agent'``）；直接渲染不调用本函数。
    """
    if status not in RUN_STATUSES:
        raise ValueError(f"status 必须是 {RUN_STATUSES} 之一，实际 {status!r}")
    if run_kind not in RUN_KINDS:
        raise ValueError(f"run_kind 必须是 {RUN_KINDS} 之一，实际 {run_kind!r}")
    now = utc_now()
    started = started_at or now
    finished = None if status == "running" else now
    cursor = conn.execute(
        """
        INSERT INTO agent_runs(
            run_id, session_id, provider, model, temperature, seed, code_version,
            status, run_kind, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            run_kind,
            started,
            finished,
        ),
    )
    if cursor.rowcount == 0:
        row = conn.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is not None and (
            row["run_kind"] != run_kind or row["session_id"] != session_id
        ):
            raise RunIdentityConflict(
                f"run_id={run_id!r} 已存在且身份不一致："
                f"run_kind={row['run_kind']!r}/{run_kind!r}, "
                f"session_id={row['session_id']!r}/{session_id!r}"
            )
        # 冲突时补齐创建方当时不知道的列（provider/model/temperature/seed/
        # code_version），已有非空值不覆盖（P1-2）。
        if row is not None:
            conn.execute(
                """
                UPDATE agent_runs SET
                    provider = COALESCE(provider, ?),
                    model = COALESCE(model, ?),
                    temperature = COALESCE(temperature, ?),
                    seed = COALESCE(seed, ?),
                    code_version = COALESCE(code_version, ?)
                WHERE run_id = ? AND (
                    provider IS NULL OR model IS NULL OR temperature IS NULL
                    OR seed IS NULL OR code_version IS NULL
                )
                """,
                (provider, model, temperature, seed, code_version, run_id),
            )
    conn.commit()
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    finished_at: str | None = None,
    token_usage: int = 0,
    prompt_tokens: int = 0,
    step_count: int | None = None,
    error: str | None = None,
) -> str:
    """结束 run：写 status/finished_at/token/步数，禁止留下永久 ``running``。

    ``token_usage`` 记入 ``completion_tokens``。当前网关只汇总整轮总 token、
    不拆 prompt/completion，因此未拆分时这里记总数、``prompt_tokens`` 保持 0；
    成本口径统一用 ``prompt_tokens + completion_tokens`` 求和（P2-4）。
    已标记 ``failed`` 的 run 不会被降级为 success/partial，也不会清空 error（P1-4）。
    """
    if status not in RUN_STATUSES or status == "running":
        raise ValueError(f"finish_run 的 status 必须是 {RUN_STATUSES[:-1]}/failed 之一")
    row = conn.execute(
        "SELECT status, error FROM agent_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"run 不存在：{run_id!r}")
    now = finished_at or utc_now()
    final_status = status
    final_error = error
    if row["status"] == "failed" and status != "failed":
        final_status = "failed"
        final_error = row["error"] or error
    assignments = [
        "status = ?",
        "finished_at = ?",
        "completion_tokens = ?",
        "prompt_tokens = ?",
        "error = ?",
    ]
    values: list[Any] = [
        final_status,
        now,
        max(int(token_usage), 0),
        max(int(prompt_tokens), 0),
        final_error,
    ]
    if step_count is not None:
        assignments.append("steps = ?")
        values.append(max(int(step_count), 0))
    values.append(run_id)
    cursor = conn.execute(
        f"UPDATE agent_runs SET {', '.join(assignments)} WHERE run_id = ?", tuple(values)
    )
    if cursor.rowcount == 0:
        raise KeyError(f"run 不存在：{run_id!r}")
    conn.commit()
    return run_id


def fail_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    error: str | None = None,
    finished_at: str | None = None,
) -> str:
    """把 run 显式置为 ``failed``（PNG require 失败等硬失败）。"""
    now = finished_at or utc_now()
    conn.execute(
        "UPDATE agent_runs SET status = 'failed', finished_at = ?, error = ? WHERE run_id = ?",
        (now, error, run_id),
    )
    conn.commit()
    return run_id


# ---------------------------------------------------------------------------
# artifact 内容 / 版本 / 引用
# ---------------------------------------------------------------------------


def put_content(
    payload: bytes | str,
    filename: str,
    *,
    root: str | Path | None = None,
) -> tuple[str, str, Path]:
    """内容寻址写文件，返回 ``(digest, 相对路径, 绝对路径)``；同 digest 复用目录。"""
    name = _safe_filename(filename)
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    digest = sha256_bytes(data)
    rel = artifact_rel_path(digest, name)
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    absolute = base / rel
    absolute.parent.mkdir(parents=True, exist_ok=True)
    if not absolute.exists():
        absolute.write_bytes(data)
    return digest, rel, absolute


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: str
    path: str
    content_digest: str
    env_fingerprint: str
    created_at: str
    version: int
    meta: dict[str, Any]
    run_id: str | None = None
    step: int | None = None
    #: 该版本写入时的 meta 摘要（``sha256:<hex>``）；回放时用来校验库内 meta 未被篡改。
    meta_digest: str = ""

    @property
    def digest(self) -> str:
        """裸 sha256（兼容旧调用方；库内 ``content_digest`` 带 ``sha256:`` 前缀）。"""
        return self.content_digest.split(":", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "step": self.step,
            "kind": self.kind,
            "path": self.path,
            "digest": self.digest,
            "content_digest": self.content_digest,
            "env_fingerprint": self.env_fingerprint,
            "version": self.version,
            "meta": dict(self.meta),
            "meta_digest": self.meta_digest,
            "created_at": self.created_at,
        }


def _load_meta(raw: Any) -> dict[str, Any]:
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _merge_artifact_meta(
    existing: Mapping[str, Any], newest: Mapping[str, Any]
) -> dict[str, Any]:
    """成功态只升不降：新版本没出 PNG 时沿用旧 PNG 与 provenance。

    v2 把每次 meta 写成新版本，但「先降级、后补齐 PNG」和「已成功、后失败」不能
    让最新版本丢掉可用 PNG；这里保持 v1 评审 P1 的升级语义。
    """
    merged = dict(newest)
    previous_png = existing.get("png_path")
    if previous_png and not merged.get("png_path"):
        merged["png_path"] = previous_png
        merged["degraded"] = False
        for key in (
            "png_rel_path",
            "png_digest",
            "png_bytes",
            "png_width",
            "png_height",
            "png_scale",
            "png_elapsed_ms",
            "font_family",
            "kaleido_version",
            "chrome_version",
            "chrome_path",
        ):
            if existing.get(key) is not None and merged.get(key) is None:
                merged[key] = existing[key]
        merged["png_reused"] = True
        merged["png_source_mode"] = existing.get("png_source_mode") or existing.get("png_mode")
        attempt_error = merged.get("png_error")
        if attempt_error:
            merged["png_last_attempt_error"] = attempt_error
        merged["png_error"] = None
        warnings = merged.get("warnings")
        if isinstance(warnings, list):
            merged["warnings"] = [
                item
                for item in warnings
                if not (isinstance(item, str) and item.startswith(PNG_DEGRADED_WARNING_PREFIX))
            ]
    return merged


def _normalize_kind(kind: str) -> str:
    mapped = LEGACY_KIND_MAP.get(kind, kind)
    if mapped not in ARTIFACT_KINDS:
        raise ValueError(f"kind 必须是 {ARTIFACT_KINDS} 之一，实际 {kind!r}")
    return mapped


def _find_link(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    version: int,
    run_id: str | None,
    source: str,
    step: int | None,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM agent_run_artifacts
        WHERE artifact_id = ? AND artifact_version = ? AND run_id IS ?
          AND source = ? AND step IS ?
        ORDER BY link_id LIMIT 1
        """,
        (artifact_id, version, run_id, source, step),
    ).fetchone()


def record_artifact(
    conn: sqlite3.Connection,
    *,
    kind: str,
    path: str,
    content_digest: str | None = None,
    env_fingerprint: str = "",
    meta: Mapping[str, Any] | None = None,
    artifact_id: str | None = None,
    run_id: str | None = None,
    step: int | None = None,
    source: str = "render",
    session_id: str | None = None,
    reason: str = "render",
    actor: str = "system",
    created_at: str | None = None,
    digest: str | None = None,
) -> tuple[str, int]:
    """写 artifact/version/link 三件套，返回 ``(artifact_id, version)``。

    - artifact 不存在则插入；已存在时校验 ``content_digest``/``env_fingerprint``；
    - meta_digest 与最新版本相同则复用版本，否则追加 ``version+1``（原版本不动）；
    - 同一 ``(artifact, version, run_id, source, step)`` 只保留一条 link（幂等）。
    """
    if source not in ("agent", "render", "import", "legacy"):
        raise ValueError(f"source 非法：{source!r}")
    raw_digest = content_digest if content_digest is not None else digest
    if raw_digest is None:
        raise TypeError("record_artifact 缺少 content_digest")
    digest_value = normalize_content_digest(raw_digest)
    safe_path = _validate_rel_path(path)
    artifact_kind = _normalize_kind(kind)
    if not env_fingerprint:
        raise ValueError("record_artifact 需要 env_fingerprint（环境身份不可省）")
    if source == "agent" and run_id is None:
        raise ValueError("source='agent' 必须提供 run_id")
    meta_map = dict(meta or {})
    if artifact_id is not None:
        derived_id = artifact_id
    else:
        spec_snapshot = meta_map.get("spec")
        canonical_spec = (
            canonical_json(spec_snapshot) if spec_snapshot is not None else ""
        )
        data_digest = str(meta_map.get("data_digest") or digest_value)
        derived_id = artifact_id_for(
            data_digest,
            env_fingerprint=env_fingerprint,
            canonical_spec=canonical_spec,
        )
    if not ARTIFACT_ID_RE.match(derived_id):
        raise ValueError(f"artifact_id 必须是 art_<16 hex>，实际 {derived_id!r}")
    now = created_at or utc_now()

    existing = conn.execute(
        "SELECT * FROM agent_artifacts WHERE artifact_id = ?", (derived_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO agent_artifacts(
                artifact_id, kind, path, content_digest, env_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (derived_id, artifact_kind, safe_path, digest_value, env_fingerprint, now),
        )
    else:
        if (
            existing["content_digest"] != digest_value
            or existing["env_fingerprint"] != env_fingerprint
        ):
            raise ArtifactIdentityConflict(
                f"{derived_id} 身份冲突：content_digest="
                f"{existing['content_digest']!r}/{digest_value!r}, "
                f"env_fingerprint={existing['env_fingerprint']!r}/{env_fingerprint!r}"
            )
        # 同内容不同文件名：保留首行路径，link 仍指向同一 artwork 行（规格 §4.4）。

    latest = conn.execute(
        """
        SELECT version, meta_json FROM agent_artifact_versions
        WHERE artifact_id = ? ORDER BY version DESC LIMIT 1
        """,
        (derived_id,),
    ).fetchone()
    newest = dict(meta or {})
    if latest is not None:
        merged = _merge_artifact_meta(_load_meta(latest["meta_json"]), newest)
        newest = merged
    payload = canonical_json(newest)
    new_digest = "sha256:" + sha256_bytes(payload.encode("utf-8"))
    if latest is not None and latest["meta_json"] == payload:
        version = int(latest["version"])
    else:
        version = int(latest["version"]) + 1 if latest is not None else 1
        conn.execute(
            """
            INSERT INTO agent_artifact_versions(
                artifact_id, version, meta_json, meta_digest, reason, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (derived_id, version, payload, new_digest, reason, actor, now),
        )

    if _find_link(
        conn,
        artifact_id=derived_id,
        version=version,
        run_id=run_id,
        source=source,
        step=step,
    ) is None:
        conn.execute(
            """
            INSERT INTO agent_run_artifacts(
                artifact_id, artifact_version, run_id, step, source, session_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (derived_id, version, run_id, step, source, session_id, now),
        )
    conn.commit()
    return derived_id, version


def _latest_version_row(conn: sqlite3.Connection, artifact_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM agent_artifact_versions
        WHERE artifact_id = ? ORDER BY version DESC LIMIT 1
        """,
        (artifact_id,),
    ).fetchone()


def _link_row(
    conn: sqlite3.Connection, artifact_id: str, version: int | None
) -> sqlite3.Row | None:
    if version is None:
        return conn.execute(
            """
            SELECT * FROM agent_run_artifacts
            WHERE artifact_id = ? ORDER BY link_id DESC LIMIT 1
            """,
            (artifact_id,),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM agent_run_artifacts
        WHERE artifact_id = ? AND artifact_version = ?
        ORDER BY link_id DESC LIMIT 1
        """,
        (artifact_id, version),
    ).fetchone()


def _row_to_record(
    row: sqlite3.Row | None,
    version_row: sqlite3.Row | None,
    link_row: sqlite3.Row | None,
) -> ArtifactRecord | None:
    if row is None or version_row is None:
        return None
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        kind=row["kind"],
        path=row["path"],
        content_digest=row["content_digest"],
        env_fingerprint=row["env_fingerprint"],
        created_at=row["created_at"],
        version=int(version_row["version"]),
        meta=_load_meta(version_row["meta_json"]),
        run_id=link_row["run_id"] if link_row is not None else None,
        step=link_row["step"] if link_row is not None else None,
        meta_digest=str(version_row["meta_digest"] or ""),
    )


def get_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    *,
    version: int | None = None,
) -> ArtifactRecord | None:
    """读取 artifact（默认最新 meta 版本）；``run_id``/``step`` 取对应最新 link。"""
    row = conn.execute(
        "SELECT * FROM agent_artifacts WHERE artifact_id = ?", (artifact_id,)
    ).fetchone()
    if row is None:
        return None
    version_row = (
        _latest_version_row(conn, artifact_id)
        if version is None
        else conn.execute(
            "SELECT * FROM agent_artifact_versions WHERE artifact_id = ? AND version = ?",
            (artifact_id, version),
        ).fetchone()
    )
    return _row_to_record(row, version_row, _link_row(conn, artifact_id, version))


def find_artifacts_by_digest(
    conn: sqlite3.Connection, digest: str
) -> list[ArtifactRecord]:
    normalized = normalize_content_digest(digest)
    rows = conn.execute(
        "SELECT * FROM agent_artifacts WHERE content_digest = ? ORDER BY created_at",
        (normalized,),
    ).fetchall()
    records = []
    for row in rows:
        record = get_artifact(conn, row["artifact_id"])
        if record is not None:
            records.append(record)
    return records


def correct_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    patch: Mapping[str, Any],
    *,
    reason: str,
    actor: str,
    created_at: str | None = None,
) -> int:
    """追加一个修正版本（v+1），原版本不动；每个字段写一条 corrections 流水。"""
    if not str(reason or "").strip():
        raise ValueError("correction 必须填写理由（reason）")
    if not str(actor or "").strip():
        raise ValueError("correction 必须填写操作者（actor）")
    unknown = sorted(set(patch) - set(CORRECTION_FIELDS))
    if unknown:
        raise ValueError(f"不允许修正字段：{unknown}；允许 {list(CORRECTION_FIELDS)}")
    latest = _latest_version_row(conn, artifact_id)
    if latest is None:
        raise KeyError(f"artifact 不存在：{artifact_id!r}")
    old_meta = _load_meta(latest["meta_json"])
    new_meta = {**old_meta, **dict(patch)}
    payload = canonical_json(new_meta)
    digest = "sha256:" + sha256_bytes(payload.encode("utf-8"))
    version = int(latest["version"]) + 1
    now = created_at or utc_now()
    conn.execute(
        """
        INSERT INTO agent_artifact_versions(
            artifact_id, version, meta_json, meta_digest, reason, actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (artifact_id, version, payload, digest, reason, actor, now),
    )
    for field_name, new_value in patch.items():
        conn.execute(
            """
            INSERT INTO corrections(
                artifact_id, artifact_version, field, old_value, new_value,
                reason, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                version,
                field_name,
                canonical_json(old_meta.get(field_name)),
                canonical_json(new_value),
                reason,
                actor,
                now,
            ),
        )
    conn.commit()
    return version


def data_snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """数据快照的 ``sha256:`` digest（canonical JSON，供 data_digest/data_blob 校验）。"""
    return "sha256:" + sha256_bytes(canonical_json(snapshot).encode("utf-8"))


def load_data_snapshot(
    meta: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    allow_legacy: bool = True,
) -> dict[str, Any]:
    """从独立 blob 读回数据快照，并校验 ``data_digest``。

    新版 artifact 的 ``meta`` 只存 ``data_digest``/``data_blob``/``data_rows``/
    ``data_truncated``，完整 rows 落在 content-addressed blob（data.json）。
    ``allow_legacy`` 时兼容 v1/迁移前把快照塞进 ``meta['data']`` 的旧行。
    """
    blob = meta.get("data_blob")
    digest = meta.get("data_digest")
    if blob:
        base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
        absolute = (base / str(blob)).resolve()
        if not absolute.is_relative_to(base.resolve()):
            raise ArtifactFileMissing(f"数据快照路径越界：{blob!r}")
        if not absolute.is_file():
            raise ArtifactFileMissing(f"数据快照缺失：{absolute}")
        payload = absolute.read_bytes()
        actual = "sha256:" + sha256_bytes(payload)
        if digest is not None and actual != digest:
            raise ArtifactDigestMismatch(
                f"数据快照校验失败：记录 {digest}，实际 {actual}"
            )
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("数据快照不是 JSON object")
        return dict(value)
    if allow_legacy and isinstance(meta.get("data"), Mapping):
        return dict(meta["data"])
    raise ArtifactReplayUnavailable("没有 data_digest/data_blob 快照，无法回放")


def load_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    *,
    version: int | None = None,
    mode: str = "as_recorded",
    root: str | Path | None = None,
) -> dict[str, Any]:
    """按记录读取 artifact：校验文件存在与 ``content_digest``，不静默降级。

    ``mode='as_recorded'`` 返回落盘字节；``mode='recomputed'`` 只返回数据快照，
    缺 ``data_digest`` 时抛 :class:`ArtifactReplayUnavailable`。
    """
    record = get_artifact(conn, artifact_id, version=version)
    if record is None:
        raise KeyError(f"artifact 不存在：{artifact_id!r}")
    if mode == "recomputed":
        snapshot = load_data_snapshot(record.meta, root=root)
        return {
            "artifact_id": record.artifact_id,
            "version": record.version,
            "mode": mode,
            "data": snapshot,
            "data_digest": record.meta.get("data_digest"),
            "meta": record.meta,
        }
    if mode != "as_recorded":
        raise ValueError(f"未知 mode：{mode!r}")

    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    absolute = (base / record.path).resolve()
    if not absolute.is_relative_to(base.resolve()):
        raise ArtifactFileMissing(f"artifact 路径越界：{record.path!r}")
    if not absolute.is_file():
        raise ArtifactFileMissing(f"artifact 文件缺失：{absolute}")
    payload = absolute.read_bytes()
    actual = "sha256:" + sha256_bytes(payload)
    if actual != record.content_digest:
        raise ArtifactDigestMismatch(
            f"{artifact_id} 内容校验失败：记录 {record.content_digest}，实际 {actual}"
        )
    return {
        "artifact_id": record.artifact_id,
        "version": record.version,
        "mode": mode,
        "content": payload,
        "content_digest": record.content_digest,
        "meta": record.meta,
    }


# ---------------------------------------------------------------------------
# step / 指标
# ---------------------------------------------------------------------------


def record_step(
    conn: sqlite3.Connection,
    run_id: str,
    step: int,
    kind: str,
    *,
    tool: str | None = None,
    args: Mapping[str, Any] | list[Any] | None = None,
    result_digest: str | None = None,
    result_preview: str | None = None,
    latency_ms: int | None = None,
    created_at: str | None = None,
) -> None:
    """写 ``agent_steps``（同 ``(run_id, step)`` 幂等）。

    带 ``result_preview`` 时 ``args_json`` 是审计信封
    ``{"arguments": ..., "result_preview": ...}``：参数完整保留，同时留下结果摘要
    （ADR-017「digest + 截断预览」）；不传预览时仍是纯参数 JSON（兼容旧调用）。
    """
    if kind not in STEP_KINDS:
        raise ValueError(f"kind 必须是 {STEP_KINDS} 之一，实际 {kind!r}")
    value: Any = args if args is not None else {}
    if result_preview is not None:
        value = {
            "arguments": value,
            "result_preview": str(result_preview)[:500],
        }
    payload = canonical_json(value)
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
    result_preview: str | None = None,
    latency_ms: int | None = None,
    created_at: str | None = None,
) -> None:
    """工具调用落一行 ``kind='tool'`` 的 step（参数 + 结果摘要 + 耗时）。"""
    record_step(
        conn,
        run_id,
        step,
        "tool",
        tool=tool,
        args=args,
        result_digest=result_digest,
        result_preview=result_preview,
        latency_ms=latency_ms,
        created_at=created_at,
    )


def agent_metrics(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    include_render: bool = False,
) -> dict[str, int]:
    """token/步数指标；默认只统计 ``run_kind='agent'``，排除渲染合成 run。"""
    clauses = ["status IN ('success','partial')"]
    params: list[Any] = []
    if not include_render:
        clauses.append("run_kind = 'agent'")
    if since is not None:
        clauses.append("started_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("started_at <= ?")
        params.append(until)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS runs,
               COALESCE(SUM(steps), 0) AS steps,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
        FROM agent_runs WHERE {' AND '.join(clauses)}
        """,
        tuple(params),
    ).fetchone()
    return {"runs": int(row["runs"]), "steps": int(row["steps"]), "tokens": int(row["tokens"])}


# ---------------------------------------------------------------------------
# GC：只删文件，不删审计行
# ---------------------------------------------------------------------------


def _protected_paths(conn: sqlite3.Connection, base: Path) -> set[str]:
    """仍被引用的 artifact 相对路径（行 path + 版本 meta 的 PNG / data blob 路径）。"""
    protected = {
        row["path"] for row in conn.execute("SELECT DISTINCT path FROM agent_artifacts")
    }
    for row in conn.execute("SELECT meta_json FROM agent_artifact_versions"):
        meta = _load_meta(row["meta_json"])
        for key in ("png_rel_path", "png_path", "data_blob"):
            value = meta.get(key)
            if not isinstance(value, str) or not value:
                continue
            if key == "png_path":
                with contextlib.suppress(ValueError):
                    protected.add(Path(value).resolve().relative_to(base.resolve()).as_posix())
            else:
                protected.add(PurePosixPath(value).as_posix())
    return protected


def _remove_empty_dirs(base: Path) -> int:
    removed = 0
    artifacts_root = base / "artifacts"
    if not artifacts_root.is_dir():
        return 0
    for directory in sorted(
        (path for path in artifacts_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
            removed += 1
        except OSError:
            pass
    with contextlib.suppress(OSError):
        artifacts_root.rmdir()
        removed += 1
    return removed


def _cutoff_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def prune_artifacts(
    conn: sqlite3.Connection,
    *,
    before: str,
    keep_digests: Sequence[str] = (),
    root: str | Path | None = None,
    delete_files: bool = True,
) -> dict[str, int]:
    """只删「创建早于 before 且已无引用」的文件；artifact/version/link 行永久保留。"""
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    keep = {
        normalize_content_digest(digest).split(":", 1)[1]
        for digest in keep_digests
        if digest
    }
    protected = _protected_paths(conn, base)
    cutoff = _cutoff_datetime(before)
    artifacts_root = base / "artifacts"
    removed_files = 0
    if delete_files and artifacts_root.is_dir():
        for path in sorted(artifacts_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.resolve().relative_to(base.resolve()).as_posix()
            if rel in protected:
                continue
            parts = PurePosixPath(rel).parts
            if len(parts) >= 3 and parts[2] in keep:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if mtime >= cutoff:
                continue
            path.unlink()
            removed_files += 1
    removed_dirs = _remove_empty_dirs(base) if delete_files else 0
    return {"rows": 0, "files": removed_files, "pruned_digests": removed_dirs}


def prune_orphan_blobs(
    conn: sqlite3.Connection,
    *,
    older_than_days: int = 90,
    root: str | Path | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """清理无 artifact 行引用、且超过保留期的文件（默认 90 天、默认 dry-run）。"""
    if older_than_days < 0:
        raise ValueError("older_than_days 不能为负")
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    artifacts_root = base / "artifacts"
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    protected = _protected_paths(conn, base)
    candidates: list[str] = []
    deleted = 0
    if artifacts_root.is_dir():
        for path in sorted(artifacts_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.resolve().relative_to(base.resolve()).as_posix()
            if rel in protected:
                continue
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if mtime >= cutoff:
                continue
            candidates.append(rel)
            if not dry_run:
                path.unlink()
                deleted += 1
    removed_dirs = 0 if dry_run else _remove_empty_dirs(base)
    return {
        "dry_run": dry_run,
        "candidates": candidates,
        "deleted": deleted,
        "pruned_digests": removed_dirs,
        "older_than_days": older_than_days,
    }


def prune_runs(
    conn: sqlite3.Connection,
    *,
    older_than_days: int = 90,
    run_kinds: Sequence[str] = PRUNE_RUN_KINDS,
    now: datetime | None = None,
) -> int:
    """删旧 run（只级联 link/steps）；默认真实 Agent run 永不删除（ADR-017 C3）。"""
    if older_than_days < 0:
        raise ValueError("older_than_days 不能为负")
    kinds = tuple(dict.fromkeys(run_kinds))
    unknown = [kind for kind in kinds if kind not in RUN_KINDS]
    if unknown:
        raise ValueError(f"run_kinds 非法：{unknown}")
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=older_than_days)).isoformat(
        timespec="seconds"
    )
    placeholders = ",".join("?" for _ in kinds)
    cursor = conn.execute(
        f"DELETE FROM agent_runs WHERE started_at < ? AND run_kind IN ({placeholders})",
        (cutoff, *kinds),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


# ---------------------------------------------------------------------------
# v1 → v2 迁移
# ---------------------------------------------------------------------------


def _backup_database(conn: sqlite3.Connection, db_path: Path | None) -> Path | None:
    """在迁移前做一致性备份；WAL 未落盘或备份不可用时直接中止（P2-1）。

    ``db_path=None`` 表示内存库或未知路径：没有文件可备份，直接跳过。
    """
    if db_path is None or not db_path.exists() or str(db_path) == ":memory:":
        return None
    try:
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"迁移前 wal_checkpoint 失败：{exc}") from exc
    if checkpoint is not None and int(checkpoint[0]) != 0:
        raise RuntimeError(
            "迁移前 wal_checkpoint 未完成（数据库仍被占用），拒绝在 WAL 未落盘时备份"
        )
    backup = db_path.with_name(
        f"{db_path.name}.bak-{datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)}"
    )
    dest = sqlite3.connect(str(backup))
    try:
        conn.backup(dest)
        integrity = dest.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(f"备份校验失败：{integrity!r}")
    except Exception:
        dest.close()
        with contextlib.suppress(OSError):
            backup.unlink()
        raise
    finally:
        dest.close()
    if not backup.is_file() or backup.stat().st_size == 0:
        raise RuntimeError(f"备份文件不可用：{backup}")
    return backup


def migrate_v1_to_v2(
    conn: sqlite3.Connection,
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """把 v1 三表迁移到 v2；空库直接建表，已迁移库幂等返回。

    步骤：``wal_checkpoint`` → 备份 → 单事务内 ``foreign_keys=OFF`` →
    旧表改名 → 建 v2 表 → 拷贝（合成 run 标 ``run_kind='render'``）→
    校验行数 → 删旧表 → ``foreign_key_check`` → 提交 → 恢复 ``foreign_keys=ON``。
    """
    state = schema_state(conn)
    if state == "v2":
        conn.executescript(AGENT_SCHEMA)
        conn.commit()
        return {"status": "already_v2", "backup": None}
    if state == "empty":
        conn.executescript(AGENT_SCHEMA)
        conn.commit()
        return {"status": "created", "backup": None}
    if state != "v1":
        raise RuntimeError("无法识别的 agent_* 表结构，拒绝迁移")

    # 内存库没有可备份文件：不要回退 DEFAULT_DB_PATH，否则会生成错误位置的 .bak。
    path = Path(db_path) if db_path is not None else _connection_db_path(conn)
    backup = _backup_database(conn, path)
    old_runs = conn.execute("SELECT * FROM agent_runs").fetchall()
    old_artifacts = conn.execute("SELECT * FROM agent_artifacts").fetchall()
    old_steps = conn.execute("SELECT * FROM agent_steps").fetchall()

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE agent_steps RENAME TO agent_steps_v1")
        conn.execute("ALTER TABLE agent_artifacts RENAME TO agent_artifacts_v1")
        conn.execute("ALTER TABLE agent_runs RENAME TO agent_runs_v1")
        conn.execute("DROP VIEW IF EXISTS agent_artifact_links")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.execute(
            """
            INSERT INTO agent_runs(
                run_id, session_id, provider, model, temperature, seed, code_version,
                status, run_kind, started_at, finished_at, steps,
                prompt_tokens, completion_tokens, error
            )
            SELECT run_id, session_id, provider, model, temperature, seed, code_version,
                   status,
                   CASE WHEN run_id LIKE 'run_chart\\_%' ESCAPE '\\' THEN 'render' ELSE 'agent' END,
                   started_at, finished_at, steps, prompt_tokens, completion_tokens, error
            FROM agent_runs_v1
            """
        )
        conn.execute(
            """
            INSERT INTO agent_steps(
                run_id, step, kind, tool, args_json, result_digest, latency_ms, created_at
            )
            SELECT run_id, step, kind, tool, args_json, result_digest, latency_ms, created_at
            FROM agent_steps_v1
            """
        )
        for row in old_artifacts:
            digest = str(row["digest"] or "")
            content_digest = (
                digest if digest.startswith("sha256:") else f"sha256:{digest}"
            )
            kind = LEGACY_KIND_MAP.get(str(row["kind"]), str(row["kind"]))
            if kind not in ARTIFACT_KINDS:
                kind = "file"
            conn.execute(
                """
                INSERT INTO agent_artifacts(
                    artifact_id, kind, path, content_digest, env_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, 'legacy', ?)
                """,
                (
                    row["artifact_id"],
                    kind,
                    row["path"],
                    content_digest,
                    row["created_at"],
                ),
            )
            meta = _load_meta(row["meta_json"])
            meta["schema_version"] = "legacy"
            payload = canonical_json(meta)
            conn.execute(
                """
                INSERT INTO agent_artifact_versions(
                    artifact_id, version, meta_json, meta_digest, reason, actor, created_at
                ) VALUES (?, 1, ?, ?, 'migration', 'migration', ?)
                """,
                (
                    row["artifact_id"],
                    payload,
                    "sha256:" + sha256_bytes(payload.encode("utf-8")),
                    row["created_at"],
                ),
            )
            run_id = row["run_id"]
            run_row = conn.execute(
                "SELECT session_id FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                run_id = None
            conn.execute(
                """
                INSERT INTO agent_run_artifacts(
                    artifact_id, artifact_version, run_id, step, source, session_id, created_at
                ) VALUES (?, 1, ?, ?, 'legacy', ?, ?)
                """,
                (
                    row["artifact_id"],
                    run_id,
                    row["step"],
                    run_row["session_id"] if run_row is not None else None,
                    row["created_at"],
                ),
            )
        counts = {
            "runs": conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0],
            "steps": conn.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0],
            "artifacts": conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0],
            "versions": conn.execute(
                "SELECT COUNT(*) FROM agent_artifact_versions"
            ).fetchone()[0],
            "links": conn.execute("SELECT COUNT(*) FROM agent_run_artifacts").fetchone()[0],
        }
        if counts["runs"] != len(old_runs) or counts["steps"] != len(old_steps):
            raise RuntimeError(f"迁移行数不一致：{counts}")
        if counts["artifacts"] != len(old_artifacts) or counts["versions"] != len(
            old_artifacts
        ):
            raise RuntimeError(f"迁移 artifact 行数不一致：{counts}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"迁移后外键校验失败：{violations[:5]}")
        conn.execute("DROP TABLE agent_artifacts_v1")
        conn.execute("DROP TABLE agent_steps_v1")
        conn.execute("DROP TABLE agent_runs_v1")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if integrity is not None and integrity[0] != "ok":
        raise RuntimeError(f"迁移后 integrity_check 失败：{integrity[0]}")
    return {
        "status": "migrated",
        "backup": str(backup) if backup is not None else None,
        "counts": counts,
        "render_runs": conn.execute(
            "SELECT COUNT(*) FROM agent_runs WHERE run_kind = 'render'"
        ).fetchone()[0],
    }
