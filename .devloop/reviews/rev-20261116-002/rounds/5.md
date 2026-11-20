# PR 评审：评审：复权口径收口：标准后复权基准 1.0 + 因子缺失硬失败 + 分钟复权标注

> 自动生成于 2026-11-20T05:10:38.950Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261116-002`
- 计划项：`plan-20261114-003`
- 分支：`fix/data-adj-caliber` → `main`
- 轮次：第 5 轮（diff）
- 结论：通过（approve）
- 开发会话：未关联
- 本轮问题：3（P0 0 / P1 0 / P2 3）
- 线程未闭环：P0 0 / P1 0 / P2 3

## 本轮结论

## 第 5 轮评审结论（scope=diff，main..HEAD=80cc765）

### 范围确认
- 实际 diff 只有 4 个文件：astock/charts.py、astock/data.py、tests/test_charts_summary.py（新增）、tests/test_data_adj_caliber.py；用户列出的 astock/contracts.py 与 tests/test_data_pit_events.py 与 main 无差异，本轮无可评审内容（对应验收③④⑤ 的既有覆盖仍由 test_data_adj_caliber.py 的 25 用例承担）。
- 分支说明：线程与 plan.links.branch 仍记 fix/data-adj-caliber，该分支已合并（PR #15/196cfb2）；本轮实际评审 fix/adj-meta-display（PR #37/4b74337 + 80cc765 docs）。建议同步 plan.links.branch 与线程 branch，避免后续轮次误判 base。

### 前序问题复核（7/7 已闭环）
r1 两条 P1（分钟盘中硬失败、adj_factor 非分页截断）、r1/r2 三条 P2、r3 P1（死覆盖断言）均已在 code 中消失：分钟路径改为 partial 因子 + 缺因子硬失败（data.py:717-747），日线覆盖校验只在 _check_factor_coverage，缓存带 adj_base。本轮 diff 未回退这些行为。

### 本轮变更判定
- astock/data.py:862-865、876-877 在 resample_ohlcv / enrich 尾部整体回填 df.attrs，与 chart_render.py:516/1542-1543、chart_service.py:299-302、track.py:858-859 的既有 attrs 约定一致；backtest.run_strategy（backtest.py:429-444）返回 enrich(daily) 后即可携带 adj/adj_base/base_factor/adj_source，验收⑥ 的「标注活着走到展示层」主链已闭合。
- astock/charts.py:404-432 _adj_caption 区分 qfq/hfq/none/未知，hfq 标「基准 X 起 1.0」、qfq 标基准日+因子、none 隐藏基准，且优先读 attrs 保证与数据层同源；tests/test_charts_summary.py 6 个用例覆盖了「hfq 不得标不复权」这一核心错误模式与 summary_markdown 真实输出。
- 结论：0 P0、0 P1，本轮 approve；plan-20261114-003 的验收①–⑤ 与 ⑥ 的口径区分部分完全解决。

### 未闭环 P2（不阻断 resolved，需由开发在后续小 PR 修）
1. iss-mwoit6on-7d19f2（astock/data.py:744-747）分钟盘中 partial/factor_as_of 全仓无消费方，「盘中近似」提示未落地（ADR-008 第 7 条为「下游可提示」性质，故记 P2 不阻断）。责任人：plan-20261114-003 开发。二选一：a) chart_render._caption 与 charts._adj_caption 追加「盘中近似（因子截至 X）」+ market_tools.MarketSeries.metadata 增 partial/factor_as_of + 一条 partial=True 用例；b) 拆独立计划项并在 ADR-008 第 7 条注明推迟。判完全解决条件：a 落地且测试通过，或 b 的计划项 id 写进 ADR 与 plan.links。
2. iss-mwoit6pr-6c8c9f（astock/data.py:841-842、871-872）enrich/resample_ohlcv 空帧快速返回丢 attrs，空区间回测/图注退回调用方 adj。判完全解决条件：两个空帧分支补 attrs 回填 + test_empty_frame_keeps_adjustment_attrs 通过。
3. iss-mwoit6qf-174fb1（tests/test_data_adj_caliber.py:552-557）period_frame 用例缺 quarter（data.py:882 有 QE 规则）。判完全解决条件：subTest 覆盖 ("week","month","quarter") 且断言不弱化。

### 测试充分性
开发交付证据（ho-20261120-001/002）：目标 4 文件 99 passed、全量 796 passed/1 skipped、门禁 33、ruff + mypy 44 files 全绿、CI 44573239310（head 4b74337）success、PR #37 OPEN/CLEAN。本轮不重复执行测试（执行归开发），仅核对覆盖点：新增用例覆盖 attrs 三条传播路径与真实 load_daily→enrich 链，未覆盖空帧与 quarter，已按 P2 记录。

### 判定
verdict=approve，openP0=0、openP1=0 → 工具按规则将 plan-20261114-003 标为完全解决。3 条 P2 为延后项，若不修则「盘中近似提示」保持缺失、空区间标注缺失、季线标注通道无回归保护；修复以 3 条 issue 各自列出的判据为准。

## 本轮问题

### P2 · 分钟盘中 partial/factor_as_of 已产出但全仓无消费方，ADR-008 第 7 条要的「盘中近似」提示至今不存在

- 位置：`astock/data.py:744-747`
- 状态：未闭环
- 置信度：0.7

**证据**

data.py:744-747 写入 meta["partial"]=True 与 meta["factor_as_of"]，但 astock/ 下检索 factor_as_of|partial 只命中 data.py 自身（696-697、717-718、734-735、744-747）；展示侧三处都不读：charts.py:404-432 `_adj_caption` 只拼 adj/adj_base/base_factor/adj_source，astock/agent/chart_render.py:680-687 `_adj_label` 只按 qfq/hfq/none 给标签，astock/agent/tools/market_tools.py:207-219 `MarketSeries.metadata` 无 partial/factor_as_of。验收⑥ 已由 ho-20261120-002 声明具备收口条件，而 ho-20261120-001 的 next 自认「分钟盘中 partial/factor_as_of 尚无下游消费」。后果：盘中分钟图用昨日因子复权的最后一根 K 线，在图上与历史 bar 无差别呈现，用户无法得知口径是近似的。

**建议**

二选一并落文档：(a) 补消费方——chart_render._caption（分钟图）与 charts._adj_caption 读 meta.get("partial")/meta.get("factor_as_of")，追加「盘中近似（因子截至 <factor_as_of>）」，market_tools.MarketSeries.metadata 增加 partial/factor_as_of 两键，并在 tests/test_charts_summary.py 或 test_data_adj_caliber.py 增加 partial=True 用例；(b) 若本轮不做，把该条拆成独立计划项（如 plan-xxxx 分钟盘中标注），在 ADR-008 第 7 条注明「推迟到该计划项」，同时不在验收⑥ 里声称已覆盖。推荐 (a)，改动量约 10 行。

### P2 · enrich / resample_ohlcv 空帧快速返回绕过了 attrs 回填，空帧仍丢复权标注

- 位置：`astock/data.py:841-842, 871-872`
- 状态：未闭环
- 置信度：0.7

**证据**

resample_ohlcv 在 :841-842 `if df.empty: return pd.DataFrame()`，enrich 在 :871-872 同样直接 `return pd.DataFrame()`，两处都早于本轮新增的 `out.attrs.update(df.attrs)`（:862-865、:876-877）。本轮测试只断言非空帧（tests/test_data_adj_caliber.py:542-557 全用 _daily_frame() 且断言 assertFalse(out.empty)），所以遗漏未被发现。影响：选到停牌区间/无数据区间时，回测返回帧与图表图注退回调用方 adj 参数、丢失 adj_base/base_factor，标注在空数据场景下失效；charts.summary_markdown 的空帧分支直接返回「⚠️ 无数据」，也无口径可显示。

**建议**

两个空帧分支改为保留 attrs，例如 `empty = pd.DataFrame(); empty.attrs.update(df.attrs); return empty`（enrich 保持现有空返回列语义则不复制 df 列，只带 attrs）；在 AdjustmentMetadataPropagationTests 增加 `test_empty_frame_keeps_adjustment_attrs`，用 `df.iloc[0:0].copy()`（attrs 已设）分别过 enrich 与 resample_ohlcv 并断言 attrs 仍在。

### P2 · period_frame 标注用例只测 week/month，quarter 通道（QE）未覆盖

- 位置：`tests/test_data_adj_caliber.py:552-557`
- 状态：未闭环
- 置信度：0.7

**证据**

test_period_frame_keeps_adjustment_attrs 的 subTest 只遍历 ("week", "month")（tests/test_data_adj_caliber.py:553），而 data.py:882 的 rules 明确包含 "quarter": "QE"，走的是与 week/month 相同的 resample_ohlcv+enrich 链；一旦有人改动 quarter 规则或这条路被旁路，现有用例不会失败。ADR-008 落地第 1 条与图表「周/月/季」口径都覆盖季线，缺一条即为通道漏测。

**建议**

把遍历改为 ("week", "month", "quarter") 并保留 assertFalse(out.empty) 与 attrs 断言；若季度在 _daily_frame() 数据窗（约数日）内无足够数据导致空帧，则改用跨季的固定帧或直接断言 attrs 而非非空，避免为了通过而弱化断言。

## 变更文件

- `astock/contracts.py`
- `astock/data.py`
- `tests/test_data_adj_caliber.py`
- `tests/test_data_pit_events.py`

## 交给开发会话

1. 在分支 `fix/data-adj-caliber` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
