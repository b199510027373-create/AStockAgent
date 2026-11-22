# PR 评审：评审：文件检索 / 表格查询 + 强制引用

> 自动生成于 2026-11-17T13:26:04.426Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261117-002`
- 计划项：`plan-20261107-004`
- 分支：`feat/plan-20261107-004-file-tools` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-846420cf-30ed-483d-a5e4-5928a779f575`
- 本轮问题：2（P0 0 / P1 0 / P2 2）
- 线程未闭环：P0 0 / P1 0 / P2 3

## 本轮结论

第 3 轮（scope=full，提交 dfcd286 / 记录 b1b9799，PR #28 run 44091690423）复审结论：approve。P0=0、P1=0，本轮仅 2 条 P2 加固项（非阻断）。前两轮 9 条问题全部闭环且未回归。

一、上一轮 3 条逐条核实（均已在 dfcd286 修复并带测试）
- iss-mwkoyzkc-cf9ddb（P1 file_list 被误收口）：已修。astock/agent/runtime.py:58 `_CITING_FILE_TOOLS = FILE_TOOL_NAMES - {"file_list"}`，:75 `if step.tool in _CITING_FILE_TOOLS`；file_list 的 source 是 `files:local`（astock/agent/tools/file_tools.py:221），不命中 runtime.py:77 的 `startswith("file:")` 兜底，豁免成立。证据测试 tests/test_agent_runtime.py:777-792 `test_file_list_only_answer_is_not_rewritten`（断言 run.answer 原样保留）。
- iss-mwkoyzlk-7502ab（P1 正文引用污染白名单）：已修。runtime.py:82-109 `_collect_file_citations` 只取 `metadata["citations"]`、`metadata["citation"]` 与 table 行 `citation`，原 :103 的 `extract_citations(result.text)` 分支已删除。证据测试 tests/test_agent_runtime.py:794-809 `test_document_body_citation_is_not_trusted`（正文含《虚构.pdf》第 9 页 → answer == "引用：《公告.txt》第 1 页"，断言不含「虚构」）。
- iss-mwkoz2re-30598e（P2 TRIGGER_DDL 两次赋值）：已修（台账 status 仍显示 open，建议按 fixed 结案）。astock/agent/files.py:187-189 仅剩 `FILE_CHUNKS_TRIGGER_DDL = ""` 一次赋值，旧 ai/ad/au 脚本已删，仅保留 :182-185 `FILE_CHUNKS_LEGACY_TRIGGERS_DDL` 供迁移 drop；证据测试 tests/test_agent_tools.py:240-245 `test_trigger_ddl_is_empty_for_contentless_tokens_layout`（断言空串 + DDL 含 tokens/contentless_delete + 3 条 DROP TRIGGER）。

二、前两轮 6 条未回归（抽查确认）
- 运行时收口仍在：runtime.py:238-246 COMPLETED 前 `finalize_file_answer(answer, _collect_file_citations(steps))`；citations.py:193-213 白名单比对与「引用：…」重建逻辑未变。
- FTS5 中文：files.py:173-174 contentless tokens 表 + files.py:198-199 CJK bigram 常量仍在；tests/test_agent_tools.py:234 `assert found.metadata["backend"] == "fts5"`、:248 旧库迁移用例仍在。
- 默认装配：runtime.py:61-69 `_default_registry()` + :161 默认使用；tests/test_agent_runtime.py:703-730 集成用例仍在。
- table_query 计时器：file_tools.py:497 CREATE TEMP TABLE → :500 才启动计时器，:510-516 `with lock: state["done"]=True` → cancel → join(1.0) → close；:448-468 fire() 持锁检查 done；:507 文案「（表格装载不计入）」。安全侧未变：:498 `SET enable_external_access=false` 在用户语句前，sql_guard 与 table_path 校验未改。
- 文档同步（A10）与 15 字段/bbox 两类口径未变；tools/__init__.py 导出面无新增缺口。

三、验收 A1–A10 复核：A1/A2/A3/A4 现已端到端可达（默认注册表装配 + 运行时收口 + 白名单 + 无引用兜底）；A5 查询 <3s 口径与证据一致；A6 有 backend=fts5 命中与迁移测试；A7 15 字段与缺字段拒绝测试仍在；A8/A9 bbox 两类与 DOCX 源行号、Excel 多 sheet 引用测试仍在；A10 冻结清单/ADR-015 已与 contentless tokens 布局一致。

四、测试充分性：pytest tests/test_agent_tools.py tests/test_agent_runtime.py → 79 passed；全量 coverage run pytest → 655 passed / 5 deselected（68.01s）、TOTAL 77.51% ≥ 76.45；ruff / mypy（40 files）干净；架构门禁通过；PR #28 提交 dfcd286 的 CI（test、png-export）全绿。测试执行由开发负责，本轮未复跑，结论基于代码与测试静态证据。新增 3 个用例正好覆盖本轮 3 条修复路径，覆盖面无新增缺口。

五、本轮 2 条 P2（非阻断，可合并后跟进，均已在 issue 里给出具体改法）
- iss-mwkq6seh-07fa38（runtime.py:238-246）：零工具调用但回答自带「《X》第 N 页」时不进收口，凭记忆补写仍可落库；改法是条件加 `or has_file_citation(answer)`（file_list 清单回答不含页码，不会误伤豁免），并补一例 FakeModel。
- iss-mwkq6sfo-5ffa17（docs/AStockAgent_interface_freeze_v1.md:306 + docs/AStockAgent_contract_rule_map_v1.md:41）：r2 的 file_list 豁免与「正文不进白名单」未回写，且规则映射 :41 与 :103 自相矛盾；属文档回写遗漏，不影响代码行为。

六、完全解决判定与后续（明确责任与条件）
- 本轮 approve 后，plan-20261107-004 可由承担人合并 PR #28 并标 done；合并前不再改代码，合并后 planItem 置 done。
- 上述 2 条 P2 由 plan-20261107-004 承担人在合并后 3 个工作日内以一个小提交（代码 1 行 + 1 个测试 + 2 处文档）处理，或书面接受并登记到后续计划项；任选其一即视为完全解决。若选择修复，改完只需 PR #28 追加 CI 全绿，无需重新开评审轮次——除非改动超出这两条的范围（超出则必须开新一轮复审）。
- 本条线程不再要求开发重新提交 reviewer 复审；如后续有跨计划项的装配/行情引用改动（plan-20261107-005/-006），在新线程评审。

## 本轮问题

### P2 · 未调用任何文件工具但回答自带「《X》第 N 页」时不做收口，凭记忆补写的引用仍可直接落库

- 位置：`astock/agent/runtime.py:238-246`
- 状态：未闭环
- 置信度：0.8

**证据**

收口条件是「本轮用过会产生定位引用的文件工具」（runtime.py:240 `if _uses_file_tools(steps):`，判定见 runtime.py:72-79），因此「完全没调工具、直接凭记忆写页码」的路径不进收口。\n\n- astock/agent/runtime.py:238-246：`if reply.tool_call is None: answer = reply.content or \"（模型没有给出回答）\"; if _uses_file_tools(steps): answer = finalize_file_answer(...)`；steps 为空时 `_uses_file_tools` 为 False，回答原样写库；\n- astock/agent/tools/citations.py:203-206：若此时调用 finalize_file_answer，cites 为空会直接返回 CITATION_NOT_FOUND，即这条兜底完全有能力拦住，只是没被调用；\n- 复现：用户问「年报里营收增长多少？」→ FakeModel 第一步直接返回「营业收入同比增长 10%（《年报.pdf》第 2 页）。」且不发 ToolCall → run.answer 原样保留伪造定位，违反 AGENT-04「不得凭记忆补写文件内容」；\n- 测试缺口：本轮新增的 tests/test_agent_runtime.py:777-809 只覆盖「调用过 file_list / file_read」的路径，测试桩 `_runtime(...)` 里 FakeModel 第一个动作都是 ToolCall，没有「零工具调用 + 带引用回答」的用例。\n\n这是本线程最后一条同类缺口，且修复成本极低：`has_file_citation` 已存在（astock/agent/tools/citations.py:173-175），file_list 类清单回答（如「《年报.pdf》、《财务.xlsx》」）不含页码/工作表，不会被 `_CITATION_RE` 命中，因此不会误伤刚修好的 file_list 豁免路径。

**建议**

把 runtime.py:240 的条件改为 `if _uses_file_tools(steps) or has_file_citation(answer):`（`has_file_citation` 从 astock.agent.tools.citations 延迟导入，与 finalize_file_answer 同一行导入即可）。这样：① 有文件工具结果 → 走原白名单比对；② 没有任何文件工具结果但回答里出现页码/工作表引用 → 白名单为空 → 返回「未在文件中找到」，拦住凭记忆补写；③ file_list 清单类回答不含页码 → 不受影响。并在 tests/test_agent_runtime.py::FileCitationTests 补一例：FakeModel 只返回一条 `"营业收入同比增长 10%（《年报.pdf》第 2 页）。"`、不发 ToolCall，断言 `run.answer == "未在文件中找到"`。该条为非阻断的加固项，可随本轮合并前的小提交一起提交，或经评审书面接受后由后续计划项承接。

### P2 · r2 的两处收口语义未同步到冻结清单与契约规则映射，且规则映射内部自相矛盾

- 位置：`docs/AStockAgent_interface_freeze_v1.md:306`
- 状态：未闭环
- 置信度：0.85

**证据**

r2 改了两条收口语义，但文档仍写成「本轮用过文件工具时」不加区分，且契约规则映射里 AGENT-04 的证据行与实现不符。\n\n- docs/AStockAgent_interface_freeze_v1.md:306：「本轮用过文件工具时，最终回答在 COMPLETED 前经 finalize_file_answer() 收口——正文没有引用或引用不在工具结果中时回答「未在文件中找到」/仅保留可信引用」。但实现已把 file_list 排除（astock/agent/runtime.py:58 `_CITING_FILE_TOOLS = FILE_TOOL_NAMES - {"file_list"}`，:75 按它判断），且白名单只取结构化字段（runtime.py:82-109，正文 `extract_citations(result.text)` 分支已删）。按现文档，读者会认为「问有哪些文件」的回答也会被改写成「未在文件中找到」，与 tests/test_agent_runtime.py:777 `test_file_list_only_answer_is_not_rewritten` 相反。\n- docs/AStockAgent_contract_rule_map_v1.md:41（AGENT-04 行）：「现阶段：tests/test_agent_runtime.py::test_tool_result_trim_and_signature（字段透传）；工具级必填校验待 plan-20261107-005 / -006 补测试」——与本 PR 自己改过的同文档 :103「文件侧 tests/test_agent_tools.py::test_file_result_requires_source（已落地）」矛盾，也没提已落地的 tests/test_agent_runtime.py::FileCitationTests 两例与 test_document_body_citation_is_not_trusted。\n\n验收 A10 要求「冻结清单与 ADR-015 同步更新」，本轮 r2 改了实现与测试但没回写这两处表述，属同步遗漏；不影响代码行为，故记 P2。

**建议**

在同一提交里补两处：① docs/AStockAgent_interface_freeze_v1.md:306 改为「本轮调用过 file_search/file_read/table_schema/table_query（file_list 等清单类工具除外）时…；引用白名单只取工具返回的 metadata.citations / metadata.citation / table 行 citation，工具结果正文属不可信数据不参与白名单（AGENT-05）」，并补测试锚点 `tests/test_agent_runtime.py::test_document_body_citation_is_not_trusted`；② docs/AStockAgent_contract_rule_map_v1.md:41 的 AGENT-04 行证据更新为「tests/test_agent_tools.py::test_file_result_requires_source（工具层 source 必填）+ tests/test_agent_runtime.py::FileCitationTests（回答装配：无引用→未在文件中找到、白名单外引用→重建）」，并把「部分覆盖」限定为只剩行情侧 data_as_of/data_time（plan-20261107-005/-006）。改完在 PR #28 里追加一条 CI run 即可，无需改代码。

## 历史未闭环问题

- P2 · FILE_CHUNKS_TRIGGER_DDL 被两次赋值，遗留的旧 ai/ad/au 触发器脚本与新 contentless 表结构冲突 · `astock/agent/files.py:186-202`（第 2 轮）

## 变更文件

- `astock/agent/files.py`
- `astock/agent/runtime.py`
- `astock/agent/tools/__init__.py`
- `astock/agent/tools/citations.py`
- `astock/agent/tools/file_tools.py`
- `astock/agent/tools/sql_guard.py`
- `docs/AStockAgent_contract_rule_map_v1.md`
- `docs/AStockAgent_interface_freeze_v1.md`
- `tests/test_agent_runtime.py`
- `tests/test_agent_tools.py`
- `tests/test_ci_quality_gates.py`

## 交给开发会话

1. 在分支 `feat/plan-20261107-004-file-tools` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
