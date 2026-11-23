"""文件检索与表格查询工具（ADR-015 C4）。

六个只读工具：

- ``file_list``：列出 ``data/agent/files`` 下的文件（15 键 meta 摘要）；
- ``file_search``：FTS5 全文检索文本块，返回 chunk + 页码/工作表引用；
- ``file_read``：按页码/块号回读 ``extracted.jsonl``；
- ``table_schema``：列出工作表与列，给出「工作表名 + 单元格范围」引用；
- ``table_query``：DuckDB 只读查询 parquet，只允许单条 SELECT、强制
  ``LIMIT``、5 秒超时，且连接不落任何临时文件；
- ``table_chart``：按列分组聚合后生成 ChartSpec v1，交给
  :func:`astock.agent.chart_service.render_chart` 渲染并写审计。

所有基于文件的结论都通过 :mod:`astock.agent.tools.citations` 生成
``《文件名》第 N 页`` / ``《文件名》工作表「名」A2:D10`` 形式的引用；没有命中
任何内容时统一返回「未在文件中找到」。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from astock.agent.files import (
    META_FIELDS,
    files_root,
    fts_tokens,
    index_db_path,
    iter_chunks,
    load_meta,
    table_path,
)
from astock.agent.registry import (
    Permission,
    ToolRegistry,
    ToolSpec,
    current_tool_context,
)
from astock.agent.tools.citations import (
    CITATION_NOT_FOUND,
    citation_for_chunk,
    citation_for_sheet,
)
from astock.agent.tools.sql_guard import build_limited_sql
from astock.agent.types import MAX_TEXT_CHARS, ToolResult

__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MAX_QUERY_LIMIT",
    "MAX_SEARCH_LIMIT",
    "TABLE_TIMEOUT_SECONDS",
    "build_file_registry",
    "describe_table",
    "list_files",
    "query_table",
    "read_file",
    "search_files",
    "table_chart",
]

DEFAULT_SEARCH_LIMIT = 8
MAX_SEARCH_LIMIT = 20
DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 200
#: 表格查询的硬超时（ADR-015 C4 / 冻结清单 §4）。
TABLE_TIMEOUT_SECONDS = 5.0

SNIPPET_CHARS = 160


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _jsonable(value: Any) -> Any:
    """把 DuckDB/pandas 标量转成可 JSON 序列化的值。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _meta_rows(root: str | Path | None = None) -> list[dict[str, Any]]:
    """枚举文件根目录下所有合法 meta.json（跳过损坏/字段不全的目录）。"""
    base = files_root(root)
    rows: list[dict[str, Any]] = []
    if not base.is_dir():
        return rows
    for meta_path in sorted(base.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, Mapping) and all(key in meta for key in META_FIELDS):
            rows.append(dict(meta))
    return rows


def _file_meta(file_id: str, root: str | Path | None = None) -> dict[str, Any]:
    try:
        return load_meta(file_id, root=root)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"找不到文件 {file_id!r}；请先用 file_list 查看可用文件") from exc


def _chunk_map(file_id: str, root: str | Path | None = None) -> dict[int, dict[str, Any]]:
    """块号 → extracted.jsonl 行（用于回查 bbox 与页码）。"""
    mapping: dict[int, dict[str, Any]] = {}
    for chunk in iter_chunks(file_id, root=root):
        block = chunk.get("block")
        if isinstance(block, int):
            mapping[block] = chunk
    return mapping


def _fts_query(text: str) -> str:
    """把检索词编成 FTS5 MATCH 表达式（CJK bigram + 英文词，全部必须命中）。"""
    tokens = fts_tokens(text)[:8]
    if not tokens:
        raise ValueError("检索词必须包含字母、数字或汉字")
    return " AND ".join(f'"{token}"' for token in tokens)


def _like_hits(
    query: str, metas: list[dict[str, Any]], *, file_id: str | None, limit: int, root
) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    hits: list[dict[str, Any]] = []
    for meta in metas:
        if file_id and meta["file_id"] != file_id:
            continue
        for chunk in iter_chunks(meta["file_id"], root=root):
            text = str(chunk.get("text") or "")
            if needle and needle in text.lower():
                hits.append({**chunk, "_meta": meta})
                if len(hits) >= limit:
                    return hits
    return hits


def _fts_hits(
    query: str, *, file_id: str | None, limit: int, db_path: str | Path | None
) -> list[dict[str, Any]] | None:
    """FTS5 检索；索引不存在/不可用时返回 ``None`` 让调用方回退。"""
    path = Path(db_path) if db_path is not None else index_db_path()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        sql = (
            "SELECT c.file_id, c.page, c.sheet, c.block, c.text "
            "FROM file_chunks_fts JOIN file_chunks c ON c.chunk_id = file_chunks_fts.rowid "
            "WHERE file_chunks_fts MATCH ?"
        )
        params: list[Any] = [_fts_query(query)]
        if file_id:
            sql += " AND c.file_id = ?"
            params.append(file_id)
        sql += " ORDER BY bm25(file_chunks_fts) LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [
        {"file_id": row[0], "page": row[1], "sheet": row[2], "block": row[3], "text": row[4]}
        for row in rows
    ]


def _snippet(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[: SNIPPET_CHARS - 1] + "…"


def list_files(*, limit: int = 20, root: str | Path | None = None) -> ToolResult:
    """列出已导入文件（按上传时间倒序）。"""
    metas = _meta_rows(root)
    metas.sort(key=lambda meta: (str(meta.get("uploaded_at") or ""), str(meta.get("name") or "")), reverse=True)
    selected = metas[:limit]
    if not selected:
        return ToolResult(
            text="还没有导入任何文件。",
            source="files:local",
            metadata={"files": 0},
        )
    lines = [f"共 {len(metas)} 个文件（显示前 {len(selected)} 个）："]
    table: list[dict[str, Any]] = []
    for meta in selected:
        sheets = meta.get("sheets") or []
        pages = meta.get("pages")
        pages_text = f"{pages} 页" if pages else "无页码"
        lines.append(
            f"- 《{meta['name']}》（{meta['file_id']}，{meta['ext']}，{pages_text}，"
            f"{len(sheets)} 张表，{meta['chunks_count']} 个文本块，状态 {meta['status']}，"
            f"上传于 {meta['uploaded_at']}）"
        )
        table.append(
            {
                "file_id": meta["file_id"],
                "name": meta["name"],
                "ext": meta["ext"],
                "pages": pages,
                "sheets": len(sheets),
                "chunks_count": meta["chunks_count"],
                "status": meta["status"],
                "uploaded_at": meta["uploaded_at"],
            }
        )
    return ToolResult(
        text="\n".join(lines),
        table=table,
        source="files:local",
        metadata={"files": len(metas), "returned": len(selected)},
    )


def search_files(
    query: str,
    *,
    file_id: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
) -> ToolResult:
    """在文本块中检索，命中返回 chunk + 页码/工作表引用。"""
    needle = str(query or "").strip()
    if not needle:
        raise ValueError("检索词不能为空")
    metas = _meta_rows(root)
    if file_id:
        metas = [meta for meta in metas if meta["file_id"] == file_id]
        if not metas:
            _file_meta(file_id, root)  # 抛出「找不到文件」
    by_id = {meta["file_id"]: meta for meta in metas}
    hits = _fts_hits(needle, file_id=file_id, limit=limit, db_path=db_path)
    backend = "fts5"
    if hits is None:
        backend = "like"
        hits = _like_hits(needle, metas, file_id=file_id, limit=limit, root=root)
    elif not hits:
        fallback = _like_hits(needle, metas, file_id=file_id, limit=limit, root=root)
        if fallback:
            backend = "like"
            hits = fallback

    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    seen: set[tuple[str, int | None, int | None]] = set()
    for hit in hits:
        meta = by_id.get(hit["file_id"]) or _file_meta(hit["file_id"], root)
        if meta["file_id"] not in by_id:
            by_id[meta["file_id"]] = meta
        block = hit.get("block")
        chunk = _chunk_map(hit["file_id"], root).get(block, hit) if isinstance(block, int) else hit
        citation = citation_for_chunk(meta, chunk)
        key = (citation, chunk.get("block"), chunk.get("page"))
        if key in seen:
            continue
        seen.add(key)
        snippet = _snippet(str(chunk.get("text") or hit.get("text") or ""))
        lines.append(f"- {citation}：{snippet}")
        rows.append(
            {
                "file": meta["name"],
                "file_id": meta["file_id"],
                "page": chunk.get("page"),
                "sheet": chunk.get("sheet"),
                "block": chunk.get("block"),
                "citation": citation,
                "text": snippet,
            }
        )
        if len(rows) >= limit:
            break

    if not rows:
        return ToolResult(
            text=CITATION_NOT_FOUND,
            source="files:search",
            metadata={"query": needle, "hits": 0, "backend": backend},
        )
    return ToolResult(
        text=_clip(f"命中 {len(rows)} 个文本块：\n" + "\n".join(lines)),
        table=rows,
        source="files:search",
        metadata={
            "query": needle,
            "hits": len(rows),
            "backend": backend,
            "citations": [row["citation"] for row in rows],
        },
    )


def read_file(
    file_id: str,
    *,
    page: int | None = None,
    block: int | None = None,
    max_chars: int = MAX_TEXT_CHARS,
    root: str | Path | None = None,
) -> ToolResult:
    """按页码/块号回读文本块，返回带引用的原文。"""
    meta = _file_meta(file_id, root)
    chunks = list(iter_chunks(meta["file_id"], root=root))
    if page is not None:
        chunks = [chunk for chunk in chunks if chunk.get("page") == page]
    if block is not None:
        chunks = [chunk for chunk in chunks if chunk.get("block") == block]
    if not chunks:
        return ToolResult(
            text=CITATION_NOT_FOUND,
            source=f"file:{meta['name']}",
            metadata={"file_id": meta["file_id"], "page": page, "block": block, "chunks": 0},
        )

    citations: list[str] = []
    parts: list[str] = []
    used = 0
    truncated = False
    for chunk in chunks:
        citation = citation_for_chunk(meta, chunk)
        text = str(chunk.get("text") or "").strip()
        piece = f"{citation}\n{text}"
        if parts and used + len(piece) + 2 > max_chars:
            truncated = True
            break
        parts.append(piece)
        citations.append(citation)
        used += len(piece) + 2
        if used >= max_chars:
            truncated = True
            break
    return ToolResult(
        text=_clip("\n\n".join(parts), max_chars),
        source=f"file:{meta['name']}",
        truncated=truncated,
        metadata={
            "file_id": meta["file_id"],
            "file": meta["name"],
            "page": page,
            "block": block,
            "chunks": len(citations),
            "citations": list(dict.fromkeys(citations)),
        },
    )


def _find_sheet(meta: Mapping[str, Any], sheet: str | None) -> dict[str, Any] | None:
    sheets = [dict(item) for item in (meta.get("sheets") or [])]
    names = "、".join(str(item.get("name")) for item in sheets) or "无"
    needle = str(sheet or "").strip()
    if needle:
        for item in sheets:
            if str(item.get("name")) == needle:
                return item
        lowered = needle.lower()
        for item in sheets:
            if str(item.get("name", "")).lower() == lowered or str(item.get("slug", "")).lower() == lowered:
                return item
        raise ValueError(f"文件《{meta['name']}》中找不到工作表 {needle!r}；可用工作表：{names}")
    if len(sheets) == 1:
        return sheets[0]
    if not sheets:
        return None
    return None


def _sheet_columns(path: Path) -> list[str]:
    return [str(field.name) for field in pq.read_schema(path)]


def _resolve_query_sheet(meta: Mapping[str, Any], sheet: str | None) -> dict[str, Any]:
    sheets = [dict(item) for item in (meta.get("sheets") or [])]
    if not sheets:
        raise ValueError(f"文件《{meta['name']}》没有可查询的表格")
    found = _find_sheet(meta, sheet)
    if found is None:
        names = "、".join(f"「{item.get('name')}」" for item in sheets)
        raise ValueError(f"文件《{meta['name']}》有多张表，请用 sheet 指定其中一张：{names}")
    return found


def describe_table(
    file_id: str, *, sheet: str | None = None, root: str | Path | None = None
) -> ToolResult:
    """列出文件的工作表与列，并给出工作表名 + 单元格范围引用。"""
    meta = _file_meta(file_id, root)
    sheets = [dict(item) for item in (meta.get("sheets") or [])]
    if not sheets:
        return ToolResult(
            text=f"《{meta['name']}》没有可查询的表格（ext={meta['ext']}）。",
            source=f"file:{meta['name']}",
            metadata={"file_id": meta["file_id"], "sheets": 0},
        )
    if sheet is None:
        selected = list(sheets)
    else:
        found = _find_sheet(meta, sheet)
        assert found is not None
        selected = [found]
    rows: list[dict[str, Any]] = []
    lines: list[str] = [f"《{meta['name']}》共 {len(sheets)} 张表："]
    for item in selected:
        citation = citation_for_sheet(meta, item)
        columns: list[str] = []
        try:
            columns = _sheet_columns(table_path(meta["file_id"], str(item["slug"]), root=root))
        except (OSError, ValueError) as exc:  # 表格文件损坏时仍返回表结构
            lines.append(f"- {citation}：读取列失败（{type(exc).__name__}: {exc}）")
        else:
            lines.append(
                f"- {citation}：{item['rows']} 行 × {item['cols']} 列，列：{'、'.join(columns)}"
            )
        rows.append(
            {
                "sheet": item["name"],
                "slug": item["slug"],
                "rows": item["rows"],
                "cols": item["cols"],
                "header_row": item.get("header_row"),
                "columns": columns,
                "citation": citation,
            }
        )
    return ToolResult(
        text=_clip("\n".join(lines)),
        table=rows,
        source=f"file:{meta['name']}",
        metadata={
            "file_id": meta["file_id"],
            "file": meta["name"],
            "sheets": len(sheets),
            "citations": [row["citation"] for row in rows],
        },
    )


def _interrupt_on_timeout(
    conn: duckdb.DuckDBPyConnection,
    seconds: float,
    state: dict[str, bool],
    lock: threading.Lock,
) -> threading.Timer:
    """到点中断 *查询*；用锁保证 ``done`` 检查与 ``interrupt()`` 不交错。"""

    def fire() -> None:
        with lock:
            if state["done"]:
                return
            try:
                conn.interrupt()
            except Exception:  # noqa: BLE001 - 连接可能已关闭；超时语义已由外层判定
                return

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return timer


def query_table(
    file_id: str,
    sheet: str,
    sql: str,
    *,
    limit: int = DEFAULT_QUERY_LIMIT,
    root: str | Path | None = None,
    timeout_seconds: float = TABLE_TIMEOUT_SECONDS,
) -> ToolResult:
    """只读查询某张表（单条 SELECT + 强制 LIMIT + 查询超时，装载不计入）。"""
    statement = build_limited_sql(sql, limit)
    meta = _file_meta(file_id, root)
    info = _resolve_query_sheet(meta, sheet)
    path = table_path(meta["file_id"], str(info["slug"]), root=root)
    if not path.is_file():
        raise FileNotFoundError(f"找不到表格文件：{info['slug']}")

    conn = duckdb.connect(":memory:")
    state = {"done": False}
    lock = threading.Lock()
    timer: threading.Timer | None = None
    try:
        conn.execute("SET temp_directory=''")
        conn.execute("SET threads=1")
        conn.execute("SET memory_limit='512MB'")
        escaped = str(path).replace("'", "''")
        conn.execute(f"CREATE TEMP TABLE t AS SELECT * FROM read_parquet('{escaped}')")
        conn.execute("SET enable_external_access=false")
        # 装载完成后再计时：装载读本地 parquet，不计入查询预算，也不套用查询超时文案。
        timer = _interrupt_on_timeout(conn, timeout_seconds, state, lock)
        try:
            cursor = conn.execute(statement)
            columns = [str(item[0]) for item in cursor.description]
            raw_rows = cursor.fetchall()
        except duckdb.InterruptException as exc:
            raise TimeoutError(
                f"table_query 查询超过 {timeout_seconds:g}s 超时（表格装载不计入），"
                "请缩小查询范围或先聚合"
            ) from exc
    finally:
        if timer is not None:
            with lock:
                state["done"] = True
            timer.cancel()
            timer.join(timeout=1.0)
        conn.close()

    rows = [
        {column: _jsonable(value) for column, value in zip(columns, row, strict=True)}
        for row in raw_rows
    ]
    citation = citation_for_sheet(meta, info)
    lines = [
        f"{citation} 查询返回 {len(rows)} 行（已强制 LIMIT {limit}，执行超时 {timeout_seconds:g}s）。",
        f"引用：{citation}",
    ]
    if columns:
        preview = "、".join(columns[:12])
        lines.insert(1, f"列：{preview}" + ("…" if len(columns) > 12 else ""))
    return ToolResult(
        text=_clip("\n".join(lines)),
        table=rows,
        source=f"file:{meta['name']}",
        metadata={
            "file_id": meta["file_id"],
            "file": meta["name"],
            "sheet": info["name"],
            "slug": info["slug"],
            "limit": limit,
            "timeout_seconds": timeout_seconds,
            "rows": len(rows),
            "citation": citation,
        },
    )


_TABLE_CHART_AGGS = frozenset({"sum", "avg", "count", "min", "max"})


def _quote_identifier(name: str) -> str:
    """引用 DuckDB 标识符；列名来自用户输入，必须防止引号逃逸。"""
    return '"' + str(name).replace('"', '""') + '"'


def table_chart(
    file_id: str,
    sheet: str,
    columns: list[str],
    group_by: str,
    metric: str,
    *,
    agg: str = "sum",
    chart_type: str = "bar",
    title: str | None = None,
    limit: int = 50,
    root: str | Path | None = None,
    audit_db_path: str | Path | None = None,
    artifact_root: str | Path | None = None,
    timeout_seconds: float = TABLE_TIMEOUT_SECONDS,
) -> ToolResult:
    """按 ``group_by`` 聚合 ``metric``，生成 ChartSpec 并走 chart_service 渲染/审计。

    ``columns`` 是用户声明的可用列白名单；``group_by`` 与 ``metric`` 必须在其
    中。图表数据以来源表的工作表引用（``《文件》工作表「名」A1:D13``）标注。
    """
    columns = [str(column) for column in columns]
    if not columns:
        raise ValueError("columns 不能为空")
    if len(set(columns)) != len(columns):
        raise ValueError("columns 不能有重复列")
    if group_by not in columns:
        raise ValueError(f"group_by {group_by!r} 不在 columns 中")
    if metric not in columns:
        raise ValueError(f"metric {metric!r} 不在 columns 中")
    if agg not in _TABLE_CHART_AGGS:
        raise ValueError(f"agg 只允许 {sorted(_TABLE_CHART_AGGS)}")
    if chart_type not in ("bar", "line"):
        raise ValueError("chart_type 只允许 bar/line")
    if not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit 必须在 1..{MAX_QUERY_LIMIT}")

    metric_ident = _quote_identifier(metric)
    metric_expr = (
        f"COUNT({metric_ident})"
        if agg == "count"
        else f"{agg.upper()}(CAST({metric_ident} AS DOUBLE))"
    )
    sql = (
        f"SELECT {_quote_identifier(group_by)} AS category, "
        f"{metric_expr} AS value FROM t GROUP BY 1 ORDER BY 1"
    )
    queried = query_table(
        file_id,
        sheet,
        sql,
        limit=limit,
        root=root,
        timeout_seconds=timeout_seconds,
    )
    rows = [
        {"category": str(row.get("category")), "value": row.get("value")}
        for row in (queried.table or [])
    ]
    citation = str((queried.metadata or {}).get("citation") or "")
    file_name = str((queried.metadata or {}).get("file") or file_id)
    if not rows:
        return ToolResult(
            text=f"{citation} 查询没有返回可绘制数据。",
            source=f"file:{file_name}",
            metadata={"citation": citation, "file_id": file_id, "sheet": sheet},
        )

    chart_title = (title or f"{metric} 按 {group_by}").strip() or f"{metric} 按 {group_by}"
    spec = {
        "version": 1,
        "kind": chart_type,
        "title": chart_title[:120],
        "data": {
            "source": "inline",
            "rows": rows,
            "fields": ["category", "value"],
            "period": "day",
            "range": "1y",
            "adj": "none",
            "time_semantics": "none",
        },
        "encode": {"x": "category", "y": "value"},
        "transforms": [],
        "layers": [],
        "theme": "light",
        "annotations": [],
        "meta": {
            "file_id": file_id,
            "sheet": sheet,
            "citation": citation,
            "group_by": group_by,
            "metric": metric,
            "agg": agg,
        },
    }

    # 延迟导入：chart_service 只应在真正出图时加载，避免 file_tools 与审计层循环。
    from astock.agent.chart_service import render_chart

    context = current_tool_context()
    rendered = render_chart(
        spec,
        db_path=audit_db_path,
        artifact_root=artifact_root,
        run_id=context.run_id if context is not None else None,
        step=context.step if context is not None else None,
        session_id=context.session_id if context is not None else None,
        include_plotlyjs="inline",
        # 会话 UI 需要直接下载 PNG；``auto`` 在 kaleido/Chrome 不可用时降级为仅 HTML，
        # 不把一次表格绘图打成工具失败（ADR-016 / 接口冻结 v2 §3.5）。
        export_png=True,
        png_mode="auto",
    )
    warnings = [str(item) for item in rendered.warnings]
    return ToolResult(
        text=(
            f"{citation} 已生成{chart_type}图：{len(rows)} 个分类，"
            f"图表产物 {rendered.artifact_id}。"
            + (f"；{'；'.join(warnings)}" if warnings else "")
        ),
        table=rows,
        figure=rendered.html,
        source=f"file:{file_name}",
        metadata={
            "file_id": file_id,
            "file": file_name,
            "sheet": sheet,
            "citation": citation,
            "citations": [citation],
            "title": chart_title,
            "chart_spec": rendered.spec,
            "artifact_id": rendered.artifact_id,
            "artifact_version": rendered.artifact_version,
            "artifact_path": rendered.path,
            "png_path": rendered.png_path or "",
            "data_as_of": rendered.data_as_of,
            "data_time": rendered.data_time,
            "warnings": warnings,
        },
    )


def build_file_registry(
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    audit_db_path: str | Path | None = None,
    artifact_root: str | Path | None = None,
    timeout_seconds: float = TABLE_TIMEOUT_SECONDS,
    table_row_limit: int = MAX_QUERY_LIMIT,
) -> ToolRegistry:
    """构造只含文件工具的注册表（``file:read`` + 表格图 ``chart:render`` 权限）。"""
    registry = ToolRegistry(table_row_limit=table_row_limit)
    handlers = {
        "file_list": partial(list_files, root=root),
        "file_search": partial(search_files, root=root, db_path=db_path),
        "file_read": partial(read_file, root=root),
        "table_schema": partial(describe_table, root=root),
        "table_query": partial(query_table, root=root, timeout_seconds=timeout_seconds),
        "table_chart": partial(
            table_chart,
            root=root,
            audit_db_path=audit_db_path,
            artifact_root=artifact_root,
            timeout_seconds=timeout_seconds,
        ),
    }
    specs = [
        ToolSpec(
            name="file_list",
            description="列出已导入的文件（文件名、格式、页数、表数、状态）。",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "additionalProperties": False,
            },
            handler=handlers["file_list"],
            permissions=frozenset({Permission.FILE_READ}),
        ),
        ToolSpec(
            name="file_search",
            description=(
                "在已导入文件的文本块中全文检索，返回命中的文件、页码/工作表与原文片段；"
                "没有命中时返回「未在文件中找到」。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "file_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "default": DEFAULT_SEARCH_LIMIT,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=handlers["file_search"],
            permissions=frozenset({Permission.FILE_READ}),
        ),
        ToolSpec(
            name="file_read",
            description="按页码或文本块号回读已导入文件的原文，返回带页码/工作表的引用。",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "page": {"type": "integer", "minimum": 1},
                    "block": {"type": "integer", "minimum": 0},
                    "max_chars": {
                        "type": "integer",
                        "minimum": 200,
                        "maximum": MAX_TEXT_CHARS,
                        "default": MAX_TEXT_CHARS,
                    },
                },
                "required": ["file_id"],
                "additionalProperties": False,
            },
            handler=handlers["file_read"],
            permissions=frozenset({Permission.FILE_READ}),
        ),
        ToolSpec(
            name="table_schema",
            description="查看文件的工作表、行数、列名，并给出工作表名 + 单元格范围引用。",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "sheet": {"type": "string", "minLength": 1, "maxLength": 64},
                },
                "required": ["file_id"],
                "additionalProperties": False,
            },
            handler=handlers["table_schema"],
            permissions=frozenset({Permission.FILE_READ}),
        ),
        ToolSpec(
            name="table_query",
            description=(
                "用单条只读 SQL 查询文件中的表格（表名固定为 t），只允许 SELECT，"
                "结果强制 LIMIT、5 秒超时；引用带工作表名与单元格范围。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "sheet": {"type": "string", "minLength": 1, "maxLength": 64},
                    "sql": {"type": "string", "minLength": 1, "maxLength": 20000},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_QUERY_LIMIT,
                        "default": DEFAULT_QUERY_LIMIT,
                    },
                },
                "required": ["file_id", "sheet", "sql"],
                "additionalProperties": False,
            },
            handler=handlers["table_query"],
            permissions=frozenset({Permission.FILE_READ}),
        ),
        ToolSpec(
            name="table_chart",
            description=(
                "按 group_by 列聚合 metric 列并生成真实图表（ChartSpec v1 → "
                "chart_service 渲染/审计）；返回图表 HTML、聚合表和表格引用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "sheet": {"type": "string", "minLength": 1, "maxLength": 64},
                    "columns": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 64},
                        "minItems": 1,
                        "maxItems": 20,
                    },
                    "group_by": {"type": "string", "minLength": 1, "maxLength": 64},
                    "metric": {"type": "string", "minLength": 1, "maxLength": 64},
                    "agg": {
                        "type": "string",
                        "enum": ["sum", "avg", "count", "min", "max"],
                        "default": "sum",
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "line"],
                        "default": "bar",
                    },
                    "title": {"type": "string", "minLength": 1, "maxLength": 120},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_QUERY_LIMIT,
                        "default": 50,
                    },
                },
                "required": ["file_id", "sheet", "columns", "group_by", "metric"],
                "additionalProperties": False,
            },
            handler=handlers["table_chart"],
            permissions=frozenset({Permission.FILE_READ, Permission.CHART_RENDER}),
        ),
    ]
    for spec in specs:
        registry.register(spec)
    return registry
