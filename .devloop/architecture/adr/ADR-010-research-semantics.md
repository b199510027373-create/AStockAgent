# ADR-010 研究语义与「无 alpha」一等公民

- 状态：accepted
- 裁决：**accepted-with-conditions**（2026-11-10 架构契约评审门禁 plan-20261110-003）
- 日期：2026-11-03
- 来源：评审 §6；2026-11-02 评审会话（会话 74b9abee）

## 背景

产品当前最危险的状态不是"结论错了"，而是"结论自洽、产品自相矛盾"：

- 文档第五章写"alpha 未证明"，第四章却把 Top2、综合分 90.7、"关注/回避"做成了主界面；
- 平均超额表在 38 个重叠窗口上计算，用户会读成"跟着买能赚"；
- 页脚免责声明无法抵消主界面数字的暗示。

评审建议把"无显著 alpha"做成实时提示，把动作词改成研究语义，把对外唯一指标改成组合净值。

## 决策

1. 动作词使用研究语义：`关注/观察/回避` → `技术面强/技术面中性/技术面弱`；综合分同时给出池内分位（**候选池内** `rank(pct)`，如"候选池分位（N=15）：92%"）。
2. 「每日推荐」顶部固定展示未验证状态：实盘样本数、有效独立窗口、Top2−其余 的 p 值；数字由 ledger 实时计算，样本足够后自动消失。
3. 「推荐跟踪」首屏改为**按批次等权买入 Top5 的组合净值 vs 沪深300**；平均超额表降级到折叠区。
4. 所有对外文案禁止出现"建议买入/卖出"类表述，只描述技术面强弱与研究结论。

## 实现回写（2026-11-10）

ADR 原文的「后果/现状」已过期，实际实现已超出原描述（**不重写代码，只记录**）：

1. 动作词已改为研究语义：`recommend.py:264-270`（`_action(score)`，≥68 强 / ≥52 中性 / 其余弱）与
   `backfill.py:99` 一致；旧批次动作词由 `contracts.normalize_action` 做兼容映射。
   分位口径统一为**候选池分位**：`recommend.py:278-287`（`assign_percentiles`）按综合分截断到
   `candidate_n`（默认 15，上限 30）后在池内 `rank(pct)*100`，live 与 `backfill.py` 共用；展示带 N，
   见 `docs/AStockAgent_interface_freeze_v1.md` §6.4。分位不参与 `_action` 判定。
   批次 JSON 同时落盘池快照 `diagnostics.candidate_pool`（`[{code, composite}]`）与
   `diagnostics.candidate_n`，可从落盘文件复算分位（`table` 只含入选行，不能复算）。
2. 横幅已接 ledger 实时聚合，不再是静态提示：`recommend_ui.py:32-34 banner_markdown()` 调
   `use_cases.run_alpha_banner()`（`use_cases.py:574`），后者读 ledger 聚合；
   统计与状态机在 `track.py:251-445`：`alpha_banner` / `alpha_banner_snapshot` / `alpha_banner_markdown`，
   T+h 实盘样本、有效独立窗口、NW lag=h-1、p 值。统计状态共**五个**：
   `insufficient`/`unavailable`/`no_alpha`/`positive`/`negative`（`track.py:37`）；
   读库失败是独立的错误路径，走 `alpha_banner_error_markdown()`（`track.py:387`），不归入上述五态。
3. 组合净值已实现：「推荐跟踪」首屏为按批次等权 Top5 组合净值 vs 沪深300
   （`track.py:683+` 净值拼接、`track.py:945-994` Top5 vs 沪深300 图），平均超额表折叠在其下。
4. 验收证据：`tests/test_alpha_banner.py`（17 项，含"横幅每次读库"、"pending 未被隐藏为样本不足"、
   五状态各一句说明且静态提示不复述「样本不足/未验证」）、`tests/test_track_nav.py`（29 项，含净值、入场、停牌结转、状态文案）、
   `tests/test_research_semantics.py`（池快照复算与展示带 N）。
5. 对应已交付计划项：plan-20261103-004（commit `9b5b026`）、plan-20261103-005（PR #6 已合并 `e5b98e8`，
   评审 rev-20261110-002 approved）。

## 条件

以下两条不阻塞实现，但必须在后续项内完成，否则本 ADR 的「产品自洽」目标未达成：

- **C1 全站动作词与分位一致**：动作词的新语义只覆盖了推荐生成路径，简报、AI 对话、导出 CSV 等出口
  仍需逐处核查是否残留「关注/回避」，并确认综合分在展示处同时给池内分位。
  承接：plan-20261103-006（2026-11-19）。验收：全站检索无面向用户的建议类表述，且分位与综合分同屏。
- **C2 横幅五状态文案去重**：`insufficient/unavailable/no_alpha/positive/negative` 五状态已在统计层实现
  （`track.py:37`），但 UI 上与原有静态风险提示存在重复表述，且各状态缺少用户可读说明。
  承接：plan-20261103-006 的 UI 验收项。验收：同一屏幕只出现一处"未验证/样本不足"提示，
  五状态各有一句说明文案；读库错误路径（`alpha_banner_error_markdown`）单独展示、不冒充样本不足。

## 后果

- 正面：降低分数误用；产品叙事与统计事实一致；用户看到的是"跟随会怎样"而不是"平均超额的均值"。
- 负面：短期看"信息量变少/不够刺激"；横幅与净值依赖 ledger 聚合，读库失败时必须显式失败（`test_alpha_banner.py::test_database_error_propagates_instead_of_looking_like_no_sample`）。
- 现状：见「实现回写」；残留问题见「条件」。

## 证据

- `astock/recommend.py:264-270`（`_action(score)`）、`recommend.py:278-287`（`assign_percentiles` 候选池分位）、`astock/backfill.py:99`
- `astock/track.py:37`（五状态常量）、`track.py:251-445`（横幅统计）、`track.py:387`（读库错误路径）、`track.py:683+`（净值）、`track.py:945-994`（对比图）
- `astock/use_cases.py:574`（`run_alpha_banner`）、`astock/recommend_ui.py:32-34`
- `tests/test_alpha_banner.py`、`tests/test_track_nav.py`
- 评审 §6 三条产品建议
