# PR 评审：评审：复权口径收口：标准后复权基准 1.0 + 因子缺失硬失败 + 分钟复权标注

> 自动生成于 2026-11-20T05:28:31.100Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261116-002`
- 计划项：`plan-20261114-003`
- 分支：`fix/data-adj-caliber` → `main`
- 轮次：第 6 轮（diff）
- 结论：通过（approve）
- 开发会话：未关联
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 0 / P2 4

## 本轮结论

## 第 6 轮评审结论（scope=diff，80cc765..c8d59ee，代码提交 ac5f853）

### 范围与凭据
按开发交付 ho-20261120-005 复核第 5 轮三条 P2。本轮代码 diff 在 80cc765..ac5f853（fix(agent,chart,data)），c8d59ee 仅为交付/文档记录；未触碰上一轮已关闭的 P1 路径（761e9fa 的分钟缺因子硬失败、query_all 分页、缓存 adj_base）。测试执行归开发，本轮不重复执行；引用 ho-20261120-005 证据：目标 3 文件 66 passed/16 subtests、全量 801 passed/1 skipped、门禁 33、ruff 全仓通过、mypy 44 files、CI run 44679937924（head ac5f853）success。

### 第 5 轮三条 P2：逐条核验为 fixed
1. **iss-mwoit6on-7d19f2（partial 无消费方）fixed**：market_tools.py:205-210 `MarketSeries` 增 `partial`/`factor_as_of` 字段，:220-224 `metadata()` 透出两键；load_series:584-594 从 `frame.attrs["adjustment"]` 读取并追加 notes（「盘中近似：…（截至 X）复权，收盘后因子发布才变为终值」）；_render_kline:1092-1098 把 `{partial, factor_as_of}` 写进 `ChartSpec.meta`；chart_render.py:743-747 `_caption` 与 charts.py:432-436 `_adj_caption` 据此追加「盘中近似（因子截至 X）」。测试 test_kline_surfaces_partial_intraday_factor 覆盖 attrs→metadata→spec.meta→图注全链路，published 用例断言不出现提示；`meta` 经 chart_spec.py:110/770-782 校验合法（Mapping+JSON+≤1KB），MarketSeries 唯一构造点用关键字参数，无位置参数破坏。
2. **iss-mwoit6pr-6c8c9f（空帧丢 attrs）fixed**：data.py 新增 `_empty_with_attrs()`（≈:835-846），resample_ohlcv 空帧分支（:852）与 enrich 空帧分支（:883）改用它；test_empty_frame_keeps_adjustment_attrs 对三个空帧路径断言 attrs 仍在，去掉修复即失败，非空断言。
3. **iss-mwoit6qf-174fb1（缺 quarter）fixed**：tests/test_data_adj_caliber.py 的 test_period_frame_keeps_adjustment_attrs 已遍历 ("week","month","quarter") 并保留 assertFalse(empty)+attrs 断言，与 data.py:882 的 QE 规则对齐。

附带核验：`load_series` 在 :521 `frame.copy()` 与 :524 sort/reset 之后 attrs 仍可达（新用例已实证）；astock/ui.py:138 的「前复权」写死属 qfq-only 视图（ui.py:129 入参固定 "qfq"、load_minute 默认 qfq），不是口径错标，开发列为可选 UI 文案可接受。

### 本轮新问题（1 条，P2，不阻断）
**iss-mwojg80u-28e11e**：ADR-008 第 6 条（ADR 文件 :35）要求「图表与行情工具的 metadata 暴露 adj/adj_base/adj_source」，但 market_tools.py:207-224 `metadata()` 仍只有 adjustment/label + 本轮新增的 partial/factor_as_of，缺 adj_base/base_factor/adj_source（load_series:587-589 的 adjust_meta 已具备）；图表侧 ChartSpec.data 白名单仅 "adj"（chart_spec.py:112-122），spec["meta"] 只带 partial/factor_as_of，基准日只存在于图注文本。后果是 Agent/LLM 读 ToolResult.metadata 无法回答 hfq 基准日/因子来源。建议见 issue：metadata() 补三键（缺省 None 不造假基准）、_render_kline 的 meta 两种情况都带三键、test_kline_surfaces_partial_intraday_factor 补三键断言与无 attrs 用例、ADR 第 6 条注明落地位置。

### 判定与责任人/收口条件
- verdict=approve；openP0=0、openP1=0（新 P2 不阻断）→ 线程按规则标记完全解决；第 5 轮三条 P2 在本轮闭环。
- 责任人与条件：plan-20261120-001 开发在合并 PR #37 前，二选一——(a) 顺手补齐 iss-mwojg80u-28e11e（约 3+3 行 + 2 条断言）并推送新 commit、CI 绿；(b) 若明确不在本项做，则把该条拆为独立计划项（如「行情工具/图表 metadata 暴露 adj_base/adj_source」）写进 plan.links 与 ADR-008 第 6 条，注明推迟项 id 与预期落地计划项。
- 何时算完全解决：选 (a) 时以「metadata 三键可读 + 用例通过 + ADR 第 6 条注明落地位置」为准；选 (b) 时以「计划项 id 已写入 ADR 与 plan.links，且验收⑥ 的表述不再声称 metadata 已含三键」为准。plan-20261120-001 的验收 1/2/3 已满足，可在 PR #37 合并且 CI 绿后置 done；plan-20261114-003（本线程 planItem）保持 done。

## 本轮问题

### P2 · 行情工具/图表 metadata 仍缺 adj_base/adj_source，ADR-008 第 6 条只落地了 adj 与 partial

- 位置：`astock/agent/tools/market_tools.py:207-224`
- 状态：未闭环
- 置信度：0.7

**证据**

ADR-008 第 6 条（.devloop/architecture/adr/ADR-008-price-adjustment-limits.md:35）明确要求「图表与行情工具的 metadata 暴露 adj/adj_base/adj_source」，但本轮改动的 market_tools.py:207-224 `MarketSeries.metadata()` 只输出 adjustment/adjustment_label/source/period/session/freshness/notes/partial/factor_as_of，缺 adj_base/base_factor/adj_source；同函数 load_series 在 :587-589 已经把 adjust_meta（含 adj、adj_base、base_factor、adj_source）从 frame.attrs 解析出来，数据就在手边。图表侧同样缺：ChartSpec.data 白名单只有 "adj"（chart_spec.py:112-122），本轮新增的 spec["meta"] 只带 {partial, factor_as_of}（market_tools.py:1092-1098），基准日只出现在渲染后的图注文本里（charts.py:418-431），结构化 metadata 拿不到。后果：Agent/LLM 读 ToolResult.metadata 时无法回答「这只票 hfq 的基准日/因子来源是什么」，而 hfq 的语义（基准 1.0 起于最早交易日）恰恰依赖 adj_base；ADR 第 6 条至今没有落地位置。

**建议**

最小改动补齐 ADR 第 6 条：1) `MarketSeries.metadata()` 增加 adj_base、base_factor、adj_source 三键，值取 load_series 已解析的 adjust_meta（缺省 None/""，不要给假基准）；2) `_render_kline` 的 spec["meta"] 不论 partial 与否都带同样三键（meta 是自由对象，无需改 data 白名单），旧 spec 无该键时图注已按缺省处理；3) 在 tests/test_data_time_semantics.py::test_kline_surfaces_partial_intraday_factor 里对 metadata 的 adj_base/base_factor/adj_source 各加一条断言，并补一条「无 attrs 时三键为 None 且不崩」；4) 落地后在 ADR-008 第 6 条注明位置（market_tools.MarketSeries.metadata + ChartSpec.meta）。

## 历史未闭环问题

- P2 · 分钟盘中 partial/factor_as_of 已产出但全仓无消费方，ADR-008 第 7 条要的「盘中近似」提示至今不存在 · `astock/data.py:744-747`（第 5 轮）
- P2 · enrich / resample_ohlcv 空帧快速返回绕过了 attrs 回填，空帧仍丢复权标注 · `astock/data.py:841-842, 871-872`（第 5 轮）
- P2 · period_frame 标注用例只测 week/month，quarter 通道（QE）未覆盖 · `tests/test_data_adj_caliber.py:552-557`（第 5 轮）

## 变更文件

- `astock/contracts.py`
- `astock/data.py`
- `tests/test_data_adj_caliber.py`
- `tests/test_data_pit_events.py`

## 交给开发会话

1. 在分支 `fix/data-adj-caliber` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
