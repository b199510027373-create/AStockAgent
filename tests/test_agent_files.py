"""文件导入解析（ADR-015）离线测试。

覆盖：TXT/MD、CSV（编码/分隔符/表头/前导零代码列）、XLSX/XLS（多 sheet、
行列坐标、parquet）、DOCX（段落/分页/表格）、PDF（用 pdfplumber 桩：页码/表格
bbox/加密/扫描件/页数上限），以及内容寻址、冻结布局与字段、限额、FTS 索引及其
降级、原子写失败清理、读取辅助与 sheet_slug 越界防护。

pdfplumber 与本机无关：``_load_pdfplumber`` 被 monkeypatch 成假模块，
所以这些 PDF 用例在任何环境都能跑；CI 上装了真依赖时 DOCX 用例会走
python-docx 分支，本机则走 zipfile 兜底分支，两边断言一致。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pandas as pd
import pytest

from astock.agent import files as F

UPLOADED_AT = "2026-11-13T00:00:00+08:00"
FIXTURES = Path(__file__).with_name("fixtures")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """默认权限的临时目录。

    pytest 自带的 ``tmp_path`` 用 0o700 建目录，在受限 Windows 沙箱里会变成
    不可读（``PermissionError``），所以这里显式用默认权限建目录并在结束时清理。
    """
    base = Path(tempfile.gettempdir()) / f"astock-files-{uuid.uuid4().hex}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


def _make_xlsx(path: Path, sheets: dict[str, list[list[Any]]]) -> Path:
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    book.save(path)
    return path


_DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOCX_DOCUMENT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>第一页正文</w:t></w:r></w:p>
<w:p><w:r><w:br w:type="page"/></w:r></w:p>
<w:p><w:r><w:t>第二页正文</w:t></w:r></w:p>
<w:tbl>
<w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>金额</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>收入</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>100</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
</w:body></w:document>"""


def _make_docx(path: Path) -> Path:
    with zipfile_zip(path) as archive:
        archive.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        archive.writestr("_rels/.rels", _DOCX_RELS)
        archive.writestr("word/document.xml", _DOCX_DOCUMENT)
    return path


def zipfile_zip(path: Path) -> Any:
    import zipfile

    return zipfile.ZipFile(path, "w")


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


class _FakePDF:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _fake_plumber(
    pages: list[_FakePage],
    *,
    version: str = "0.11.0",
    open_error: BaseException | None = None,
) -> Any:
    def _open(_path: str) -> _FakePDF:
        if open_error is not None:
            raise open_error
        return _FakePDF(pages)

    return SimpleNamespace(__version__=version, open=_open)


def _patch_pdf(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    plumber = _fake_plumber(**kwargs)
    monkeypatch.setattr(F, "_load_pdfplumber", lambda: plumber)


def _blocks(result: F.IngestResult, root: Path) -> list[dict[str, Any]]:
    assert result.doc is not None
    return list(F.iter_chunks(result.doc.file_id, root=root))


def _assert_layout(result: F.IngestResult, root: Path, ext: str) -> dict[str, Any]:
    assert result.doc is not None
    directory = root / result.meta["sha256"]  # type: ignore[index]
    assert directory.is_dir()
    assert (directory / f"original.{ext}").is_file()
    assert (directory / "extracted.jsonl").is_file()
    assert (directory / "meta.json").is_file()
    assert (directory / "tables").is_dir()
    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    assert list(meta) == list(F.META_FIELDS)
    assert meta["status"] in F.STATUSES
    assert meta["file_id"] == "f_" + meta["sha256"][:16]
    return meta


@pytest.mark.parametrize("ext", ["txt", "csv", "xlsx", "xls", "docx", "pdf"])
def test_layout_matches_frozen_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ext: str
) -> None:
    """ADR-015 锚点：六种格式都落成 §4 的四件产物（original/extracted/tables/meta）。"""
    path = tmp_path / f"spec.{ext}"
    if ext == "txt":
        _write(path, "标题\n\n正文")
    elif ext == "csv":
        _write(path, "a,b\n1,2\n")
    elif ext == "xlsx":
        _make_xlsx(path, {"Sheet1": [["a", "b"], [1, 2]]})
    elif ext == "xls":
        shutil.copyfile(FIXTURES / "legacy_two_sheets.xls", path)
    elif ext == "docx":
        _make_docx(path)
    else:
        _patch_pdf(monkeypatch, pages=[_FakePage("只有文字")])
        path.write_bytes(b"%PDF-1.4 fake bytes")

    result = F.ingest_file(path, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    meta = _assert_layout(result, tmp_path / "store", ext)
    assert meta["pages"] is None or meta["pages"] >= 1
    directory = tmp_path / "store" / meta["sha256"]
    assert (directory / "extracted.jsonl").stat().st_size > 0


# ---------------------------------------------------------------------------
# TXT / MD
# ---------------------------------------------------------------------------


def test_txt_ingest_layout_and_frozen_fields(tmp_path: Path) -> None:
    source = _write(tmp_path / "公告.txt", "第一段内容。\n\n第二段内容，含营收数据。")
    result = F.ingest_file(source, root=tmp_path / "store", uploaded_at=UPLOADED_AT)

    assert result.status == F.STATUS_OK
    assert result.ok
    meta = _assert_layout(result, tmp_path / "store", "txt")
    assert meta["name"] == "公告.txt"
    assert meta["ext"] == "txt"
    assert meta["media_type"] == "text/plain"
    assert meta["uploaded_at"] == UPLOADED_AT
    assert meta["pages"] is None
    assert meta["sheets"] == []
    assert meta["parser"] == "plain-text"
    assert meta["size_bytes"] == source.stat().st_size
    assert meta["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()

    rows = _blocks(result, tmp_path / "store")
    assert len(rows) == 2
    assert meta["chunks_count"] == len(rows)
    assert meta["text_chars"] == sum(len(row["text"]) for row in rows)
    for block, row in enumerate(rows):
        assert list(row) == list(F.BLOCK_FIELDS)
        assert row["file_id"] == meta["file_id"]
        assert row["block"] == block
        assert row["page"] is None and row["sheet"] is None
        assert row["bbox"] is None
    assert rows[0]["text"] == "第一段内容。"


def test_original_is_immutable_and_content_addressed(tmp_path: Path) -> None:
    root = tmp_path / "store"
    first = _write(tmp_path / "a.txt", "同一份内容")
    renamed = _write(tmp_path / "b.txt", "同一份内容")

    r1 = F.ingest_file(first, root=root)
    original = root / r1.meta["sha256"] / "original.txt"  # type: ignore[index]
    before = original.read_bytes()

    r2 = F.ingest_file(renamed, root=root)

    assert r1.doc is not None and r2.doc is not None
    assert r1.doc.file_id == r2.doc.file_id
    assert r2.status == F.STATUS_OK
    assert original.read_bytes() == before
    assert r2.meta is not None and r2.meta["name"] == "b.txt"
    # 只应有一个内容目录，且仍只有一件 original.<ext>
    assert [p.name for p in root.iterdir()] == [r1.meta["sha256"]]  # type: ignore[index]
    assert [p.name for p in original.parent.glob("original.*")] == ["original.txt"]


def test_same_bytes_different_extension_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "store"
    text = "看起来像正文"
    txt = _write(tmp_path / "note.txt", text)
    md = _write(tmp_path / "note.md", text)

    assert F.ingest_file(txt, root=root).status == F.STATUS_OK
    result = F.ingest_file(md, root=root)

    assert result.status == F.STATUS_UNSUPPORTED
    assert any("不同扩展名" in warning for warning in result.warnings)


def test_markdown_paragraphs_and_long_text_split(tmp_path: Path) -> None:
    long_line = "长" * (F.MAX_CHUNK_CHARS * 2 + 10)
    source = _write(tmp_path / "研报.md", f"# 标题\n\n{long_line}")
    result = F.ingest_file(source, root=tmp_path / "store")

    rows = _blocks(result, tmp_path / "store")
    assert rows[0]["text"] == "# 标题"
    assert all(len(row["text"]) <= F.MAX_CHUNK_CHARS for row in rows)
    assert sum(len(row["text"]) for row in rows) > F.MAX_CHUNK_CHARS


def test_empty_text_file_reports_empty_text(tmp_path: Path) -> None:
    source = _write(tmp_path / "空白.txt", "   \n\n   \n")
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_EMPTY_TEXT
    assert result.warnings
    assert any(warning.startswith(F.EXTRACTION_WARNING_PREFIX) for warning in result.warnings)


def test_zero_byte_and_missing_and_unsupported(tmp_path: Path) -> None:
    empty = tmp_path / "zero.txt"
    empty.write_bytes(b"")
    assert F.ingest_file(empty, root=tmp_path / "store").status == F.STATUS_EMPTY_TEXT

    missing = F.ingest_file(tmp_path / "nope.txt", root=tmp_path / "store")
    assert missing.status == F.STATUS_UNSUPPORTED
    assert any("文件不存在" in warning for warning in missing.warnings)

    archive = tmp_path / "x.zip"
    archive.write_bytes(b"PK\x03\x04")
    unsupported = F.ingest_file(archive, root=tmp_path / "store")
    assert unsupported.status == F.STATUS_UNSUPPORTED
    assert any(".zip" in warning for warning in unsupported.warnings)


def test_macro_file_is_rejected_with_reason(tmp_path: Path) -> None:
    macro = tmp_path / "策略.xlsm"
    macro.write_bytes(b"PK\x03\x04dummy")
    result = F.ingest_file(macro, root=tmp_path / "store")

    assert result.status == F.STATUS_UNSUPPORTED
    assert any("宏文件" in warning for warning in result.warnings)
    assert not (tmp_path / "store").exists()


def test_oversize_file_is_rejected_with_limit_hint(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    with big.open("wb") as handle:
        handle.seek(F.MAX_FILE_BYTES)
        handle.write(b"x")

    result = F.ingest_file(big, root=tmp_path / "store")

    assert result.status == F.STATUS_UNSUPPORTED
    assert any("超过单文件上限" in warning for warning in result.warnings)
    assert any("20 MiB" in warning for warning in result.warnings)


def test_batch_limit_rejects_sixth_file(tmp_path: Path) -> None:
    paths = [_write(tmp_path / f"f{i}.txt", f"内容 {i}") for i in range(6)]
    results = F.ingest_files(paths, root=tmp_path / "store")

    assert len(results) == 6
    assert [result.status for result in results[:5]] == [F.STATUS_OK] * 5
    assert results[5].status == F.STATUS_UNSUPPORTED
    assert any("单轮最多导入 5 个文件" in warning for warning in results[5].warnings)


def test_describe_limits_reuses_constants() -> None:
    text = F.describe_limits()
    assert "20 MiB" in text
    assert f"{F.MAX_FILES_PER_BATCH} 个文件" in text
    assert f"{F.MAX_PDF_PAGES} 页" in text
    assert ".pdf" in text and ".md" in text


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_csv_utf8_bom_header_and_coordinates(tmp_path: Path) -> None:
    source = _write(tmp_path / "行情.csv", "代码,收盘价,成交量\n600000,12.5,1000\n000001,13.5,2000\n", "utf-8-sig")
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    meta = _assert_layout(result, tmp_path / "store", "csv")
    assert meta["sheets"][0]["name"] == "行情"
    slug = meta["sheets"][0]["slug"]
    assert meta["sheets"][0]["rows"] == 2
    assert meta["sheets"][0]["cols"] == 3
    assert meta["sheets"][0]["header_row"] == 1

    rows = _blocks(result, tmp_path / "store")
    header = [row for row in rows if row["kind"] == "text"]
    assert header and header[0]["bbox"] == [1, 1, 1, 3]
    data = [row for row in rows if row["kind"] == "table"]
    assert [row["bbox"] for row in data] == [[2, 1, 2, 3], [3, 1, 3, 3]]
    assert data[0]["text"].startswith("600000")

    frame = F.load_table(result.doc.file_id, slug, root=tmp_path / "store")  # type: ignore[union-attr]
    assert list(frame.columns) == ["代码", "收盘价", "成交量"]
    assert frame.shape == (2, 3)
    assert frame["收盘价"].tolist() == [12.5, 13.5]
    # 前导零代码列不得被转成 1.0：整列保持文本，并给出 extraction_warning。
    assert frame["代码"].tolist() == ["600000", "000001"]
    assert any(
        warning.startswith(F.EXTRACTION_WARNING_PREFIX) and "前导零" in warning
        for warning in result.warnings
    )


@pytest.mark.parametrize("delimiter", [";", "\t", "|"])
def test_csv_delimiter_detection(tmp_path: Path, delimiter: str) -> None:
    body = delimiter.join(["alpha", "beta"]) + "\n" + delimiter.join(["1", "2"]) + "\n"
    source = _write(tmp_path / "data.csv", body)
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    frame = F.load_table(result.doc.file_id, "data", root=tmp_path / "store")  # type: ignore[union-attr]
    assert list(frame.columns) == ["alpha", "beta"]
    assert frame.shape == (1, 2)


def test_csv_gb18030_is_decoded(tmp_path: Path) -> None:
    source = tmp_path / "中文.csv"
    source.write_bytes("名称,价格\n苹果,10\n香蕉,12\n".encode("gb18030"))
    result = F.ingest_file(source, root=tmp_path / "store")

    rows = _blocks(result, tmp_path / "store")
    assert any("苹果" in row["text"] for row in rows)
    slug = result.meta["sheets"][0]["slug"]  # type: ignore[index]
    frame = F.load_table(result.doc.file_id, slug, root=tmp_path / "store")  # type: ignore[union-attr]
    assert frame["价格"].tolist() == [10.0, 12.0]


def test_csv_without_header_uses_column_names(tmp_path: Path) -> None:
    source = _write(tmp_path / "raw.csv", "1,2\n3,4\n")
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.meta is not None
    assert result.meta["sheets"][0]["header_row"] is None
    slug = result.meta["sheets"][0]["slug"]
    frame = F.load_table(result.doc.file_id, slug, root=tmp_path / "store")  # type: ignore[union-attr]
    assert list(frame.columns) == ["col_1", "col_2"]
    assert frame.shape == (2, 2)
    rows = _blocks(result, tmp_path / "store")
    data = [row for row in rows if row["kind"] == "table"]
    assert data[0]["bbox"] == [1, 1, 1, 2]


def test_empty_csv_reports_empty_text(tmp_path: Path) -> None:
    source = tmp_path / "empty.csv"
    source.write_bytes(b"")
    # 0 字节走文件级拒绝
    assert F.ingest_file(source, root=tmp_path / "store").status == F.STATUS_EMPTY_TEXT

    blank = _write(tmp_path / "blank.csv", "\n\n")
    result = F.ingest_file(blank, root=tmp_path / "store")
    assert result.status == F.STATUS_EMPTY_TEXT
    assert result.warnings


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def test_xlsx_multi_sheet_with_coordinates(tmp_path: Path) -> None:
    source = _make_xlsx(
        tmp_path / "财报.xlsx",
        {
            "利润表": [["项目", "本期", "上期"], ["营收", 100, 90], ["净利", 20, 18]],
            "现金流": [["科目", "金额"], ["经营", 30]],
        },
    )
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    meta = _assert_layout(result, tmp_path / "store", "xlsx")
    assert [sheet["name"] for sheet in meta["sheets"]] == ["利润表", "现金流"]
    assert [sheet["slug"] for sheet in meta["sheets"]] == ["sheet", "sheet_2"]
    assert meta["sheets"][0]["rows"] == 2
    assert meta["sheets"][0]["cols"] == 3
    assert meta["sheets"][0]["header_row"] == 1
    assert meta["pages"] is None

    rows = _blocks(result, tmp_path / "store")
    data = [row for row in rows if row["kind"] == "table"]
    assert data[0]["sheet"] == "利润表"
    assert data[0]["bbox"] == [2, 1, 2, 3]
    assert data[1]["bbox"] == [3, 1, 3, 3]

    frame = F.load_table(result.doc.file_id, "sheet", root=tmp_path / "store")  # type: ignore[union-attr]
    assert list(frame.columns) == ["项目", "本期", "上期"]
    assert frame["本期"].tolist() == [100.0, 20.0]
    assert set(p.name for p in (tmp_path / "store" / meta["sha256"] / "tables").iterdir()) == {  # type: ignore[index]
        "sheet.parquet",
        "sheet_2.parquet",
    }


def test_xlsx_sheet_slug_collision_gets_suffix(tmp_path: Path) -> None:
    source = _make_xlsx(
        tmp_path / "dup.xlsx",
        {"A B": [["x", "y"], [1, 2]], "a_b": [["x", "y"], [3, 4]]},
    )
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.meta is not None
    assert [sheet["slug"] for sheet in result.meta["sheets"]] == ["a_b", "a_b_2"]


def test_xlsx_mixed_code_column_stays_text(tmp_path: Path) -> None:
    """文本代码与数值单元格混排时整列归一化为文本，parquet 列类型稳定。"""
    source = _make_xlsx(
        tmp_path / "持仓.xlsx",
        {"持仓": [["代码", "市值", "备注"], ["000001", 12.5, "A"], [600000, 13.0, 1]]},
    )
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    meta = _assert_layout(result, tmp_path / "store", "xlsx")
    slug = meta["sheets"][0]["slug"]
    frame = F.load_table(result.doc.file_id, slug, root=tmp_path / "store")  # type: ignore[union-attr]
    # 混合列（str + int）若保留 object，pyarrow 会抛 ArrowTypeError 让整份导入失败。
    assert frame["代码"].tolist() == ["000001", "600000"]
    assert str(frame["代码"].dtype) == "string"
    assert frame["备注"].tolist() == ["A", "1"]
    assert frame["市值"].tolist() == [12.5, 13.0]
    assert any(
        warning.startswith(F.EXTRACTION_WARNING_PREFIX) and "前导零" in warning
        for warning in result.warnings
    )


def test_legacy_xls_is_parsed_with_coordinates(tmp_path: Path) -> None:
    """真实 .xls（BIFF8 两 sheet 夹具，见 tests/fixtures/build_legacy_xls.py）走 xlrd。"""
    source = tmp_path / "legacy.xls"
    shutil.copyfile(FIXTURES / "legacy_two_sheets.xls", source)

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    meta = _assert_layout(result, tmp_path / "store", "xls")
    assert meta["parser"] == "xlrd"
    assert meta["pages"] is None
    assert [sheet["name"] for sheet in meta["sheets"]] == ["利润表", "现金流"]
    assert [sheet["slug"] for sheet in meta["sheets"]] == ["sheet", "sheet_2"]
    assert meta["sheets"][0]["rows"] == 2
    assert meta["sheets"][0]["cols"] == 3
    assert meta["sheets"][0]["header_row"] == 1

    rows = _blocks(result, tmp_path / "store")
    first_sheet = [row for row in rows if row["sheet"] == "利润表"]
    data = [row for row in first_sheet if row["kind"] == "table"]
    assert [row["bbox"] for row in data] == [[2, 1, 2, 3], [3, 1, 3, 3]]

    profit = F.load_table(result.doc.file_id, "sheet", root=tmp_path / "store")  # type: ignore[union-attr]
    assert list(profit.columns) == ["项目", "本期", "上期"]
    assert profit.shape == (2, 3)
    assert profit["本期"].tolist() == [100.0, 20.0]

    cash = F.load_table(result.doc.file_id, "sheet_2", root=tmp_path / "store")  # type: ignore[union-attr]
    assert cash["代码"].tolist() == ["000001", "600000"]
    assert cash["金额"].tolist() == [30.0, 12.5]


def test_xls_without_engine_reports_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.xls"
    source.write_bytes(b"\xd0\xcf\x11\xe0 dummy")

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ImportError("Missing optional dependency 'xlrd'")

    monkeypatch.setattr(F.pd, "read_excel", _boom)
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_UNSUPPORTED
    assert any("xlrd" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def test_docx_paragraphs_page_break_and_table(tmp_path: Path) -> None:
    source = _make_docx(tmp_path / "报告.docx")
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    meta = _assert_layout(result, tmp_path / "store", "docx")
    assert meta["pages"] == 2
    assert meta["parser_version"]
    assert [sheet["name"] for sheet in meta["sheets"]] == ["table_1"]
    assert meta["sheets"][0]["header_row"] == 1
    assert meta["sheets"][0]["rows"] == 1

    rows = _blocks(result, tmp_path / "store")
    assert any(row["page"] == 1 and "第一页正文" in row["text"] for row in rows)
    assert any(row["page"] == 2 and "第二页正文" in row["text"] for row in rows)
    table_rows = [row for row in rows if row["sheet"] == "table_1"]
    assert table_rows and table_rows[0]["page"] == 2
    data_rows = [row for row in table_rows if row["kind"] == "table"]
    assert data_rows and data_rows[0]["bbox"] == [2, 1, 2, 2]

    frame = F.load_table(result.doc.file_id, "table_1", root=tmp_path / "store")  # type: ignore[union-attr]
    assert list(frame.columns) == ["项目", "金额"]
    assert frame.shape == (1, 2)


def test_encrypted_ooxml_container_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "secret.xlsx"
    source.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_ENCRYPTED
    assert any("不支持解密" in warning for warning in result.warnings)
    assert not (tmp_path / "store").exists()


def test_docx_invalid_package_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "broken.docx"
    source.write_bytes(b"not a zip at all")
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_UNSUPPORTED
    assert any("DOCX" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# PDF（pdfplumber 桩）
# ---------------------------------------------------------------------------


def test_pdf_keeps_page_numbers_and_table_bbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = [
        _FakeTable([["项目", "值"], ["营收", "100"]], bbox=(10.0, 20.0, 110.0, 60.0))
    ]
    pages = [_FakePage("第一页文字"), _FakePage("第二页文字", tables)]
    _patch_pdf(monkeypatch, pages=pages)
    source = tmp_path / "年报.pdf"
    source.write_bytes(b"%PDF-1.4 fake bytes")

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    meta = _assert_layout(result, tmp_path / "store", "pdf")
    assert meta["pages"] == 2
    assert meta["parser"] == "pdfplumber"
    assert meta["parser_version"] == "0.11.0"
    assert [sheet["name"] for sheet in meta["sheets"]] == ["p2_table_1"]

    rows = _blocks(result, tmp_path / "store")
    text_rows = [row for row in rows if row["kind"] == "text" and row["sheet"] is None]
    assert [(row["page"], row["text"]) for row in text_rows] == [
        (1, "第一页文字"),
        (2, "第二页文字"),
    ]
    summary = [row for row in rows if row["sheet"] == "p2_table_1"][0]
    assert summary["page"] == 2
    assert summary["bbox"] == [10.0, 20.0, 110.0, 60.0]
    assert "工作表 p2_table_1" in summary["text"]

    frame = F.load_table(result.doc.file_id, "p2_table_1", root=tmp_path / "store")  # type: ignore[union-attr]
    assert frame.shape == (1, 2)


def test_pdf_encrypted_is_reported_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pdf(monkeypatch, pages=[], open_error=ValueError("password required to open PDF"))
    source = tmp_path / "secret.pdf"
    source.write_bytes(b"%PDF-1.4 encrypted")

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_ENCRYPTED
    assert any("加密" in warning for warning in result.warnings)
    assert not (tmp_path / "store").exists()


def test_pdf_encryption_detected_on_page_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _LateEncryptedPDF:
        @property
        def pages(self) -> list[_FakePage]:
            raise RuntimeError(ValueError("PDFPasswordIncorrect"))

        def close(self) -> None:
            return None

    plumber = SimpleNamespace(__version__="0.11.0", open=lambda _path: _LateEncryptedPDF())
    monkeypatch.setattr(F, "_load_pdfplumber", lambda: plumber)
    source = tmp_path / "late.pdf"
    source.write_bytes(b"%PDF-1.4 late encrypted")

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_ENCRYPTED
    assert any("加密" in warning for warning in result.warnings)


def test_pdf_page_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pdf(monkeypatch, pages=[_FakePage("p")] * (F.MAX_PDF_PAGES + 1))
    source = tmp_path / "huge.pdf"
    source.write_bytes(b"%PDF-1.4 many pages")

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_UNSUPPORTED
    assert any(f"{F.MAX_PDF_PAGES} 页" in warning for warning in result.warnings)


def test_scan_like_pdf_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pdf(monkeypatch, pages=[_FakePage(""), _FakePage("")])
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 image only")

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_SUSPECTED_SCAN
    assert any(
        warning.startswith(F.EXTRACTION_WARNING_PREFIX) and "扫描件" in warning
        for warning in result.warnings
    )
    assert result.meta is not None and result.meta["status"] == F.STATUS_SUSPECTED_SCAN


def test_pdf_partial_pages_warn_about_missing_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pdf(monkeypatch, pages=[_FakePage("有文字"), _FakePage(""), _FakePage("也有")])
    source = tmp_path / "mixed.pdf"
    source.write_bytes(b"%PDF-1.4 mixed")

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK
    assert any("第 2 页" in warning for warning in result.warnings)


def test_pdf_without_pdfplumber_explains_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(F, "_load_pdfplumber", lambda: None)
    source = tmp_path / "x.pdf"
    source.write_bytes(b"%PDF-1.4")

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_UNSUPPORTED
    assert any("pdfplumber" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# PDF（真实 pdfplumber + 已提交的 PDF 夹具；未安装 pdfplumber 时跳过）
# ---------------------------------------------------------------------------


def test_real_pdf_text_layer_keeps_pages_and_tables(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    source = FIXTURES / "pdf_text_tables.pdf"

    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.status == F.STATUS_OK, result.warnings
    meta = _assert_layout(result, tmp_path / "store", "pdf")
    assert meta["pages"] == 2
    assert meta["parser"] == "pdfplumber"
    assert meta["parser_version"].startswith("0.11")
    assert meta["text_chars"] > 0
    assert [sheet["slug"] for sheet in meta["sheets"]] == ["p1_table_1", "p2_table_1"]
    assert all(sheet["header_row"] == 1 for sheet in meta["sheets"])

    rows = _blocks(result, tmp_path / "store")
    assert {row["page"] for row in rows if row["page"] is not None} == {1, 2}
    first_page_text = " ".join(
        row["text"] for row in rows if row["page"] == 1 and row["kind"] == "text"
    )
    assert "Quarterly Report 2026 Q3" in first_page_text

    row_chunks = [row for row in rows if row["sheet"] == "p1_table_1" and row["kind"] == "table"]
    assert [row["bbox"] for row in row_chunks] == [[2, 1, 2, 3], [3, 1, 3, 3]]
    assert all(row["page"] == 1 for row in row_chunks)

    summary = next(row for row in rows if row["sheet"] == "p1_table_1" and row["kind"] == "text")
    assert summary["bbox"] is not None and len(summary["bbox"]) == 4
    assert summary["bbox"][2] > summary["bbox"][0]

    frame = F.load_table(result.doc.file_id, "p1_table_1", root=tmp_path / "store")  # type: ignore[union-attr]
    assert frame.shape == (2, 3)
    assert list(frame.columns) == ["Item", "Amount", "YoY"]
    assert frame["Amount"].tolist() == [1250000.0, 230000.0]
    assert frame["Item"].tolist() == ["Revenue", "Net profit"]


def test_real_encrypted_pdf_is_reported_as_encrypted(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    result = F.ingest_file(FIXTURES / "pdf_encrypted.pdf", root=tmp_path / "store")

    assert result.status == F.STATUS_ENCRYPTED, result.warnings
    assert any("加密" in warning for warning in result.warnings)
    assert not (tmp_path / "store").exists()


def test_real_scan_like_pdf_is_suspected_scan(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    result = F.ingest_file(FIXTURES / "pdf_scan_like.pdf", root=tmp_path / "store")

    assert result.status == F.STATUS_SUSPECTED_SCAN, result.warnings
    assert any("扫描件" in warning for warning in result.warnings)
    assert result.meta is not None and result.meta["text_chars"] == 0


def test_real_cjk_pdf_keeps_chinese_text(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    result = F.ingest_file(FIXTURES / "pdf_cjk.pdf", root=tmp_path / "store")

    assert result.status == F.STATUS_OK, result.warnings
    text = "".join(row["text"] for row in _blocks(result, tmp_path / "store"))
    assert "贵州茅台" in text
    assert "营业收入 125.6 亿元" in text
    assert "同比增长 12.5%" in text
    assert result.meta is not None and result.meta["pages"] == 1


def test_encryption_detection_unwraps_nested_exception() -> None:
    inner = ValueError("PDFPasswordIncorrect")
    wrapped = RuntimeError("PdfminerException")
    wrapped.__cause__ = inner

    assert F._is_encryption_error(wrapped) is True
    assert F._is_encryption_error(RuntimeError(inner)) is True
    assert F._is_encryption_error(ValueError("bad xref table")) is False


# ---------------------------------------------------------------------------
# FTS 索引与读取辅助
# ---------------------------------------------------------------------------


def test_index_writes_fts_rows_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "store"
    db = tmp_path / "idx.db"
    source = _write(tmp_path / "notes.txt", "revenue growth\n\nmargin expansion")

    result = F.ingest_file(source, root=root, db_path=db)
    assert result.doc is not None
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM file_chunks WHERE file_id = ?", (result.doc.file_id,)
        ).fetchone()[0]
        hits = conn.execute(
            "SELECT rowid FROM file_chunks_fts WHERE file_chunks_fts MATCH 'revenue'"
        ).fetchall()
    assert count == 2
    assert len(hits) == 1

    F.ingest_file(source, root=root, db_path=db)
    with sqlite3.connect(db) as conn:
        again = conn.execute(
            "SELECT COUNT(*) FROM file_chunks WHERE file_id = ?", (result.doc.file_id,)
        ).fetchone()[0]
        hits = conn.execute(
            "SELECT rowid FROM file_chunks_fts WHERE file_chunks_fts MATCH 'revenue'"
        ).fetchall()
    assert again == 2
    assert len(hits) == 1


def test_index_can_be_disabled(tmp_path: Path) -> None:
    source = _write(tmp_path / "a.txt", "no index here")
    db = tmp_path / "never.db"

    result = F.ingest_file(source, root=tmp_path / "store", db_path=db, index=False)

    assert result.status == F.STATUS_OK
    assert not db.exists()


def test_load_meta_rejects_extra_fields(tmp_path: Path) -> None:
    result = F.ingest_file(_write(tmp_path / "a.txt", "内容"), root=tmp_path / "store")
    assert result.meta is not None
    meta_path = tmp_path / "store" / result.meta["sha256"] / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["extra_field"] = 1
    meta_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="extra_field"):
        F.load_meta(result.meta["file_id"], root=tmp_path / "store")


def test_file_dir_accepts_sha256_and_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "store"
    result = F.ingest_file(_write(tmp_path / "a.txt", "内容"), root=root)
    assert result.meta is not None

    assert F.file_dir(result.meta["sha256"], root=root).name == result.meta["sha256"]
    assert F.file_dir(result.meta["file_id"], root=root).is_dir()
    with pytest.raises(FileNotFoundError):
        F.file_dir("../../etc", root=root)
    with pytest.raises(FileNotFoundError):
        F.file_dir("f_deadbeefdeadbeef", root=root)


@pytest.mark.parametrize(
    "bad_slug",
    ["../../x", "../meta", "C:/Windows/win.ini", "..\\..\\escape", "sheet/../sheet"],
)
def test_load_table_rejects_traversal(tmp_path: Path, bad_slug: str) -> None:
    """sheet_slug 来自工具/模型参数，必须按白名单拒绝越出 tables/ 的路径。"""
    root = tmp_path / "store"
    result = F.ingest_file(_write(tmp_path / "a.csv", "a,b\n1,2\n"), root=root)
    assert result.meta is not None

    with pytest.raises(ValueError, match="sheet_slug"):
        F.load_table(result.meta["file_id"], bad_slug, root=root)


def test_atomic_write_failure_leaves_no_tmp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子写失败时不得残留 .meta.json.tmp-*，也不得留下半截 meta.json。"""
    source = _write(tmp_path / "a.txt", "内容")
    store = tmp_path / "store"
    real_write_bytes = Path.write_bytes

    def failing_write(self: Path, payload: bytes) -> int:
        if self.name.startswith(".meta.json.tmp-"):
            raise OSError("disk full")
        return real_write_bytes(self, payload)

    monkeypatch.setattr(Path, "write_bytes", failing_write)

    result = F.ingest_file(source, root=store)

    assert result.status == F.STATUS_UNSUPPORTED
    assert any("disk full" in warning for warning in result.warnings)
    directory = store / hashlib.sha256(source.read_bytes()).hexdigest()
    assert directory.is_dir()
    assert not (directory / "meta.json").exists()
    assert not list(directory.glob(".meta.json.tmp-*"))


def test_index_failure_does_not_block_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FTS 索引失败只降级检索能力，抽取结果与 status 不受影响。"""
    source = _write(tmp_path / "notes.txt", "第一段\n\n第二段")

    def failing_index(*_args: Any, **_kwargs: Any) -> int:
        raise sqlite3.OperationalError("fts5 unavailable")

    monkeypatch.setattr(F, "index_chunks", failing_index)
    result = F.ingest_file(source, root=tmp_path / "store", db_path=tmp_path / "idx.db")

    assert result.status == F.STATUS_OK
    assert any(
        warning.startswith(F.EXTRACTION_WARNING_PREFIX) and "索引" in warning
        for warning in result.warnings
    )
    assert result.meta is not None
    meta = F.load_meta(result.meta["file_id"], root=tmp_path / "store")
    assert list(meta) == list(F.META_FIELDS)
    chunks = list(F.iter_chunks(meta["file_id"], root=tmp_path / "store"))
    assert len(chunks) == meta["chunks_count"]


def test_extracted_doc_pages_group_by_page_and_sheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_pdf(monkeypatch, pages=[_FakePage("a\n\nb"), _FakePage("c")])
    source = tmp_path / "group.pdf"
    source.write_bytes(b"%PDF-1.4")
    result = F.ingest_file(source, root=tmp_path / "store")

    assert result.doc is not None
    assert [(page["page"], page["sheet"]) for page in result.doc.pages] == [
        (1, None),
        (2, None),
    ]
    assert result.doc.pages[0]["text"] == "a\nb"
