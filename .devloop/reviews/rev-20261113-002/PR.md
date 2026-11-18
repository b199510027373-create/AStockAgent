# PR 评审：评审：文件导入解析：PDF / Excel / CSV / DOCX

> 自动生成于 2026-11-13T08:23:13.673Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261113-002`
- 计划项：`plan-20261107-003`
- 分支：`feat/agent-file-ingest` → `main`
- 轮次：第 2 轮（full）
- 结论：通过（approve）
- 开发会话：`session-3100e9bd-3b63-4022-9649-c18a3f5278f8`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 3 / P2 4

## 本轮结论

第 2 轮（scope=full，rulesVersion v1.1）复核 2531119「fix(agent): 评审 P1/P2 修复」及后续 499f60b/c400343/6987a7a，逐条核对第 1 轮 6 条问题：

1) P1 iss-mweouh8f-6c3d2e（前导零被 float 篡改）→ fixed：files.py:926-949 `_coerce_column` 引入 `_LEADING_ZERO_RE = re.compile(r"^0\d")`，命中即整列保留原始值并返回 warning「含前导零标识符，已按文本保留…」；_add_sheet files.py:776-783 逐列收集并追加工作表名，warning 进入 meta.warnings。测试：CSV 用例补 `frame["代码"].tolist() == ["600000","000001"]` 与 warning 断言，xls 用例断言代码列为 ["000001","600000"]；48 passed 覆盖。
2) P1 iss-mweoulp2-c72028（load_table 越权）→ fixed：files.py:1315-1330 先用 `_SHEET_SLUG_RE = re.compile(r"[a-z0-9_]{1,48}")` fullmatch 白名单，再 `resolve()` + `is_relative_to(tables_dir)` 双保险；新增 test_load_table_rejects_traversal 覆盖 "../../x"、"../meta"、"C:/Windows/win.ini"、"..\\..\\escape"、"sheet/../sheet" 五个向量，均抛 ValueError。
3) P1 iss-mweov3d3-db2784（XLS 无真实夹具）→ fixed：新增真实 BIFF8 夹具 tests/fixtures/legacy_two_sheets.xls（5632B，两工作表）与生成脚本 build_legacy_xls.py；_parse_excel files.py:573-583 显式传 engine="openpyxl"/"xlrd"；test_legacy_xls_is_parsed_with_coordinates 断言 parser=="xlrd"、sheets 名与 slug、rows/cols/header_row、bbox [[2,1,2,3],[3,1,3,3]]、load_table 形状与代码列。
4) P2 iss-mweov5r6-6cc726（临时文件只用 pid）→ fixed：files.py:1202-1210 tmp 名改为 `.{name}.tmp-{pid}-{uuid4().hex}`，finally `unlink(missing_ok=True)`；新增 test_atomic_write_failure_leaves_no_tmp_file 断言失败后无 meta.json 且无 .tmp-* 残留。
5) P2 iss-mweovi84-c000f9（索引降级无测试）→ fixed：新增 test_index_failure_does_not_block_ingest，monkeypatch index_chunks 抛 sqlite3.OperationalError，断言 status==ok、warning 含「索引」、meta 15 键、iter_chunks 与 chunks_count 一致，符合「不静默成功、不影响落盘」的契约意图。
6) P2 iss-mweoveax-3cb396（锚点名/13→15/bbox 语义）→ 部分 fixed：test_layout_matches_frozen_spec（parametrize txt/csv/xlsx/xls/docx/pdf）与 test_scan_like_pdf_is_flagged 已存在并跑通；ADR-015「13 个必填字段」→15 与 §4 中 Excel 复用 bbox 承载行列坐标的语义说明仍待订正，开发 handoff 明确转为架构文档任务、要求在 plan-20261107-004 开工前完成——作为文档漂移 defer 接受，不影响本轮结论。

本轮新增 1 条 P2（不阻断）：iss-mweply8i-fc899f —— 前导零分支返回未归一化的 object 列（files.py:936-944），Excel 列同时含文本代码与数值单元格时 parquet dtype 不稳定。请开发在 plan-20261107-004 表格查询联调前按建议 `.map(...)` 归一化为文本并补混合单元格用例。

测试充分性：本轮改动触及解析器、落盘原子性、路径校验、索引降级四条风险路径，均有对应测试；证据链为 48 passed（两套解释器）、全量 368 passed 1 skipped、ruff/mypy 通过、coverage 73%、CI run 43391713138 与 43391962183 success，与 2531119 后的代码一致（requirements.txt 未改）。参数化布局测试通过 _patch_pdf/_FakePage 替身而非真实 PDF 二进制，属既有约束下的合理选择，不另记问题。

结论：第 1 轮 3 条 P1 全部 fixed，P2 已修 3/4、剩余 1 条转架构文档订正（有责任人与截止条件）；本轮 openP0=0、openP1=0，verdict=approve。完全解决的判定条件：(a) 架构在 plan-20261107-004 开工前完成 ADR-015「13→15」与 §4 Excel bbox 语义订正；(b) 开发在 plan-20261107-004 开工前处理 iss-mweply8i-fc899f（含混合单元格测试）。这两项均为 P2，不改变本轮 approve；修正后本线程即视为完全解决，无需再开新轮。

## 本轮问题

### P2 · 前导零保护分支返回未归一化的 object 列，混合类型列落 parquet 时类型不稳定

- 位置：`astock/agent/files.py:936-944`
- 状态：未闭环
- 置信度：0.75

**证据**

files.py:936-944 命中 leading_zero 后直接 `return series, warning`，series 仍为 `pd.Series(list(values), dtype=object)`；而 _normalize_cell（files.py:841-855）对 Excel 数值单元格返回 int、对文本单元格返回 str，因此「代码」列若同时含文本 "000001" 与数值 600000，该列就是 object 混合类型列。_write_tables 用 `frame.to_parquet(...)` 落盘，pyarrow 对 int/str 混合的 object 列无法推断统一类型（ArrowInvalid），异常会被 _ingest 的兜底捕获成 status=unsupported——即修正前能导入（虽被 float 篡改）、修正后整份文件导入失败；即使 stub/未来版本能写出，列 dtype 也会随首行类型变化，与同批文本列的 schema 不一致。当前测试只覆盖全文本列（CSV 用例 frame["代码"] 全为字符串；test_legacy_xls_is_parsed_with_coordinates 的 xls 夹具两行都是文本），没有混合单元格用例暴露该路径。

**建议**

在 leading_zero 分支显式归一化为文本列，例如 `return series.map(lambda v: "" if v is None else str(v)), warning`（或 `series.astype("string")`，注意 None/NaN 语义），使 parquet 列 dtype 恒为 string、与 extracted.jsonl 文本块一致；同时在 tests/test_agent_files.py 用 openpyxl 造一份「代码」列同时含 "000001"（文本）与 600000（数值）的 xlsx，断言 status==ok、load_table 该列 == ["000001", "600000"]、warnings 含前导零提示。该条为 P2，不阻断本轮 approve，但请随 plan-20261107-004 表格查询开工前处理。

## 历史未闭环问题

- P1 · 数值列强转 float 会把股票代码等前导零标识符改坏（000001 → 1.0），且无告警 · `astock/agent/files.py:911-920`（第 1 轮）
- P1 · load_table 的 sheet_slug 未做白名单/归属校验，可路径穿越或绝对路径逃逸出 data/agent/files · `astock/agent/files.py:1282-1287`（第 1 轮）
- P1 · XLS 解析只有"缺 xlrd 报错"的负向测试，没有真实 .xls 通过性证据，验收项「XLS 可解析」未闭合 · `tests/test_agent_files.py:454-467`（第 1 轮）
- P2 · 原子写临时文件名只用 pid，同进程并发导入同一 sha256 时会共用 tmp 路径 · `astock/agent/files.py:1173-1177`（第 1 轮）
- P2 · ADR-015 的验收锚点测试名不存在、字段数写 13（实际 15）、Excel bbox 语义未入契约，文档与实现漂移 · `tests/test_agent_files.py:162-174`（第 1 轮）
- P2 · 索引不可用降级路径（index_chunks 抛错只告警）无测试，ADR-015 C2 的"不阻断导入"没有可执行证据 · `tests/test_agent_files.py:396-430`（第 1 轮）

## 变更文件

- `astock/agent/files.py`
- `requirements.txt`
- `tests/fixtures/build_legacy_xls.py`
- `tests/fixtures/legacy_two_sheets.xls`
- `tests/test_agent_files.py`

## 交给开发会话

1. 在分支 `feat/agent-file-ingest` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
