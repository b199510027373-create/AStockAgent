# ADR-015 文件导入与检索：PDF / Excel / CSV / DOCX

- 状态：accepted
- 裁决：**accepted-with-conditions**（2026-11-10 架构契约评审门禁 plan-20261110-003）
- 日期：2026-11-07
- 来源：用户 2026-11-07「AI 对话中导入并读取 PDF 和 Excel 等文件」
- 关联：ADR-013（Agent 工具）、ADR-017（安全）

## 背景

用户需要把研报 PDF、财务 Excel、导出 CSV 直接丢进对话，让 Agent 读取、检索、汇总，并能基于表格数据画统计图。当前对话只检索 Tushare 事件库，没有文件入口。

## 决策

1. 支持格式（v1）：`.pdf`（文本层）、`.xlsx` / `.xls`、`.csv`、`.docx`、`.md` / `.txt`。扫描件 OCR、加密 PDF、宏（`.xlsm`）明确不支持，给出提示。
2. 上传约束：单文件 ≤ 20 MB，单轮 ≤ 5 个文件，PDF ≤ 200 页；超限拒绝并说明。
3. 解析管线 `astock/agent/files.py`：
   - PDF：`pdfplumber` 抽文本 + 表格；保留页码；
   - Excel：`openpyxl` / `pandas` 读所有 sheet，表头探测，单元格保留行列坐标；
   - CSV：编码探测（utf-8/gbk），分隔符探测；
   - DOCX：`python-docx` 抽段落与表格；
   - 统一产出 `ExtractedDoc {file_id, name, pages[], tables[]}`。
4. 存储（内容寻址，不可变）：
   ```text
   data/agent/files/<sha256>/
     original.<ext>
     extracted.jsonl      # 段落/页
     tables/<sheet>.parquet
     meta.json            # 类型、页数、表结构、上传时间
   ```
5. 索引：
   - 文本块入 SQLite FTS5（`file_chunks(file_id, page, chunk, text)`）；
   - 表格用 **DuckDB 只读**直接查 Parquet（`SELECT` only，强制 `LIMIT`），避免自己写 SQL 解析。
6. Agent 工具：
   - `file_list()`、`file_search(file_id, query, top_k)`、`file_read(file_id, page_from, page_to)`；
   - `table_schema(file_id, sheet)`、`table_query(file_id, sheet, sql)`（只读、限行、超时 5s）；
   - `file_summarize(file_id)`（长文分段摘要，带页码引用）。
7. **引用强制**：基于文件的回答必须给出 `文件名 + 页码/工作表/单元格范围`；无引用的结论标记为"未在文件中找到"。
8. 文件内容一律视为**数据**，不得当作指令执行（prompt injection 防护，见 ADR-017）。

## 预评审结论（2026-11-10）

本 ADR 对应代码尚未开工（`astock/agent/files.py` 不存在，`astock/agent/tools/` 不存在），本次只做接口级评审：
布局、索引、工具签名在 `docs/AStockAgent_interface_freeze_v1.md` §4 **逐字冻结**（目录、四件产物、
`meta.json` 15 个必填字段、`extracted.jsonl` 单行结构、`sheet_slug` 规则、FTS5 建表语句与触发器）。
冻结要点与原文的差异：

1. 增加 `file_id = "f_" + sha256[:16]`：目录仍以完整 sha256 命名（内容寻址），但对外引用用短 id，
   避免 UI 与工具参数里出现 64 位十六进制。
2. 原文的 `ExtractedDoc {pages[], tables[]}` 在落盘时拆成两处：`extracted.jsonl`（逐块、带 `bbox`/`cells`）
   与 `tables/<sheet_slug>.parquet`（结构化表格）。`ExtractedDoc` 仅作内存态返回类型，不是持久格式。
3. FTS5 原文只写字段名，冻结为显式 DDL + `content='file_chunks'` 外部内容表 + `ai/ad/au` 触发器，
   并要求 `tokenize='unicode61'`（中文按字切分可用，不引入 jieba 依赖）。列名收敛：原文的 `chunk`
   改名为 `block`（与 `extracted.jsonl` 的 `block` 字段对齐），并新增 `chunk_id` 主键与 `sheet` 列，
   使文本块能同时索引 PDF 页与 Excel 工作表。
4. 工具签名冻结（v1 不变）：`file_list()`、`file_search(file_id, query, top_k=5)`、
   `file_read(file_id, page_from=1, page_to=None)`、`table_schema(file_id, sheet)`、
   `table_query(file_id, sheet, sql)`、`file_summarize(file_id)`；权限标签统一 `file:read`。

## 条件

以下条件不阻塞 003/005（SQLite ledger / 时间语义）开工。按「建」（落盘与解析，plan-20261107-003）与「用」（检索与表格查询，plan-20261107-004）拆成两组，避免同一条件被两个计划项同时认领：

- **C1 布局与索引逐字一致（承接 plan-20261107-003）**：`files.py` 必须按冻结布局落盘（四件产物齐全），`meta.json` 15 个必填字段
  一个不缺，FTS5 建表语句与 `docs/AStockAgent_interface_freeze_v1.md` §4 字符级一致；FTS5 索引由 `files.py` 在抽取同批写入，
  不另起批处理；检索结果能回溯到 `file_id + page + block`。验收：`tests/test_agent_files.py::test_layout_matches_frozen_spec`。
- **C2 依赖与限额集中管理（承接 plan-20261107-003）**：`pdfplumber` / `python-docx` / `duckdb` 写进 `requirements.txt` 并给版本下限；
  20 MiB / 5 文件 / 200 页三个常量定义在 `files.py` 顶部，由工具层复用而不是各自硬编码；
  OCR/加密/宏/超限一律返回 `status` + `warnings`，不得抛未捕获异常。
- **C3 抽取质量自检与降级可见（承接 plan-20261107-003）**：解析后必须自检（页数 vs 文本块数、`text_chars`、表行列是否为空、
  空文本比例），异常时写 `meta.status = suspected_scan|empty_text|encrypted|unsupported` 并在 `warnings`
  给出可读提示；**禁止静默返回空文档**。验收：`tests/test_agent_files.py::test_scan_like_pdf_is_flagged`。
- **C4 检索与表格查询契约（承接 plan-20261107-004）**：`file_search` 命中必须能回查 `file_id + page + block`；
  `table_query` 只允许单条 `SELECT` + 强制 `LIMIT` + 5 秒超时，写语句/DDL 一律拒绝；DuckDB 只读连接不得落临时文件；
  文件内容进 prompt 必须包在不可信数据块内（ADR-017 §5）。验收：`tests/test_agent_files.py::test_table_query_rejects_write`。

## 字段与 bbox 订正（2026-11-14）

旧稿两处口径与冻结清单/实现不一致，按最新版本订正：

1. **meta 必填字段是 15 个**，不是 13：`file_id`、`sha256`、`name`、`ext`、`media_type`、`size_bytes`、`uploaded_at`、`pages`、`sheets`、`text_chars`、`chunks_count`、`parser`、`parser_version`、`status`、`warnings`。缺任一字段视为解析失败；`docs/AStockAgent_interface_freeze_v1.md` §4 的表已按 15 个列出。
2. **bbox 分两类，不得混用**：
   - PDF 页面坐标：`[x0,y0,x1,y1]`，单位 points，左上原点；
   - Excel / CSV / DOCX 表格坐标：`[row_start,col_start,row_end,col_end]`，1-based 闭区间；DOCX 的行号是源表格行号，Excel 需同时带 `sheet`。
   - 文本块 `bbox=null`；表格块必须落 `cells`（行×列）。冻结清单 §4 的 `extracted.jsonl` 单行结构同步注明两种坐标。

## 后果

- 正面：研报/财报可直接进对话；表格数据可参与统计图与因子对比；引用可核验。
- 负面：新增依赖（pdfplumber、python-docx、duckdb）；复杂版式 PDF 的表格抽取质量不稳定；大文件解析耗时。
- 质量策略：抽取后做一次结构自检（页数、表格行列、空文本比例），异常时提示"该 PDF 可能是扫描件"。
- 清理：文件与索引按 90 天保留，可配置（`AGENT_FILE_RETENTION_DAYS`）；删除会话不自动删文件（用户可手动清理）；
  GC 只删目录与索引行，不删 `agent_steps` / `agent_artifacts`（审计不断链，见 ADR-017 §8）。

## 非目标（v1 明确不做）

- OCR / 扫描件还原：只识别并提示 `suspected_scan`，不做识别；
- 加密 PDF、`.xlsm` 宏、压缩包、外部链接抓取；
- 让模型生成 SQL 后直接在 DuckDB 执行未受限语句：只允许单条 `SELECT` + 强制 `LIMIT` + 5s 超时。

## 证据

- 当前对话入口只检索 `events.py`，无文件上传路径；`astock/agent/files.py` 与 `astock/agent/tools/` 均不存在（2026-11-10 核验）
- 用户 2026-11-07 消息
- 项目已有 `pandas` / `pyarrow`，DuckDB 为新增轻量依赖
- 冻结布局：`docs/AStockAgent_interface_freeze_v1.md` §4
