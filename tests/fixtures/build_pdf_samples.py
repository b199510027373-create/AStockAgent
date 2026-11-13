"""生成文件导入解析用的真实 PDF 夹具（需要 reportlab / pypdf，仅本地一次性运行）。

用法：
    .\\.venv\\Scripts\\python.exe tests\\fixtures\\build_pdf_samples.py

产物（已提交到仓库，CI 不重新生成、也不依赖 reportlab / pypdf）：
- pdf_text_tables.pdf：两页，第 1 页含标题、正文与一张 3x3 表格（报表线框），
  第 2 页含另一段正文与第二张表格；用于验证文本层解析、页码保留与表格落盘。
- pdf_scan_like.pdf：只有一张位图、没有任何文本层，用于验证扫描件识别。
- pdf_encrypted.pdf：使用用户口令加密的 PDF，用于验证 encrypted 状态提示。
- pdf_cjk.pdf：使用 reportlab 内置 CID 字体（STSong-Light）排版的中文单页报表，
  用于验证中文文本层抽取（A 股公告/财报的常见形态）。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FIXTURES = Path(__file__).resolve().parent


def _build_text_tables(path: Path) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4, title="Quarterly Report", author="AStockAgent")
    table_style = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.8, (0, 0, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), (0.9, 0.9, 0.9)),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    )
    flow = [
        Paragraph("Quarterly Report 2026 Q3", styles["Title"]),
        Spacer(1, 12),
        Paragraph(
            "Revenue grew steadily across all business segments this quarter. "
            "The storage layer keeps the original bytes immutable and records every "
            "chunk with its page number.",
            styles["BodyText"],
        ),
        Spacer(1, 12),
        Table(
            [
                ["Item", "Amount", "YoY"],
                ["Revenue", "1250000", "12.5%"],
                ["Net profit", "230000", "8.1%"],
            ],
            style=table_style,
        ),
        PageBreak(),
        Paragraph("Segment Detail", styles["Heading1"]),
        Spacer(1, 8),
        Paragraph(
            "The cloud segment contributed the largest share, followed by retail. "
            "All figures below are unaudited and stated in CNY.",
            styles["BodyText"],
        ),
        Spacer(1, 12),
        Table(
            [
                ["Segment", "Revenue", "Margin"],
                ["Cloud", "700000", "31.2%"],
                ["Retail", "550000", "22.8%"],
            ],
            style=table_style,
        ),
    ]
    doc.build(flow)


def _build_scan_like(path: Path) -> None:
    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 590, 190), outline="black", width=2)
    for y in range(40, 180, 30):
        draw.line((30, y, 560, y), fill="black", width=3)
    image_path = FIXTURES / ".scan_source.png"
    image.save(image_path)
    try:
        doc = SimpleDocTemplate(str(path), pagesize=A4)
        from reportlab.platypus import Image as RLImage

        doc.build([RLImage(str(image_path), width=400, height=140)])
    finally:
        image_path.unlink(missing_ok=True)


def _build_encrypted(source: Path, path: Path) -> None:
    reader = PdfReader(str(source))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("secret")
    with path.open("wb") as handle:
        writer.write(handle)


def _build_cjk(path: Path) -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    doc = canvas.Canvas(str(path), pagesize=A4)
    doc.setFont("STSong-Light", 14)
    doc.drawString(60, 780, "贵州茅台 2026 年三季报")
    doc.drawString(60, 750, "营业收入 125.6 亿元，同比增长 12.5%")
    doc.showPage()
    doc.save()


def main() -> None:
    text_tables = FIXTURES / "pdf_text_tables.pdf"
    scan_like = FIXTURES / "pdf_scan_like.pdf"
    encrypted = FIXTURES / "pdf_encrypted.pdf"
    cjk = FIXTURES / "pdf_cjk.pdf"
    _build_text_tables(text_tables)
    _build_scan_like(scan_like)
    _build_encrypted(text_tables, encrypted)
    _build_cjk(cjk)
    for path in (text_tables, scan_like, encrypted, cjk):
        print(f"{path.name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
