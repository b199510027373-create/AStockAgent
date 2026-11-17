"""``table_query`` 的 SQL 守卫：只允许单条只读 ``SELECT``（ADR-015 C4）。

守卫是白名单式的最外层过滤：去掉注释后只接受一条以 ``SELECT``/``WITH`` 开头
的语句，拒绝写语句、DDL、``COPY``/``ATTACH`` 等会碰文件系统的命令，以及
``read_parquet``/``glob`` 之类可越权读文件的表函数。真正执行时还依赖
DuckDB 连接只挂载了一张临时表 ``t`` 且 ``enable_external_access=false``。
"""

from __future__ import annotations

import re

__all__ = [
    "MAX_SQL_CHARS",
    "SqlGuardError",
    "build_limited_sql",
    "ensure_select",
]

#: 单条 SQL 的长度上限，避免把整段脚本塞进守卫。
MAX_SQL_CHARS = 20_000

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|create|replace|merge|truncate|"
    r"copy|attach|detach|pragma|set|reset|export|import|install|load|"
    r"vacuum|checkpoint|call|grant|revoke|summarize|describe|explain|"
    r"begin|commit|rollback|transaction|use"
    r")\b",
    re.IGNORECASE,
)

_FORBIDDEN_FUNCTIONS = re.compile(
    r"\b("
    r"read_\w+|parquet_scan|csv_scan|json_scan|glob|"
    r"sqlite_scan|postgres_scan|mysql_scan|iceberg_scan|delta_scan|httpfs"
    r")\s*\(",
    re.IGNORECASE,
)

_QUOTED_TABLE = re.compile(r"\b(?:from|join)\s*'", re.IGNORECASE)


class SqlGuardError(ValueError):
    """SQL 未通过只读守卫。"""


def _strip_comments(sql: str) -> str:
    """去掉 ``--`` 与 ``/* */`` 注释，保留字符串字面量原样。"""
    out: list[str] = []
    index = 0
    quote: str | None = None
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote is not None:
            out.append(char)
            if char == quote:
                if index + 1 < length and sql[index + 1] == quote:
                    out.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline < 0 else newline
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _split_statements(sql: str) -> list[str]:
    """按引号外的 ``;`` 切分语句。"""
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote is not None:
            buffer.append(char)
            if char == quote:
                if index + 1 < length and sql[index + 1] == quote:
                    buffer.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            buffer.append(char)
        elif char == ";":
            statements.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
        index += 1
    statements.append("".join(buffer))
    return [statement.strip() for statement in statements if statement.strip()]


def _mask_literals(sql: str) -> str:
    """把字符串字面量替换成空串，避免 ``WHERE note = 'created'`` 误判关键字。"""
    out: list[str] = []
    index = 0
    quote: str | None = None
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < length and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "'":
            quote = char
            out.append("''")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def ensure_select(sql: str) -> str:
    """校验并返回去掉注释与结尾分号的单条 ``SELECT`` 语句。"""
    if not isinstance(sql, str):
        raise SqlGuardError("SQL 必须是字符串")
    text = sql.strip()
    if not text:
        raise SqlGuardError("SQL 不能为空")
    if len(text) > MAX_SQL_CHARS:
        raise SqlGuardError(f"SQL 长度 {len(text)} 超过上限 {MAX_SQL_CHARS}")
    cleaned = _strip_comments(text)
    statements = _split_statements(cleaned)
    if len(statements) != 1:
        raise SqlGuardError("只允许一条 SELECT 语句，不能包含多条语句")
    statement = statements[0].strip()
    head = statement.split(None, 1)[0].lower() if statement.split() else ""
    if head not in {"select", "with"}:
        raise SqlGuardError("只允许 SELECT 查询（可带 WITH），写语句与 DDL 一律拒绝")
    masked = _mask_literals(statement)
    forbidden = _FORBIDDEN_KEYWORDS.search(masked)
    if forbidden:
        raise SqlGuardError(f"SQL 含禁止的关键字 {forbidden.group(0).upper()}，只允许只读查询")
    function = _FORBIDDEN_FUNCTIONS.search(masked)
    if function:
        raise SqlGuardError(f"SQL 不允许调用文件读取函数 {function.group(0)}")
    if _QUOTED_TABLE.search(masked):
        raise SqlGuardError("SQL 不允许直接引用文件路径，只能查询表 t")
    if not re.search(r"\bt\b", masked):
        raise SqlGuardError("查询必须引用当前表格视图 t（例如 SELECT * FROM t LIMIT 10）")
    return statement


def build_limited_sql(sql: str, limit: int) -> str:
    """在用户查询外层再包一层 ``LIMIT``，保证返回行数绝不超过 ``limit``。"""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit 必须是 >= 1 的整数：{limit!r}")
    return f"SELECT * FROM ({ensure_select(sql)}) AS _limited LIMIT {limit}"
