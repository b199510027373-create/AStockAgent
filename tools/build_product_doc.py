from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

OUTPUT = Path("deliverables/AStockAgent_产品文档_v1.0.docx")
NAVY = "1F3A5F"
BLUE = "2F5597"
TEAL = "2A9D8F"
ORANGE = "E76F51"
GRAY = "5F6B7A"
LIGHT = "F5F8FC"
LIGHT_BLUE = "EEF3FA"
LIGHT_TEAL = "E8F6F3"
LIGHT_ORANGE = "FDF0EC"
WHITE = "FFFFFF"
FONT = "Microsoft YaHei"
MONO = "Consolas"


def set_east_asia(run, name=FONT):
    run.font.name = name
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.rFonts
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), name)


def style_font(style, name=FONT, size=None, bold=None, color=None):
    style.font.name = name
    if size is not None:
        style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    if color is not None:
        style.font.color.rgb = RGBColor.from_string(color)
    rPr = style.element.get_or_add_rPr()
    rFonts = rPr.rFonts
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), name)


def shade(cell, hex_color):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def set_cell_margins(cell, top=60, bottom=60, left=100, right=100):
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = tcPr.first_child_found_in("w:tcMar")
    if tcMar is None:
        tcMar = OxmlElement("w:tcMar")
        tcPr.append(tcMar)
    for tag, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = tcMar.find(qn(f"w:{tag}"))
        if node is None:
            node = OxmlElement(f"w:{tag}")
            tcMar.append(node)
        node.set(qn("w:w"), str(val))
        node.set(qn("w:type"), "dxa")


def set_table_borders(table, color="D9E2EC", size=4):
    tblPr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(size))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tblPr.append(borders)


def repeat_header(table):
    trPr = table.rows[0]._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    trPr.append(header)


def add_run(paragraph, text, size=10.5, bold=False, color="202124", mono=False):
    run = paragraph.add_run(text)
    set_east_asia(run, MONO if mono else FONT)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)
    return run


def p(text="", size=10.5, bold=False, color="202124", align=None, space_after=5, indent=False):
    paragraph = doc.add_paragraph()
    if align is not None:
        paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(space_after)
    paragraph.paragraph_format.line_spacing = 1.35
    if indent:
        paragraph.paragraph_format.first_line_indent = Cm(0.74)
    if text:
        add_run(paragraph, text, size=size, bold=bold, color=color)
    return paragraph


def h1(text, page_break=True):
    if page_break:
        doc.add_page_break()
    heading = doc.add_heading(level=1)
    heading.paragraph_format.space_before = Pt(4)
    heading.paragraph_format.space_after = Pt(10)
    heading.paragraph_format.keep_with_next = True
    add_run(heading, text, size=17, bold=True, color=NAVY)


def h2(text):
    heading = doc.add_heading(level=2)
    heading.paragraph_format.space_before = Pt(10)
    heading.paragraph_format.space_after = Pt(5)
    heading.paragraph_format.keep_with_next = True
    add_run(heading, text, size=13, bold=True, color=BLUE)


def h3(text):
    heading = doc.add_heading(level=3)
    heading.paragraph_format.space_before = Pt(7)
    heading.paragraph_format.space_after = Pt(3)
    heading.paragraph_format.keep_with_next = True
    add_run(heading, text, size=11.5, bold=True, color="333333")


def bullets(items, size=10.5):
    for item in items:
        paragraph = doc.add_paragraph(style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.line_spacing = 1.3
        add_run(paragraph, item, size=size)


def table(headers, rows, widths, font_size=8.8, header_fill=NAVY, zebra=True):
    table_obj = doc.add_table(rows=1, cols=len(headers))
    table_obj.alignment = WD_TABLE_ALIGNMENT.CENTER
    table_obj.autofit = False
    table_obj.allow_autofit = False
    table_obj.style = "Table Grid"
    set_table_borders(table_obj)
    hdr = table_obj.rows[0]
    repeat_header(table_obj)
    for i, head in enumerate(headers):
        cell = hdr.cells[i]
        cell.width = Cm(widths[i])
        shade(cell, header_fill)
        set_cell_margins(cell)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        para = cell.paragraphs[0]
        para.alignment = WD_ALIGN_PARAGRAPH.LEFT
        para.paragraph_format.space_after = Pt(0)
        add_run(para, head, size=font_size, bold=True, color=WHITE)
    for r_idx, row in enumerate(rows):
        cells = table_obj.add_row().cells
        trPr = table_obj.rows[-1]._tr.get_or_add_trPr()
        cant = OxmlElement("w:cantSplit")
        cant.set(qn("w:val"), "true")
        trPr.append(cant)
        for i, value in enumerate(row):
            cell = cells[i]
            cell.width = Cm(widths[i])
            set_cell_margins(cell)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            if zebra and r_idx % 2 == 1:
                shade(cell, LIGHT)
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.line_spacing = 1.08
            add_run(para, str(value), size=font_size, color="202124")
    return table_obj


def callout(title, body, fill=LIGHT_ORANGE, border=ORANGE, title_color=ORANGE):
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    cell = tbl.cell(0, 0)
    cell.width = Cm(16.6)
    shade(cell, fill)
    set_cell_margins(cell, top=110, bottom=110, left=150, right=150)
    tblPr = tbl._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single" if edge == "left" else "nil")
        el.set(qn("w:sz"), "18" if edge == "left" else "0")
        el.set(qn("w:color"), border)
        borders.append(el)
    tblPr.append(borders)
    para = cell.paragraphs[0]
    para.paragraph_format.space_after = Pt(2)
    add_run(para, title, size=10, bold=True, color=title_color)
    para2 = cell.add_paragraph()
    para2.paragraph_format.space_after = Pt(0)
    para2.paragraph_format.line_spacing = 1.3
    add_run(para2, body, size=9.5, color="333333")
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def caption(text):
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(8)
    add_run(paragraph, text, size=8.5, color=GRAY)


def add_page_number_footer(section):
    footer = section.footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_run(paragraph, "AStockAgent 产品文档 v1.0  ·  ", size=8, color=GRAY)
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor.from_string(GRAY)


def add_header(section):
    header = section.header
    paragraph = header.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    add_run(paragraph, "AStockAgent · 产品与架构输入文档", size=8, color=GRAY)
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "4")
    bottom.set(qn("w:color"), "D9E2EC")
    pBdr.append(bottom)
    pPr.append(pBdr)


def code_block(text):
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = tbl.cell(0, 0)
    cell.width = Cm(16.6)
    shade(cell, "F3F4F6")
    set_cell_margins(cell, top=80, bottom=80, left=120, right=120)
    for idx, line in enumerate(text.splitlines()):
        para = cell.paragraphs[0] if idx == 0 else cell.add_paragraph()
        para.paragraph_format.space_after = Pt(0)
        para.paragraph_format.line_spacing = 1.1
        add_run(para, line, size=8.5, color="333333", mono=True)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


doc = Document()
doc.core_properties.title = "AStockAgent 智能投研助手 产品文档 v1.0"
doc.core_properties.subject = "产品定义、当前进度、量化结论与架构设计输入"
doc.core_properties.author = "AStockAgent 项目组"
doc.core_properties.keywords = "AStockAgent, 量化投研, Gradio, Tushare, 架构设计"

style_font(doc.styles["Normal"], FONT, 10.5, False, "202124")
doc.styles["Normal"].paragraph_format.line_spacing = 1.35
doc.styles["Normal"].paragraph_format.space_after = Pt(5)
for name, size, color in (
    ("Heading 1", 17, NAVY),
    ("Heading 2", 13, BLUE),
    ("Heading 3", 11.5, "333333"),
):
    style_font(doc.styles[name], FONT, size, True, color)

section = doc.sections[0]
section.page_width = Cm(21.0)
section.page_height = Cm(29.7)
section.top_margin = Cm(2.0)
section.bottom_margin = Cm(1.9)
section.left_margin = Cm(2.2)
section.right_margin = Cm(2.2)
add_header(section)
add_page_number_footer(section)

# 封面
cover = doc.add_table(rows=1, cols=1)
cover.alignment = WD_TABLE_ALIGNMENT.CENTER
cover.autofit = False
cell = cover.cell(0, 0)
cell.width = Cm(16.6)
cover.rows[0].height = Cm(8.2)
shade(cell, NAVY)
set_cell_margins(cell, top=300, bottom=260, left=320, right=260)
para = cell.paragraphs[0]
para.paragraph_format.space_after = Pt(8)
add_run(para, "AStockAgent", size=34, bold=True, color=WHITE)
para = cell.add_paragraph()
para.paragraph_format.space_after = Pt(8)
add_run(para, "智能投研助手 · 产品文档", size=21, bold=True, color="D8E6F3")
para = cell.add_paragraph()
para.paragraph_format.space_after = Pt(0)
add_run(para, "v1.0  ·  当前进度快照与架构设计输入", size=11.5, color="BFD4EA")

p("", space_after=10)
p("内部开发版  ·  2026-11-02", size=11, color=GRAY)
p("适用：产品规划 / 架构设计 / 研发交接 / 研究复盘", size=10.5, color=GRAY)
p("声明：本项目仅供技术学习与研究，所有输出不构成投资建议。", size=9.5, color=ORANGE)

# 封面 KPI
kpi = doc.add_table(rows=1, cols=4)
kpi.alignment = WD_TABLE_ALIGNMENT.CENTER
kpi.autofit = False
for idx, (num, label) in enumerate(
    [("13", "产品 Tab"), ("26", "astock 模块"), ("18", "自动化测试"), ("38", "推荐批次")]
):
    cell = kpi.rows[0].cells[idx]
    cell.width = Cm(4.15)
    shade(cell, LIGHT_BLUE)
    set_cell_margins(cell, top=150, bottom=140, left=80, right=80)
    para = cell.paragraphs[0]
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para.paragraph_format.space_after = Pt(0)
    add_run(para, num, size=24, bold=True, color=NAVY)
    para2 = cell.add_paragraph()
    para2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para2.paragraph_format.space_after = Pt(0)
    add_run(para2, label, size=9, color=GRAY)

p("", space_after=4)
loop = doc.add_table(rows=1, cols=1)
loop.alignment = WD_TABLE_ALIGNMENT.CENTER
loop.autofit = False
cell = loop.cell(0, 0)
cell.width = Cm(16.6)
shade(cell, LIGHT_TEAL)
set_cell_margins(cell, top=110, bottom=110, left=120, right=120)
para = cell.paragraphs[0]
para.alignment = WD_ALIGN_PARAGRAPH.CENTER
para.paragraph_format.space_after = Pt(0)
add_run(
    para,
    "数据  →  指标/事件  →  推荐/跟踪  →  回测/因子/优化  →  AI 解释",
    size=10.5,
    bold=True,
    color="1F5C55",
)
p("", space_after=2)
p(
    "当前定位：研究工作流已成型，量化 alpha 仍在验证。",
    size=9,
    color=GRAY,
    align=WD_ALIGN_PARAGRAPH.CENTER,
)

doc.add_page_break()

# 文档信息
h1("文档信息", page_break=False)
table(
    ["项目", "内容"],
    [
        ["文档版本", "v1.0（当前进度快照）"],
        ["更新日期", "2026-11-02"],
        ["产品阶段", "本地单用户可用版；研究能力已成型，量化 alpha 尚未被验证"],
        ["代码基线", "astock 26 个模块 / 6,009 行；app.py 541 行；tests 18 个"],
        ["运行环境", "Windows 11 / Python 3.13.5 / Gradio 6.15 / Tushare 代理 / DeepSeek"],
        ["当前入口", "http://127.0.0.1:7861（本机）"],
        ["核心资产", "49 只自选池、日线/分钟缓存、38 个推荐批次、190 条跟踪记录"],
        ["发布状态", "release/AStockAgent 已同步，未 commit / push"],
    ],
    widths=[2.8, 13.8],
    font_size=9.2,
)

p("", space_after=2)
callout(
    "阅读建议",
    "如果只看一页：先看「执行摘要」与「当前进度」；如果做架构设计：直接跳到第六章「架构设计输入」，"
    "并按 6.8 的目标拆分与 6.9 的 ADR 做取舍。量化结论只作为研究现状，不作为收益承诺。",
    fill=LIGHT_BLUE,
    border=BLUE,
    title_color=BLUE,
)

h2("目录导航")
table(
    ["章节", "内容"],
    [
        ["一、执行摘要", "产品定位、当前阶段、核心结论与架构判断"],
        ["二、产品定位与目标", "用户、场景、目标/非目标、设计原则"],
        ["三、当前进度", "版本快照、13 个 Tab、数据资产、工程质量"],
        ["四、功能规格", "多周期、每日推荐、跟踪、回测、优化、因子、AI"],
        ["五、量化方法与结论", "回填显著性、PIT 因子、Walk-forward 结果"],
        ["六、架构设计输入", "分层架构、模块清单、数据流、接口、数据模型、目标架构"],
        ["七、非功能与风险", "性能基线、可靠性、安全、技术债登记"],
        ["八、演进路线", "P0–P3 阶段、验收指标、开放决策"],
    ],
    widths=[3.2, 13.4],
    font_size=9.2,
)

# 一、执行摘要
h1("一、执行摘要")
h2("1.1 一句话定位")
p(
    "AStockAgent 是一个本地单用户的 A 股智能投研工作台：把行情、技术指标、公告/研报/新闻/政策、"
    "策略回测、参数优化、因子研究、每日推荐与推荐跟踪收敛到同一条可追溯的工作流，并用 LLM 做解释与问答，"
    "而不是让 LLM 直接生成交易信号。",
    size=11,
)

h2("1.2 当前阶段")
p(
    "项目已经从早期的「单文件行情看板」演进为「数据层 + 领域能力 + 应用编排 + 研究闭环」的结构。"
    "当前有 13 个产品 Tab、26 个 astock 模块、18 个自动化测试；日线/分钟/公告/研报/新闻/政策接口已接通，"
    "每日推荐可在交易日 16:40 自动运行。"
)
table(
    ["指标", "当前值", "说明"],
    [
        ["产品入口", "13 个 Tab", "6 个遗留 Tab + 7 个新增研究/推荐 Tab"],
        ["代码规模", "6,550 行", "astock 6,009 行 + app.py 541 行"],
        ["自动化测试", "18 个", "量化规则、数据口径、推荐、跟踪、优化、因子"],
        ["推荐批次", "38 个", "实盘 3 个批次 / 15 条；技术分回填 35 个批次 / 175 条"],
        ["最新推荐", "2026-10-30 收盘", "2026-11-02 08:31 生成，覆盖 49 只股票池"],
        ["本地缓存", "约 13 MB", "日线/复权/分钟/指数 Parquet"],
    ],
    widths=[3.3, 4.0, 9.3],
    font_size=9.0,
)

h2("1.3 核心结论")
callout(
    "工程能力已形成，alpha 尚未被证明",
    "回填 38 个批次后，Top2 与其余在 T+1/T+5 没有显著差异；T+20 的名义差异受重叠窗口影响，"
    "独立窗口只有约 1 个。PIT 因子池下没有任何因子达到常规显著性；MA/MACD 的 Walk-forward 样本外也没有跑赢沪深300。"
    "因此当前产品的价值是「研究工作流 + 可追溯性 + 自动化」，不是「稳定收益」。",
    fill="FFF7E8",
    border=ORANGE,
    title_color=ORANGE,
)

h2("1.4 给架构设计的五个判断")
bullets(
    [
        "先拆「数据/事件」与「推荐/跟踪/研究」边界：前者是供应商适配与缓存，后者是可复现的策略与统计。",
        "推荐结果必须从 JSON 文件升级为带 run_id 的 ledger（运行记录 + 明细 + 证据 + 跟踪），否则无法做审计与多版本对比。",
        "调度器必须从「应用内线程」升级为有锁、幂等、可重试、可观测的 job；当前已经出现过漏跑和一次失败。",
        "PIT 股票池与历史市值/退市名单是研究可信度的前置条件；当前 PIT 仍只含今天仍上市的股票。",
        "LLM 只做解释层：输入证据、输出文字；不要进入信号、排序和回测。",
    ]
)

# 二、产品定位
h1("二、产品定位与目标", page_break=False)
h2("2.1 背景与问题")
p(
    "个人 A 股投研通常被拆散在多个工具里：行情软件看 K 线、Excel 算指标、新闻 App 看公告、"
    "券商研报 PDF、脚本做回测、LLM 聊天做总结。数据口径、复权、交易日历、前视偏差和证据来源很难统一，"
    "复盘结果也无法沉淀。AStockAgent 的目标是把这些环节收敛为一个本地工作台。"
)
table(
    ["痛点", "当前产品回应", "尚未解决"],
    [
        ["数据源多、口径不一", "TushareClient 统一入口；qfq/交易日历/缓存", "供应商单点、退市/历史成分缺失"],
        ["指标与图表重复劳动", "指标库 + 多周期 K 线 + 四宫格/区间对比", "图表性能与移动端适配"],
        ["新闻/研报/公告分散", "events 统一检索 + 证据链 + AI 引用", "历史新闻覆盖与全文检索"],
        ["策略验证不成体系", "回测 + 参数网格 + Walk-forward + 策略实验室", "多标的组合回测、分钟级回测"],
        ["推荐无法检验", "T+1/T+5/T+20 跟踪 + NW/block 检验 + 回填", "实盘批次仍少、PIT 不完整"],
        ["LLM 幻觉", "LLM 只解释、证据注入、强制引用", "证据不足时的拒答仍需打磨"],
    ],
    widths=[4.0, 7.0, 5.6],
    font_size=8.8,
)

doc.add_page_break()
h2("2.2 目标用户")
table(
    ["角色", "核心诉求", "典型使用方式"],
    [
        ["个人投资者", "盘后快速复盘、看每日候选、理解消息面", "16:40 后打开每日推荐；看证据链；查个股"],
        ["策略研究者", "验证均线/MACD/RSI、参数稳健性、因子有效性", "回测 → 参数优化 → 策略实验室 → 因子 IC"],
        ["开发者/维护者", "扩展数据源、增加因子、接新模型", "按 astock 模块边界开发；用 tests 回归"],
    ],
    widths=[3.4, 7.2, 6.0],
    font_size=9.0,
)

h2("2.3 产品目标与非目标")
table(
    ["类型", "内容"],
    [
        ["目标", "本地可运行；数据/指标/事件/策略/推荐/跟踪可追溯；每日自动生成；研究结论可复现"],
        ["目标", "把 LLM 用在解释与检索，不让它参与交易信号；所有推荐附证据与数据截止时间"],
        ["非目标", "公开 SaaS / 多租户；投顾牌照；实盘自动下单；收益承诺；高频交易"],
        ["非目标", "当前不做全市场逐笔/Level-2；不做复杂期权/期货组合"],
    ],
    widths=[2.4, 14.2],
    font_size=9.2,
)

h2("2.4 设计原则")
bullets(
    [
        "本地优先：密钥、数据、推荐结果都在本机；不依赖云端数据库。",
        "可追溯：每个推荐有 as_of、来源、证据、生成时间；每个回测有参数和成本假设。",
        "不使用未来数据：信号 t 日生成、t+1 成交；因子按日截断；PIT 股票池按当日市值。",
        "LLM 只解释：模型输出不改变量化排序；没有证据时明确说资料不足。",
        "渐进式拆分：保留可用的 Gradio 页面，先把数据/研究能力模块化，再替换 UI 与存储。",
    ]
)

# 三、当前进度
h1("三、当前进度")
h2("3.1 版本快照")
table(
    ["维度", "状态", "证据/位置"],
    [
        ["产品形态", "本地 Gradio，多 Tab 工作台", "http://127.0.0.1:7861"],
        ["数据", "日线/复权/周月季/1–60 分钟/公告/研报/新闻/政策/市值", "astock/data.py、events.py、universe.py"],
        ["推荐", "技术+事件+LLM 归因；CSV/JSON/证据链；自动任务", "recommend.py、recommend_ui.py、scheduler.py"],
        ["跟踪", "T+1/5/20、NW/block、回填、来源标注", "track.py、track_ui.py、backfill.py"],
        ["研究", "回测、MA 网格/WF、MACD/RSI 多标的 WF、11 因子 IC", "backtest*.py、optimize*.py、strategy_lab.py、factors.py"],
        ["UI", "13 个 Tab（6 遗留 + 7 新）", "app.py、astock/*_ui.py"],
        ["测试", "18 个 unittest 全部通过", "tests/"],
        ["发布", "release 已同步，未 commit/push", "release/AStockAgent"],
    ],
    widths=[3.2, 7.3, 6.1],
    font_size=8.8,
)

h2("3.2 功能矩阵")
table(
    ["功能域", "入口", "当前能力", "成熟度"],
    [
        ["单股体检", "🔍 单股", "基础 K 线、全指标、AI 点评、新闻引用", "可用（遗留）"],
        ["组合体检", "📊 组合", "权重、净值、相关性、波动率、AI 点评", "可用（遗留）"],
        ["简报/选股/自选", "📰/🎯/📋", "自选池、11 条件过滤、每日简报", "可用（遗留）"],
        ["多周期 K 线", "📈 多周期", "日/周/月/季 + 1/5/15/30/60 分钟；四宫格；区间对比；前复权", "可用"],
        ["每日推荐", "📰 每日推荐", "技术 65% + 事件 35%；行业上限 2；LLM 理由；证据链；16:40", "可用"],
        ["推荐跟踪", "📈 推荐跟踪", "T+1/5/20；NW/block；回填；显著性；来源", "可用（样本不足）"],
        ["回测", "🧪 回测", "T+1、涨跌停、成本、基准、交易明细", "可用"],
        ["参数优化", "🧪 参数优化", "MA 网格、热力图、单标的 WF", "可用"],
        ["策略实验室", "🧪 策略实验室", "MACD/RSI/MA 网格、多标的等权 WF", "可用（研究）"],
        ["因子 IC", "📊 因子 IC", "11 因子、PIT/中性化、RankIC、分层", "研究原型"],
        ["AI 对话", "💬 AI 对话", "新闻/公告/研报/政策检索 + 引用", "可用"],
        ["自动任务", "应用内线程 / bat", "交易日 16:40；失败重试；日志", "可用但不稳"],
    ],
    widths=[3.2, 3.2, 8.2, 2.0],
    font_size=8.5,
)

h2("3.3 数据与运行资产（截至 2026-11-02）")
table(
    ["资产", "规模/状态", "说明"],
    [
        ["自选池", "49 只", "stocks.csv；用于推荐/因子/回测"],
        ["日线缓存", "3 年 qfq + 原始", "cache/daily/*.parquet + meta"],
        ["分钟数据", "1/5/15/30/60 分钟", "当日 rt_min；历史 stk_mins 分块；内存缓存"],
        ["推荐文件", "38 个批次 / 190 条", "实盘 3 批 / 15 条；回填 35 批 / 175 条"],
        ["最新推荐", "as_of 2026-10-30", "5 只：600519、600000、600887、000001、000333"],
        ["调度日志", "有成功/失败记录", "10-27 成功；10-29 失败；11-02 成功"],
        ["本地缓存", "约 13 MB", "不含 .venv 与 deliverables"],
    ],
    widths=[3.2, 5.2, 8.2],
    font_size=8.8,
)

# 四、功能规格
h1("四、功能规格", page_break=False)
h2("4.1 多周期 K 线")
table(
    ["项目", "规格"],
    [
        ["输入", "股票代码、历史区间（1/3/5 年/全部）、周期、叠加项、展示模式"],
        ["周期", "日线、周线、月线、季线；1/5/15/30/60 分钟；四宫格可任选 2–4 个"],
        ["口径", "日/周/月/季前复权；分钟按最新复权因子前复权；周/月/季由日线重采样"],
        ["交互", "rangebreaks 去周末/节假日/午休；周期按钮；十字光标；图例/成交量"],
        ["性能", "5 分钟近 5 日约 203 根；1 分钟近 60 日约 14,293 根；首次加载约 10s"],
    ],
    widths=[2.8, 13.8],
    font_size=9.0,
)
p("验收要点：分钟成交量/成交额与日线一致；复权前后在除权日连续；多日图日期标签不重复只显示时间。")

h2("4.2 每日推荐")
table(
    ["项目", "规格"],
    [
        ["股票池", "stocks.csv 或手填代码/名称；最多 60 只；技术预筛默认 15"],
        ["技术分", "MA20/60/120、MACD、RSI、量比、20/60 日动量、ATR/回撤；横截面排名混合"],
        ["事件分", "30 天公告、7 天研报评级/标题、3 天新闻、7 天政策；别名匹配 + 情感/评级"],
        ["综合", "0.65 × 技术 + 0.35 × 事件；同行业最多 2 只；输出 3–10 只"],
        ["AI", "DeepSeek 只写逻辑/催化/风险；失败时规则理由；证据 ID 可追溯"],
        ["输出", "表格 + Markdown 报告 + 证据链 + CSV/JSON + latest.json"],
        ["时间", "as_of 取最新日线收盘；默认交易日 16:40 自动生成"],
        ["性能", "49 只约 35–40 秒（含 LLM）；无 LLM 约 25 秒；首次加载日线较慢"],
    ],
    widths=[2.8, 13.8],
    font_size=9.0,
)

h2("4.3 推荐跟踪")
table(
    ["项目", "规格"],
    [
        ["对象", "带 as_of 的 recommend_*.json；同日多版本保留最后一次"],
        ["收益", "推荐日收盘 → T+1/T+5/T+20 收盘；超额 = 个股 − 沪深300"],
        ["统计", "日期层面均值；t 值、Newey-West、block bootstrap、95% CI、独立窗口"],
        ["分组", "Top2 / 其余 / 综合分≥75 / <75 / 全部；Top2−其余差异检验"],
        ["回填", "历史技术分回填 30 批；事件分固定 50；source=backfill_technical"],
        ["限制", "T+20 独立窗口约 1–2 个；回填不等于完整推荐；样本仍需积累"],
    ],
    widths=[2.8, 13.8],
    font_size=9.0,
)

h2("4.4 回测")
table(
    ["项目", "规格"],
    [
        ["规则", "long-only、满仓、t 日收盘信号、t+1 开盘成交、T+1、涨跌停/停牌过滤"],
        ["成本", "佣金万 2.5（最低 5 元）、印花税卖出万 5、滑点单边万 5、100 股整数倍"],
        ["输出", "累计/年化/最大回撤/夏普/卡玛/胜率/盈亏比/Beta/Alpha/IR/超额；K 线买卖点、净值、回撤、交易表"],
        ["基准", "沪深300；策略模板 MA/MACD/RSI/布林/买入持有"],
    ],
    widths=[2.8, 13.8],
    font_size=9.0,
)

h2("4.5 参数优化")
p("MA 的 fast × slow 网格 + 热力图 + 单标的 Walk-forward：训练窗口选参数，测试窗口验证，滚动前进。全样本最优仅作 in-sample 参考。")

h2("4.6 策略实验室")
p("MA/MACD/RSI 固定网格；每只股票独立 Walk-forward；样本外收益等权拼接；最多 10 只股票；输出分标的指标、窗口明细、组合净值。")

h2("4.7 因子 IC")
table(
    ["项目", "规格"],
    [
        ["因子", "20/60 日动量、5 日反转、20 日波动、量比、成交额比、Amihud、MA20 乖离、RSI14、MACD 柱、ATR 占比"],
        ["股票池", "自选池、最新市值前 100/300、PIT 动态前 100/300、手填"],
        ["中性化", "每日横截面：行业哑变量 + log(总市值) 回归取残差；PIT 用历史市值，非 PIT 用最新快照"],
        ["输出", "IC 均值/标准差/ICIR/IC>0/t/p/有效日数；Q1–Q5、Q5-Q1、单调性"],
        ["限制", "PIT 仍只含当前上市公司；小样本/多重检验未校正"],
    ],
    widths=[2.8, 13.8],
    font_size=9.0,
)

h2("4.8 AI 对话的新闻数据源")
bullets(
    [
        "从问题中识别股票代码/名称；检索近 30 天公告、7 天研报、3 天新闻、7 天政策。",
        "证据以 [1] [2] 注入 system prompt；回答末尾自动附来源；资料不足时明确说明。",
        "同轮多只股票分别检索；未提股票时返回市场要闻/政策。",
        "单股 AI 体检也接入同一套新闻上下文和来源。",
    ]
)

h2("4.9 遗留模块与技术债")
p(
    "单股/组合/简报/选股/自选仍主要位于 app.py 的 541 行入口内，部分指标逻辑与新 astock/indicators.py 重复；"
    "组合页「统计图表」会重复调用 portfolio() 与 LLM；这些不影响新增能力，但会增加维护成本。"
)

# 五、量化结论
h1("五、量化方法与当前结论", page_break=False)
h2("5.1 回填批次与显著性")
p(
    "截至 2026-11-02：38 个批次 / 190 条记录，其中实盘 15 条、技术分回填 175 条。"
    "显著性按推荐日聚合，避免把同一天的多只股票当成独立样本；同时给出 Newey-West 与 block bootstrap。"
)
table(
    ["分组", "T+1 平均超额", "T+5 平均超额", "T+20 平均超额"],
    [
        ["Top2", "+0.23%", "+1.61%", "+8.92%"],
        ["其余", "+0.29%", "+1.38%", "+7.15%"],
        ["Top2 − 其余", "−0.06%（p≈0.73）", "+0.23%（p≈0.58）", "+1.77%（NW p≈0.013；独立窗口≈1）"],
    ],
    widths=[4.0, 4.2, 4.2, 4.2],
    font_size=9.0,
)
p(
    "解释：T+1/T+5 没有显著差异，说明技术分排序没有稳定区分度；T+20 的名义显著性来自重叠窗口，"
    "独立窗口只有约 1 个，不能当作可靠 alpha。绝对超额为正也可能来自股票池/风格，而非综合分。"
)

h2("5.2 PIT 因子 IC")
p(
    "3 年、PIT 市值前 100、11 因子、行业+市值中性化：66 个 IC 日。"
)
table(
    ["因子", "IC 均值", "ICIR", "p 值"],
    [
        ["MACD柱", "−0.0155", "−0.158", "0.200"],
        ["Amihud非流动性", "−0.0113", "−0.110", "0.373"],
        ["20日波动", "−0.0108", "−0.105", "0.395"],
        ["60日动量", "−0.0078", "−0.072", "0.560"],
    ],
    widths=[6.0, 3.4, 3.4, 3.8],
    font_size=9.0,
)
p(
    "对比：静态「当前市值前 100」3 年样本中，60 日动量 IC 为 −0.0207、p≈0.038；换成 PIT 后降到 −0.0078、p≈0.56。"
    "这提示之前的动量显著性很可能来自幸存者/当前成分偏差。"
)

h2("5.3 Walk-forward 结果")
table(
    ["实验", "样本外累计", "同期沪深300", "超额"],
    [
        ["MA 600519（3 年，99 组 + 5 窗口）", "−4.76%", "+19.50%", "−24.27%"],
        ["MACD 3 只等权（27 组 + 5 窗口）", "+3.73%", "+18.95%", "−15.22%"],
    ],
    widths=[7.6, 3.2, 3.2, 2.6],
    font_size=9.0,
)
p(
    "MACD 分标的：000001 +13.70%、600519 +0.61%、600036 −2.96%。单只正超额不能外推；"
    "多标的等权组合仍显著跑输基准。"
)

h2("5.4 对产品的含义")
bullets(
    [
        "当前最可靠的产出是「研究工作流」：数据统一、推荐可追溯、跟踪可统计、策略可复现。",
        "alpha 尚未被证明；不要用推荐分数直接下单，也不要把回填批次的显著性当成实盘能力。",
        "下一步优先级应是数据正确性（PIT/退市/历史成分）与统计严谨性（独立窗口/多重检验），而不是继续堆参数。",
    ]
)

# 六、架构设计输入
h1("六、架构设计输入")
h2("6.1 逻辑架构")
doc.add_picture("deliverables/architecture.png", width=Cm(16.6))
caption("图 1  AStockAgent 逻辑架构：用户层 / 应用编排 / 领域能力 / 基础设施 / 外部服务")

h2("6.2 分层职责")
table(
    ["层", "职责", "当前实现", "架构关注点"],
    [
        ["用户层", "页面、交互、展示", "Gradio 13 Tab", "状态管理、长任务、移动端"],
        ["应用编排", "用例编排、参数校验、结果组装", "app.py + *_ui.py", "是否拆 FastAPI/服务层"],
        ["领域能力", "指标、事件、推荐、跟踪、回测、因子", "astock 模块", "纯函数、可测试、无 UI 依赖"],
        ["基础设施", "供应商客户端、缓存、LLM、配置、调度", "tushare_client/data/llm/scheduler", "重试、限流、幂等、可观测"],
        ["外部服务", "Tushare 代理、DeepSeek", "HTTP", "单点、权限、SLA"],
    ],
    widths=[2.8, 5.4, 5.0, 3.4],
    font_size=8.8,
)

h2("6.3 模块清单")
modules = [
    ("app.py", "541", "Gradio 入口；遗留 Tab + 新 Tab 组装 + 调度启动", "所有 astock UI"),
    ("config.py", "23", ".env、路径、缓存目录", "dotenv"),
    ("tushare_client.py", "156", "无系统代理 HTTP 客户端、重试、权限错误", "requests"),
    ("data.py", "560", "日线/分钟/复权/缓存/交易日历/重采样", "TushareClient"),
    ("indicators.py", "111", "向量化指标库", "pandas/numpy"),
    ("charts.py", "460", "K 线、区间对比、分时 rangebreaks", "plotly"),
    ("ui.py", "289", "多周期 Tab", "data/charts"),
    ("backtest.py", "401", "回测引擎、信号、指标/基准", "data/indicators"),
    ("backtest_chart.py", "253", "回测图与指标表", "plotly"),
    ("backtest_ui.py", "118", "回测 Tab", "backtest"),
    ("events.py", "678", "公告/研报/新闻/政策/基础信息/别名/情感", "TushareClient"),
    ("recommend.py", "652", "技术分 + 事件分 + 推荐流水线", "events/data/llm"),
    ("recommend_ui.py", "86", "每日推荐 Tab", "recommend"),
    ("track.py", "350", "T+N 跟踪、NW/block/bootstrap", "data"),
    ("track_ui.py", "51", "推荐跟踪 Tab", "track/backfill"),
    ("backfill.py", "134", "历史技术分回填", "recommend"),
    ("optimize.py", "298", "MA 网格、单标的 WF", "backtest/data"),
    ("optimize_ui.py", "158", "参数优化 Tab", "optimize"),
    ("strategy_lab.py", "310", "MACD/RSI/MA 网格、多标的 WF", "backtest/optimize"),
    ("strategy_ui.py", "98", "策略实验室 Tab", "strategy_lab"),
    ("factors.py", "391", "因子面板、RankIC、中性化、分层", "data/universe"),
    ("factor_ui.py", "73", "因子 IC Tab", "factors"),
    ("universe.py", "107", "市值前 N / PIT 股票池、市值快照", "daily_basic"),
    ("scheduler.py", "151", "交易日 16:40 自动推荐、重试、日志", "recommend"),
    ("llm.py", "74", "DeepSeek 直连/代理自动切换", "openai/httpx"),
    ("utils.py", "24", "代码归一化/校验", "re"),
    ("__init__.py", "3", "版本", "-"),
]
table(
    ["模块", "行数", "职责", "关键依赖"],
    [(a, b, c, d) for a, b, c, d in modules],
    widths=[4.0, 1.3, 7.6, 3.7],
    font_size=7.8,
)

h2("6.4 关键数据流")
table(
    ["流程", "步骤", "落盘/缓存"],
    [
        ["行情", "Tushare → TushareClient → daily/minute → Parquet/memory → indicators/charts", "cache/daily、内存 30s/5min"],
        ["事件", "anns_d/report_rc/research_report/npr/news → 归一化 → 别名/情感 → evidence", "内存 5–10 分钟"],
        ["推荐", "universe → 技术分/预筛 → 事件分 → 综合分 → LLM 理由 → table/report/evidence", "data/recommendations/*.json + latest.json"],
        ["跟踪", "ledger → qfq 日线 → T+N 收益/基准 → 日期级统计 → UI", "无独立表（内存计算）"],
        ["AI 对话", "问题 → 实体识别 → events 检索 → prompt+引用 → 回答+来源", "无长期会话存储"],
        ["调度", "16:40 → run_once → recommendation → state/log", "scheduler_state.json、scheduler.log"],
    ],
    widths=[2.4, 10.2, 4.2],
    font_size=8.6,
)

h2("6.5 外部接口")
table(
    ["类型", "接口/服务", "用途", "关键说明"],
    [
        ["数据", "daily / adj_factor / weekly / monthly", "日线、复权、周月线", "qfq 以最新因子为基准"],
        ["数据", "index_daily / trade_cal", "沪深300、交易日历", "rangebreaks 依赖日历"],
        ["数据", "stock_basic / daily_basic", "名称/行业/市值", "PIT 与市值中性化"],
        ["数据", "stk_mins / rt_min", "历史/实时分钟", "1/5/15/30/60 分钟；前复权"],
        ["事件", "anns_d / report_rc / research_report", "公告/研报评级/研报标题", "按日期/股票批量"],
        ["事件", "npr / news / major_news", "政策/快讯/长篇新闻", "别名匹配与情感"],
        ["AI", "DeepSeek chat", "归因/问答", "直连失败自动走代理"],
        ["UI", "Gradio", "本地 Web", "127.0.0.1:7861"],
        ["网络", "Tushare 代理", "HTTP 数据", "系统代理会导致 502，客户端强制 trust_env=False"],
    ],
    widths=[2.2, 5.4, 5.0, 4.2],
    font_size=8.5,
)

h2("6.6 数据模型（建议目标形态）")
table(
    ["对象", "当前存储", "关键字段", "建议"],
    [
        ["DailyBar", "Parquet", "ts_code, trade_date, OHLCV, amount, adj_factor", "保留；加数据版本/质量标记"],
        ["MinuteBar", "内存 + 供应商", "trade_time, OHLCV, vol, amount", "按需落 Parquet；加复权因子版本"],
        ["Event", "内存", "pub_time, kind, title, source, url, content", "落 SQLite/全文索引"],
        ["RecommendationRun", "JSON 文件", "as_of, generated_at, universe, scores, ai", "run_id + 状态 + 版本"],
        ["RecommendationItem", "JSON table", "code, rank, composite, action, reason", "拆表；关联 run_id"],
        ["Evidence", "JSON evidence", "kind, date, title, source, url", "独立表/去重/内容哈希"],
        ["TrackingResult", "内存计算", "run_id, code, horizon, return, excess", "持久化，支持回看"],
        ["JobRun", "scheduler_state", "date, status, error, paths", "独立 job 表/重试次数"],
        ["PITMembership", "内存", "date, code, total_mv", "物化到 SQLite/Parquet"],
    ],
    widths=[3.4, 4.2, 5.8, 3.4],
    font_size=8.4,
)

h2("6.7 运行与部署")
code_block(
    """Windows 11 + Python 3.13 venv
启动 Web：start_app.bat  ->  python app.py  ->  http://127.0.0.1:7861
自动推荐：应用内线程（交易日 16:40）或 start_scheduler.bat
关键环境变量：TUSHARE_TOKEN / TUSHARE_API_URL / DEEPSEEK_API_KEY
                ASTOCK_AUTO_RECOMMEND / ASTOCK_RECOMMEND_TIME
日志：data/recommendations/scheduler.log；状态：scheduler_state.json
缓存：cache/（约 13MB）；推荐：data/recommendations/*.json|*.csv
测试：python -m unittest discover -s tests -v
发布：release/AStockAgent（git 工作区已同步，尚未 commit/push）"""
)
p(
    "当前是单用户本地部署，没有鉴权、队列、数据库和容器。生产化前需要补齐进程守护、任务队列、数据契约与观测。"
)

h2("6.8 目标架构建议")
table(
    ["目标组件", "职责", "由当前什么演进", "优先级"],
    [
        ["MarketData Service", "行情/复权/日历/缓存/质量检查", "data.py + tushare_client.py", "P0"],
        ["EventData Service", "公告/研报/新闻/政策、别名、去重、全文检索", "events.py", "P1"],
        ["Recommendation Service", "universe→评分→证据→LLM→ledger", "recommend.py + backfill.py", "P0"],
        ["Tracking Service", "T+N、日期级统计、显著性", "track.py", "P0"],
        ["Research Service", "回测、WF、因子 IC、组合优化", "backtest/optimize/strategy_lab/factors", "P1"],
        ["AI Gateway", "提示模板、引用、成本/超时、拒答", "llm.py + ai_chat", "P1"],
        ["Job Orchestrator", "16:40、锁、幂等、重试、运行台账", "scheduler.py", "P0"],
        ["Storage", "Parquet 行情 + SQLite ledger/证据/跟踪", "JSON/parquet 混合", "P0"],
        ["Observability", "结构化日志、run_id、指标、告警", "scheduler.log", "P1"],
    ],
    widths=[3.6, 6.4, 5.2, 1.4],
    font_size=8.6,
)
p(
    "建议不要一开始拆微服务：先做「模块边界 + 明确接口 + SQLite ledger + 单实例锁」，"
    "等需要多用户/远程访问时再把 Gradio 背后的用例层暴露为 FastAPI。"
)

h2("6.9 关键架构决策（ADR 摘要）")
table(
    ["ADR", "决策", "理由", "代价/后续"],
    [
        ["ADR-01", "本地 Gradio 作为当前 UI", "快速迭代、单用户、零前端成本", "长任务/多用户/鉴权弱"],
        ["ADR-02", "Parquet + JSON 作为当前存储", "简单、可读、易迁移", "无事务/查询/并发；需 SQLite"],
        ["ADR-03", "LLM 只解释，不生成信号", "可复现、避免幻觉进回测", "解释价值受证据质量限制"],
        ["ADR-04", "TushareClient 强制 trust_env=False", "绕过系统代理导致的 502", "依赖代理地址/权限"],
        ["ADR-05", "PIT 股票池优先于参数堆叠", "避免幸存者偏差", "数据成本高，当前仍不完整"],
    ],
    widths=[2.2, 5.6, 5.0, 4.0],
    font_size=8.6,
)

# 七、非功能与风险
h1("七、非功能与风险")
h2("7.1 性能基线")
table(
    ["场景", "当前耗时/规模", "备注"],
    [
        ["推荐（49 只，含 LLM）", "约 35–40 秒", "首次日线加载较慢；缓存后更快"],
        ["推荐（49 只，无 LLM）", "约 25 秒", "主要用于规则/数据验证"],
        ["因子 PIT 3 年（前 100）", "约 86 秒", "11 因子、66 个 IC 日"],
        ["参数优化（MA 99 组 + 5 窗口）", "约 20 秒", "单标的"],
        ["策略实验室（3 只 MACD）", "约 13–18 秒", "27 组 × 5 窗口/只"],
        ["分钟 60 日 1 分钟", "约 14,293 根", "加载约 10 秒；图表较重"],
        ["测试套件", "18 个 / 约 2 秒", "unittest"],
    ],
    widths=[5.2, 4.6, 6.8],
    font_size=8.8,
)

h2("7.2 非功能要求")
table(
    ["维度", "要求", "现状/差距"],
    [
        ["正确性", "无前视；复权一致；PIT；指标边界", "规则已做；PIT 仍缺退市/历史成分"],
        ["可靠性", "每日任务成功、可重试、可追溯", "曾漏跑/失败；无单实例锁/job 表"],
        ["性能", "推荐 <60s；研究任务可等待", "基本满足；PIT/1min 图偏慢"],
        ["可测试", "指标/回测/跟踪/因子有回归", "18 个测试；缺数据契约/端到端"],
        ["可观测", "run_id、日志、失败告警", "仅 scheduler.log；无指标/告警"],
        ["安全", "密钥不入库、不提交", ".env 已忽略；旧 token 已清理，仍建议轮换"],
        ["可维护", "模块边界清晰、无重复逻辑", "app.py 遗留 Tab；两份指标实现"],
        ["可扩展", "新数据源/因子/模型可插拔", "已有模块边界；缺少接口协议"],
    ],
    widths=[2.4, 7.0, 7.2],
    font_size=8.8,
)

doc.add_page_break()
h2("7.3 风险登记")
table(
    ["风险", "影响", "当前状态", "建议"],
    [
        ["数据供应商单点/代理故障", "推荐/回测失败", "10-29 一次失败；代理可用", "多源降级、健康检查、缓存"],
        ["调度依赖应用进程", "漏跑", "10-28 16:40 未跑", "Windows 任务计划/独立进程 + 锁"],
        ["无单实例锁", "重复推荐/覆盖", "存在多 Python 进程风险", "文件锁/租约 + 幂等键"],
        ["PIT 不完整", "因子结论偏差", "仅当前上市公司", "退市名单/历史指数成分"],
        ["小样本/重叠窗口", "显著性不可靠", "T+20 独立窗口≈1", "block bootstrap/更多批次"],
        ["LLM 幻觉/延迟", "解释错误/超时", "已做证据/引用/回退", "结构化输出/证据校验/成本上限"],
        ["遗留单文件与重复指标", "维护成本/口径漂移", "app.py 541 行", "逐步迁移到 astock"],
        ["无 CI/DB/schema", "回归与审计弱", "JSON/parquet", "pytest + SQLite + 迁移"],
        ["密钥/发布", "泄露/未同步", ".env 忽略；release 未提交", "轮换、密钥管理、CI 发布"],
    ],
    widths=[4.0, 4.2, 4.2, 4.2],
    font_size=8.4,
)

# 八、演进路线
h1("八、演进路线与验收", page_break=False)
h2("8.1 阶段路线")
table(
    ["阶段", "目标", "关键任务", "验收"],
    [
        ["P0 稳定化（1–2 周）", "每日任务可靠、可追溯", "单实例锁、job 表、重试/告警、CI、密钥轮换", "连续 5 个交易日无漏跑；失败可重试"],
        ["P1 数据正确性（2–3 周）", "研究结论可信", "PIT 指数成分/退市名单、历史市值/估值、数据质量检查", "PIT 审计通过；无前视测试"],
        ["P2 研究增强（3–4 周）", "统计与组合能力", "多重检验、因子正交化、组合优化、分钟回测", "20+ 实盘批次；显著性报告"],
        ["P3 产品化（可选）", "多用户/远程/打包", "FastAPI、鉴权、任务队列、容器、监控", "SLO/权限/部署文档"],
    ],
    widths=[4.0, 4.0, 5.2, 3.4],
    font_size=8.6,
)

h2("8.2 验收指标")
bullets(
    [
        "数据：日线/分钟/复权/交易日历一致性；PIT 股票池可审计；无未来数据。",
        "推荐：每个 run 有 run_id、as_of、版本、证据；失败可重跑且幂等。",
        "跟踪：至少 20 个实盘批次；T+1/T+5/T+20 的日期级 NW/block 结果可复现。",
        "研究：参数/WF/因子结果带样本区间、成本、基准；不允许只报最优值。",
        "工程：18+ 测试通过；CI；关键路径日志/告警；密钥不落库。",
    ]
)

h2("8.3 需要拍板的开放问题")
table(
    ["问题", "选项", "建议"],
    [
        ["是否多用户/远程", "本地单用户 / 局域网 / 云端", "先本地，接口留出 FastAPI"],
        ["存储", "JSON+Parquet / SQLite / DuckDB", "行情 Parquet + ledger SQLite"],
        ["调度", "应用内线程 / 独立进程 / Windows 任务计划", "独立进程 + 任务计划 + 锁"],
        ["数据源", "单一 Tushare / 多源降级", "先单源，加健康检查与降级"],
        ["研究范围", "继续扩因子 / 先做 PIT 与统计", "先 PIT/统计，再扩因子"],
    ],
    widths=[4.0, 7.2, 5.4],
    font_size=8.8,
)

# 附录
h1("附录", page_break=False)
h2("A. 目录结构")
code_block(
    """app.py                    Gradio 入口（13 Tab）
astock/
  config.py                .env / 路径 / 缓存
  tushare_client.py        无系统代理客户端
  data.py                  日线/分钟/复权/缓存/日历
  indicators.py            指标库
  charts.py / ui.py        多周期 K 线
  events.py                公告/研报/新闻/政策
  recommend.py / *_ui.py   每日推荐
  track.py / backfill.py   T+N 跟踪与回填
  backtest*.py             回测
  optimize*.py             MA 网格/WF
  strategy_lab.py           MACD/RSI/多标的
  factors.py / factor_ui.py 因子 IC
  universe.py              PIT/市值股票池
  scheduler.py             16:40 自动任务
  llm.py                   DeepSeek 客户端
tests/                     18 个 unittest
data/recommendations/      推荐/跟踪/调度输出
cache/                     Parquet 行情缓存
deliverables/              文档/预览图"""
)

h2("B. 运行命令")
code_block(
    """.\\.venv\\Scripts\\activate
python app.py                 # 启动 Web，默认 7861
start_app.bat                 # 双击启动
start_scheduler.bat           # 独立自动推荐
python -m unittest discover -s tests -v
"""
)

doc.add_page_break()
h2("C. 接口权限与限制")
table(
    ["接口", "当前状态", "备注"],
    [
        ["daily / adj_factor / weekly / monthly", "可用", "复权与日线"],
        ["stk_mins / rt_min", "可用", "历史/实时分钟"],
        ["anns_d / report_rc / research_report", "可用", "公告/研报"],
        ["npr / news / major_news", "可用", "政策/新闻"],
        ["daily_basic", "可用", "市值/估值快照"],
        ["index_weight", "返回 0", "PIT 指数成分不可用；当前用市值前 N"],
        ["DeepSeek", "直连可用，代理回退", "仅解释层"],
    ],
    widths=[6.2, 3.2, 7.2],
    font_size=8.8,
)

h2("D. 术语")
table(
    ["术语", "含义"],
    [
        ["PIT", "Point-in-time，按历史时点可见的信息构建股票池/因子"],
        ["qfq", "前复权；以最新复权因子为基准"],
        ["NW", "Newey-West，重叠窗口下的稳健标准误"],
        ["block bootstrap", "按连续块重采样，保留时间相关性"],
        ["Walk-forward", "滚动训练/测试，检验样本外表现"],
        ["IC / RankIC", "因子值与未来收益的横截面相关（排名相关）"],
        ["as_of", "推荐/因子使用的数据截止交易日"],
        ["WF", "Walk-forward 的缩写"],
    ],
    widths=[3.4, 13.2],
    font_size=8.8,
)

p("", space_after=8)
p("— 文档结束 —", size=9, color=GRAY, align=WD_ALIGN_PARAGRAPH.CENTER)
p("本文件由当前代码与运行数据自动整理；量化结论仅代表截至 2026-11-02 的样本。", size=8.5, color=GRAY, align=WD_ALIGN_PARAGRAPH.CENTER)

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
doc.save(OUTPUT)
print("saved", OUTPUT.resolve())
