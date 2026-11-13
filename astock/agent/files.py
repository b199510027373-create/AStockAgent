"""文件导入解析：PDF / Excel / CSV / DOCX / TXT / MD（ADR-015）。

冻结布局见 ``docs/AStockAgent_interface_freeze_v1.md`` §4，逐字约束：

```text
data/agent/files/<sha256>/
  original.<ext>                 # 原样字节，只写一次（不可变）
  extracted.jsonl                # 逐块抽取结果，一行一个 chunk
  tables/<sheet_slug>.parquet    # 每张工作表 / 每个表格一个 parquet
  meta.json                      # 15 个必填键，不增不减
```

设计要点
--------
* :class:`ExtractedDoc` 只是内存态返回类型；持久化的抽取结果**只有**
  ``extracted.jsonl``（每行一个 chunk，带 ``page``/``sheet`` 定位）。
* 工作表 chunk 的位置写入 ``bbox``：``[row_start, col_start, row_end, col_end]``
  （1 基，行号即 Excel 行号）；PDF 文本块 ``bbox`` 为 ``null``，PDF 表格为
  pdfplumber 的 ``[x0, y0, x1, y1]``。这样既不增字段，又能回查行号。
* 不支持的能力（扫描件 OCR、加密 PDF、宏文件、超限）返回 ``status`` +
  ``warnings``，不抛未捕获异常（C2），也**不静默返回空文档**（C3）。
* 三个限额常量定义在本模块顶部，工具层必须复用，不得各处硬编码。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import platform
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence
from xml.etree import ElementTree as ET

import pandas as pd

from ..config import BASE_DIR

__all__ = [
    "BLOCK_FIELDS",
    "DEFAULT_FILES_DIR",
    "EXTRACTION_WARNING_PREFIX",
    "ExtractedDoc",
    "FILE_CHUNKS_DDL",
    "FILE_CHUNKS_TRIGGER_DDL",
    "IngestResult",
    "MAX_CHUNK_CHARS",
    "MAX_FILES_PER_BATCH",
    "MAX_FILE_BYTES",
    "MAX_PDF_PAGES",
    "META_FIELDS",
    "STATUS_EMPTY_TEXT",
    "STATUS_ENCRYPTED",
    "STATUS_OK",
    "STATUS_SUSPECTED_SCAN",
    "STATUS_UNSUPPORTED",
    "STATUSES",
    "SUPPORTED_EXTS",
    "describe_limits",
    "ensure_index_schema",
    "file_dir",
    "index_chunks",
    "ingest_file",
    "ingest_files",
    "iter_chunks",
    "load_meta",
    "load_table",
]

# ---------------------------------------------------------------------------
# 限额与常量（C2：单一定义点，工具层复用）
# ---------------------------------------------------------------------------

#: 单文件大小上限：20 MiB（不是 20 MB）。
MAX_FILE_BYTES = 20 * 1024 * 1024

#: 单轮最多导入的文件数。
MAX_FILES_PER_BATCH = 5

#: PDF 页数上限。
MAX_PDF_PAGES = 200

#: 单个文本块进入 ``extracted.jsonl`` 的最大字符数。
MAX_CHUNK_CHARS = 2000

#: 单张工作表写入 FTS 的最大行数（parquet 仍保留全量）。
MAX_SHEET_CHUNK_ROWS = 2000

#: 表头探测只在最前面这几行里找。
HEADER_SCAN_ROWS = 5

STATUS_OK = "ok"
STATUS_EMPTY_TEXT = "empty_text"
STATUS_SUSPECTED_SCAN = "suspected_scan"
STATUS_ENCRYPTED = "encrypted"
STATUS_UNSUPPORTED = "unsupported"

#: ``meta.json.status`` 的封闭枚举（冻结，不得增删）。
STATUSES = (
    STATUS_OK,
    STATUS_EMPTY_TEXT,
    STATUS_SUSPECTED_SCAN,
    STATUS_ENCRYPTED,
    STATUS_UNSUPPORTED,
)

#: 自检发现问题时写进 ``meta.json.warnings`` 的固定前缀（不新增字段）。
EXTRACTION_WARNING_PREFIX = "extraction_warning:"

#: ``meta.json`` 的全部键，顺序即写入顺序（冻结 15 键）。
META_FIELDS = (
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

#: ``extracted.jsonl`` 单行的全部键（冻结 8 键）。
BLOCK_FIELDS = ("file_id", "page", "sheet", "block", "kind", "text", "bbox", "cells")

#: 支持的扩展名（小写，不含点）。
SUPPORTED_EXTS = ("pdf", "xlsx", "xls", "csv", "docx", "txt", "md")

#: 宏文件：v1 明确不支持。
MACRO_EXTS = ("xlsm", "xltm", "xlam", "docm", "dotm", "pptm", "potm")

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "md": "text/markdown",
}

#: 内容寻址根目录，可用 ``AGENT_FILES_DIR`` 覆盖（测试与多环境部署）。
DEFAULT_FILES_DIR = Path(
    os.getenv("AGENT_FILES_DIR") or (BASE_DIR / "data" / "agent" / "files")
)

#: 冻结的 FTS5 建表语句（与《接口冻结清单》§4 字符级一致）。
FILE_CHUNKS_DDL = """CREATE TABLE IF NOT EXISTS file_chunks(
  chunk_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL, page INTEGER,
  sheet TEXT, block INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_fts USING fts5(
  text, content='file_chunks', content_rowid='chunk_id', tokenize='unicode61');
CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks(file_id, page);"""

#: ``file_chunks_fts`` 与 ``file_chunks`` 的同步触发器（ADR-015 §5）。
FILE_CHUNKS_TRIGGER_DDL = """CREATE TRIGGER IF NOT EXISTS file_chunks_ai AFTER INSERT ON file_chunks BEGIN
  INSERT INTO file_chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS file_chunks_ad AFTER DELETE ON file_chunks BEGIN
  INSERT INTO file_chunks_fts(file_chunks_fts, rowid, text)
  VALUES ('delete', old.chunk_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS file_chunks_au AFTER UPDATE ON file_chunks BEGIN
  INSERT INTO file_chunks_fts(file_chunks_fts, rowid, text)
  VALUES ('delete', old.chunk_id, old.text);
  INSERT INTO file_chunks_fts(rowid, text) VALUES (new.chunk_id, new.text);
END;"""

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def describe_limits() -> str:
    """给用户看的限额提示（工具层直接复用，避免各处硬编码文案）。"""
    return (
        f"文件导入上限：单文件 ≤ {MAX_FILE_BYTES // (1024 * 1024)} MiB、"
        f"单轮 ≤ {MAX_FILES_PER_BATCH} 个文件、PDF ≤ {MAX_PDF_PAGES} 页；"
        f"支持 {', '.join('.' + e for e in SUPPORTED_EXTS)}。"
    )


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


@dataclass
class ExtractedDoc:
    """内存态抽取结果（``ExtractedDoc {file_id, name, pages[], tables[]}``）。"""

    file_id: str
    name: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IngestResult:
    """一次导入的结果：失败也返回对象，不抛未捕获异常。"""

    name: str
    status: str
    warnings: list[str] = field(default_factory=list)
    doc: ExtractedDoc | None = None
    meta: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.doc is not None and self.status == STATUS_OK


@dataclass
class _Chunk:
    page: int | None
    sheet: str | None
    kind: str
    text: str
    bbox: list[float] | None = None
    cells: int | None = None


@dataclass
class _Parsed:
    parser: str
    parser_version: str
    pages: int | None
    chunks: list[_Chunk] = field(default_factory=list)
    sheet_meta: list[dict[str, Any]] = field(default_factory=list)
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class _Rejected(Exception):
    """内部拒绝信号：转成 ``status`` + ``warnings`` 返回给调用方。"""

    def __init__(self, status: str, warnings: Sequence[str]) -> None:
        super().__init__(warnings[0] if warnings else status)
        self.status = status
        self.warnings = list(warnings)


# ---------------------------------------------------------------------------
# 导入入口
# ---------------------------------------------------------------------------


def ingest_file(
    path: str | Path,
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    uploaded_at: str | None = None,
    index: bool = True,
) -> IngestResult:
    """解析一个文件并按 sha256 落盘；任何失败都以 ``status`` + ``warnings`` 返回。"""
    upload = uploaded_at or _now_iso()
    source = Path(path)
    try:
        return _ingest(source, root=root, db_path=db_path, uploaded_at=upload, index=index)
    except _Rejected as exc:
        return IngestResult(name=source.name, status=exc.status, warnings=exc.warnings)
    except Exception as exc:  # 兜底：解析器内部异常也不让工具层崩（C2）
        return IngestResult(
            name=source.name,
            status=STATUS_UNSUPPORTED,
            warnings=[f"解析失败：{type(exc).__name__}: {exc}"],
        )


def ingest_files(
    paths: Sequence[str | Path],
    *,
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    uploaded_at: str | None = None,
    index: bool = True,
) -> list[IngestResult]:
    """批量导入；超过 :data:`MAX_FILES_PER_BATCH` 的文件逐个拒绝并说明原因。"""
    upload = uploaded_at or _now_iso()
    results: list[IngestResult] = []
    for position, item in enumerate(paths, start=1):
        source = Path(item)
        if position > MAX_FILES_PER_BATCH:
            results.append(
                IngestResult(
                    name=source.name,
                    status=STATUS_UNSUPPORTED,
                    warnings=[
                        f"单轮最多导入 {MAX_FILES_PER_BATCH} 个文件，第 {position} 个"
                        f"（{source.name}）未处理；请分批导入。"
                    ],
                )
            )
            continue
        results.append(ingest_file(source, root=root, db_path=db_path, uploaded_at=upload, index=index))
    return results


def _ingest(
    source: Path,
    *,
    root: str | Path | None,
    db_path: str | Path | None,
    uploaded_at: str,
    index: bool,
) -> IngestResult:
    ext = source.suffix.lower().lstrip(".")
    if not source.is_file():
        raise _Rejected(STATUS_UNSUPPORTED, [f"文件不存在或不是普通文件：{source}"])

    if ext in MACRO_EXTS:
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [
                f"暂不支持宏文件（.{ext}）：v1 不执行、也不解析宏，请另存为 .xlsx/.docx 后重试。"
            ],
        )
    if ext not in SUPPORTED_EXTS:
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [
                f"暂不支持 .{ext or '(无扩展名)'} 格式；"
                f"当前支持 {', '.join('.' + e for e in SUPPORTED_EXTS)}。"
            ],
        )

    size = source.stat().st_size
    if size > MAX_FILE_BYTES:
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [
                f"文件大小 {size / (1024 * 1024):.2f} MiB 超过单文件上限 "
                f"{MAX_FILE_BYTES // (1024 * 1024)} MiB，未解析。{describe_limits()}"
            ],
        )
    if size == 0:
        raise _Rejected(STATUS_EMPTY_TEXT, [f"文件为空（0 字节）：{source.name}"])

    payload = source.read_bytes()
    if ext in ("xlsx", "docx") and payload.startswith(b"\xd0\xcf\x11\xe0"):
        # 加密的 OOXML 会被包成 OLE 复合文档（CFB），而不是 zip。
        raise _Rejected(
            STATUS_ENCRYPTED,
            [
                f".{ext} 是加密的 OLE 复合文档（文件头 D0 CF 11 E0），v1 不支持解密；"
                "请另存为未加密副本后重试。"
            ],
        )
    digest = hashlib.sha256(payload).hexdigest()
    parsed = _parse(source, ext)

    directory = _files_root(root) / digest
    # 原文件不可变：只写一次，已有则校验内容一致。
    _write_original(directory, ext, payload, digest)

    text_chars = sum(len(chunk.text) for chunk in parsed.chunks)
    file_id = f"f_{digest[:16]}"
    meta: dict[str, Any] = {
        "file_id": file_id,
        "sha256": digest,
        "name": source.name,
        "ext": ext,
        "media_type": MEDIA_TYPES.get(ext, "application/octet-stream"),
        "size_bytes": len(payload),
        "uploaded_at": uploaded_at,
        "pages": parsed.pages,
        "sheets": [dict(sheet) for sheet in parsed.sheet_meta],
        "text_chars": text_chars,
        "chunks_count": len(parsed.chunks),
        "parser": parsed.parser,
        "parser_version": parsed.parser_version,
        "status": STATUS_OK,
        "warnings": list(parsed.warnings),
    }

    chunk_rows = _write_chunks(directory, file_id, parsed.chunks)
    _write_tables(directory, parsed.tables)

    status, self_warnings = _self_check(directory, meta, chunk_rows)
    meta["status"] = status
    meta["warnings"] = list(parsed.warnings) + self_warnings

    if index and chunk_rows:
        try:
            index_chunks(file_id, chunk_rows, db_path=db_path)
        except Exception as exc:  # 索引失败不应丢掉已落盘的抽取结果
            meta["warnings"].append(
                f"{EXTRACTION_WARNING_PREFIX} FTS 索引写入失败（{type(exc).__name__}: {exc}），"
                "文本检索将查不到该文件。"
            )

    _atomic_write_text(directory / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))

    doc = ExtractedDoc(
        file_id=file_id,
        name=source.name,
        pages=_pages_view(parsed.chunks),
        tables=[
            {
                "sheet": sheet["name"],
                "slug": sheet["slug"],
                "rows": sheet["rows"],
                "cols": sheet["cols"],
                "header_row": sheet["header_row"],
                "path": f"tables/{sheet['slug']}.parquet",
            }
            for sheet in meta["sheets"]
        ],
    )
    return IngestResult(
        name=source.name,
        status=meta["status"],
        warnings=list(meta["warnings"]),
        doc=doc,
        meta=meta,
    )


def _files_root(root: str | Path | None) -> Path:
    return Path(root) if root is not None else DEFAULT_FILES_DIR


# ---------------------------------------------------------------------------
# 分派与解析器
# ---------------------------------------------------------------------------


def _parse(source: Path, ext: str) -> _Parsed:
    if ext == "pdf":
        return _parse_pdf(source)
    if ext in ("xlsx", "xls"):
        return _parse_excel(source, ext)
    if ext == "csv":
        return _parse_csv(source)
    if ext == "docx":
        return _parse_docx(source)
    if ext in ("txt", "md"):
        return _parse_text(source, ext)
    raise _Rejected(STATUS_UNSUPPORTED, [f"暂不支持 .{ext} 格式。"])


def _load_pdfplumber() -> Any | None:
    """惰性导入 pdfplumber；缺失时返回 ``None`` 由调用方降级（不抛 ImportError）。"""
    try:
        import pdfplumber  # type: ignore[import-not-found]
    except Exception:
        return None
    return pdfplumber


def _is_encryption_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "password" in text or "encrypt" in text or "加密" in text


def _parse_pdf(source: Path) -> _Parsed:
    plumber = _load_pdfplumber()
    if plumber is None:
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [
                "解析 PDF 需要 pdfplumber（requirements.txt 中 pdfplumber>=0.11），"
                "当前环境未安装，无法抽取文本层；请安装后重试。"
            ],
        )
    version = str(getattr(plumber, "__version__", "unknown"))
    try:
        pdf = plumber.open(str(source))
    except Exception as exc:
        if _is_encryption_error(exc):
            raise _Rejected(
                STATUS_ENCRYPTED,
                [f"PDF 已加密（{type(exc).__name__}），v1 不支持解密；请提供无密码副本。"],
            ) from exc
        raise _Rejected(
            STATUS_UNSUPPORTED, [f"PDF 无法打开：{type(exc).__name__}: {exc}"]
        ) from exc

    parsed = _Parsed(parser="pdfplumber", parser_version=version, pages=None)
    used_slugs: set[str] = set()
    try:
        pages = list(pdf.pages)
        if len(pages) > MAX_PDF_PAGES:
            raise _Rejected(
                STATUS_UNSUPPORTED,
                [
                    f"PDF 共 {len(pages)} 页，超过上限 {MAX_PDF_PAGES} 页，未解析；"
                    "请拆分后重试。"
                ],
            )
        parsed.pages = len(pages)
        for number, page in enumerate(pages, start=1):
            text = _safe_extract_text(page)
            for block in _split_paragraphs(text):
                parsed.chunks.append(
                    _Chunk(page=number, sheet=None, kind="text", text=block)
                )
            for index, (grid, bbox) in enumerate(_page_tables(page), start=1):
                if not grid:
                    continue
                _add_sheet(
                    parsed,
                    grid,
                    f"p{number}_table_{index}",
                    used_slugs,
                    page=number,
                    bbox=bbox,
                )
    finally:
        close = getattr(pdf, "close", None)
        if callable(close):
            close()
    return parsed


def _safe_extract_text(page: Any) -> str:
    try:
        text = page.extract_text()
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


def _page_tables(page: Any) -> list[tuple[list[list[Any]], list[float] | None]]:
    """抽取一页里的表格，尽量保留 bbox（pdfplumber ``find_tables``）。"""
    found: list[tuple[list[list[Any]], list[float] | None]] = []
    finder = getattr(page, "find_tables", None)
    if callable(finder):
        try:
            tables = finder()
        except Exception:
            tables = []
        for table in tables:
            try:
                grid = table.extract()
            except Exception:
                continue
            bbox = getattr(table, "bbox", None)
            box = [float(v) for v in bbox] if bbox else None
            if isinstance(grid, list):
                found.append((grid, box))
        if found:
            return found
    extractor = getattr(page, "extract_tables", None)
    if callable(extractor):
        try:
            raw = extractor()
        except Exception:
            raw = []
        for grid in raw or []:
            if isinstance(grid, list):
                found.append((grid, None))
    return found


def _parse_excel(source: Path, ext: str) -> _Parsed:
    try:
        frames = pd.read_excel(source, sheet_name=None, header=None, dtype=object)
    except ImportError as exc:
        engine = "openpyxl>=3.1" if ext == "xlsx" else "xlrd>=2.0"
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [f"解析 .{ext} 需要 {engine}，当前环境未安装：{exc}"],
        ) from exc
    except Exception as exc:
        if _is_encryption_error(exc):
            raise _Rejected(
                STATUS_ENCRYPTED,
                [f"工作簿已加密（{type(exc).__name__}），v1 不支持解密。"],
            ) from exc
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [f"Excel 无法读取：{type(exc).__name__}: {exc}"],
        ) from exc

    if ext == "xlsx":
        parser, version = "openpyxl", _package_version("openpyxl")
    else:
        parser, version = "xlrd", _package_version("xlrd")
    parsed = _Parsed(parser=parser, parser_version=version, pages=None)
    used_slugs: set[str] = set()
    for name, frame in frames.items():
        grid = _grid_from_frame(frame)
        if not grid:
            parsed.warnings.append(f"工作表 {name} 为空。")
            continue
        _add_sheet(parsed, grid, str(name), used_slugs)
    return parsed


def _parse_csv(source: Path) -> _Parsed:
    data = source.read_bytes()
    text, encoding, decode_warnings = _decode_bytes(data)
    delimiter = _detect_delimiter(text[:8192])
    rows: list[list[Any]] = []
    try:
        for row in csv.reader(io.StringIO(text), delimiter=delimiter):
            rows.append([_normalize_cell(cell) for cell in row])
    except csv.Error as exc:
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [f"CSV 无法解析（分隔符 {delimiter!r}）：{exc}"],
        ) from exc

    parsed = _Parsed(
        parser="csv", parser_version=platform.python_version(), pages=None
    )
    parsed.warnings.extend(decode_warnings)
    used_slugs: set[str] = set()
    if not rows:
        parsed.warnings.append("CSV 没有可解析的行。")
        return parsed
    _add_sheet(parsed, rows, source.stem or "csv", used_slugs)
    return parsed


def _parse_text(source: Path, ext: str) -> _Parsed:
    text, _encoding, warnings = _decode_bytes(source.read_bytes())
    parsed = _Parsed(
        parser="plain-text", parser_version=platform.python_version(), pages=None
    )
    parsed.warnings.extend(warnings)
    for block in _split_paragraphs(text):
        parsed.chunks.append(_Chunk(page=None, sheet=None, kind="text", text=block))
    return parsed


def _parse_docx(source: Path) -> _Parsed:
    body, parser, version = _docx_body(source)
    parsed = _Parsed(parser=parser, parser_version=version, pages=None)
    used_slugs: set[str] = set()
    page = 1
    table_index = 0
    for element in list(body):
        tag = element.tag
        if tag == f"{_W_NS}p":
            text = "".join(node.text or "" for node in element.iter(f"{_W_NS}t")).strip()
            breaks = _docx_page_breaks(element)
            if text:
                for block in _split_paragraphs(text):
                    parsed.chunks.append(
                        _Chunk(page=page, sheet=None, kind="text", text=block)
                    )
            page += breaks
        elif tag == f"{_W_NS}tbl":
            grid = _docx_table_grid(element)
            if grid:
                table_index += 1
                _add_sheet(
                    parsed, grid, f"table_{table_index}", used_slugs, page=page
                )
    parsed.pages = page
    return parsed


def _docx_body(source: Path) -> tuple[Any, str, str]:
    """返回 (body 元素, parser, version)；优先 python-docx，缺失时用 zipfile 兜底。"""
    DocxDocument: Any
    try:
        from docx import Document as _DocxDocument
    except Exception:
        DocxDocument = None
    else:
        DocxDocument = _DocxDocument

    if DocxDocument is not None:
        try:
            document = DocxDocument(str(source))
        except Exception as exc:
            if _is_encryption_error(exc):
                raise _Rejected(
                    STATUS_ENCRYPTED,
                    [f"DOCX 已加密（{type(exc).__name__}），v1 不支持解密。"],
                ) from exc
            raise _Rejected(
                STATUS_UNSUPPORTED,
                [f"DOCX 无法打开：{type(exc).__name__}: {exc}"],
            ) from exc
        return document.element.body, "python-docx", _package_version("python-docx")

    try:
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                raise _Rejected(
                    STATUS_UNSUPPORTED,
                    ["不是有效的 DOCX（缺少 word/document.xml）。"],
                )
            root = ET.fromstring(archive.read("word/document.xml"))
    except zipfile.BadZipFile as exc:
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [f"DOCX 不是有效的 OOXML 包：{exc}"],
        ) from exc
    body = root.find(f"{_W_NS}body")
    if body is None:
        raise _Rejected(STATUS_UNSUPPORTED, ["DOCX 缺少正文节点 w:body。"])
    return body, "zipfile-ooxml", platform.python_version()


def _docx_page_breaks(element: Any) -> int:
    count = 0
    for br in element.iter(f"{_W_NS}br"):
        if br.get(f"{_W_NS}type") == "page":
            count += 1
    for _ in element.iter(f"{_W_NS}lastRenderedPageBreak"):
        count += 1
    return count


def _docx_table_grid(table: Any) -> list[list[str | None]]:
    grid: list[list[str | None]] = []
    for row in table.iter(f"{_W_NS}tr"):
        cells: list[str | None] = []
        for cell in row.iter(f"{_W_NS}tc"):
            text = "".join(node.text or "" for node in cell.iter(f"{_W_NS}t")).strip()
            cells.append(text or None)
        grid.append(cells)
    return grid


# ---------------------------------------------------------------------------
# 工作表 / 表格归一化
# ---------------------------------------------------------------------------


def _add_sheet(
    parsed: _Parsed,
    grid: Sequence[Sequence[Any]],
    name: str,
    used_slugs: set[str],
    *,
    page: int | None = None,
    bbox: list[float] | None = None,
) -> int:
    """把一张表写进 ``parsed``（parquet + chunks + meta），返回数据行数。"""
    cleaned = _trim_grid(grid)
    if not cleaned:
        parsed.warnings.append(f"{name} 没有可抽取的单元格。")
        return 0
    header_row = _detect_header_row(cleaned)
    if header_row is not None:
        header_cells = cleaned[header_row]
        data_rows = cleaned[header_row + 1 :]
        columns = _columns_from(header_cells)
        header_label = header_row + 1
    else:
        columns = [f"col_{i}" for i in range(1, max(len(r) for r in cleaned) + 1)]
        data_rows = list(cleaned)
        header_label = None

    padded = [_pad_row(row, len(columns)) for row in data_rows]
    frame = pd.DataFrame(
        {column: _coerce_column([row[i] for row in padded]) for i, column in enumerate(columns)}
    )
    slug = _sheet_slug(name, used_slugs)
    parsed.tables[slug] = frame
    parsed.sheet_meta.append(
        {
            "name": name,
            "slug": slug,
            "rows": int(frame.shape[0]),
            "cols": int(frame.shape[1]),
            "header_row": header_label,
        }
    )

    header_text = "、".join(str(c) for c in columns)
    row_box: list[float] | None = (
        None if header_label is None else [header_label, 1, header_label, len(columns)]
    )
    parsed.chunks.append(
        _Chunk(
            page=page,
            sheet=name,
            kind="text",
            text=f"工作表 {name}：{frame.shape[0]} 行 × {frame.shape[1]} 列；列：{header_text}",
            bbox=bbox if bbox is not None else row_box,
        )
    )

    first_data_row = (header_label or 0) + 1
    for offset, row in enumerate(padded[:MAX_SHEET_CHUNK_ROWS]):
        text = " | ".join(str(value) for value in row if value is not None and str(value).strip())
        if not text:
            continue
        row_number = first_data_row + offset
        parsed.chunks.append(
            _Chunk(
                page=page,
                sheet=name,
                kind="table",
                text=text,
                bbox=[row_number, 1, row_number, len(columns)],
                cells=sum(1 for value in row if value is not None and str(value).strip()),
            )
        )
    if len(padded) > MAX_SHEET_CHUNK_ROWS:
        parsed.warnings.append(
            f"工作表 {name} 共 {len(padded)} 行，仅将前 {MAX_SHEET_CHUNK_ROWS} 行写入文本索引；"
            "parquet 保留全量，可用表格查询读取剩余行。"
        )
    return int(frame.shape[0])


def _grid_from_frame(frame: pd.DataFrame) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for values in frame.itertuples(index=False, name=None):
        rows.append([_normalize_cell(value) for value in values])
    return rows


def _normalize_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is pd.NaT:  # pragma: no cover - 防御性分支
        return None
    return value


def _trim_grid(grid: Sequence[Sequence[Any]]) -> list[list[Any]]:
    rows = [list(row) for row in grid]
    while rows and all(_is_blank(cell) for cell in rows[-1]):
        rows.pop()
    if not rows:
        return []
    width = 0
    for row in rows:
        for index in range(len(row), 0, -1):
            if not _is_blank(row[index - 1]):
                width = max(width, index)
                break
    return [_pad_row(row, width) for row in rows]


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _pad_row(row: Sequence[Any], width: int) -> list[Any]:
    values = list(row[:width])
    values.extend([None] * (width - len(values)))
    return values


def _detect_header_row(grid: Sequence[Sequence[Any]]) -> int | None:
    best: int | None = None
    best_score = 0.0
    for index, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        cells = [cell for cell in row if not _is_blank(cell)]
        if len(cells) < 2:
            continue
        fill = len(cells) / max(1, len(row))
        textual = sum(1 for cell in cells if not _is_numeric_like(cell))
        if textual < max(1, math.ceil(len(cells) / 2)) or fill < 0.5:
            continue
        score = textual + fill
        if score > best_score:
            best = index
            best_score = score
    return best


def _is_numeric_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.replace(",", ""))
        except ValueError:
            return False
        return True
    return False


def _columns_from(header: Sequence[Any]) -> list[str]:
    columns: list[str] = []
    seen: dict[str, int] = {}
    for index, cell in enumerate(header, start=1):
        base = str(cell).strip() if not _is_blank(cell) else f"col_{index}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        columns.append(base if count == 1 else f"{base}_{count}")
    return columns


def _coerce_column(values: Sequence[Any]) -> pd.Series:
    series = pd.Series(list(values), dtype=object)
    non_null = series.dropna()
    if non_null.empty:
        return series
    numeric = pd.to_numeric(non_null, errors="coerce")
    if numeric.notna().all():
        converted = pd.to_numeric(series, errors="coerce")
        return converted.astype(float)
    return series


def _sheet_slug(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:40] or "sheet"
    slug = base
    counter = 2
    while slug in used:
        slug = f"{base}_{counter}"
        counter += 1
    used.add(slug)
    return slug


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def _write_original(directory: Path, ext: str, payload: bytes, digest: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"original.{ext}"
    others = [p.name for p in directory.glob("original.*") if p.name != target.name]
    if others:
        # 冻结布局只允许一件 original.<ext>：同字节不同扩展名会让布局出现第二件产物。
        raise _Rejected(
            STATUS_UNSUPPORTED,
            [
                f"同一内容的文件已按 {others[0]} 导入过；内容寻址目录 "
                f"{directory.name[:12]}… 只保留一份原始文件，请勿用不同扩展名重复导入。"
            ],
        )
    if target.exists():
        existing = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing != digest:  # 内容寻址目录被污染
            raise _Rejected(
                STATUS_UNSUPPORTED,
                [
                    f"内容寻址目录 {directory.name} 内的 original.{ext} 与 sha256 不一致，"
                    "拒绝覆盖；请人工检查该目录。"
                ],
            )
        return
    _atomic_write_bytes(target, payload)


def _write_chunks(directory: Path, file_id: str, chunks: Sequence[_Chunk]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block, chunk in enumerate(chunks):
        rows.append(
            {
                "file_id": file_id,
                "page": chunk.page,
                "sheet": chunk.sheet,
                "block": block,
                "kind": chunk.kind,
                "text": chunk.text,
                "bbox": chunk.bbox,
                "cells": chunk.cells,
            }
        )
    line = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    _atomic_write_text(directory / "extracted.jsonl", line)
    return rows


def _write_tables(directory: Path, tables: dict[str, pd.DataFrame]) -> None:
    target = directory / "tables"
    target.mkdir(parents=True, exist_ok=True)
    for slug, frame in tables.items():
        path = target / f"{slug}.parquet"
        frame.to_parquet(path, index=False)


def _pages_view(chunks: Sequence[_Chunk]) -> list[dict[str, Any]]:
    """把 chunk 折叠成 ``ExtractedDoc.pages``（按页，其次按 sheet）。"""
    grouped: dict[tuple[int | None, str | None], list[str]] = {}
    for chunk in chunks:
        grouped.setdefault((chunk.page, chunk.sheet), []).append(chunk.text)
    return [
        {"page": page, "sheet": sheet, "text": "\n".join(texts)}
        for (page, sheet), texts in grouped.items()
    ]


# ---------------------------------------------------------------------------
# 自检（C3）
# ---------------------------------------------------------------------------


def _self_check(
    directory: Path, meta: dict[str, Any], chunks: Sequence[dict[str, Any]]
) -> tuple[str, list[str]]:
    """回读落盘产物做抽样比对；返回 (status, warnings)。"""
    warnings: list[str] = []
    prefix = EXTRACTION_WARNING_PREFIX
    status = str(meta["status"])

    rows: list[dict[str, Any]] = []
    try:
        with (directory / "extracted.jsonl").open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        return STATUS_EMPTY_TEXT, [f"{prefix} extracted.jsonl 回读失败：{exc}"]

    if len(rows) != meta["chunks_count"]:
        warnings.append(
            f"{prefix} 回读块数 {len(rows)} 与 meta.chunks_count {meta['chunks_count']} 不一致。"
        )
    if len(rows) != len(chunks):
        warnings.append(f"{prefix} 回读块数 {len(rows)} 与写入块数 {len(chunks)} 不一致。")

    text_chars = sum(len(row.get("text") or "") for row in rows)
    if text_chars != meta["text_chars"]:
        warnings.append(
            f"{prefix} 回读 text_chars {text_chars} 与 meta.text_chars {meta['text_chars']} 不一致。"
        )

    all_text = "".join(row.get("text") or "" for row in rows)
    ratio = _garbled_ratio(all_text)
    if ratio > 0.02:
        warnings.append(
            f"{prefix} 抽取文本中 {ratio:.1%} 为替换字符/控制字符，疑似乱码或编码判断错误。"
        )

    pages = meta["pages"]
    if pages:
        covered = {
            int(row["page"])
            for row in rows
            if row.get("page") is not None and (row.get("text") or "").strip()
        }
        missing = [page for page in range(1, int(pages) + 1) if page not in covered]
        if len(missing) == int(pages):
            warnings.append(
                f"{prefix} 共 {pages} 页，一页文本也没抽到；"
                "该文件很可能是扫描件（无文本层），v1 不做 OCR。"
            )
            status = STATUS_SUSPECTED_SCAN if meta["ext"] == "pdf" else STATUS_EMPTY_TEXT
        elif missing:
            shown = "、".join(str(page) for page in missing[:10])
            suffix = "…" if len(missing) > 10 else ""
            warnings.append(
                f"{prefix} 第 {shown}{suffix} 页未抽到文本（{len(missing)}/{pages} 页），"
                "其余页正常。"
            )
            if len(missing) / int(pages) >= 0.5:
                status = STATUS_SUSPECTED_SCAN if meta["ext"] == "pdf" else STATUS_EMPTY_TEXT

    for sheet in meta["sheets"]:
        path = directory / "tables" / f"{sheet['slug']}.parquet"
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:
            warnings.append(f"{prefix} 工作表 {sheet['name']} 的 parquet 回读失败：{exc}")
            continue
        if list(frame.shape) != [sheet["rows"], sheet["cols"]]:
            warnings.append(
                f"{prefix} 工作表 {sheet['name']} 回读形状 {list(frame.shape)} "
                f"与 meta 记录 {[sheet['rows'], sheet['cols']]} 不一致。"
            )
        if sheet["rows"] == 0:
            warnings.append(f"{prefix} 工作表 {sheet['name']} 抽到 0 行数据。")

    if not rows and not meta["sheets"]:
        if status == STATUS_OK:
            status = STATUS_EMPTY_TEXT
        if not any(prefix in item for item in warnings):
            warnings.append(f"{prefix} 没有抽到任何文本或表格，文件可能是空文档。")
    elif text_chars == 0 and not warnings:
        status = STATUS_EMPTY_TEXT
        warnings.append(f"{prefix} 抽取文本为 0 字符，仅有表格结构。")

    return status, warnings


def _garbled_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(
        1
        for char in text
        if char == "\ufffd" or (ord(char) < 32 and char not in "\n\t\r")
    )
    return bad / len(text)


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------


def _split_paragraphs(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not parts and text.strip():
        parts = [text.strip()]
    blocks: list[str] = []
    for part in parts:
        remaining = part
        while len(remaining) > MAX_CHUNK_CHARS:
            window = remaining[:MAX_CHUNK_CHARS]
            cut = window.rfind("\n")
            if cut < MAX_CHUNK_CHARS // 2:
                cut = len(window)
            blocks.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            blocks.append(remaining)
    return [block for block in blocks if block]


def _decode_bytes(data: bytes) -> tuple[str, str, list[str]]:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(encoding), encoding, []
        except UnicodeDecodeError:
            continue
    return (
        data.decode("latin-1"),
        "latin-1",
        [
            f"{EXTRACTION_WARNING_PREFIX} 无法判定编码（尝试 utf-8/gb18030/gbk 均失败），"
            "已按 latin-1 解码，中文可能乱码。"
        ],
    )


def _detect_delimiter(sample: str) -> str:
    if not sample.strip():
        return ","
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {delimiter: sample.count(delimiter) for delimiter in ",;\t|"}
        best = max(counts, key=lambda key: counts[key])
        return best if counts[best] else ","


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # pragma: no cover - 元数据缺失时降级
        return "unknown"


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    tmp.write_bytes(payload)
    os.replace(tmp, target)


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


# ---------------------------------------------------------------------------
# 索引与读取（写入侧；检索契约见 plan-20261107-004）
# ---------------------------------------------------------------------------


def ensure_index_schema(conn: sqlite3.Connection) -> None:
    """建 ``file_chunks`` / ``file_chunks_fts`` 与同步触发器（幂等）。"""
    conn.executescript(FILE_CHUNKS_DDL)
    conn.executescript(FILE_CHUNKS_TRIGGER_DDL)


def index_chunks(
    file_id: str,
    rows: Sequence[dict[str, Any]],
    *,
    db_path: str | Path | None = None,
) -> int:
    """把 chunk 写入 FTS5 索引（同库 ``data/astock.db``），返回写入条数。"""
    if not rows:
        return 0
    path = Path(db_path) if db_path is not None else _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        ensure_index_schema(conn)
        conn.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
        conn.executemany(
            "INSERT INTO file_chunks(file_id, page, sheet, block, text) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    file_id,
                    row.get("page"),
                    row.get("sheet"),
                    int(row.get("block", 0)),
                    row.get("text") or "",
                )
                for row in rows
                if (row.get("text") or "").strip()
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return len([row for row in rows if (row.get("text") or "").strip()])


def _default_db_path() -> Path:
    from .. import ledger  # 延迟导入：避免模块级耦合

    return Path(ledger.DB_PATH)


# ---------------------------------------------------------------------------
# 读取辅助（按 file_id 或完整 sha256 定位）
# ---------------------------------------------------------------------------


def file_dir(file_id: str, *, root: str | Path | None = None) -> Path:
    """按 ``file_id``（``f_<sha16>``）或完整 sha256 找到内容寻址目录。"""
    base = _files_root(root)
    candidate = file_id.strip()
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        directory = base / candidate
        if directory.is_dir():
            return directory
        raise FileNotFoundError(f"找不到文件目录：{candidate}")
    if not base.is_dir():
        raise FileNotFoundError(f"文件根目录不存在：{base}")
    for meta_path in sorted(base.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("file_id") == candidate:
            return meta_path.parent
    raise FileNotFoundError(f"找不到 file_id={candidate}")


def load_meta(file_id: str, *, root: str | Path | None = None) -> dict[str, Any]:
    """读取并校验 ``meta.json``（15 个冻结键一个不少）。"""
    path = file_dir(file_id, root=root) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    missing = [key for key in META_FIELDS if key not in meta]
    extra = [key for key in meta if key not in META_FIELDS]
    if missing or extra:
        raise ValueError(f"meta.json 字段不符合冻结规范：缺 {missing}，多 {extra}")
    return meta


def iter_chunks(file_id: str, *, root: str | Path | None = None) -> Iterator[dict[str, Any]]:
    """逐行回读 ``extracted.jsonl``。"""
    path = file_dir(file_id, root=root) / "extracted.jsonl"
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_table(
    file_id: str, sheet_slug: str, *, root: str | Path | None = None
) -> pd.DataFrame:
    """读取某张表的 parquet（只读）。"""
    path = file_dir(file_id, root=root) / "tables" / f"{sheet_slug}.parquet"
    return pd.read_parquet(path)
