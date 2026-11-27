# 事件分权重单点化与人工标注校准（2026-11-28）

- 计划项：`plan-20261103-010`
- 版本：`event-score-v2-20261128`
- 标注文件：`docs/calibration/event_labels_v1.csv`
- 实现：`astock/scoring.py`（唯一实现）；`astock/events.py` 只做兼容 re-export；
  `astock/recommend.py` 通过 `event_scoring_params()` 把权重快照写入 `run.params`
- 边界：不调用 LLM，不修改推荐动作/置信度，不把事件分当交易指令（ADR-009）

## 1. 单点化后的权重

所有事件分只读取 `astock/scoring.py` 顶部常量。`run.params["event_scoring"]` 是同一组常量的
JSON 快照，含 `version`、`type_weights`、`rating_weight`、`policy_bonus`、
`time_decay_days`、`neutral_score`、`default_type_weight`、正负阈值，以及校准元数据
（标注文件、样本数、案例数、方法、日期、`llm_used=false`）。历史推荐保留当时写入的快照，
不会被新常量覆盖。

| 参数 | 校准前（legacy） | 校准后（当前常量） | 说明 |
| --- | --- | --- | --- |
| 公告权重 | 1.0 | **1.2** | 公告在样本中信号最稳定 |
| 研报权重 | 1.2 | **0.8** | 研报评级在标注中不可识别，按 tie-break 降权 |
| 新闻权重 | 0.8 | **0.4** | 新闻噪声/价格重复报道多，降权 |
| 政策权重 | 0.7 | **0.6** | 政策偏中性，降权 |
| 研报评级权重 | 0.35 | **0.35** | 网格 0/0.35/0.7 同分，保留先验 |
| 政策加分 | +0.05 | **0.0** | 修复中性政策被推成正面 |
| 时间衰减 | 45 天 | 45 天 | 样本内不可识别，固定 |
| 中性分 | 50 | 50 | 不变 |
| 正/负阈值 | 50/50 | 50/50 | 严格大于/小于才判正/负 |

## 2. 人工标注样本

- 153 条 item，51 个 case，每个 case 3 条（同一事件的多条证据）。
- item 标签：正面 55、中性 50、负面 48。
- case 标签：正面 22、中性 10、负面 19。
- 类型分布：公告 59、新闻 59、研报 27、政策 8。
- 样本覆盖正/中/负三类，并包含少量“人工标签与词典不一致”的噪声样本（如
  `c17` 的“品牌影响力提升/机构关注度上升”不含词典词，`c36` 的“央行开展逆回购操作”
  被 `回购` 子串误命中）。这些样本用于暴露词典边界，不为了刷高准确率而删除。

## 3. 校准方法与结果

在 153 条 item / 51 个 case 上做 648 点网格搜索：

- 公告：0.8 / 1.0 / 1.2
- 研报：0.8 / 1.0 / 1.2
- 新闻：0.4 / 0.6 / 0.8 / 1.0
- 政策：0.4 / 0.6 / 0.8
- 研报评级权重：0 / 0.35 / 0.7
- 政策加分：0 / 0.05

目标函数：`100 * item_accuracy + 20 * case_accuracy + case_IC - 0.002 * deviation`，
其中 `deviation = Σ|type_weight - 1|` 抑制极端权重；同分时取离先验更近的配置。
选中配置即当前 `astock/scoring.py` 常量。

| 指标 | 校准前 | 校准后 | 变化 |
| --- | ---: | ---: | ---: |
| item 准确率 | 0.9477（145/153） | **0.9869（151/153）** | +0.0392 |
| case 准确率 | 0.9020（46/51） | **0.9804（50/51）** | +0.0784 |
| case macro-F1 | 0.8549 | **0.9750** | +0.1201 |
| case Rank IC | 0.9222 | **0.9438** | +0.0216 |
| item Rank IC | 0.9584 | **0.9647** | +0.0063 |

校准后仅剩 2 条 item 错误：

1. `c17` 新闻“公司品牌影响力提升 机构关注度上升”，人工正面，词典无词 → 50 分（中性）。
2. `c36` 政策“央行开展逆回购操作”，人工中性，`逆回购` 命中 `回购` → 100 分（正面）。

`c36` 同时是唯一一个 case 错误。校准前的 8 条 item 错误中，6 条是“中性政策被 +0.05
加分推过 50”；把政策加分归零后修复。剩余 `逆回购` 属于词典子串匹配问题，单靠权重
无法修复，需后续做短语级词表（本轮不引入 LLM）。

## 4. 校准前后推荐排序对比

### 4.1 真实批次（`recommend_20261127_191820_8a3d6229`，5 只有完整证据）

`rank_correlation = 0.8`，Top5 重叠 5/5。复合分口径保持 `0.65 * technical + 0.35 * event`。

| 标的 | 旧排名 | 新排名 | 事件分旧→新 | 复合分旧→新 |
| --- | ---: | ---: | ---: | ---: |
| 中芯国际 `688981.SH` | 2 | **1** | 76.2 → 77.5 | 86.0 → 86.4 |
| 工商银行 `601398.SH` | 1 | **2** | 69.5 → 65.5 | 87.1 → 85.7 |
| 美的集团 `000333.SZ` | 4 | **3** | 63.6 → 64.7 | 85.0 → 85.4 |
| 交通银行 `601328.SH` | 3 | **4** | 64.7 → 57.5 | 85.4 → 82.9 |
| 格力电器 `000651.SZ` | 5 | 5 | 51.0 → 56.6 | 80.6 → 82.6 |

解读：公告权重上调、新闻/政策权重下调后，公告驱动的事件分略升；银行股的事件分
主要来自政策/新闻，降权后排名互换。Top5 成员未变，说明权重调整没有把边缘标的
突然推入头部。

### 4.2 合成 10 候选压力测试

`rank_correlation = 0.9268`，Top5 重叠 4/5。

| 候选 | 旧排名 | 新排名 | 事件分旧→新 | 复合分旧→新 |
| --- | ---: | ---: | ---: | ---: |
| 混合信号 | 1 | 1 | 100.0 → 100.0 | 95.8 → 95.8 |
| 研报驱动 | 2 | 2 | 100.0 → 100.0 | 94.8 → 94.8 |
| 政策驱动 | 3 | 3 | 100.0 → 100.0 | 94.8 → 94.8 |
| 公告驱动 | 5 | **4** | 77.8 → 87.5 | 87.0 → 90.4 |
| 公告加研报 | 6 | **5** | 73.3 → 83.3 | 86.1 → 89.6 |
| 新闻利空 | 7 | **6** | 55.6 → 75.0 | 78.9 → 85.7 |
| 新闻驱动 | 4 | **7** | 80.8 → 70.0 | 88.1 → 84.3 |
| 中性事件 | 8 | 8 | 50.0 → 50.0 | 76.7 → 76.7 |
| 公告利空 | 9 | 9 | 44.4 → 25.0 | 75.0 → 68.2 |
| 纯负面 | 10 | 10 | 0.0 → 0.0 | 58.5 → 58.5 |

解读：

- 公告权重上调后，公告驱动/公告加研报各升 1 位；纯新闻驱动降 3 位。
- 公告利空从 44.4 降到 25.0，负面公告的惩罚更明显。
- **风险点**：“新闻利空 + 公告增持”这类混合信号从 55.6 升到 75.0（7→6）。
  加权平均下正面公告压过负面新闻，单点权重无法表达“负面新闻一票否决”。报告保留
  该反例，后续若要处理需在证据聚合层引入规则（不在本任务范围）。

## 5. 复现与验证

```powershell
# 重新跑校准（输出 legacy/calibrated/top 配置）
.venv\Scripts\python.exe -m astock.scoring --now 2026-11-28

# 目标测试
.venv\Scripts\python.exe -m pytest tests/test_research_semantics.py -q

# 相关回归
.venv\Scripts\python.exe -m pytest tests/test_recommend.py tests/test_contracts.py tests/test_data_pit_events.py tests/test_architecture_gate.py tests/test_ci_quality_gates.py -q

# 静态检查
.venv\Scripts\python.exe -m ruff check astock/scoring.py astock/events.py astock/recommend.py tests/test_research_semantics.py tests/test_ci_quality_gates.py
```

本次在干净 worktree（`origin/main` + 本任务改动）的验证结果（2026-11-28）：

- `pytest tests/test_research_semantics.py -q`：32 passed。
- `pytest tests/test_recommend.py tests/test_contracts.py tests/test_data_pit_events.py tests/test_architecture_gate.py tests/test_ci_quality_gates.py -q`：126 passed。
- `mypy astock/scoring.py astock/events.py astock/recommend.py`：Success。
- `ruff check` 上述 5 个 Python 文件：All checks passed。
- `astock/scoring.py` 单文件覆盖率：97.88%（236 statements / 5 miss，仅剩 CSV 异常分支与 `__main__` 入口）。

## 6. 限制与后续

- 本轮为单标注者标注，未做双人一致性/Kappa；153 条只够做一次轻量校准，不足以证明
  跨市场阶段的稳定性。
- 真实批次只有 5 只有完整证据，排序对比是 sanity check，不构成 alpha/收益声明。
- 词典仍是子串计数，`逆回购` 这类误命中未修复；本轮不引入 LLM 或语义模型。
- 时间衰减、复合分 0.65/0.35 未参与校准；任务只收敛事件分权重。
- LLM 仍只解释：`scoring.py` 不 import 任何 LLM 模块，`recommend._llm_rank` 的
  输入/输出契约未改。
