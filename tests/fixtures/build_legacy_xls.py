"""生成 ``legacy_two_sheets.xls``：真实 BIFF8 工作簿（两个 sheet），用于 XLS 回归。

一次性生成、提交到仓库，CI 不需要 xlwt；若需重建：
``.venv\\Scripts\\python.exe -m pip install xlwt`` 后运行本脚本。

内容刻意包含前导零代码列（"000001"），回归 ``_coerce_column`` 不得把编号改成数值。
"""

from __future__ import annotations

from pathlib import Path

import xlwt

TARGET = Path(__file__).with_name("legacy_two_sheets.xls")

SHEETS: dict[str, list[list[object]]] = {
    "利润表": [
        ["项目", "本期", "上期"],
        ["营收", 100, 90],
        ["净利", 20, 18],
    ],
    "现金流": [
        ["代码", "科目", "金额"],
        ["000001", "经营", 30],
        ["600000", "投资", 12.5],
    ],
}


def main() -> None:
    book = xlwt.Workbook()
    for name, rows in SHEETS.items():
        sheet = book.add_sheet(name)
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                sheet.write(row_index, col_index, value)
    book.save(str(TARGET))
    print(f"wrote {TARGET} ({TARGET.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
