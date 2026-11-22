# PR 评审：评审：图表 spec 与渲染管线

> 自动生成于 2026-11-16T01:00:15.506Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261113-003`
- 计划项：`plan-20261107-005`
- 分支：`feat/agent-chart-pipeline` → `main`
- 轮次：第 6 轮（diff）
- 结论：通过（approve）
- 开发会话：`session-45206c8b-ff3f-4343-8869-4ba95568b063`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 2 / P2 1

## 本轮结论

第 6 轮 diff 评审（base 07f4af7 → fix/agent-chart-review-r5，本地 e65e9a0 / 远端 726bf06，PR #16）。逐条核对 r5 两条 P1：

1) iss-mwijghko-86154a 已实质修复：astock/agent/store.py:423-438 新增 _protected_digests = keep_digests ∪ 所有行 digest ∪ 所有 meta.png_path 的 digest（_path_digest :411-420 从路径段提取 64 hex），prune_artifacts 在 :483 与孤儿目录扫描 :516 均跳过 protected，不再删除仍被活跃 HTML 引用的 PNG。新增 tests/test_agent_store.py::test_prune_artifacts_keeps_png_referenced_by_live_artifact 与 ::test_prune_artifacts_keeps_keep_digests_png 覆盖“行已过期但 PNG 被引用/被 keep”的两条路径。
2) iss-mwijgl22-5e004b 已实质修复：docs/AStockAgent_interface_freeze_v1.md 恢复 v1 的 §3.4/§5/§7 正文（meta 以最新为准、time_point_scope、行按保留期删除），文件头 :9-12 明确“main 实现口径就是 v1 正文，v2 草案未实现”；未实现的 v2 内容拆到 docs/AStockAgent_interface_freeze_v2_draft.md 并标注“草案、未实现、目标 plan-20261114-002”。新增 test_freeze_v1_doc_stays_v1_and_v2_only_lives_in_draft 锁定边界。
3) r4 遗留的 P2 iss-mwfciyhq-d889ce 同步修复：store.py:250-272 _merge_artifact_meta 在 newest 无 png_path 且 existing 有时沿用旧 PNG、degraded=False、过滤“PNG 导出降级”告警；:275-308 record_artifact 先读旧 meta 再合并写回；新增 test_record_artifact_never_downgrades_success_meta。chart_render.py:49/1242-1244 复用 PNG_DEGRADED_WARNING_PREFIX，告警口径一致。

本轮新增 1 条 P2（不阻断）：iss-mwik3v22-213a0f——恢复 v1 时把 §4 已实现的 bbox 两分类语义删成单一 [x0,y0,x1,y1]，并把该已实现语义误标进 v2 草案。需在 v1 恢复表格 [row_start,col_start,row_end,col_end] 说明、从 v2 草案 §4 移除该段，并补文档断言。

结论：r5 的 2 条 P1 均已修复，代码与文档一致；本轮 openP0=0、openP1=0，P2 仅 1 条且不阻断，approve。完全解决条件（由图表模块开发执行）：a) 合并 PR #16 到 main 后，在 main 复跑 tests/test_agent_store.py、tests/test_agent_chart_spec.py、全量 pytest、mypy、architecture gate 与 CI，确认与 handoff 的 495 passed/2 skipped、mypy 35 files 一致；b) 在同一 PR 或紧随的文档 PR 修掉 iss-mwik3v22-213a0f 并补断言；c) plan-20261107-005 从 changes_requested 回 in_review，合并完成并复验后再置 done。上述 a/b 完成前，本线程虽 approve，plan-005 不能再次标 done；r5 两条 issue 需由开发在评审线程回写 fixed，否则工具侧仍显示 open，影响后续 resolved 判定。

## 本轮问题

### P2 · 恢复 v1 时把 bbox 的两类已实现语义删成单一 [x0,y0,x1,y1]，并把它误标成 v2 草案

- 位置：`docs/AStockAgent_interface_freeze_v1.md:262-263`
- 状态：未闭环
- 置信度：0.92

**证据**

当前 v1 只写 `"bbox": [x0,y0,x1,y1]|null`（docs/AStockAgent_interface_freeze_v1.md:262-263），删掉了原先“表格 bbox 为 [row_start,col_start,col_end,col_end]（1 基）、PDF 为 [x0,y0,x1,y1]”的区分。但 main 代码 astock/agent/files.py:17-18 明确工作表 chunk 的 bbox 是 `[row_start, col_start, row_end, col_end]`（1 基），PDF 表格才是 pdfplumber 的 `[x0,y0,x1,y1]`；files.py:803-823 也按该格式写入，tests/test_agent_files.py:373-375/426/468-469/534/585/617-645 均断言该格式。另一方面，新增的 docs/AStockAgent_interface_freeze_v2_draft.md:20-26 却把这段已在 main 实现的 bbox 语义整体标成“v2 草案”，等于把已实现契约降级为未实现草案，而 v1 正文丢失了该约束。注：v1:233 的“files.py 待建/未实现”是 07f4af7 之前就存在的陈旧状态，不计入本次。

**建议**

在 v1 §4 恢复 bbox 两分类原文：PDF 页面坐标 `[x0,y0,x1,y1]`（points，左上原点）、Excel/CSV/DOCX 表格坐标 `[row_start,col_start,row_end,col_end]`（1 基闭区间），文本块 `bbox=null`、表格块带 `cells` 与 `sheet`；并在 `extracted.jsonl` 行结构处写成 `"bbox": [x0,y0,x1,y1] | [row_start,col_start,row_end,col_end] | null` 或加一句指向该说明。同时从 docs/AStockAgent_interface_freeze_v2_draft.md §4 删掉这段（它已在 v1/main 实现，不属于 v2 草案），v2 草案只保留真正未实现的 DDL/meta 变更。补一条文档回归断言（可加在现有 test_freeze_v1_doc_stays_v1_and_v2_only_lives_in_draft 旁）：v1 含 `row_start` 与 `x0` 两种 bbox 说明，v2 草案不再重复声明 bbox 为草案。

## 历史未闭环问题

- P1 · r4 两条 store.py 问题被标 fixed，但代码零改动且 PR 已带 open P1 合并，prune_artifacts 仍会删除活跃 artifact 引用的 PNG · `astock/agent/store.py:432-452`（第 5 轮）
- P1 · 冻结清单 v1 被就地改成尚未实现的 v2 口径，与 main 上的 v1 代码及 plan-005 done 状态冲突 · `docs/AStockAgent_interface_freeze_v1.md:9-12, 223-226, 294-297, 357-358`（第 5 轮）

## 变更文件

- `astock/agent/chart_render.py`
- `astock/agent/chart_spec.py`
- `astock/agent/store.py`
- `astock/data.py`
- `docs/AStockAgent_chart_pipeline_decisions_v1.md`
- `docs/AStockAgent_contract_rule_map_v1.md`
- `docs/AStockAgent_interface_freeze_v1.md`
- `tests/test_agent_chart_png_export.py`
- `tests/test_agent_chart_render.py`
- `tests/test_agent_chart_spec.py`
- `tests/test_agent_store.py`
- `tests/test_data_pit_events.py`

## 交给开发会话

1. 在分支 `feat/agent-chart-pipeline` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
