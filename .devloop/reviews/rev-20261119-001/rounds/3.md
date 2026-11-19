# PR 评审：评审：Agent 会话 UI：流式 / 步骤 / 附件 / 内嵌图

> 自动生成于 2026-11-19T07:10:56.621Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261119-001`
- 计划项：`plan-20261107-007`
- 分支：`feat/agent-chat-ui` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-b2a69179-5e60-4872-91f0-f30a7d3402d1`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 0 / P2 1

## 本轮结论

第 3 轮（scope=full，范围 main → a1e15d1，本轮变更集中在 5cceb3c 的 astock/agent/chat.py 与 tests/test_agent_chat.py）结论：approve，openP0=0 / openP1=0；新记 1 条 P2（iss-mwn7nzfl-0c196c，不阻断验收，需在合并 PR #32 前二选一定稿）。

一、第 2 轮 P1（iss-mwmvlhso-cd6199）已闭环，逐条核实
1. 三态中断判定（astock/agent/chat.py:703-731）：run 在途（`self._running.is_set()`）→ 置 `_cancelled` + `runtime.request_interrupt()`，返回「已请求中断…」；空闲（`not self._busy.locked()`）→ 直接返回「当前没有正在生成的回答。」且不改任何状态；busy 但 run 未在途（建模型/检索阶段或流式阶段）→ 只置 `_cancelled`/`_stop_output`，绝不碰 runtime 的中断标记。第 2 轮的两条污染路径（run 已返回后点中断、runtime 已缓存时预运行阶段中断）均已被这条分支切断。
2. worker 侧生命周期（chat.py:779-796）：`self._running.set()` 在 `runtime.run(...)` 之前，`finally: self._running.clear()` 在投递 result 之前——与注释一致，主线程开始流式输出时 runtime 已不在途，因此流式阶段的点击不会再被误判为「在途中断」。
3. run 在途时的既有语义未被破坏：`_cancelled` 仍作为 `cancel_event` 传入（chat.py:783-787），`_guard` 会在检查点生效；`AgentRuntime._interrupt` 只在 run() finally 清理（runtime.py:512）这一约束没有被绕过，也没有改成「run() 入口 clear」，tests/test_agent_runtime.py:215-229 钉住的 pre-run 语义保持原样。
4. 回归用例到位（tests/test_agent_chat.py 新增 3 条）：test_interrupt_after_finished_run_does_not_poison_next_question（空闲点击 → 「没有正在生成」→ 第二问 COMPLETED、1 步、answer 含 1680.00、done 不含「已中断」）、test_stop_output_during_streaming_keeps_text_and_next_question_completes（流式阶段点击 → 保留已产出分片、下一问 COMPLETED）、test_interrupt_while_runtime_ready_does_not_poison_next_question（runtime 已缓存时在 context_provider 阶段中断 → 本轮 0 步中断收尾、下一问仍 COMPLETED）。三条都直击第 2 轮缺陷，不是形式化断言。
5. 真机证据：.smoke-agent-ui/round3_drive.py 存在，shots/9-round3-idle-interrupt.png、10-round3-next-question.png、11-round3-midrun-interrupt.png 在评审工作区可见，handoff ho-20261119-015/016/017 记录 PASS 4/4（基线 3 步/408 字、空闲中断提示、第二问 3 步/408 字、生成中中断保留 121 字）。
6. 质量门禁与交付记录：87 passed（84+3）、全量 747 passed / 79.07%、ruff/mypy 43 files/import app 全过、CI run 44439808455 与 44440100842 success（因本会话无 shell 工具，这两项依据 handoff ho-20261119-016/017 记录，未由我独立执行）。

二、第 1 轮 6 条复核（台账里已由开发回执 fixed，本轮再抽验，确认属实）
- iss-mwmtsmue-f7a49c：app.py:481-487 传 `db_path=AGENT_DB_PATH`，chat.py:677-680 透传；test_run_and_step_records_are_persisted 直查 agent_runs/agent_steps。
- iss-mwmtsmv4-4f6d7f：8 张截图 + CDP 驱动脚本 + AC1–AC5 断言记录。
- iss-mwmtt39d-d8e449：citations_block() 指纹缓存 + render_citations_html(memo=...)，test_citation_html_rendered_once_per_round 约束。
- iss-mwmtt3ab-775ed6：已由 chat.py:748-756 前置检查替代，并补齐三态语义（本轮再确认无误）。
- iss-mwmtta2g-c95a18：matched_scope + 工作表级 note，test_sheet_level_match_with_cell_does_not_claim_cell_precision。
- iss-mwmtta3c-cfdaaa：页未命中回退、>20 截断、busy 并发三条分支均有用例。

三、验收标准对照（全部有代码 + 测试/真机证据）
AC1 附件 1–5 + 解析状态：ingest_attachments/attachment_summary + handle_uploads，测试 csv+txt 与 6→5 上限；真机 2/2。
AC2 流式 + 步骤时间线：iter_answer_chunks（40 字）+ render_steps_html（enumerate start=1、data-status），真机 20 帧/3 步。
AC3 内嵌图 + spec/来源：figure_embed_html（iframe sandbox=allow-scripts、srcdoc 已转义、details 内 spec ≤4000/来源/artifact），真机 3 个 iframe。
AC4 引用定位：collect_citations/resolve_citation/render_citations_html（cite-N 锚点、limit 20、未命中退回首段并给 note），真机点击 #cite-1。
AC5 中断保留：三态中断 + 3 条回归 + 真机 121 字保留且可立刻再问。
降级：model_banner 捕获异常出横幅、ask 在 _ensure_runtime 失败时 done 提示，真机 degraded 横幅 + 128 字回答。

四、残存风险结论（第 2 轮第三节的答复）
开发在 ho-20261119-015/016 明确答复「接受 respond() 里 queue_.get() 的阻塞式收取：生产路径阻塞有界（httpx 客户端 read timeout + runtime 每检查点 timeout_seconds），仅注入的 FakeModel 可无限阻塞，不用于生产」。我接受该取舍，不记为问题。补充两条供后续排期参考（不阻断）：①interrupt() 在 `_running.is_set()` 与 `request_interrupt()` 之间有极小的 TOCTOU 窗口（run 恰好在此刻返回并在 finally 清标记），若后续要彻底消除，建议在 AgentRuntime 侧加「非在途则忽略 request_interrupt」的自检；②.smoke-agent-ui/ 未被 git 跟踪，冒烟脚本与截图只存在于评审工作区与 handoff，换机器不可复现，建议后续入计划项或纳入仓库。

五、本轮唯一未闭环项（P2，不阻断）
iss-mwn7nzfl-0c196c：astock/agent_chat_ui.py:208-214 的 done 分支用完整 `result.answer` 覆盖了控制器已截断的分片（chat.py:817-831），导致流式阶段点「停止输出」后界面仍一次性显示全文，与状态栏「已停止输出：已生成的回答保留」矛盾。由开发在合并 PR #32 前二选一：方案 A 给 ChatEvent 加 stopped 标记并跳过覆盖 + 控制器/UI 两层用例 + 真机 shots/12；方案 B 保留现状但改文案为「已停止流式输出，完整回答已显示」并在 handoff 写明取舍。此条不影响 AC1–AC5 达标，故本轮 approve，plan-20261107-007 可置 done / progress 100；若选方案 A 建议同一 PR 内完成，选方案 B 则需在 PR 描述里写明该产品取舍。

## 本轮问题

### P2 · 流式阶段点「停止输出」被 done 帧覆盖：状态栏说已停止输出，界面却一次性显示完整回答

- 位置：`astock/agent_chat_ui.py:208-214`
- 状态：未闭环
- 置信度：0.86

**证据**

5cceb3c 新增的「流式阶段停止输出」在控制器侧生效，但在 UI 层被 done 帧覆盖掉：

1) 控制器截断：astock/agent/chat.py:817-831 —— 分片循环每 yield 一次就检查 `if self._stop_output.is_set(): break`，并把 done 文案改为「⏹ 已停止输出：已生成的回答保留。」，done 事件仍带 `result=result`（完整 answer）。
2) UI 覆盖：astock/agent_chat_ui.py:208-214 的 done 分支
       result: AgentRunResult | None = event.result
       if result is not None:
           ...
           if result.answer:
               chat[-1]["content"] = result.answer
   即 done 帧无条件用完整 `result.answer` 覆盖已累积的分片内容。于是浏览器里点「⏹」后：流式追加确实停住（控制器 break），但紧接着 done 帧把整段回答一次性贴上去，用户看到的是「先停住、再全文出现」，与状态栏同时显示的「已停止输出：已生成的回答保留」自相矛盾，本轮新加的 `_stop_output` 在 UI 上等于只省了几帧、没有真正「停止输出」。
3) 现有用例覆盖不到这个组合：新加的 tests/test_agent_chat.py::test_stop_output_during_streaming_keeps_text_and_next_question_completes 只在控制器事件层断言（done.text 含「停止输出」、answer 非空、下一问 COMPLETED），完全不经过 respond()；真机 round3_drive.py 的 4 条断言是基线 / 空闲点中断 / 第二问 / 生成中（run 在途）中断，也没有覆盖「run 已返回、流式中途点停止」这一路（.smoke-agent-ui/shots/11-round3-midrun-interrupt.png 是 run 在途场景）。

影响：不丢内容、不影响 AC5 的「已产出内容保留」，因此按提示级（P2）记录，不阻断本轮；但按钮语义与界面文案不一致，会让人误以为有 bug。

**建议**

二选一，合并 PR #32 前定稿并写进 handoff：

方案 A（推荐，真正实现「停止输出」）：给 ChatEvent 增加一个明确标记（如 `stopped: bool = False`），控制器在 chat.py:827-828 置 `_stop_output` 时把它设为 True 并在 done 事件上带出；agent_chat_ui.py:213-214 改成
    if result.answer and not getattr(event, "stopped", False):
        chat[-1]["content"] = result.answer
这样停止输出时保留已累积的分片，done 只更新状态栏。补两条用例：①控制器层断言 done.stopped is True 且 `"".join(answer 分片)` 是 result.answer 的严格前缀；②UI 层用 respond() 直接驱动（像 test_citation_html_rendered_once_per_round 那样调用生成器），在第二个 answer 帧后调 controller.interrupt()，断言最终 chat[-1]["content"] 等于已累积分片而不是完整 result.answer。并把 round3_drive.py 增加一条「流式中途点停止」的真机断言（截图 shots/12）。

方案 B（接受现状）：在 astock/agent_chat_ui.py:213-214 上方加注释说明「停止输出只停止流式追加，done 帧仍显示完整回答」，同时把控制器 done 文案改成不宣称截断（例如「⏹ 已停止流式输出，完整回答已显示」），避免文案与界面不一致；并在 handler/handoff 里明确这是产品取舍。

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
