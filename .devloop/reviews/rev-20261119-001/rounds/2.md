# PR 评审：评审：Agent 会话 UI：流式 / 步骤 / 附件 / 内嵌图

> 自动生成于 2026-11-19T01:33:00.191Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261119-001`
- 计划项：`plan-20261107-007`
- 分支：`feat/agent-chat-ui` → `main`
- 轮次：第 2 轮（diff）
- 结论：需要修改（request_changes）
- 开发会话：`session-b2a69179-5e60-4872-91f0-f30a7d3402d1`
- 本轮问题：1（P0 0 / P1 1 / P2 0）
- 线程未闭环：P0 0 / P1 3 / P2 4

## 本轮结论

第 2 轮（scope=diff，base=1ba2293 → head=eca6667）复核结论：第 1 轮 6 条问题全部有落地修复与证据，但本轮修复自身新引入 1 条 P1（iss-mwmvlhso-cd6199），因此仍 request_changes，openP0=0 / openP1=1。

一、第 1 轮 6 条逐条复核（均已修复，接受关闭）
1. iss-mwmtsmue-f7a49c（P1 审批审计未接线）→ 已修：app.py:28 import DEFAULT_DB_PATH as AGENT_DB_PATH，app.py:481-487 把 db_path=AGENT_DB_PATH 传入 build_agent_chat_tab；astock/agent/chat.py 新增 db_path property（chat.py:677-680）并透传到 AgentRuntime(db_path=...)，agent_runs/agent_steps 真正落库。证据：tests/test_agent_chat.py test_run_and_step_records_are_persisted（sqlite 直查 agent_runs 1 行、agent_steps == len(steps)+1 且末行 kind=final）、test_db_path_reaches_controller，加真机 agent_runs=9 / agent_steps=27。
2. iss-mwmtsmv4-4f6d7f（P1 AC2–AC5 无真机证据）→ 已修：.smoke-agent-ui/shots/1-figure-iframe.png … 8-citation-1280.png 共 8 张截图 + smoke_drive.py / degraded_drive.py / visual_drive.py / cdp.py 等驱动脚本已随分支提交；handoff ho-20261119-011/012/013 记录 CDP 断言（AC1 附件 2/2、AC2 20 帧+3 步、AC3 三个 iframe srcdoc、AC4 点击 #cite-1、AC5 中断保留 121 字且可立刻再问、降级横幅+128 字回答）。CI run 44436480771 全绿（含 png-export），PR #32 未合并。
3. iss-mwmtt39d-d8e449（P2 每帧重算引用）→ 已修：agent_chat_ui.py 内嵌 citations_block() 按 (label, page, sheet, file_id) 指纹缓存 + render_citations_html(memo=...)，只在变化时重算；证据 test_citation_html_rendered_once_per_round、test_memo_reuses_resolved_details。
4. iss-mwmtt3ab-775ed6（P2 建模型/检索阶段中断失效 + 死 cancel 参数）→ 已修：worker 直接传 cancel_event=self._cancelled，删除局部 threading.Event，ask() 在 _ensure_runtime 后新增前置检查（chat.py:752-756），证据 test_interrupt_before_runtime_starts_skips_run。但该修复的「无条件 runtime.request_interrupt()」引入了本轮新 P1，见下。
5. iss-mwmtta2g-c95a18（P2 工作表级匹配却宣称行/单元格定位）→ 已修：resolve_citation 记录 matched_scope，工作表级命中且引用带行/单元格时 note 改为「工作表级定位：未精确到第 N 行/单元格 X，先展示该工作表首段内容。」；证据 test_sheet_level_match_with_cell_does_not_claim_cell_precision。
6. iss-mwmtta3c-cfdaaa（P2 三处测试缺口）→ 已修：补 test_page_mismatch_falls_back_to_first_chunk、test_more_than_limit_truncates_with_hint（21 条 → 20 卡 +「还有 1 条引用未展示」）、test_busy_round_is_rejected_and_lock_released 等 10 例，目标测试 84 passed。

二、本轮新问题（必须修）
- iss-mwmvlhso-cd6199（P1，astock/agent/chat.py:700-708、752-756）：interrupt() 现在无条件把中断转发给缓存复用的 runtime，而 runtime._interrupt 只在 run() 的 finally（runtime.py:512）清理。于是在「run 已返回、回答仍在流式输出」或「建模型/新闻检索阶段」点「⏹」会让下一问在首个 _guard（runtime.py:645-647）直接判 INTERRUPTED：零步骤、文案「已中断」。修法：控制器用 self._running 事件标记 run 是否真在途（worker set/finally clear），仅 _running.is_set() 时调 request_interrupt()；不要改成 run() 入口 clear（tests/test_agent_runtime.py:215-229 已钉住 pre-run 语义）；并按 issue 里的用例补「run 已结束后 interrupt() → 下一问仍 COMPLETED」的回归测试。

三、残存风险（不计问题，要求第 3 轮带一句话结论）
去掉 stop.click 的 cancels 后，若某次工具/模型调用长时间不返回（runtime.py:266-267 明确无法抢占），respond() 的 queue_.get()（chat.py:775-776）无超时会一直阻塞，busy 锁直到该调用返回才释放，期间提问均为「上一轮仍在生成」。请在第 3 轮 handoff 里说明是否接受该取舍（httpx 客户端有 read timeout，属于有界阻塞）或改为 queue_.get(timeout=...) 轮询 + 可中断收尾。

四、何时算完全解决
开发在 PR #32 上按 iss-mwmvlhso-cd6199 的修法改 chat.py，补上「run 已结束中断不污染下一问」的回归用例并跑通目标测试 + 全量 + ruff/mypy，附一条真机 CDP 冒烟（连续两问之间点一次「⏹」，第二问仍返回带步骤的正常回答）；第 3 轮我只复核该条与残存风险说明，届时 openP0=0 且 openP1=0 即 approve 并标记完全解决。

## 本轮问题

### P1 · 空闲/流式阶段点「中断」会给 runtime 留下未消费的中断标记，下一次提问被立刻判为「已中断」且零步骤

- 位置：`astock/agent/chat.py:700-708,752-756`
- 状态：未闭环
- 置信度：0.9

**证据**

ed4e44b 把 interrupt() 改成无条件转发（astock/agent/chat.py:700-708）：

    self._cancelled.set()
    runtime = self._runtime
    if runtime is not None:
        runtime.request_interrupt()
    if self._busy.locked():
        return "⏹ 已请求中断：当前步骤完成后停止，已产出的内容会保留。"

1) runtime.request_interrupt() 只做 `self._interrupt.set()`（astock/agent/runtime.py:247-253），而 `self._interrupt` 全仓唯一 clear 点是 run() 的 finally（astock/agent/runtime.py:512），run() 入口（runtime.py:273-291）不 reset。
2) self._runtime 首次 ask 后缓存复用（astock/agent/chat.py:832-843），因此从第二轮起 interrupt() 的 runtime 分支一定命中。
3) 触发路径 A：一轮 run 已返回、生成器仍在推 answer 分片时点「⏹」→ busy 仍 locked、runtime 非 None → _interrupt 置位，但这次 run 的 finally 早在返回值时就清过了，之后无人清理。下一问 ask()：chat.py:727 只 clear `self._cancelled`，没有清 runtime._interrupt；worker 进入 runtime.run → 首个 _guard 命中 `if self._interrupt.is_set(): return StopReason.INTERRUPTED`（runtime.py:645-647）→ 零步骤，answer 为 _partial_answer(INTERRUPTED, []) = "未能完整完成本轮任务：用户中断。\n尚未执行任何工具。"，随后 chat.py:800-801 又覆盖成 _STOP_LABELS["interrupted"]。用户刚打的新问题一行步骤都没有就被判「已中断」。
4) 触发路径 B：第二轮起在建模型/新闻检索阶段点「⏹」→ runtime 已存在 → 同样置位 _interrupt；chat.py:752-756 的前置检查直接 return，worker 从未启动，_interrupt 没人消费 → 下一问照样秒断。
5) 现有用例覆盖不到：test_interrupt_before_runtime_starts_skips_run 用的是 BlockingRuntime（其 request_interrupt 只计数、run 不读中断标记），test_user_interrupt_returns_partial_result 只覆盖「run 进行中」；没有任何用例覆盖「run 已结束时 interrupt() → 下一问仍能正常回答」。清空会话按钮不受影响（new_session 在 chat.py:671-675 interrupt() 后立刻 self._runtime = None，被污染的实例被丢弃）。

影响：每多点一次「⏹」（AC5 的按钮常驻可点），就白烧掉一个完整提问，且页面文案把用户的新问题说成「已中断」，与 AC2/AC5 的可预期行为不符。

**建议**

把「是否真的有 run 在跑」显式建模，只在 run 真正在途时才转发中断；注意不要改成「run() 入口 clear self._interrupt」——tests/test_agent_runtime.py:215-229 已钉住「pre-run interrupt 在首个 guard 生效、本次消费、下次恢复」的 runtime 语义。

建议实现（改动集中在 astock/agent/chat.py）：
1) __init__ 增加 `self._running = threading.Event()`。
2) ask() 的 worker 改为：
       def worker() -> None:
           self._running.set()
           try:
               result = runtime.run(prompt, cancel_event=self._cancelled, on_step=...)
               queue_.put(("result", result))
           except Exception as exc:
               queue_.put(("error", exc))
           finally:
               self._running.clear()
3) interrupt() 改为 `if runtime is not None and self._running.is_set(): runtime.request_interrupt()`；`_busy.locked()` 分支保留，用于返回文案（「已请求中断…」vs「当前没有正在生成的回答。」）。前置检查（chat.py:752-756）依赖 `self._cancelled` 收尾即可，此时不会再置 _interrupt，路径 B 随之消失。
4) 补一条回归用例（真实 AgentRuntime + FakeModel，不 mock runtime）：先完成一问并断言 stop_reason==COMPLETED；再调 controller.interrupt() 断言返回「当前没有正在生成的回答。」；然后 ask("再查一次贵州茅台价格") 断言 result.stop_reason==COMPLETED、len(result.steps)>=1、answer 含 "1680.00"；同一用例再覆盖路径 B（runtime 已存在时在建模型阶段 interrupt → 该轮 0 次 run，随后一问仍 COMPLETED）。当前代码下这两条断言都会失败，可作为修复的验证标准。

## 历史未闭环问题

- P1 · 生产入口未传 db_path：AI 对话的 run 与工具调用都不落 agent_runs/agent_steps（AGENT-02 / ADR-017） · `astock/agent_chat_ui.py:78-84`（第 1 轮）
- P1 · AC2–AC5 缺真机 UI 证据：流式刷新、iframe 渲染、引用跳转、中断按钮只测了纯函数 · `tests/test_agent_chat.py:436-474`（第 1 轮）
- P2 · 每个流式分片都重算引用卡片并同步读盘（glob 全部 meta.json + 整读正文），回答越长越卡 · `astock/agent_chat_ui.py:184-185`（第 1 轮）
- P2 · 中断在「新闻检索/建模型」阶段完全失效且提示误导，本地 cancel Event 是死参数 · `astock/agent/chat.py:676-683`（第 1 轮）
- P2 · 引用只匹配到工作表却显示「定位：…第 N 行/单元格」，精度被夸大且 note 为空 · `astock/agent/chat.py:515-525`（第 1 轮）
- P2 · 测试缺口：引用未命中退回首段、>20 条截断提示、并发发送 busy 分支均无用例 · `tests/test_agent_chat.py:283-340`（第 1 轮）

## 变更文件

- `app.py`
- `astock/agent/chat.py`
- `astock/agent/runtime.py`
- `astock/agent/tools/citations.py`
- `astock/agent_chat_ui.py`
- `tests/test_agent_chat.py`
- `tests/test_agent_runtime.py`
- `tests/test_ci_quality_gates.py`

## 交给开发会话

1. 在分支 `feat/agent-chat-ui` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
