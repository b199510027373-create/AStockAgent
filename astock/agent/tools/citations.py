"""文件引用的统一格式与强制校验（ADR-015 C4 / 契约 AGENT-04）。

约定：

- 引用一律以 ``《文件名》`` 开头，后接页码或工作表/表格定位；
- 表格引用带 1 基闭区间的单元格范围（Excel/CSV/DOCX），PDF 表格只带页码；
- 回答若没有任何可核对引用，一律落到 :data:`CITATION_NOT_FOUND`
  「未在文件中找到」，不允许用无出处的文字冒充文件结论。

``bbox`` 的两类语义（与 ``astock/agent/files.py`` 落盘一致）：
PDF 页面坐标 ``[x0, y0, x1, y1]``（points）与 Excel/CSV/DOCX 源表格坐标
``[row_start, col_start, row_end, col_end]``（1 基闭区间）不得混用。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "BBOX_PAGE",
    "BBOX_TABLE",
    "CITATION_NOT_FOUND",
    "PDF_EXTS",
    "TABLE_BBOX_EXTS",
    "bbox_kind",
    "cell_range",
    "citation_for_chunk",
    "citation_for_sheet",
    "column_label",
    "finalize_file_answer",
    "format_citation",
    "has_file_citation",
]

#: 没有命中任何文件内容时的唯一回答（验收标准，不得改写）。
CITATION_NOT_FOUND = "未在文件中找到"

#: PDF 表格 bbox：页面坐标 ``[x0, y0, x1, y1]``（points）。
BBOX_PAGE = "page"

#: Excel/CSV/DOCX 表格 bbox：源表格坐标 ``[row_start, col_start, row_end, col_end]``。
BBOX_TABLE = "table"

PDF_EXTS = frozenset({"pdf"})
TABLE_BBOX_EXTS = frozenset({"xlsx", "xls", "csv", "docx"})

#: 能识别 ``《文件名》第 3 页`` / ``《文件名》工作表「利润表」A2:D2`` /
#: ``《文件名》第 2 页 表格「table_1」第 5 行`` 三种引用写法。
_CITATION_RE = re.compile(
    r"《[^》]+》(?:"
    r"\s*第\s*\d+\s*页"
    r"|\s*(?:工作表|表格)「[^」]+」(?:\s*第\s*\d+\s*行|\s*[A-Z]{1,3}\d+(?::[A-Z]{1,3}\d+)?)?"
    r")"
)


def bbox_kind(ext: str | None) -> str | None:
    """按扩展名判断 bbox 语义；``txt``/``md`` 没有表格坐标。"""
    key = (ext or "").strip().lower().lstrip(".")
    if key in PDF_EXTS:
        return BBOX_PAGE
    if key in TABLE_BBOX_EXTS:
        return BBOX_TABLE
    return None


def column_label(index: int) -> str:
    """1 基列号转 Excel 列名（1 → ``A``，27 → ``AA``）。"""
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ValueError(f"列号必须是 >= 1 的整数：{index!r}")
    label = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def cell_range(row_start: int, col_start: int, row_end: int, col_end: int) -> str:
    """1 基闭区间的单元格范围，如 ``A2:D2``。"""
    values = (row_start, col_start, row_end, col_end)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values):
        raise ValueError(f"单元格范围必须全部是 >= 1 的整数：{values!r}")
    if row_start > row_end or col_start > col_end:
        raise ValueError(f"单元格范围起点不能晚于终点：{values!r}")
    start = f"{column_label(col_start)}{row_start}"
    end = f"{column_label(col_end)}{row_end}"
    return f"{start}:{end}"


def format_citation(
    name: str,
    *,
    page: int | None = None,
    sheet: str | None = None,
    sheet_word: str = "工作表",
    row: int | None = None,
    cell: str | None = None,
) -> str:
    """拼出 ``《文件名》第 3 页 工作表「利润表」A2:D2`` 形式的引用。"""
    label = str(name or "").strip()
    if not label:
        raise ValueError("引用必须包含文件名")
    if page is None and not sheet and row is None:
        raise ValueError("引用至少需要页码、工作表或行号之一")
    text = f"《{label}》"
    if page is not None:
        text += f"第 {int(page)} 页"
    if sheet:
        text += f"{'' if page is None else ' '}{sheet_word}「{sheet}」"
    if row is not None:
        text += f"第 {int(row)} 行" if sheet else f" 第 {int(row)} 行"
    return text if not cell else f"{text}{cell}"


def _display_name(meta: Mapping[str, Any]) -> str:
    return str(meta.get("name") or meta.get("file_id") or "未知文件")


def citation_for_chunk(meta: Mapping[str, Any], chunk: Mapping[str, Any]) -> str:
    """一条 chunk 的引用：页码（PDF/DOCX）或工作表 + 单元格范围（表格）。"""
    name = _display_name(meta)
    ext = str(meta.get("ext") or "").lower()
    page = chunk.get("page")
    page_no = int(page) if isinstance(page, int) and page > 0 else None
    sheet = str(chunk["sheet"]) if chunk.get("sheet") else None
    bbox = chunk.get("bbox")

    kind = bbox_kind(ext)
    if kind == BBOX_PAGE:
        return format_citation(name, page=page_no or 1)

    if sheet:
        if ext == "docx":
            row = int(bbox[0]) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None
            return format_citation(
                name, page=page_no, sheet=sheet, sheet_word="表格", row=row
            )
        cell = None
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            cell = cell_range(bbox[0], bbox[1], bbox[2], bbox[3])
        return format_citation(name, sheet=sheet, cell=cell)

    if page_no:
        return format_citation(name, page=page_no)
    return format_citation(name, page=1)


def citation_for_sheet(meta: Mapping[str, Any], sheet: Mapping[str, Any]) -> str:
    """一整张表的引用：``《文件》工作表「名」A1:D101``（DOCX 用表格 + 行范围）。"""
    name = _display_name(meta)
    ext = str(meta.get("ext") or "").lower()
    sheet_name = str(sheet.get("name") or sheet.get("slug") or "")
    rows = int(sheet.get("rows") or 0)
    cols = int(sheet.get("cols") or 0)
    header = sheet.get("header_row")

    if ext in PDF_EXTS:
        match = re.match(r"p(\d+)_table_", sheet_name)
        page = int(match.group(1)) if match else None
        return format_citation(name, page=page, sheet=sheet_name, sheet_word="表格")
    if ext == "docx":
        cell = cell_range(1, 1, max(rows, 1), max(cols, 1)) if rows and cols else None
        return format_citation(name, sheet=sheet_name, sheet_word="表格", cell=cell)
    if rows and cols:
        row_start = int(header) if isinstance(header, int) and header >= 1 else 1
        row_end = (int(header) + rows) if isinstance(header, int) and header >= 1 else rows
        return format_citation(name, sheet=sheet_name, cell=cell_range(row_start, 1, row_end, cols))
    return format_citation(name, sheet=sheet_name)


def has_file_citation(text: str | None) -> bool:
    """回答里是否带可核对的「文件名 + 页码/工作表」引用。"""
    return bool(text) and bool(_CITATION_RE.search(str(text)))


def _normalize_citation(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def extract_citations(text: str | None) -> list[str]:
    """抽出一段文字里所有「《文件名》…」引用（去重保序）。"""
    return list(dict.fromkeys(match.group(0) for match in _CITATION_RE.finditer(str(text or ""))))


def _grounded(found: str, allowed: list[str]) -> bool:
    """回答中的引用必须是工具结果里出现过的定位（允许省略工作表/行号后缀）。"""
    needle = _normalize_citation(found)
    return any(needle and needle in _normalize_citation(item) for item in allowed)


def finalize_file_answer(
    answer: str | None, citations: Iterable[str] | None = None
) -> str:
    """把文件结论收敛成带引用的回答；只保留工具结果里真实存在的引用。

    - 没有任何工具引用，或回答正文没有引用：返回「未在文件中找到」；
    - 回答里的引用都能在 *citations* 中找到：原样返回；
    - 回答出现白名单外的引用（如模型编造的《虚构.pdf》）：丢弃正文，
      只用可信引用重建「引用：…」。
    """
    cites = list(dict.fromkeys(str(item).strip() for item in (citations or []) if str(item).strip()))
    text = str(answer or "").strip()
    if not text or not cites:
        return CITATION_NOT_FOUND
    found = extract_citations(text)
    if not found:
        return CITATION_NOT_FOUND
    if all(_grounded(item, cites) for item in found):
        return text
    # 出现工具结果里没有的引用（模型编造）：丢弃正文，只用可信引用重建。
    return f"引用：{'；'.join(cites)}"
