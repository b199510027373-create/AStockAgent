"""图表审计**唯一入口**（Q5 / D9，2026-11-14 裁决）。

分工：

- :mod:`astock.agent.chart_render` 是纯渲染器：spec → HTML / PNG bytes / 数据快照，
  不碰数据库、不落盘、不合成 run；
- **本模块**是唯一写审计库的地方：算 artifact 身份（spec + 数据快照 + 环境版本）、
  写 HTML/数据快照/PNG 三个 blob、调 :func:`astock.agent.store.record_artifact`
  落 ``agent_artifacts`` / ``agent_artifact_versions`` / ``agent_run_artifacts``；
- :class:`ArtifactReader` 是**只读**协议，供 ``data.source="artifact"`` 取数
  （渲染器只拿到 ``reader.frame`` 这个函数，拿不到连接）。

``run_id`` 语义（Q7/D9）：真实 Agent 渲染传 ``run_id`` + ``step``（link ``source="agent"``），
该 run 必须由 :class:`astock.agent.runtime.AgentRuntime` 先 ``ensure_run``——这里不造 run，
``run_id`` 不存在直接报错；直接渲染（工具、UI 预览、导出）传 ``None``，只落
artifact/version/link（``source="render"``），**绝不**创建 ``run_chart_*`` 假 run。
成功渲染不改 ``agent_runs`` 状态（生命周期归 runtime：``finish_run``）；只有 PNG 硬失败
才会把已有 run 标成 ``failed``，避免留永久 running。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from astock.agent.chart_render import (
    CHART_META_SCHEMA_VERSION,
    PNGExportError,
    RenderedFigure,
    embed_html,
    env_fingerprint,
    render_figure,
    spec_from_artifact,
)
from astock.agent.chart_spec import ChartSpecError, SpecIssue
from astock.agent.store import (
    DEFAULT_ARTIFACT_ROOT,
    ArtifactRecord,
    ArtifactReplayUnavailable,
    artifact_id_for,
    canonical_json,
    connect,
    fail_run,
    get_artifact,
    load_artifact,
    load_data_snapshot,
    meta_digest,
    normalize_content_digest,
    put_content,
    record_artifact,
)

__all__ = [
    "ArtifactReader",
    "artifact_file",
    "load_chart_artifact",
    "render_chart",
]

#: 规范 HTML 的文件名（内容寻址目录下；同内容复用同一 blob）。
HTML_FILENAME = "chart.html"
#: 数据快照文件名（meta 只存 digest/行数，快照本身独立存放，规格 §4.7）。
DATA_FILENAME = "data.json"


# ---------------------------------------------------------------------------
# 唯一审计入口
# ---------------------------------------------------------------------------


def render_chart(
    spec: Mapping[str, Any] | Any,
    *,
    frame: pd.DataFrame | None = None,
    resolver: Any | None = None,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    artifact_root: str | Path | None = None,
    run_id: str | None = None,
    step: int | None = None,
    session_id: str | None = None,
    code_version: str | None = None,
    data_as_of: str | None = None,
    data_time: str | None = None,
    view_as_of: str | None = None,
    view_time: str | None = None,
    include_plotlyjs: str | bool = "inline",
    export_png: bool = True,
    png_mode: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    actor: str | None = None,
    created_at: str | None = None,
) -> RenderedFigure:
    """渲染一张图并**写审计三件套**，返回带 artifact 身份的 :class:`RenderedFigure`。

    这是渲染图表的唯一入口：``chart_render.render_figure`` 只产出字节与 meta 草稿，
    artifact 身份（``canonical spec + data_digest + env_fingerprint``）、blob 落盘、
    版本追加都在这里完成（规格 §4.4 / §7.3）。
    """
    owned = conn is None
    connection = conn if conn is not None else connect(db_path)
    try:
        # Q5：渲染器只拿到只读取数协议（一个 callable），拿不到连接。
        load_frame = (
            resolver
            if resolver is not None
            else ArtifactReader(connection, root=artifact_root).frame
        )

        # D9 / §7：run 生命周期只归 runtime。chart_service 不替调用方造 run，
        # 传入的 run_id 必须由 AgentRuntime 先 ensure_run，否则 fail-fast（评审 P1）。
        if run_id is not None:
            row = connection.execute(
                "SELECT run_kind FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"run_id={run_id!r} 不存在：run 必须由 AgentRuntime 先 ensure_run；"
                    "chart_service 不创建 run（直接渲染请不传 run_id）"
                )
            run_kind = row[0] if not isinstance(row, sqlite3.Row) else row["run_kind"]
            if run_kind != "agent":
                raise ValueError(
                    f"run_id={run_id!r} 的 run_kind={run_kind!r}，"
                    "只有真实 Agent run（run_kind='agent'）才能挂图表 link"
                )
            if session_id is None:
                session_id = "chart"
        try:
            rendered = render_figure(
                spec,
                frame=frame,
                resolver=load_frame,
                data_as_of=data_as_of,
                data_time=data_time,
                view_as_of=view_as_of,
                view_time=view_time,
                include_plotlyjs=include_plotlyjs,
                export_png=export_png,
                png_mode=png_mode,
                code_version=code_version,
            )
        except PNGExportError as exc:
            # 硬失败把 run 标成 failed（成功路径不改 run 状态，由 runtime finish_run）。
            if run_id is not None:
                fail_run(connection, run_id, error=str(exc))
            raise
        resolved_source = source or ("agent" if run_id is not None else "render")
        resolved_reason = reason or ("agent_render" if run_id is not None else "render")
        resolved_actor = actor or ("agent" if run_id is not None else "system")
        version = code_version or rendered.code_version

        # 1) 数据快照 blob（meta 只留 digest/行数，Q2）。
        snapshot_bytes = canonical_json(rendered.snapshot).encode("utf-8")
        data_digest, data_rel, _data_abs = put_content(
            snapshot_bytes, filename=DATA_FILENAME, root=artifact_root
        )
        data_digest = normalize_content_digest(data_digest)

        # 2) artifact 身份：canonical spec + 数据快照 digest + 环境版本（§4.4）。
        fingerprint = env_fingerprint(version)
        identity_spec = canonical_json(
            {"spec": rendered.spec, "include_plotlyjs": include_plotlyjs}
        )
        artifact_id = artifact_id_for(
            data_digest,
            env_fingerprint=fingerprint,
            canonical_spec=identity_spec,
        )

        # 3) 规范 HTML blob（固定 div id，字节稳定；嵌入时再换成实例 id）。
        html_digest, html_rel, _html_abs = put_content(
            rendered.html, filename=HTML_FILENAME, root=artifact_root
        )

        meta: dict[str, Any] = dict(rendered.meta)
        meta["data_digest"] = data_digest
        meta["data_blob"] = data_rel
        meta.setdefault("schema_version", CHART_META_SCHEMA_VERSION)
        png_path: str | None = None
        if rendered.png_bytes is not None:
            png_digest, png_rel, png_abs = put_content(
                rendered.png_bytes,
                filename=f"chart_{artifact_id}.png",
                root=artifact_root,
            )
            png_path = str(png_abs)
            meta.update(
                {
                    "png_path": png_path,
                    "png_rel_path": png_rel,
                    # png_digest 沿用 v1 的裸 hex（与 blob 目录名同源，便于直接寻址）；
                    # 只有 artifact 的 content_digest 用 "sha256:" 前缀。
                    "png_digest": png_digest,
                    "png_source_mode": meta.get("png_mode"),
                }
            )
        meta.setdefault("png_path", None)
        meta.setdefault("png_digest", None)
        meta.setdefault("png_reused", False)
        meta.setdefault("png_last_attempt_error", None)

        # 4) 审计三件套：artifact / version / link（唯一写入点）。
        artifact_id, artifact_version = record_artifact(
            connection,
            artifact_id=artifact_id,
            kind="chart",
            path=html_rel,
            content_digest=html_digest,
            env_fingerprint=fingerprint,
            meta=meta,
            run_id=run_id,
            step=step,
            source=resolved_source,
            session_id=session_id,
            reason=resolved_reason,
            actor=resolved_actor,
            created_at=created_at,
        )
        stored = get_artifact(connection, artifact_id, version=artifact_version)
    finally:
        if owned:
            connection.close()

    stored_meta = dict(stored.meta) if stored is not None else meta
    rendered.artifact_id = artifact_id
    rendered.artifact_version = artifact_version
    rendered.path = html_rel
    rendered.digest = normalize_content_digest(html_digest)
    rendered.png_path = png_path if png_path is not None else stored_meta.get("png_path")
    rendered.meta = stored_meta
    rendered.meta_digest = meta_digest(stored_meta) if stored is not None else None
    return rendered


def artifact_file(rendered: RenderedFigure, *, root: str | Path | None = None) -> str:
    """返回 HTML artifact 的绝对路径（测试与 UI 预览用）。"""
    base = Path(root) if root is not None else DEFAULT_ARTIFACT_ROOT
    return str(base / rendered.path)


# ---------------------------------------------------------------------------
# 只读协议
# ---------------------------------------------------------------------------


class ArtifactReader:
    """只读取数协议（Q5）：``data.source="artifact"`` 的数据来源。

    渲染器只拿到 :meth:`frame` 这个 callable，拿不到连接，因此不可能顺手写库；
    需要回放时用 :func:`load_chart_artifact`，同样只读。
    """

    def __init__(
        self, conn: sqlite3.Connection, *, root: str | Path | None = None
    ) -> None:
        self._conn = conn
        self._root = root

    def record(self, artifact_id: str, version: int | None = None) -> ArtifactRecord:
        record = get_artifact(self._conn, artifact_id, version=version)
        if record is None:
            raise ChartSpecError(
                [SpecIssue("$.data.ref", f"artifact {artifact_id!r} 不存在")]
            )
        return record

    def snapshot(self, artifact_id: str, version: int | None = None) -> dict[str, Any]:
        record = self.record(artifact_id, version)
        return load_data_snapshot(record.meta, root=self._root)

    def frame(self, data: Mapping[str, Any]) -> pd.DataFrame:
        """``resolver`` 签名实现：artifact → DataFrame（并带上记录的行情时点）。"""
        ref = str(data.get("ref") or "")
        if not ref:
            raise ChartSpecError(
                [SpecIssue("$.data.ref", "data.source=artifact 需要 data.ref")]
            )
        record = self.record(ref)
        if record.kind not in ("chart", "table"):
            raise ChartSpecError(
                [
                    SpecIssue(
                        "$.data.ref",
                        f"artifact {ref} 的 kind={record.kind}，不能作为图表数据源",
                    )
                ]
            )
        snapshot = load_data_snapshot(record.meta, root=self._root)
        frame = pd.DataFrame(
            snapshot.get("rows") or [], columns=list(snapshot.get("fields") or [])
        )
        # 行情时点只能来自 meta（§3.5：不得从裁剪过的快照反推 data_as_of）。
        frame.attrs["data_as_of"] = str(record.meta.get("data_as_of") or "")
        frame.attrs["data_time"] = str(record.meta.get("data_time") or "")
        frame.attrs["artifact_id"] = record.artifact_id
        frame.attrs["artifact_version"] = record.version
        return frame


# ---------------------------------------------------------------------------
# 回放：as_recorded / recomputed（§3.5）
# ---------------------------------------------------------------------------


def load_chart_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    *,
    version: int | None = None,
    mode: str = "as_recorded",
    root: str | Path | None = None,
) -> dict[str, Any]:
    """按记录回放图表。

    - ``as_recorded``：读回**当时**落盘的 HTML 字节（校验 ``content_digest``，缺文件/被篡改
      直接报错，不静默降级）；
    - ``recomputed``：用数据快照 + **当前代码**重算，与记录值不一致时只写进 ``warnings``，
      绝不覆盖记录（§3.5 / §4.6）。

    两种模式的 ``data_*`` 都以记录为准（旧记录语义未区分时标 ``legacy``）。
    """
    record = get_artifact(conn, artifact_id, version=version)
    if record is None:
        raise KeyError(f"artifact 不存在：{artifact_id!r}")
    recorded = load_artifact(conn, artifact_id, version=record.version, mode="as_recorded", root=root)
    meta = dict(record.meta)
    # 评审 P2：meta 摘要以库里写入的值为准。库里 meta_json 被直接改过（绕过
    # correction）时摘要对不上，回放必须报错，而不是拿篡改后的 meta 继续渲染。
    stored_digest = str(record.meta_digest or "")
    if stored_digest:
        actual_digest = meta_digest(meta)
        if actual_digest != stored_digest:
            raise ArtifactReplayUnavailable(
                f"artifact {artifact_id!r} v{record.version} 的 meta 摘要不一致："
                f"库内 {stored_digest}，实际 {actual_digest}（meta 被绕过 correction 修改过）"
            )
    else:  # pragma: no cover - 迁移前的旧行可能没有 meta_digest
        stored_digest = meta_digest(meta)
    legacy = CHART_META_SCHEMA_VERSION not in meta
    spec = spec_from_artifact(meta)
    payload: dict[str, Any] = {
        "artifact_id": record.artifact_id,
        "version": record.version,
        "mode": mode,
        "spec": spec,
        "legacy": legacy,
        "data_as_of": str(meta.get("data_as_of") or ""),
        "data_time": str(meta.get("data_time") or ""),
        "view_as_of": str(meta.get("view_as_of") or ""),
        "view_time": str(meta.get("view_time") or ""),
        "session": str(meta.get("session") or ""),
        "content_digest": record.content_digest,
        "meta_digest": stored_digest,
        "warnings": list(meta.get("warnings") or []),
    }
    if mode == "as_recorded":
        payload["html"] = recorded["content"].decode("utf-8")
        payload["meta"] = meta
        return payload
    if mode != "recomputed":
        raise ValueError(f"未知 mode：{mode!r}")

    try:
        snapshot = load_data_snapshot(meta, root=root)
    except ArtifactReplayUnavailable:
        raise
    frame = pd.DataFrame(
        snapshot.get("rows") or [], columns=list(snapshot.get("fields") or [])
    )
    frame.attrs["data_as_of"] = payload["data_as_of"]
    frame.attrs["data_time"] = payload["data_time"]
    recomputed = render_figure(
        spec,
        frame=frame,
        data_as_of=payload["data_as_of"] or None,
        data_time=payload["data_time"] or None,
        include_plotlyjs=str(meta.get("params", {}).get("include_plotlyjs", "cdn")),
        export_png=False,
    )
    warnings = [str(item) for item in payload["warnings"]]
    for field_name in ("data_as_of", "data_time", "view_as_of", "view_time"):
        recorded_value = str(meta.get(field_name) or "")
        current_value = str(getattr(recomputed, field_name) or "")
        if recorded_value != current_value:
            warnings.append(
                f"重算的 {field_name}={current_value or '未知'} 与记录 {recorded_value or '未知'} 不一致"
                "（只提示，不覆盖记录）"
            )
    payload.update(
        {
            "html": recomputed.html,
            "meta": recomputed.meta,
            "warnings": warnings,
            "data_as_of": payload["data_as_of"] or recomputed.data_as_of,
            "data_time": payload["data_time"] or recomputed.data_time,
            "view_as_of": recomputed.view_as_of,
            "view_time": recomputed.view_time,
        }
    )
    return payload


def embed_figure_html(html: str, *, div_id: str) -> str:
    """展示层入口：把规范 HTML 换成每个 DOM 实例唯一 id（Q3）。"""
    return embed_html(html, div_id)
