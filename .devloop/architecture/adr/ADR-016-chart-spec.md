# ADR-016 图表即工具产物：声明式 chart spec

- 状态：proposed
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

## 后果

- 正面：安全、可复现、图与口径绑定；常见统计图/K 线一次到位。
- 负面：表达力不如代码解释器；复杂自定义图需要扩展算子。
- 扩展路径：若确有需要，二期加"受限 Python 沙箱"（子进程、无网络、CPU/内存/时间限额、只读数据挂载），而不是在主进程 `exec`。
- 依赖：Plotly 已有；PNG 导出需 `kaleido`，作为可选依赖，缺失时只出 HTML。

## 证据

- `astock/charts.py`（K 线/区间图已存在）
- `astock/indicators.py`（指标唯一实现）
- `astock/stats.py` / `factors.py`（统计量来源）
- 用户 2026-11-07 消息
