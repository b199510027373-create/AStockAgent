# ADR-013 自研 Agent 运行时：工具调用循环

- 状态：accepted
- 裁决：**accepted**（2026-11-10 架构契约评审门禁 plan-20261110-003，对照 runtime.py 追认，无附加条件）
- 日期：2026-11-07
- 来源：用户 2026-11-07 架构调整；现状 `astock/llm.py` 单次问答
- 关联：ADR-009（LLM 只解释）、ADR-014（模型网关）、ADR-017（审计）

## 背景

当前「AI 对话」是一次性 RAG：检索公告/研报/新闻/政策片段 → 拼进 system prompt → 调一次 DeepSeek → 返回文本。它有三个硬限制：

- 不会"做事"：不能读用户上传的 PDF/Excel，不能按需查行情、算指标、画图；
- 不能多步：问"对比三只银行股近一年的 RSI 和回撤"时，只能靠预检索的文本，无法先查数据再计算；
- 不可审计：没有工具调用记录，无法回答"这个图/这个数字是怎么来的"。

用户要求把 AI 会话升级为**自己的 Agent**：能导入并读取文件、能按需调用内接数据源实时出图、能给出指定股票与指标的 K 线。

## 决策

1. 在 `astock/agent/` 下实现**进程内 Agent 运行时**（单用户本地，不引入独立服务）：
   - `runtime.py`：`plan → act → observe → reflect` 循环；
   - `registry.py`：工具注册表（名称、JSON Schema、处理函数、权限标签）；
   - `session.py`：会话与工作记忆；
   - `tools/`：行情、指标、图表、文件、研究、澄清六类工具。
2. 循环约束：
   - 单轮最多 **12 步**，总 token 预算可配（默认 64k）；
   - 每步只允许一个工具调用或一次最终回答；
   - 同一 `(tool, args)` 连续重复两次即判定打转，强制收敛；
   - 支持用户中断；超时（默认 120s）返回已完成的部分结果。
3. **数字与图形只来自工具**：模型负责选择工具与参数、组织语言；禁止让模型口算行情、指标或统计量。
4. 工具结果统一为 `ToolResult`：
   - `text` / `table`（超过 200 行截断并落 artifact）/ `figure`（artifact_id）/ `error`；
   - 所有结果带 `data_as_of` 与来源标签，供最终回答引用。
5. 会话记忆分两层：
   - 短期对话（最近 20 轮）；
   - 工作记忆 JSON（股票代码、日期区间、指标、已上传文件、上次图表），随 session 持久化。
6. 澄清工具 `ask_user`：信息不足时最多追问 1 次，不反复打断。
7. v1 **不执行任意代码**（图表用声明式 spec，见 ADR-016）；沙箱 Python 留作二期。
8. Agent 对推荐/台账**只读**：可以查推荐、跟踪、因子结果，但不得改排序或写推荐（ADR-009 不放宽）。

## 实现回写（2026-11-10）

对照 `astock/agent/runtime.py`、`astock/agent/types.py`、`astock/agent/session.py` 的实际实现。**只记录，不重写已通过评审的代码**：

1. 默认常量集中在 `runtime.py:48-52`，冻结：`DEFAULT_MAX_STEPS = 12`、`DEFAULT_TOKEN_BUDGET = 64_000`、
   `DEFAULT_TIMEOUT_SECONDS = 120.0`、`DEFAULT_VALIDATION_RETRIES = 1`、`DEFAULT_SPIN_LIMIT = 2`。
2. **偏差 1（模块清单）**：实现里另有 ADR 未列的两个模块——`astock/agent/types.py`（共享数据类型）
   与 `astock/agent/fake.py`（测试/离线用假模型）。`tools/` 目录尚未实现（见 ADR-015/016/017/018 条件）。
3. **偏差 2（模型接口）**：循环侧协议是 `runtime.py:65-71` 的
   `class AgentModel(Protocol): def complete(self, *, messages: list[dict], tools: list[dict]) -> ModelReply`，
   与 ADR-014 的 `ChatModel.complete(messages, tools, *, temperature=0.2)` 签名不同（全关键字、返回 `ModelReply`
   而非 `ChatResult`）。两者之间的唯一桥是 `gateway.py:1291 AgentModelAdapter`；运行时不得直接依赖 `ChatModel`。
4. `ToolResult`（`types.py:77-186`）9 个字段冻结：`text`、`table`、`figure`、`error`、`data_as_of`、`data_time`、
   `source`、`truncated`、`metadata`；`MAX_TABLE_ROWS = 200`、`MAX_TEXT_CHARS = 4000`；
   `to_dict()` 省略空字段但总带 `ok`；`trim_table()` 记录 `total_rows`/`dropped_rows`；`coerce()` 是裸返回值的唯一规范化入口。
5. `StopReason` 8 值冻结：`completed`/`max_steps`/`token_budget`/`timeout`/`interrupted`/`spinning`/`invalid_arguments`/`model_error`；
   `ModelReply(content="", tool_call=None, tokens=0)`；`StepRecord`/`AgentRunResult` 均有 `to_dict()`。
6. 「数字与图形只来自工具」由 `ToolRegistry` 的 schema 校验 + 未知工具报错落地
   （`test_agent_runtime.py::test_unknown_tool_is_reported_without_crashing`）；
   同一未知工具连续两次同样触发打转保护（`test_identical_unknown_tool_twice_triggers_spin_guard`）。
7. 会话记忆：最近 20 轮 `test_recent_twenty_turns_are_kept`；工作记忆 JSON roundtrip
   `test_working_memory_and_json_roundtrip`；损坏工作记忆容错 `test_corrupt_or_missing_working_memory_is_tolerated`。
8. 超时/中断为协作式（原文「实现注记」已述），此处确认仍成立，并由
   `test_timeout_keeps_executed_steps`、`test_user_interrupt_returns_partial_result` 覆盖。
9. 偏差小结：无阻塞偏差。两处偏差（新增 `types.py`/`fake.py`；`AgentModel` 与 `ChatModel` 分属两层）已记录并冻结，
   由 `docs/AStockAgent_interface_freeze_v1.md` §2 与 `tests/test_architecture_gate.py` 守住。

## 后果

- 正面：从"文本问答"升级为"能查、能算、能画、能读文件"的研究助手；每步可审计、可回放。
- 负面：代码量与失败面显著增加（模型选错工具、参数不合法、长会话超预算）；需要评测集与降级策略。
- 可靠性策略：工具参数校验失败时把错误回传给模型重试一次；仍失败则返回"无法完成 + 建议"。
- 依赖：需要一个支持 function calling 的模型；不支持时走 JSON 协议兜底（ADR-014）。

## 实现注记（2026-11-09，v1）

- **超时与中断为协作式**：运行时只在每步开始和工具执行后检查截止时间/中断标志，无法抢占正在阻塞的 `model.complete()` 或工具 handler。模型适配器应为 HTTP 客户端设置 read timeout，具体工具应自行约束数据源超时；否则单次阻塞调用可能突破 `timeout_seconds`（默认 120s），待其返回后才在下一个检查点返回部分结果。
- **中断语义**：`AgentRuntime.request_interrupt()` 可中断当前运行；若运行尚未开始，标记保留到下一次 `run()` 的第一次检查点，运行结束后清除。外部 `cancel_event` 在同样的检查点生效。
- **artifact 缺失可见**：未注入 artifact writer 时，表格超过 200 行仍截断返回，但在 `metadata` 标记 `artifact_missing=True` 与 `dropped_rows`，避免完整数据被静默丢弃；接入真实行情前应注入 writer（ADR-017）。
- **打转保护覆盖未知工具**：同一 `(tool, args)` 连续两次调用，无论工具是否注册，都会在第二次触发 `SPINNING`，避免模型反复调用幻觉工具名。

## 证据

- `astock/agent/runtime.py:48-52`（默认常量）、`runtime.py:65-71`（`AgentModel`）
- `astock/agent/types.py:33-37`（上限）、`types.py:40-50`（`StopReason`）、`types.py:77-186`（`ToolResult`）
- `astock/agent/gateway.py:1291`（`AgentModelAdapter`）
- `tests/test_agent_runtime.py`（40 项）
- `astock/llm.py`（当前只有 `chat()` 单次调用）、`astock/recommend.py`（LLM 只写文案，`_llm_rank`）
- 用户 2026-11-07 消息：自研 Agent + 文件导入 + 实时绘图
