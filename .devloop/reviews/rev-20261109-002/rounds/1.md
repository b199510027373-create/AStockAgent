# PR 评审：评审：Agent 运行时骨架与工具注册表

> 自动生成于 2026-11-09T02:57:17.531Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261109-002`
- 计划项：`plan-20261107-001`
- 分支：`main` → `main`
- 轮次：第 1 轮（full）
- 结论：需要修改（request_changes）
- 开发会话：`session-45c39c78-ceab-48e0-872f-be3854ecc85c`
- 本轮问题：4（P0 0 / P1 1 / P2 3）
- 线程未闭环：P0 0 / P1 1 / P2 3

## 本轮结论

## 第 1 轮（full）评审结论：request_changes

评审范围：092f563 的 7 个文件（astock/agent/__init__.py、fake.py、registry.py、runtime.py、session.py、types.py、tests/test_agent_runtime.py，2093 insertions），对照 contract v1 的 AGENT-01~06 与 ADR-013 决策。

### 一、验收标准核对（4/4 功能性条目可判定为满足，测试维度有缺口）
1. fake 模型驱动 plan→act→observe 循环 —— 满足。test_normal_case_completes 断言 model.calls 递增、tools 透传、session 里 assistant(User)→…→Final 的顺序。
2. 步数上限 / token 预算 / 超时 / 用户中断均可触发并返回部分结果 —— 功能上满足（test_max_steps、test_token_budget_stops_before_next_model_call、test_timeout、test_user_interrupt，均断言 `partial is True` 与 answer 非空）。但「超时」仅覆盖步骤边界（见 P2 iss-mw8o6nsb-4e7ceb），「中断」的 run 前时序未覆盖（见 P2 iss-mw8o6rhi-5aa4a3）。
3. 同一 (tool,args) 连续两次触发打转保护 —— 对已注册工具满足（test_identical_call_twice：handler 只执行 1 次、stop_reason=SPINNING）；对未注册工具不成立（见下述观察项 ①）。
4. 参数校验失败回传模型重试一次 —— 满足。test_invalid_arguments_retried_once（第 2 次正确参数成功）与 test_invalid_arguments_twice（stop_reason=INVALID_ARGUMENTS，steps 含 2 条校验失败 + observation 文案）覆盖了两次分支。
5. 单测覆盖正常/超预算/打转/工具报错四条路径 —— 满足，且额外覆盖了未知工具、模型异常、writer 失败。

### 二、测试证据核对
- links.test 声明的 `pytest tests/test_agent_runtime.py -q` → 23 passed，我逐个清点该文件 `def test_` 恰好 23 条，数量一致；全量 `pytest tests -q` → 147 passed、`mypy astock` → Success (28 files) 属于开发自证，我未复跑（按约定测试执行由开发负责）。
- 判别力抽查：test_timeout 用 FakeClock 在 handler 内 advance(200)，只证明「步骤边界」超时；test_long_table_is_trimmed_and_archived 显式传 writer，未覆盖默认 None；test_user_interrupt 在 run 内部触发，未覆盖 run 前。这三处是本轮 3 条 P2 的直接来源。
- 未覆盖清单（按严重度已并入各 issue 的验收条件）：畸形工具返回值、artifact_writer=None、cancel_event、run 前 request_interrupt、未知工具重复调用、session.save 失败、coerce 的 DataFrame 分支、recent_messages(limit=0)。

### 三、本轮已立项问题（4 条）
- **P1 阻断** iss-mw8o5r8h-9ccd42（registry.py:377-385）：`ToolResult.coerce` 在 try 之外 + `__post_init__` 未校验 text/figure 类型，导致 `{"table": "bad"}` / `{"text": 123}` 让整轮 run 抛异常、丢失全部步骤。必须修：coerce 纳入 try 转 error ToolResult、补齐字段类型校验、`_format_observation` 对 text 做 str 兜底，并补 2 条用例。
- P2 iss-mw8o6foe-8b29c5（registry.py:307-329,386-395）：默认 artifact_writer=None 时 >200 行的完整数据静默丢弃，与 ADR-013 决策 4 / ADR-017 的 artifact 记录冲突。tools/ 接入真实行情前必须给处置（metadata 记 artifact_missing 或注入默认 writer + 用例）。
- P2 iss-mw8o6nsb-4e7ceb（runtime.py:144,150-153,295,314,349-361）：超时/中断只在步骤边界生效，模型调用与单次工具执行不受约束。可接受为 v1 协作式语义，但必须在 docstring/ADR 写明并给 adapter 侧超时，补一条 complete 抛 TimeoutError 的用例。
- P2 iss-mw8o6rhi-5aa4a3（runtime.py:117-124）：run 开头 clear 掉 run 前设置的中断，与 docstring「或下一次运行」矛盾；cancel_event 分支无测试。要么改语义要么改文档，并补 2 条用例。

### 四、本轮以 summary 记录、因工具限制（同一文件每轮 P2 上限 2 条）未能单独立项的两条观察项，请开发一并处置
① 打转保护不覆盖未知工具路径（runtime.py:203-227 与 :263-270）：未知工具分支 `continue` 不进入 signature/repeats 统计，模型连续 12 次调用同一未注册工具（幻觉工具名是最典型的循环诱因）不会触发 SPINNING，一路走满 max_steps。建议把签名统计前移到 `registry.tool(name)` 之前，补「两次同名未知工具 → SPINNING、len(steps)==2」用例。这是 ADR-013 决策 2 的偏离，不属文档问题。
② SYSTEM_PROMPT 未声明不可信内容（runtime.py:54-59）：AGENT-05 要求工具/文件/网页内容视为不可信数据、不得执行其中指令，当前系统提示完全没有这条，而这是模型侧唯一的落点（tools/files.py 尚未落地，故本轮不定为 P1）。建议补一句明确条款 + 断言 SYSTEM_PROMPT 含该约束的用例；tools/ 落地时在 observation 里标注来源并用定界包裹。

### 五、谁在什么条件下修、何时算完全解决
- **责任方：开发（同一分支 main）**。修复 iss-mw8o5r8h-9ccd42（P1）+ 上述 3 条 P2 的「已修」路径，或对 P2 明确选择「不改」并在 ADR-013/docstring 写明理由（观察项 ① ② 必须至少给出处置结论，不接受沉默）。修完补测：畸形返回值 ×2、artifact 无 writer ×1、TimeOffset/Timeout ×1、run 前中断 + cancel_event ×2、未知工具打转 ×1、SYSTEM_PROMPT 断言 ×1，重跑 `pytest tests/test_agent_runtime.py -q`、`pytest tests -q`、`mypy astock`，并把 links.test 更新到新 commit。
- **交付维度**：当前 main ahead origin/main 2、无 PR/CI 记录，请在下轮评审前给出推送/PR 状态（不作为代码问题计级）。
- **完全解决判定**：下一轮（round 2，diff + 对 P1 做全量复检）中 P1 关闭、3 条 P2 均已「修复+测试」或「ADR 中书面接受」、观察项 ① ② 有明确结论，`devloop_review finish` verdict=approve 且 openP0=0、openP1=0，才标记为完全解决。在此之前本项不得置 done。

## 本轮问题

### P1 · 工具返回畸形 Mapping 时 ToolResult.coerce 抛未捕获异常，整轮 run 直接崩溃而不是回传 observation

- 位置：`astock/agent/registry.py:377-385`
- 状态：未闭环
- 置信度：0.85

**证据**

registry.py:377-383 只把 `spec.handler(**prepared)` 包在 try/except 里，紧接着第 385 行 `result = ToolResult.coerce(raw)` 在保护范围之外。coerce 对「键集合是 ToolResult 字段子集」的 Mapping 走 types.py:171-174 的 `cls(**dict(value))`，而 types.py:100-111 的 `__post_init__` 只校验 table 与 metadata，于是 `{"text": 123}`、`{"table": "bad"}` 这类返回值会一路通过 coerce。前者随后在 runtime.py:373-376 的 `len(text)` 上抛 `TypeError: object of type 'int' has no len()`，后者在 `__post_init__` 抛 `TypeError: ToolResult.table 必须是 list[dict]`；两处都不在 runtime.run 的任何 try 中（runtime.py:295 `result = self.registry.invoke(...)` 是裸调用，整个 run 只有 322 行的 `finally`，没有 except）。后果与设计承诺相反：模块 docstring（registry.py:12-13）写「工具异常统一转成 ToolResult(error=...)」，ADR-013 也要求工具失败回传模型重试；实际是一个字段类型写错的新工具（tools/ 目录正要在后续 plan item 里加）会让整轮 run 抛出到 Gradio，用户丢失已经执行的所有步骤，且 AgentRunResult/steps 一并丢失。tests/test_agent_runtime.py 的 23 个用例里没有任何一条构造畸形工具返回值，因此该路径全无覆盖。

**建议**

两处一起修：(1) 在 types.py:100-111 的 `ToolResult.__post_init__` 里补齐字段类型校验——`text`/`figure`/`error`/`data_as_of`/`data_time`/`source` 必须是 str 或 None，`truncated` 必须是 bool，违者抛 TypeError 并在错误信息里点名字段；(2) 在 registry.py:385 把 coerce 纳入 try/except，失败时返回 `ToolResult(error=f"工具返回结构非法：{type(exc).__name__}: {exc}", source=f"tool:{name}")`，与 handler 异常同一条回传路径。同时给 runtime.py:369-401 的 `_format_observation` 加防御（`text = str(result.text)`）。配套补用例：(a) 工具返回 `{"table": "bad"}` → run 正常结束、`steps[0].result.error` 含「工具返回结构非法」，模型仍收到一条 tool 消息；(b) 工具返回 `{"text": 123}` → 同上，不抛异常、observation 里是字符串化文本。验收判定：这两条用例通过、且 `registry.invoke` 对任意畸形返回值都只产生 error ToolResult。

### P2 · 默认 ToolRegistry 的 artifact_writer=None，表格超 200 行时完整行被静默丢弃，与 ADR-013「截断并落 artifact」不符

- 位置：`astock/agent/registry.py:307-329,386-395`
- 状态：未闭环
- 置信度：0.75

**证据**

registry.py:386-395 只有 `self._artifact_writer is not None` 成立时才调 writer 落全量行，否则直接 `trim_table`；而 runtime.py:103 的默认装配是 `ToolRegistry()`，artifact_writer 默认为 None（registry.py:307-318）。types.py:141-153 的 trim_table 只保留 total_rows/dropped_rows 两个计数，被丢掉的 51 行内容不再存在于任何地方——既不在 StepRecord.result（只剩 200 行），也不在 artifact（没有 writer），agent_steps 里只会看到一个 dropped_rows=51。这与 ADR-013 决策 4「超过 200 行截断并落 artifact」以及 types.py:8-10 的 docstring「完整行由 ToolRegistry 的 artifact writer 落盘」不一致，且是静默的：调用方无法区分「表本来就只有 200 行」和「有 51 行被扔掉」。现有测试 test_long_table_is_trimmed_and_archived 显式传了 writer，因此默认 None 的路径没有覆盖；等 tools/ 里接入 Tushare 全量行情（几千行）后，agent_artifacts 会缺内容，用户追问「把全部数据给我」时无法复现。

**建议**

二选一并写进 docstring：(a) 无 writer 时在截断后的 ToolResult.metadata 里显式记 `{"artifact_missing": True, "dropped_rows": N}`，让下游（Gradio 渲染层、后续 agent_store）能据此提示「完整数据未归档」；(b) 让 AgentRuntime 构造时注入默认 writer（等 agent_store 落地后指向 agent_artifacts），并在 runtime 的 docstring 注明默认装配必须带 writer。配套补一条 `ToolRegistry(table_row_limit=3)` 无 writer、工具返回 5 行 → 断言 rows 长度 3、metadata 含 artifact_missing 与 dropped_rows=2 的用例。这条不阻断本项合入（当前无 >200 行工具），但 tools/ 接入真实行情前必须落地，否则 ADR-017 的 artifact 记录会出现空档。

### P2 · 120s 超时与用户中断只在步骤边界生效，单次模型调用/工具执行不受约束，阻塞时无法按时返回部分结果

- 位置：`astock/agent/runtime.py:144,150-153,295,314,349-361`
- 状态：未闭环
- 置信度：0.72

**证据**

runtime.py:349-361 的 `_guard` 只在两个位置被调用：步骤开头 :144 与工具执行后 :314。也就是说超时/中断/预算都是「协作式」的：`self.model.complete(...)`（:153）与 `self.registry.invoke(...)`（:295）在执行期间无法被打断。真实场景下模型调用是网络请求（DeepSeek/其他网关，最长可到几十秒甚至挂住），Tushare 拉全量行情也可能几十秒——一次 120s 的超时承诺会被单个阻塞调用突破，且用户点「中断」也只能等到该次调用返回后才生效。ADR-013 决策 2 写的是「超时（默认 120s）返回已完成的部分结果」，验收标准 (2) 也把超时列为必须可触发；当前实现只保证「步骤之间」的超时，测试 test_timeout 用 FakeClock 在 handler 内 advance(200) 模拟，恰好掩盖了这一点（FakeModel 同步立即返回，用例根本走不到模型阻塞的情形），所以 23 passed 不能证明该路径可用。

**建议**

明确定位并写进 docstring：v1 是「协作式超时」（步骤边界判定），这本身可接受，但必须 (1) 在 `run()` 的 docstring 与 ADR-013 里写明，避免后续误以为能中断阻塞调用；(2) 在 `AgentModel` protocol 上增加可选参数（如 `deadline: float | None`）或在 adapter 层给 httpx 设 read timeout，使模型调用自身有上限；(3) 工具侧由各工具自己设 timeout（工具 handler 内部），并在 ToolSpec.description 里约定。测试补一条：FakeModel 的 complete 抛 `TimeoutError`（模拟网关超时）→ 断言 run 返回 StopReason.MODEL_ERROR（或新增 TIMEOUT）且 answer 非空。若不做 (2)(3)，请在交付说明里写明「阻塞调用无法被打断」为已知限制，不要留成隐含假设。

### P2 · run() 开头清空中断标志，导致 run 前设置的 request_interrupt() 被丢弃，与 docstring「或下一次运行」矛盾；cancel_event 分支无测试

- 位置：`astock/agent/runtime.py:117-124`
- 状态：未闭环
- 置信度：0.8

**证据**

runtime.py:117-119：`request_interrupt()` 的 docstring 是「请求中断当前正在运行（或下一次运行）的 Agent 循环」，实现只做 `self._interrupt.set()`；但 `run()` 在 :124 一进来就 `self._interrupt.clear()`。因此在没有 run 进行时调用 request_interrupt() 会立刻被下一次 run 清掉，与 docstring 承诺的「下一次运行」相反。这不是纯文档问题：Gradio 的「中断」按钮如果在 run 提交与真正开始之间（队列等待）被点击，中断会丢失，用户看到 Agent 照常跑完 12 步。同类问题：`cancel_event` 参数（:121、:349-361）没有任何测试覆盖，`_guard` 里 cancel_event 分支与 interrupt 分支是两套并行的判断，行为一致性未经核对。现有 test_user_interrupt 是在 handler 内部调用 request_interrupt()（run 已经开始），所以测不到这个时序。

**建议**

选一种语义并落实：(a) 若确实支持「下一次运行」，把 `self._interrupt.clear()` 从 :124 移到 run 的 `finally`（或改成消费式：`if self._interrupt.is_set(): self._interrupt.clear(); return INTERRUPTED` 放到循环开始前的第一次 `_guard` 里，让 run 开头那次检查先看到它）；(b) 若不支持，把 docstring 改成「中断当前正在进行的 run，未运行时调用无效果」。两种都要补用例：`rt.request_interrupt(); rt.run()` → stop_reason==INTERRUPTED 且 `len(model.calls)==0`；再补一条 `cancel_event`（threading.Event 预先 set）→ 同样在第一次 _guard 处返回 INTERRUPTED。验收判定：这两条用例通过，且 docstring 与实现一致。

## 变更文件

未获取到变更文件清单；请用 `devloop_git files` 重新获取。

## 交给开发会话

1. 在分支 `main` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
