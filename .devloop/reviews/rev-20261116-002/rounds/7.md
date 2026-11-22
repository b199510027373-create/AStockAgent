# PR 评审：评审：复权口径收口：标准后复权基准 1.0 + 因子缺失硬失败 + 分钟复权标注

> 自动生成于 2026-11-20T10:52:45.064Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261116-002`
- 计划项：`plan-20261114-003`
- 分支：`fix/data-adj-caliber` → `main`
- 轮次：第 7 轮（diff）
- 结论：通过（approve）
- 开发会话：未关联
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 0 / P2 4

## 本轮结论

## 第 7 轮评审结论（scope=diff，c8d59ee..1c31bf4，代码提交 dd79e34）

### 范围与凭据
按开发交付 ho-20261120-007 复核第 6 轮唯一 P2（iss-mwojg80u-28e11e）。代码 diff 为 dd79e34（feat(market_tools)），1c31bf4 仅为交付/文档记录；仅 market_tools.py、tests/test_data_time_semantics.py、ADR-008 三个文件变动，未触碰已闭环的数据层与图注逻辑。测试执行归开发：引用其证据 30 passed（本文件）、全量 803 passed/1 skipped/18 subtests、ruff 全仓通过、mypy 44 files、CI 44690527676（head dd79e34）test+png-export success；并按其 handoff 记录的实跑（load_series 600519.SH qfq）核对三键可读。

### iss-mwojg80u-28e11e：逐点核对为 fixed
1. **字段与 metadata 输出**：market_tools.py:207-217 `MarketSeries` 增 `adj_base: str | None`、`base_factor: float | None`、`adj_source: str | None`（全部带 None 默认，位于既有默认字段之后，唯一构造点 :624-632 用关键字传参，无位置参数破坏）；:220-226 `metadata()` 固定输出三键。无「用 1.0 或今天伪造基准」的路径。
2. **取值来源与守门**：load_series :595-606 从 `frame.attrs["adjustment"]` 读取（该 attrs 由 data 层 `_apply_minute_adj`/`_fetch_daily` 写入，r1-r6 已闭环），缺失即 None；`base_factor` 用 `isinstance(factor_value, (int, float)) and not isinstance(factor_value, bool)` 守门，避免 bool 被当成因子；`adj_base`/`adj_source` 走 `str(...) if ... else None`，不产生假值。数据层对 NaN/inf/<=0 因子本就硬失败（ADR-008 第 3 条），故 `_validate_meta` 的 `allow_nan=False` 不会被触发。
3. **图表侧**：_render_kline :1107-1123 改为**始终**写 `spec["meta"] = {partial, factor_as_of, adj_base, base_factor, adj_source}`（不再只在 partial 时写），:1124-1131 的 chart meta 同步三键；`meta` 是 chart_spec 的自由对象（chart_spec.py:110、_validate_meta 只要求 Mapping + JSON 可序列化 + ≤1KB），不占 data 白名单，旧 spec 无该键时 `_caption` 走缺省，无回放兼容问题。
4. **测试**：test_kline_surfaces_partial_intraday_factor 的 `spec["meta"]` 改为完整字典等值断言（含三键）；新增 test_kline_metadata_exposes_adjustment_caliber（metadata 与 spec.meta 三键逐个断言）与 test_kline_metadata_without_adjustment_attrs_is_not_faked（无 attrs 时三键必须为 None，明确禁止 1.0/今天伪造）。用例对实现有约束力：删掉任一键或改成伪造值都会失败，不是空断言。
5. **文档**：ADR-008 第 6 条追加「落地位置（2026-11-20 评审 iss-mwojg80u-28e11e）」，写明 metadata()/kline meta 固定三键、ChartSpec.meta 携带、图注文本另由 chart_render._caption/charts._adj_caption 渲染，与代码一致，无超卖表述。

### 本轮新问题
0 条（P0=0、P1=0、P2=0）。前六轮 12 条 issue 至此全部在代码中闭环：r1-r4 数据层 7 条、r5 三条 P2（partial 消费、空帧 attrs、quarter）、r6 一条 P2（metadata 三键）本轮复核为 fixed。线程 openP0=0、openP1=0。

### 判定与收尾条件
- verdict=approve；线程按规则标记完全解决。
- 责任人：plan-20261120-001 开发在合并 PR #37 后复跑 main CI 并把 run 号回填 plan.links.test，随后把 plan-20261120-001 置 done（验收 1/2/3 均已满足，ADR-008 第 6/7 条已有落地位置与用例）。
- 完全解决判据（均已满足或仅剩合并动作）：metadata 三键可读且有用例守护（dd79e34 + 803 passed/CI 44690527676）、ADR-008 第 6 条注明落地、PR #37 合并后 main CI 绿。plan-20261114-003（本线程 planItem）保持 done。
- 不在本项范围（已在 ADR/交付记录中）：plan-20261103-009 板块/停牌/北交所与 IPO 首日规则仍 backlog；astock/ui.py:138「前复权」属 qfq-only 视图文案，非口径错标，可选 UI 小改。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P2 · 分钟盘中 partial/factor_as_of 已产出但全仓无消费方，ADR-008 第 7 条要的「盘中近似」提示至今不存在 · `astock/data.py:744-747`（第 5 轮）
- P2 · enrich / resample_ohlcv 空帧快速返回绕过了 attrs 回填，空帧仍丢复权标注 · `astock/data.py:841-842, 871-872`（第 5 轮）
- P2 · period_frame 标注用例只测 week/month，quarter 通道（QE）未覆盖 · `tests/test_data_adj_caliber.py:552-557`（第 5 轮）
- P2 · 行情工具/图表 metadata 仍缺 adj_base/adj_source，ADR-008 第 6 条只落地了 adj 与 partial · `astock/agent/tools/market_tools.py:207-224`（第 6 轮）

## 变更文件

- `astock/contracts.py`
- `astock/data.py`
- `tests/test_data_adj_caliber.py`
- `tests/test_data_pit_events.py`

## 交给开发会话

1. 在分支 `fix/data-adj-caliber` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
