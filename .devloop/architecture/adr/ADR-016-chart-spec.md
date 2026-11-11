# ADR-016 图表即工具产物：声明式 chart spec

- 状态：accepted
- 裁决：**accepted**（2026-11-10 架构契约评审门禁 plan-20261110-003；ChartSpec v1 冻结，任意 Python 转二期非目标）
- 日期：2026-11-07
- 来源：用户 2026-11-07「实时给出绘图：统计图、指定股票的实时 K 线与指标」
- 关联：ADR-013（Agent 工具）、ADR-008（价格口径）

## 背景

"想要什么图直接给"最常见的实现是让模型写 Python 并执行。这对本地单用户系统诱惑很大，但会带来三个问题：执行任意代码的安全风险、图与数据口径脱节、结果不可复现。

我们已经有成熟的绘图与数据层（`charts.py`、`data.py`、`indicators.py`、`factors.py`、`stats.py`），缺的是**一层让 Agent 安全表达"画什么"的规格**。

## 决策

1. 定义声明式 `ChartSpec`（`astock/agent/chart_spec.py`），字段：
   ```json
   {
     "version": 1,
     "kind": "candlestick | line | bar | scatter | heatmap | histogram",
     "title": "...",
     "data": {"source": "market | artifact | inline", "ref": "600519.SH", "range": "1y"},
     "encode": {"x": "trade_date", "y": "close", "color": "series"},
     "transforms": [{"op": "groupby|agg|filter|sort|limit", "...": "..."}],
     "layers": [{"type": "ma", "window": 20}, {"type": "boll"}],
     "annotations": [{"type": "hline", "y": 0}],
     "theme": "light"
   }
   ```
2. 校验：手写 schema 校验（不引入 pydantic 重依赖），字段/枚举/长度白名单；非法 spec 返回结构化错误给 Agent 重试。
3. 渲染 `astock/agent/chart_render.py`：spec → Plotly Figure → 自包含 HTML（`gr.HTML` 内嵌）+ 可选 PNG；图与数据快照一并落 artifact。
4. 行情类图（v1 必备）：
   - 周期：日/周/月 + 1/5/15/30/60 分钟；
   - 叠加：MA5/10/20/60/120/250、BOLL、成交量、MACD、RSI、KDJ；
   - 口径：默认 qfq；涨跌停/成交用 raw（ADR-008）；分钟用 `trade_time` 与 `rangebreaks`；
   - "实时"定义：`rt_min` 最新一根 + 30s 缓存，图上标注 `data_time`（见 ADR-018）。
5. 统计类图（复用现有计算，不重新发明）：
   - 净值/回撤/超额 → `backtest.py`；
   - IC 时序、分层收益、相关性 → `factors.py`；
   - 分布、置信区间 → `stats.py`；
   - 推荐跟踪净值 → `track.py`（计划项 004）。
6. v1 **不执行任意 Python**；`transforms` 只支持白名单算子（筛选/分组/聚合/排序/截断）。若用户问题超出白名单，返回"当前不支持，请改问法或等代码沙箱"。
7. 每个 artifact 记录 `{data_as_of, source, params, spec, code_version}`，回答中引用 artifact_id。

## 预评审结论（2026-11-10）

本 ADR 对应代码尚未开工（`astock/agent/chart_spec.py`、`chart_render.py` 不存在），本次只做接口级评审。
ChartSpec v1 在 `docs/AStockAgent_interface_freeze_v1.md` §3 **逐字冻结**，冻结时补齐了原文未定的边界：

1. `version` 必填且必须等于 `1`；`additionalProperties: false`，未知键直接拒绝（不做"忽略未知字段"的宽容解析，
   否则模型会持续输出幻觉字段而看不到错误）。
2. `data` 子结构补全：`source`（`market`/`artifact`/`inline`）、`ref`（个股/指数正则或 `art_<16 hex>`）、
   `rows`（inline ≤ 5000 行）、`fields`（inline 1–20 项）、`period`（`day` 默认，分钟周期仅允许 `market`）、
   `range`（枚举或 `YYYY-MM-DD:YYYY-MM-DD`，跨度 ≤ 10 年）、`adj`（`qfq` 默认；涨跌停/成交判断固定 `none`）。
3. `transforms` 白名单参数化：`filter{cmp ∈ gt/ge/lt/le/eq/ne/in}`、`groupby|agg{by 1..3 字段, metrics ∈ sum/mean/min/max/count/last}`、
   `sort{by, desc}`、`limit{n 1..5000, from ∈ head/tail}`；不支持 `join`/`pivot`/`window`——超界一律返回字段级错误。
4. `layers` 参数区间冻结：`ma{window 2..250}`、`boll{window 5..250, k 1..3}`、`volume{}`、`macd{fast 8..20, slow 21..60, signal 5..15}`、
   `rsi{window 2..60}`、`kdj{n 5..90, k 3, m 3}`；`annotations` 三种（`hline`/`vline`/`text`）共 ≤ 10 项。
5. 渲染返回值冻结：`render(spec) -> RenderedFigure{html, png_path, artifact_id, warnings}`；
   图题与角标必须同时展示 `data_as_of` 与 `data_time`，缺任一时点写 `时点未知`，**禁止只写"实时"**；
   无 `kaleido` 时 `png_path=None` 且不抛异常。
6. 数量上限：`transforms` ≤ 8、`layers` ≤ 8、`annotations` ≤ 10、`title` ≤ 120 字符、`meta` JSON ≤ 1 KB。
   上限存在的意义是让"拒绝"有确定边界，而不是事后限流。

## 非目标（v1 明确不做）

- **任意 Python 执行 / 代码沙箱**：二期项。若确有必要，只允许"子进程 + 无网络 + CPU/内存/时间限额 + 只读数据挂载"
  的方案，绝不在主进程 `exec`；届时需单独 ADR 并升 ChartSpec 版本。
- `join`/`pivot`/自定义指标脚本：不在 transforms 白名单内，超出即拒绝。
- 交互式图表（缩放/联动/下钻）：v1 输出静态 HTML。

## 后果

- 正面：安全、可复现、图与口径绑定；常见统计图/K 线一次到位。
- 负面：表达力不如代码解释器；复杂自定义图需要扩展算子。
- 扩展路径：先用新增白名单算子满足需求；确需通用计算时再走二期沙箱。
- 依赖：Plotly 已有；PNG 导出需 `kaleido`，作为可选依赖，缺失时只出 HTML。

## 证据

- `astock/charts.py`（K 线/区间图已存在）、`astock/indicators.py`（指标唯一实现）
- `astock/stats.py` / `factors.py`（统计量来源）、`astock/backtest.py`（净值/回撤）
- 用户 2026-11-07 消息
- 冻结规格：`docs/AStockAgent_interface_freeze_v1.md` §3；承接项 plan-20261107-005
