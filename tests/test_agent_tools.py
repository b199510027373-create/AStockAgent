"""文件检索 / 表格查询与强制引用测试（plan-20261107-004，ADR-015 C4）。

覆盖验收标准：

- ``file_search`` 返回 chunk + 页码，可被回答引用；
- ``table_query`` 只允许 SELECT、强制 LIMIT、5 秒超时；
- 文件结论必须带「文件名 + 页码/工作表」引用，无命中回答「未在文件中找到」；
- FTS5 命中可回查 ``file_id`` + 页码/块号；
- meta 必填 15 字段（字段数与缺字段测试）；
- PDF 表格 bbox 用页面坐标，Excel/CSV/DOCX 用源表格行列坐标，两类不混用；
- DOCX 表格写回源行号；Excel 多 sheet 引用带工作表名与单元格范围；
- 10 万行 Excel 表格查询 < 3s。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docx import Document
from openpyxl import Workbook

from astock.agent import files as F
from astock.agent.registry import ToolArgumentError
from astock.agent.tools import citations as C
from astock.agent.tools import file_tools as T
from astock.agent.tools.sql_guard import SqlGuardError, build_limited_sql, ensure_select

UPLOADED_AT = "2026-11-16T00:00:00+08:00"


@pytest.fixture()
def tmp_path() -> Iterator[Path]:
    """默认权限的临时目录（与 test_agent_files.py 一致，防 Windows 沙箱不可读）。"""
    base = Path(tempfile.gettempdir()) / f"astock-tools-{uuid.uuid4().hex}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    return tmp_path / "store"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "idx.db"


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def _make_xlsx(path: Path, sheets: dict[str, list[list[Any]]]) -> Path:
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    book.save(path)
    return path


def _make_big_xlsx(path: Path, rows: int = 100_000) -> Path:
    book = Workbook(write_only=True)
    sheet = book.create_sheet(title="明细")
    sheet.append(["序号", "名称", "金额"])
    for index in range(rows):
        sheet.append([index + 1, f"公司{index}", index * 1.5])
    book.save(path)
    return path


def _make_docx(path: Path) -> Path:
    document = Document()
    document.add_paragraph("第一段正文，提到营业收入。")
    document.add_page_break()
    document.add_paragraph("第二页正文。")
    table = document.add_table(rows=3, cols=2)
    values = [["项目", "金额"], ["营收", "100"], ["净利", "20"]]
    for row, row_values in zip(table.rows, values, strict=True):
        for cell, value in zip(row.cells, row_values, strict=True):
            cell.text = value
    document.save(path)
    return path


class _FakeTable:
    def __init__(self, grid: list[list[Any]], bbox: Any = None) -> None:
        self.grid = grid
        self.bbox = bbox

    def extract(self) -> list[list[Any]]:
        return self.grid


class _FakePage:
    def __init__(self, text: str, tables: list[_FakeTable] | None = None) -> None:
        self.text = text
        self.tables = tables or []

    def extract_text(self) -> str:
        return self.text

    def find_tables(self) -> list[_FakeTable]:
        return self.tables


def _patch_pdf(monkeypatch: pytest.MonkeyPatch, pages: list[_FakePage]) -> None:
    class _FakePDF:
        def __init__(self) -> None:
            self.pages = pages

        def close(self) -> None:
            pass

    plumber = SimpleNamespace(__version__="0.11.0", open=lambda _path: _FakePDF())
    monkeypatch.setattr(F, "_load_pdfplumber", lambda: plumber)


def _ingest(path: Path, store: Path, db: Path | None = None, *, index: bool = True) -> F.IngestResult:
    result = F.ingest_file(path, root=store, db_path=db, uploaded_at=UPLOADED_AT, index=index)
    assert result.status == F.STATUS_OK, result.warnings
    return result


# ---------------------------------------------------------------------------
# meta 15 字段
# ---------------------------------------------------------------------------


def test_meta_fields_are_fifteen_and_match_frozen_order(store: Path) -> None:
    """ADR-015：meta.json 必填 15 字段，顺序即冻结顺序。"""
    assert len(F.META_FIELDS) == 15
    assert len(set(F.META_FIELDS)) == 15
    assert F.META_FIELDS == (
        "file_id",
        "sha256",
        "name",
        "ext",
        "media_type",
        "size_bytes",
        "uploaded_at",
        "pages",
        "sheets",
        "text_chars",
        "chunks_count",
        "parser",
        "parser_version",
        "status",
        "warnings",
    )
    result = _ingest(_write(store.parent / "公告.txt", "第一段。"), store)
    meta = F.load_meta(result.meta["file_id"], root=store)  # type: ignore[index]
    assert tuple(meta) == F.META_FIELDS


def test_missing_meta_field_is_rejected_by_file_tools(store: Path) -> None:
    """缺任一必填字段时读取辅助与工具都必须报错，而不是静默降级。"""
    result = _ingest(_write(store.parent / "公告.txt", "第一段。"), store)
    file_id = result.meta["file_id"]  # type: ignore[index]
    meta_path = F.file_dir(file_id, root=store) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("warnings")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match=r"meta\.json 字段不符合冻结规范"):
        F.load_meta(file_id, root=store)
    with pytest.raises(ValueError, match=r"meta\.json 字段不符合冻结规范"):
        T.read_file(file_id, root=store)


# ---------------------------------------------------------------------------
# file_list / file_search / file_read
# ---------------------------------------------------------------------------


def test_file_list_reports_files_and_meta_summary(store: Path) -> None:
    first = _ingest(_write(store.parent / "公告.txt", "公告正文。"), store)
    _ingest(_make_xlsx(store.parent / "财务.xlsx", {"利润表": [["名称", "营收"], ["公司A", 100]]}), store)

    result = T.list_files(root=store)

    assert "共 2 个文件" in result.text
    assert "《公告.txt》" in result.text and "《财务.xlsx》" in result.text
    assert result.table is not None and len(result.table) == 2
    ids = {row["file_id"] for row in result.table}
    assert first.meta["file_id"] in ids  # type: ignore[index]
    assert all(row["status"] == F.STATUS_OK for row in result.table)


def test_file_search_returns_chunk_with_page_citation(
    store: Path, db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [_FakePage("第一页文字"), _FakePage("第二页提到营业收入同比增长。")]
    _patch_pdf(monkeypatch, pages)
    source = store.parent / "年报.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    result = _ingest(source, store, db)

    found = T.search_files("营业收入", root=store, db_path=db)

    assert found.table is not None and len(found.table) == 1
    hit = found.table[0]
    assert hit["file_id"] == result.meta["file_id"]  # type: ignore[index]
    assert hit["page"] == 2
    assert isinstance(hit["block"], int)
    assert hit["citation"] == "《年报.pdf》第 2 页"
    assert "《年报.pdf》第 2 页" in found.text
    assert C.has_file_citation(found.text)


def test_file_search_miss_answers_not_found(store: Path, db: Path) -> None:
    _ingest(_write(store.parent / "公告.txt", "只有营业收入。"), store, db)

    result = T.search_files("毛利率", root=store, db_path=db)

    assert result.text == C.CITATION_NOT_FOUND
    assert result.metadata["hits"] == 0
    assert result.table is None


def test_file_search_filters_by_file_id_and_hits_are_rereadable(store: Path, db: Path) -> None:
    first = _ingest(_write(store.parent / "甲.txt", "关键词：应收账款。"), store, db)
    second = _ingest(_write(store.parent / "乙.txt", "关键词：应收账款（乙公司）。"), store, db)

    only_first = T.search_files("应收账款", file_id=first.meta["file_id"], root=store, db_path=db)  # type: ignore[index]
    only_second = T.search_files("应收账款", file_id=second.meta["file_id"], root=store, db_path=db)  # type: ignore[index]

    assert only_first.table is not None and len(only_first.table) == 1
    assert only_first.table[0]["file_id"] == first.meta["file_id"]  # type: ignore[index]
    assert only_second.table is not None and len(only_second.table) == 1
    assert only_second.table[0]["file_id"] == second.meta["file_id"]  # type: ignore[index]

    hit = only_first.table[0]
    chunk = T.read_file(hit["file_id"], block=hit["block"], root=store)
    assert "应收账款" in chunk.text
    assert "《甲.txt》" in chunk.text


def test_file_search_falls_back_to_like_when_chunks_not_indexed(store: Path, db: Path) -> None:
    _ingest(_write(store.parent / "公告.txt", "存货周转率下降。"), store, db, index=False)

    result = T.search_files("存货周转率", root=store, db_path=db)

    assert result.metadata["backend"] == "like"
    assert result.table is not None and len(result.table) == 1


def test_file_read_filters_page_and_block(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [_FakePage("第一页文字"), _FakePage("第二页文字")]
    _patch_pdf(monkeypatch, pages)
    source = store.parent / "年报.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    result = _ingest(source, store)

    page_two = T.read_file(result.meta["file_id"], page=2, root=store)  # type: ignore[index]
    assert page_two.text.startswith("《年报.pdf》第 2 页")
    assert "第二页文字" in page_two.text
    assert "第一页文字" not in page_two.text

    only_block = T.read_file(result.meta["file_id"], block=0, root=store)  # type: ignore[index]
    assert "第一页文字" in only_block.text
    assert "第二页文字" not in only_block.text

    missing = T.read_file(result.meta["file_id"], page=99, root=store)  # type: ignore[index]
    assert missing.text == C.CITATION_NOT_FOUND


def test_unknown_file_is_reported(store: Path) -> None:
    with pytest.raises(FileNotFoundError, match="找不到文件"):
        T.read_file("f_0000000000000000", root=store)


# ---------------------------------------------------------------------------
# 引用格式与回答强制引用
# ---------------------------------------------------------------------------


def test_citation_helpers_format_page_and_cell_range() -> None:
    assert C.bbox_kind("pdf") == C.BBOX_PAGE
    assert C.bbox_kind("xlsx") == C.BBOX_TABLE
    assert C.bbox_kind("csv") == C.BBOX_TABLE
    assert C.bbox_kind("docx") == C.BBOX_TABLE
    assert C.bbox_kind("txt") is None
    assert C.column_label(1) == "A"
    assert C.column_label(27) == "AA"
    assert C.cell_range(2, 1, 2, 4) == "A2:D2"
    assert C.format_citation("年报.pdf", page=3) == "《年报.pdf》第 3 页"
    assert (
        C.format_citation("财务.xlsx", sheet="利润表", cell="A2:D2")
        == "《财务.xlsx》工作表「利润表」A2:D2"
    )
    with pytest.raises(ValueError):
        C.format_citation("年报.pdf")
    with pytest.raises(ValueError):
        C.cell_range(0, 1, 2, 2)


def test_file_answer_without_citation_falls_back_to_not_found() -> None:
    """契约 AGENT-04：文件结论必须有引用；没有引用时只能是「未在文件中找到」。"""
    assert C.finalize_file_answer("公司A 营收 100 万元", []) == C.CITATION_NOT_FOUND
    assert C.finalize_file_answer("", ["《财务.xlsx》工作表「利润表」A2:B2"]) == C.CITATION_NOT_FOUND

    answer = C.finalize_file_answer("公司A 营收 100 万元", ["《财务.xlsx》工作表「利润表」A2:B2"])
    assert "引用：《财务.xlsx》工作表「利润表」A2:B2" in answer
    assert C.has_file_citation(answer)

    already = "公司A 营收 100 万元（《财务.xlsx》工作表「利润表」A2:B2）"
    assert C.finalize_file_answer(already, ["《财务.xlsx》工作表「利润表」A2:B2"]) == already
    assert not C.has_file_citation("第 2 页写了营收")


def test_file_tool_registry_exposes_five_read_only_tools(store: Path, db: Path) -> None:
    registry = T.build_file_registry(root=store, db_path=db)

    assert registry.names() == ("file_list", "file_search", "file_read", "table_schema", "table_query")
    for spec in registry.specs():
        assert spec.permission_names == ("file:read",)
        assert spec.parameters["additionalProperties"] is False

    _ingest(_write(store.parent / "公告.txt", "公告正文。"), store, db)
    result = registry.invoke("file_search", {"query": "公告"})
    assert result.error is None
    assert "《公告.txt》" in result.text

    missing = registry.invoke("file_read", {"file_id": "f_0000000000000000"})
    assert missing.error is not None and "找不到文件" in missing.error

    with pytest.raises(ToolArgumentError, match="query"):
        registry.invoke("file_search", {})

# ---------------------------------------------------------------------------
# 表格：table_schema / table_query
# ---------------------------------------------------------------------------


def test_table_schema_lists_sheets_with_cell_range_citations(store: Path) -> None:
    source = _make_xlsx(
        store.parent / "财务.xlsx",
        {
            "利润表": [["名称", "营收"], ["公司A", 100], ["公司B", 200], ["公司C", 300]],
            "资产负债": [["项目", "金额"], ["现金", 50]],
        },
    )
    result = _ingest(source, store)

    schema = T.describe_table(result.meta["file_id"], root=store)  # type: ignore[index]

    assert "共 2 张表" in schema.text
    assert "《财务.xlsx》工作表「利润表」A1:B4" in schema.text
    assert "《财务.xlsx》工作表「资产负债」A1:B2" in schema.text
    assert schema.table is not None
    first = schema.table[0]
    assert first["columns"] == ["名称", "营收"]
    assert first["citation"] == "《财务.xlsx》工作表「利润表」A1:B4"


def test_table_schema_single_sheet_can_be_selected(store: Path) -> None:
    source = _make_xlsx(
        store.parent / "财务.xlsx",
        {
            "利润表": [["名称", "营收"], ["公司A", 100]],
            "资产负债": [["项目", "金额"], ["现金", 50]],
        },
    )
    result = _ingest(source, store)

    schema = T.describe_table(result.meta["file_id"], sheet="资产负债", root=store)  # type: ignore[index]

    assert schema.table is not None and len(schema.table) == 1
    assert schema.table[0]["sheet"] == "资产负债"
    assert "《财务.xlsx》工作表「资产负债」A1:B2" in schema.text


def test_table_schema_rejects_unknown_sheet(store: Path) -> None:
    source = _make_xlsx(store.parent / "财务.xlsx", {"利润表": [["名称", "营收"], ["公司A", 100]]})
    result = _ingest(source, store)

    with pytest.raises(ValueError, match="找不到工作表"):
        T.describe_table(result.meta["file_id"], sheet="不存在", root=store)  # type: ignore[index]


def test_table_query_only_select_and_forced_limit(store: Path) -> None:
    source = _make_xlsx(
        store.parent / "财务.xlsx",
        {"利润表": [["名称", "营收"], ["公司A", 100], ["公司B", 200], ["公司C", 300]]},
    )
    result = _ingest(source, store)
    file_id = result.meta["file_id"]  # type: ignore[index]

    top = T.query_table(file_id, "利润表", "SELECT 名称, 营收 FROM t ORDER BY 营收 DESC", limit=2, root=store)

    assert top.table == [{"名称": "公司C", "营收": 300.0}, {"名称": "公司B", "营收": 200.0}]
    assert top.metadata["limit"] == 2
    assert "已强制 LIMIT 2" in top.text
    assert "《财务.xlsx》工作表「利润表」A1:B4" in top.text


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("DELETE FROM t", "只允许 SELECT"),
        ("INSERT INTO t VALUES (1)", "只允许 SELECT"),
        ("CREATE TABLE x AS SELECT * FROM t", "只允许 SELECT"),
        ("ATTACH 'x.db'", "只允许 SELECT"),
        ("SELECT 1; SELECT 2", "只允许一条"),
        ("SELECT * FROM '利润表.parquet'", "文件路径"),
        ("SELECT read_parquet('x.parquet') FROM t", "文件读取函数"),
        ("SELECT * FROM t; DROP TABLE t", "只允许一条"),
    ],
)
def test_table_query_rejects_unsafe_sql(store: Path, sql: str, message: str) -> None:
    source = _make_xlsx(store.parent / "财务.xlsx", {"利润表": [["名称", "营收"], ["公司A", 100]]})
    result = _ingest(source, store)
    file_id = result.meta["file_id"]  # type: ignore[index]

    with pytest.raises(SqlGuardError, match=message):
        T.query_table(file_id, "利润表", sql, root=store)


def test_table_query_requires_sheet_when_file_has_many(store: Path) -> None:
    source = _make_xlsx(
        store.parent / "财务.xlsx",
        {"利润表": [["名称", "营收"], ["公司A", 100]], "资产负债": [["项目", "金额"], ["现金", 50]]},
    )
    result = _ingest(source, store)

    with pytest.raises(ValueError, match="有多张表"):
        T.query_table(result.meta["file_id"], "", "SELECT * FROM t", root=store)  # type: ignore[index]


def test_table_query_times_out_within_five_seconds(store: Path) -> None:
    rows = [["序号", "金额"]] + [[index, float(index)] for index in range(1000)]
    source = _make_xlsx(store.parent / "大表.xlsx", {"明细": rows})
    result = _ingest(source, store, index=False)

    started = time.perf_counter()
    with pytest.raises(TimeoutError, match="超时"):
        T.query_table(
            result.meta["file_id"],  # type: ignore[index]
            "明细",
            "SELECT sum(a.金额 + b.金额 + c.金额 + d.金额) FROM t a, t b, t c, t d",
            root=store,
            timeout_seconds=0.2,
        )
    assert time.perf_counter() - started < 5.0


def test_table_query_100k_rows_under_three_seconds(store: Path) -> None:
    """验收标准：查询 10 万行 Excel 表格的响应时间 < 3s（只计时查询本身）。"""
    source = _make_big_xlsx(store.parent / "明细.xlsx")
    result = _ingest(source, store, index=False)

    started = time.perf_counter()
    answer = T.query_table(
        result.meta["file_id"],  # type: ignore[index]
        "明细",
        "SELECT 名称, 金额 FROM t WHERE 金额 > 140000 ORDER BY 金额 DESC",
        limit=20,
        root=store,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 3.0, f"10 万行查询耗时 {elapsed:.2f}s"
    assert answer.table is not None and len(answer.table) == 20
    assert "《明细.xlsx》工作表「明细」A1:C100001" in answer.text


def test_table_query_multisheet_citation_names_the_sheet(store: Path) -> None:
    source = _make_xlsx(
        store.parent / "财务.xlsx",
        {
            "利润表": [["名称", "营收"], ["公司A", 100]],
            "资产负债": [["项目", "金额"], ["现金", 50], ["负债", 30]],
        },
    )
    result = _ingest(source, store)

    answer = T.query_table(
        result.meta["file_id"], "资产负债", "SELECT * FROM t", root=store  # type: ignore[index]
    )

    assert "《财务.xlsx》工作表「资产负债」A1:B3" in answer.text
    assert answer.metadata["sheet"] == "资产负债"
    assert answer.table is not None and len(answer.table) == 2


# ---------------------------------------------------------------------------
# bbox 两类语义 / DOCX 源行号
# ---------------------------------------------------------------------------


def test_pdf_table_bbox_is_page_coordinates(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    table = _FakeTable([["项目", "金额"], ["营收", "100"]], bbox=(10.0, 20.0, 110.0, 60.0))
    _patch_pdf(monkeypatch, [_FakePage("第一页文字", [table])])
    source = store.parent / "年报.pdf"
    source.write_bytes(b"%PDF-1.4 fake")
    result = _ingest(source, store)

    chunks = list(F.iter_chunks(result.meta["file_id"], root=store))  # type: ignore[index]
    table_chunks = [chunk for chunk in chunks if chunk["sheet"] == "p1_table_1"]

    assert table_chunks
    assert table_chunks[0]["bbox"] == [10.0, 20.0, 110.0, 60.0]
    assert all(chunk["bbox"] == [10.0, 20.0, 110.0, 60.0] for chunk in table_chunks)
    assert C.bbox_kind("pdf") == C.BBOX_PAGE


def test_excel_row_bbox_is_source_row_coordinates(store: Path) -> None:
    source = _make_xlsx(
        store.parent / "财务.xlsx", {"利润表": [["名称", "营收"], ["公司A", 100], ["公司B", 200]]}
    )
    result = _ingest(source, store)

    chunks = list(F.iter_chunks(result.meta["file_id"], root=store))  # type: ignore[index]
    data_rows = [chunk for chunk in chunks if chunk["kind"] == "table"]

    assert [chunk["bbox"] for chunk in data_rows] == [[2, 1, 2, 2], [3, 1, 3, 2]]
    assert C.bbox_kind("xlsx") == C.BBOX_TABLE
    read = T.read_file(result.meta["file_id"], block=data_rows[0]["block"], root=store)  # type: ignore[index]
    assert "《财务.xlsx》工作表「利润表」A2:B2" in read.text


def test_docx_table_citation_keeps_source_row_number(store: Path) -> None:
    source = _make_docx(store.parent / "报告.docx")
    result = _ingest(source, store)

    chunks = list(F.iter_chunks(result.meta["file_id"], root=store))  # type: ignore[index]
    data_rows = [chunk for chunk in chunks if chunk["kind"] == "table"]

    assert [chunk["bbox"][0] for chunk in data_rows] == [2, 3]
    second = T.read_file(result.meta["file_id"], block=data_rows[0]["block"], root=store)  # type: ignore[index]
    assert "《报告.docx》第 2 页 表格「table_1」第 2 行" in second.text


# ---------------------------------------------------------------------------
# SQL 守卫
# ---------------------------------------------------------------------------


def test_sql_guard_allows_select_and_cte_but_ignores_literals() -> None:
    assert ensure_select("SELECT * FROM t") == "SELECT * FROM t"
    assert ensure_select("WITH x AS (SELECT * FROM t) SELECT * FROM x").startswith("WITH x")
    assert ensure_select("SELECT * FROM t WHERE note = 'created'").startswith("SELECT")
    assert build_limited_sql("SELECT * FROM t", 5) == "SELECT * FROM (SELECT * FROM t) AS _limited LIMIT 5"
    with pytest.raises(ValueError):
        build_limited_sql("SELECT * FROM t", 0)
    with pytest.raises(SqlGuardError, match="必须引用"):
        ensure_select("SELECT 1")
    with pytest.raises(SqlGuardError, match="不能为空"):
        ensure_select("   ")


# ---------------------------------------------------------------------------
# 冻结清单 / ADR 同步
# ---------------------------------------------------------------------------


def test_freeze_doc_and_adr_use_15_fields_and_two_bbox_kinds() -> None:
    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "AStockAgent_interface_freeze_v1.md").read_text(encoding="utf-8")
    adr = (root / ".devloop" / "architecture" / "adr" / "ADR-015-file-ingestion.md").read_text(
        encoding="utf-8"
    )

    for text in (doc, adr):
        assert "15" in text
        assert "13 字段" not in text and "13 个字段" not in text
        assert "[x0,y0,x1,y1]" in text or "[x0, y0, x1, y1]" in text
        assert "[row_start,col_start,row_end,col_end]" in text or "[row_start, col_start, row_end, col_end]" in text

    assert "file_search" in doc and "table_query" in doc
    assert C.CITATION_NOT_FOUND in doc
    freeze_file_section = doc.split("## 5.")[0]
    assert "astock/agent/files.py" in freeze_file_section
    assert "冻结草案，未实现" not in freeze_file_section

    adrs = json.loads((root / ".devloop" / "architecture" / "adrs.json").read_text(encoding="utf-8"))
    adr_015 = next(item for item in adrs if item["id"] == "ADR-015")
    assert "13 字段" not in adr_015["summary"]
    assert "15" in adr_015["summary"]


# ---------------------------------------------------------------------------
# 契约 AGENT-04：文件结论必须带可核对引用
# ---------------------------------------------------------------------------


def test_file_result_requires_source(store: Path, db: Path) -> None:
    _ingest(_write(store.parent / "公告.txt", "公司A 营业收入 100 万元。"), store, db)

    found = T.search_files("营业收入", root=store, db_path=db)

    assert found.source == "files:search"
    assert "《公告.txt》第 1 页" in found.text
    assert found.table is not None
    assert all("《公告.txt》" in str(row.get("citation")) for row in found.table)

    assert C.finalize_file_answer("公司A 营业收入 100 万元", []) == C.CITATION_NOT_FOUND
    grounded = C.finalize_file_answer("公司A 营业收入 100 万元", [C.format_citation("公告.txt", page=1)])
    assert grounded.endswith("引用：《公告.txt》第 1 页")
