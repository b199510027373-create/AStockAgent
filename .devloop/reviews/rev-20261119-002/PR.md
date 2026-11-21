# PR 评审：评审：图表管线 v2：语义收口、审计迁移与 Q4/Q5

> 自动生成于 2026-11-20T05:07:30.677Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261119-002`
- 计划项：`plan-20261114-002`
- 分支：`feat/chart-pipeline-v2` → `main`
- 轮次：第 3 轮（targeted）
- 结论：通过（approve）
- 开发会话：`session-7f3600bf-2abb-44b4-a4c7-372e79943be7`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 0 / P2 0

## 本轮结论

第 3 轮 targeted 复核通过。第 1 轮 6 条问题均已由开发在 89572cb 修复并在 547d737 收口边角，逐条核验结果：P0 iss-mwnlpfyt-26f425（日/周/月线指标为空）已改为仅分钟周期走 continuous_bars，并新增 daily/weekly/monthly MA20 数值对照测试；P1 iss-mwnlpizd-c9feed（Agent 出图未 link run）已由 ToolContext 贯穿 registry/runtime/market_tools，端到端测试断言 link=(run_id, step, source='agent')；P1 iss-mwnlpll3-c475bb（自建 running run）已删除 ensure_run，改为缺失/非 agent run fail-fast；P2 iss-mwnlpnd5-4be737 死代码已删；P2 iss-mwnlppp6-b47e47 已改为读取并校验库内 meta_digest，篡改抛 ArtifactReplayUnavailable；P2 iss-mwnlpsvc-3f5905 已按 aggregated/none/unknown 三态归因。第 2 轮边角 include_plotlyjs bool 回放已在 chart_service.py:311-318/393 修复，tests/test_agent_chart_render.py:1512-1532 验证 False 不被字符串化且不引 CDN。PR #36 已 squash merge 为 0e72ea4，main CI run 44558699458 双 job pass，main 本地 781 passed/5 deselected/12 subtests；plan-20261114-002 已置 done，6 条 issue 全部 fixed/effective，openP0=0/openP1=0/openP2=0。非阻断备注：.devloop/architecture/adrs.json 的 ADR-018 summary 句首仍保留旧措辞「data_time 无生产者（冻结 latest_bar_time 唯一入口）」，其后已追加落地说明；属文档措辞残留，不影响实现与验收，可由开发在后续文档清理时顺手修正，不阻塞本次完全解决。

## 本轮问题

本轮未记录问题。

## 变更文件

- `astock/agent/chart_render.py`
- `astock/agent/chart_service.py`
- `astock/agent/chart_spec.py`
- `astock/agent/chat.py`
- `astock/agent/registry.py`
- `astock/agent/runtime.py`
- `astock/agent/store.py`
- `astock/agent/tools/market_tools.py`
- `astock/data.py`
- `astock/stats.py`
- `docs/AStockAgent_chart_pipeline_decisions_v1.md`
- `docs/AStockAgent_interface_freeze_v1.md`
- `docs/AStockAgent_interface_freeze_v2.md`
- `docs/AStockAgent_interface_freeze_v2_draft.md`
- `tests/test_agent_chart_png_export.py`
- `tests/test_agent_chart_png_zh.py`
- `tests/test_agent_chart_render.py`
- `tests/test_agent_chart_spec.py`
- `tests/test_agent_runtime.py`
- `tests/test_artifact_admin.py`
- `tests/test_ci_quality_gates.py`
- `tests/test_data_pit_events.py`
- `tests/test_data_time_semantics.py`
- `tools/artifact_admin.py`

## 交给开发会话

1. 在分支 `feat/chart-pipeline-v2` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
