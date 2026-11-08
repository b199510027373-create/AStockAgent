# ADR-015 文件导入与检索：PDF / Excel / CSV / DOCX

- 状态：proposed
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

## 后果

- 正面：研报/财报可直接进对话；表格数据可参与统计图与因子对比；引用可核验。
- 负面：新增依赖（pdfplumber、python-docx、duckdb）；复杂版式 PDF 的表格抽取质量不稳定；大文件解析耗时。
- 质量策略：抽取后做一次结构自检（页数、表格行列、空文本比例），异常时提示"该 PDF 可能是扫描件"。
- 清理：文件与索引按 90 天保留，可配置；删除会话不自动删文件（用户可手动清理）。

## 证据

- 当前对话入口只检索 `events.py`，无文件上传路径
- 用户 2026-11-07 消息
- 项目已有 `pandas` / `pyarrow`，DuckDB 为新增轻量依赖
