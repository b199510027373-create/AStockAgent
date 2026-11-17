"""Agent 工具实现（行情/指标/图表/文件/研究/澄清）。

当前已实现文件检索与表格查询（plan-20261107-004，ADR-015 C4）：

- :mod:`astock.agent.tools.file_tools`：``file_list`` / ``file_search`` /
  ``file_read`` / ``table_schema`` / ``table_query`` 与 ``build_file_registry``；
- :mod:`astock.agent.tools.citations`：强制「文件名 + 页码/工作表」引用；
- :mod:`astock.agent.tools.sql_guard`：``table_query`` 的只读 SQL 守卫。
"""

from __future__ import annotations

from astock.agent.tools.citations import (
    CITATION_NOT_FOUND,
    bbox_kind,
    cell_range,
    citation_for_chunk,
    citation_for_sheet,
    finalize_file_answer,
    has_file_citation,
)
from astock.agent.tools.file_tools import (
    DEFAULT_SEARCH_LIMIT,
    MAX_QUERY_LIMIT,
    MAX_SEARCH_LIMIT,
    TABLE_TIMEOUT_SECONDS,
    build_file_registry,
    describe_table,
    list_files,
    query_table,
    read_file,
    search_files,
)
from astock.agent.tools.sql_guard import SqlGuardError, build_limited_sql, ensure_select

__all__ = [
    "CITATION_NOT_FOUND",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_QUERY_LIMIT",
    "MAX_SEARCH_LIMIT",
    "TABLE_TIMEOUT_SECONDS",
    "SqlGuardError",
    "bbox_kind",
    "build_file_registry",
    "build_limited_sql",
    "cell_range",
    "citation_for_chunk",
    "citation_for_sheet",
    "describe_table",
    "ensure_select",
    "finalize_file_answer",
    "has_file_citation",
    "list_files",
    "query_table",
    "read_file",
    "search_files",
]
