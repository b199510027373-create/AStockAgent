# ADR-018 「实时」行情口径与 Agent 数据时点

- 状态：accepted
- 裁决：**accepted-with-conditions**（2026-11-10 架构契约评审门禁 plan-20261110-003）
- 日期：2026-11-07
- 来源：用户 2026-11-07「需要谁的实时 K 线、什么指标可以直接给出」
- 关联：ADR-005（时间语义）、ADR-016（图表）

## 背景

"实时"在 A 股语境里有三种完全不同的东西：逐笔/tick、分钟快照、收盘日线。用户说"实时 K 线"，实际想要的多半是**盘中能看到的最新一根分钟 K 线 + 指标**，而不是付费 tick 流。

如果不把口径写死，Agent 会把"今天 10:31 的分钟线"和"昨天收盘的日线"混在一张图里，重演 ADR-005 的前视问题。

## 决策

1. 术语与来源：
   - **盘中快照**：`rt_min` 最新一根（1/5/15/30/60 分钟），30 秒内存缓存；
   - **历史分钟**：`stk_mins`，按交易日分块，补当日 `rt_min`；
   - **日线**：收盘后更新，`data_as_of` = 最后一根有效日线日期。
2. 每个行情结果必须携带两个时间：
   - `data_as_of`：数据所属交易日；
   - `data_time`：最后一根 bar 的时间戳（分钟级精确到分钟）。
   图表标题/角标同时展示两者；禁止只写"实时"。
3. 交易时段判定：用 `trade_cal` + 当前时间；非交易时段标注"最近交易日收盘"，不伪造"实时"。
4. 新鲜度与降级：
   - `rt_min` 失败或超过 2 分钟未更新 → 标记 `delayed`，回退到最近 `stk_mins`/日线，并在图与回答中提示；
   - 停牌股票返回最后一根并标注"停牌/无成交"。
5. 指标口径：
   - 分钟指标在分钟序列上计算，不把日线指标硬套到分钟图；
   - 叠加日线 MA 到分钟图时，用 `ffill` 映射，禁止未来值；
   - 默认 qfq；涨跌停/成交判断用 raw（ADR-008）。
6. "实时"不是 tick：不承诺逐笔、不承诺交易所 Level-2；若未来接入 tick，需单独 ADR 与数据授权。
7. Agent 回答中每个数字/图都带 `data_as_of + data_time`；没有时点的行情结论视为不合规。

## 预评审结论（2026-11-10）

对照 `astock/data.py` 与 `astock/agent/types.py` 的实际实现，发现**接口是半成品**：

1. 字段已在 `ToolResult` 中实现（`types.py:90-91`），`to_dict()` 也会输出；`data_as_of` 已有多个使用方
   （tool/runtime/recommend/ledger/track/use_cases）。
2. **`data_time` 没有任何生产者**：全库检索不到为它赋值的代码路径。也就是说 §2 的强制要求目前无法被满足，
   Agent 一旦开始出图就会出现"时点为 None"。这是本条 ADR 的唯一实质缺口。
3. `data.py` 的取数能力已具备：`load_minute`（`rt_min` 当日 30s 缓存，常量 `_MINUTE_TTL = 30.0`；历史
   `stk_mins` 300s 缓存；`days <= 1` 走 `rt_min`）、`load_daily`、`load_index_daily`、`resample_ohlcv`、
   `enrich`、`period_frame`。缺的是"从 DataFrame 产出标准时点字符串"的统一函数。

因此需要冻结一个唯一生产者，避免每个工具各写一套 `strftime`：

```python
# astock/data.py
def latest_bar_time(df: pd.DataFrame, *, freq: str = "D") -> tuple[str, str]:
    """返回 (data_as_of, data_time)，全局唯一的时点格式化入口。"""
```

`freq ∈ D/W/M/1m/5m/15m/30m/60m`，与 `ChartSpec.data.period` 同构；空 DataFrame 抛 `ValueError`
（不允许返回空串骗过必填校验）。

语义冻结（`docs/AStockAgent_interface_freeze_v1.md` §6）：

- `data_as_of` 为 `YYYY-MM-DD`，取最后一根有效 bar 所在交易日，必须能过 `trade_cal`；
- `data_time` 为 `YYYY-MM-DD HH:MM`，日线固定 `YYYY-MM-DD 15:00`，分钟写 bar 结束时间（`09:35` 表示 `09:30–09:35`）；
- 两者同一天（`data_time[:10] == data_as_of`），跨日 bar 视为数据错误，拒绝并回退；
- 行情与图表结果两者必填，`error` 结果可为空；文件结论不要求这两个字段但必须有 `source`。

## 条件

以下条件不阻塞 003/005 开工，但必须在 plan-20261107-006 内完成：

- **C1 唯一生产者落地**：`latest_bar_time` 按冻结签名实现并成为唯一入口，所有工具与渲染器不得自行格式化时点；
  行情/图表 `ToolResult` 两个字段必须非空（`error` 除外）。验收：
  `tests/test_data_time_semantics.py::test_latest_bar_time_is_single_source`、
  `tests/test_agent_tools.py::test_market_result_requires_both_timestamps`。
- **C2 降级标注键冻结**：`ToolResult.metadata` 固定 `session ∈ pre/open/lunch/closed/non_trading_day` 与
  `freshness ∈ live/delayed/eod/suspended` 两键；非交易时段必须 `eod`，`rt_min` 超 2 分钟未更新必须 `delayed`，
  停牌必须 `suspended` 且回答里写明"停牌/无成交"。UI 与图题按这两个键渲染提示，不再各自判断时段。验收：
  `tests/test_data_time_semantics.py::test_non_trading_session_marks_eod`、
  `tests/test_data_time_semantics.py::test_stale_rt_min_marks_delayed`。

## 非目标（v1 明确不做）

- tick / 逐笔 / Level-2：不接入、不承诺；未来若接入需单独 ADR 与数据授权。
- 秒级刷新与 WebSocket 推送：Agent 侧仍是"问一次取一次"，30s 缓存已足够。

## 后果

- 正面：盘中可用、口径清晰、避免前视；用户能看懂"这是几点几分的数据"。
- 负面：rt_min 受供应商权限与延迟限制；频繁轮询有频率成本（30s 缓存 + 单轮限次）。
- 验收：交易时段问"600519 现在 5 分钟 K 线 + RSI"，返回带 `data_time` 的图；非交易时段返回最近交易日并明确标注。

## 证据

- `astock/data.py`（`load_minute`：`rt_min` 当日 + `stk_mins` 历史，30s 缓存，`_MINUTE_TTL = 30.0`）
- `astock/agent/types.py:90-91`（字段已实现）、`types.py:130-133`（`to_dict` 输出）
- 全库检索：`data_time` 无生产者（2026-11-10 核验）
- ADR-005（data_as_of / tradable_from 的拆分）
- 用户 2026-11-07 消息；冻结语义：`docs/AStockAgent_interface_freeze_v1.md` §6
