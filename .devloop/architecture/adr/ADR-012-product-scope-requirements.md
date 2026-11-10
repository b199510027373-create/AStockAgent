# ADR-012 产品边界与研究闭环（需求基线）

- 状态：accepted
- 日期：2026-11-03
- 来源：2026-10-26 构建会话（27 条用户消息）、2026-11-02 评审会话

## 背景

需求不是一次给出的，而是在 8 天里逐步长出来的。把原始请求按时间排列，可以看清系统真正要解决的问题：

1. 在已有「单股/组合/选股/AI 对话」之上，增加**量化功能与长短周期 K 线**；
2. 实时分钟（`rt_min`）与历史分钟（`stk_mins`），支持 1/5/15/30/60 分钟与区间对比；
3. 用**新闻/公告/研报/政策 + 日线**做每日股票推荐，同一批事件数据也作为 AI 对话的数据源；
4. 历史分钟复权、定时自动跑、按 **T+1 / T+5 / T+20** 跟踪，检验综合分是否真有超额；
5. 积累 20+ 批次后做**显著性检验**，以及参数网格、walk-forward、多因子 IC；
6. 用 PIT 股票池消除幸存者偏差；用 SQLite 台账让推荐可审计；
7. 工程质量：文档拆分、CI lint/type/覆盖率、测试与门槛。

一句话：**用户要的不是"AI 荐股"，而是一个可复现、可审计、敢说"没证明"的 A 股研究台。**

## 决策

1. 产品定位：本地单用户研究台；不构成投资建议；不输出买卖指令。
2. 功能边界固定为 13 个 Tab（单股/组合/简报/选股/自选/AI 对话/多周期/每日推荐/推荐跟踪/参数优化/因子 IC/策略实验室/回测）。
3. 研究闭环必须端到端可复现：数据 → 评分 → 推荐 → 台账 → 跟踪 → 统计 → 结论；任一环缺失则视为未完成。
4. 需求到模块的映射：
   - 多周期/分钟 → `data.py` + `charts.py` + `ui.py`；
   - 每日推荐 → `recommend.py` + `events.py` + `llm.py`；
   - 跟踪/显著性 → `track.py` + `stats.py` + `ledger.py`；
   - 网格/WF/因子 → `optimize.py` / `strategy_lab.py` / `factors.py`；
   - PIT → `pit.py` + `universe.py`；
   - 调度 → `scheduler.py` + `jobs.py`。
5. 验收基线以 `docs/AStockAgent_product_snapshot_v1.3.md` §6 为准（页面可打开、16:40 任务可重试、`latest.json` 合法、跟踪优先读 ledger、回测用原始价限价）。

## 非目标（明确不做）

- 不做投顾/荐股产品，不承诺收益，不把"无 alpha"藏起来；
- 不做多用户、远程、FastAPI、微服务；
- 不做分钟级回测（复权接缝未验证）；
- 不让 LLM 进入排序；
- 不在 PIT 与统计修好前继续扩因子。

## 后果

- 正面：需求与模块一一对应，避免"功能做了但闭环断了"；非目标写死后，评审与计划不会反复摇摆。
- 负面：功能面看起来比"AI 选股"窄；部分需求（分钟级回测、多市场）被长期冻结。
- 维护：新增需求必须先判断是否落在闭环内，再决定改模块还是改边界。

## 追加（2026-11-07）：AI 会话升级为自研 Agent

用户提出架构调整：不再把 DeepSeek API 直接嵌进对话，而是开发自己的 Agent；同时要求对话内可导入并读取 PDF / Excel 等文件，并能按需调用内接数据源直接出统计图、给出指定股票与指标的实时 K 线。

该调整不改变产品定位与非目标，但把「AI 对话」从一个 RAG 文本入口升级为**能查、能算、能画、能读文件**的研究代理，新增 Agent 运行时、模型网关、文件解析/检索、图表 spec、运行审计与实时口径六块能力。

- 对应 ADR：ADR-013（运行时）、ADR-014（模型网关）、ADR-015（文件）、ADR-016（图表）、ADR-017（审计与安全）、ADR-018（实时口径）。
- 计划落点：新里程碑 `ms-agent`；为腾出产能，事件分校准、parquet 分区、板块规则等研究增强项转入 backlog。

## 治理附录（2026-11-10）：ADR 状态纪律

本附录由 plan-20261110-003（架构契约评审门禁）新增，约束的是**文档与实现的先后关系**，不改产品边界。

### 1. 状态语义

| 状态 | 含义 | 能否作为实现依据 |
| --- | --- | --- |
| `proposed` | 已写入、未裁决 | **否**。不得据此开工，不得在评审中当作已定接口引用 |
| `accepted` | 已裁决，接口冻结 | 是 |
| `accepted-with-conditions` | 已裁决，但带条件 | 是；条件必须写在本 ADR 的「条件」段并挂具体计划项 |
| `superseded` / `deprecated` / `rejected` | 失效 | 否 |

工作台（`devloop_architecture list`）里不允许长期存在「代码已实现但 ADR 仍是 proposed」的条目。

### 2. 先行实现与 7 天追认

允许为了验证可行性先行写代码，但必须满足：

1. 从**首个合并到 main 的实现提交**起算，**7 个自然日内**完成 ADR 追认（状态改为 accepted 或 accepted-with-conditions）；
2. 超期未追认：该实现冻结功能扩展，只允许缺陷修复；新功能必须等 ADR 裁决；
3. 追认时必须写「实现回写」段（见第 3 条），并把偏差升级为本 ADR 的正式口径，而不是留在代码注释里。

### 3. 实现回写义务

- 已 accepted 的 ADR，若实现与文本不一致，**以补写「实现回写」段为准**，不得为了对齐文档去重写已通过评审的代码；
- 「实现回写」必须写清：偏差是什么、为什么这样收敛、冻结成什么行为、哪个测试守住它；
- 例：ADR-002 的 `adj_factor` 条件必填、ADR-014 的 `complete` 无 `stream` 形参、ADR-017 的权限七标签。

### 4. 接口冻结

- `docs/AStockAgent_interface_freeze_v1.md` 冻结 6 个接口：`ToolResult`/`ToolSpec`、`ChatModel.complete`、
  `ChartSpec` v1、文件存储布局、`agent_*` 三表、`data_as_of`/`data_time`；
- 改冻结接口的顺序固定为：**先写 ADR → 状态 accepted → 再升冻结清单版本（v1 → v2）→ 同期改门禁测试**；
- 禁止"先改代码，回头补文档"。

### 5. 门禁

`tests/test_architecture_gate.py` 自动检查：不存在 `proposed` ADR、`adrs.json` 与 ADR 文件的「状态」一致、
带条件的 ADR 有「条件」段、18 条 contract 规则均有落点、冻结接口与代码签名一致。任一不满足即 CI 失败。

### 6. 状态变更记录（2026-11-10）

| ADR | 主题 | 原状态 | 结论 | 条件 | 承接项 |
| --- | --- | --- | --- | --- | --- |
| ADR-002 | 六层分层与 contracts.py | proposed | accepted | — | — |
| ADR-010 | 研究语义与「无 alpha」 | proposed | accepted-with-conditions | C1/C2 | plan-20261103-006 |
| ADR-013 | Agent 运行时 | proposed | accepted | — | — |
| ADR-014 | 模型网关 | proposed | accepted | — | — |
| ADR-015 | 文件导入与检索 | proposed | accepted-with-conditions | C1/C2/C3 | plan-20261110-004 |
| ADR-016 | 图表 ChartSpec | proposed | accepted | — | plan-20261110-005 |
| ADR-017 | 审计与安全边界 | proposed | accepted-with-conditions | C1/C2/C3 | plan-20261110-006 |
| ADR-018 | 实时口径 | proposed | accepted-with-conditions | C1/C2 | plan-20261110-007 |

无 `deferred` 条目；没有阻塞性接口问题，003（ledger）与 005（时间语义）可按原计划于 11-15 开工。

## 证据

- 构建会话用户消息：seq 18 / 1090 / 1334 / 2084 / 2518 / 3336 / 5188 / 6040 / 6443 / 6652 / 7852
- 2026-11-07 用户消息：自研 Agent + 文件导入（PDF/Excel）+ 实时绘图/实时 K 线
- `docs/AStockAgent_product_snapshot_v1.3.md`（13 Tab、限制、验收口径）
- `deliverables/AStockAgent_代码与架构评审_v1.md` §5 非目标
