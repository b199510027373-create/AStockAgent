# PR 评审：评审：Agent 会话 UI：流式 / 步骤 / 附件 / 内嵌图

> 自动生成于 2026-11-19T10:19:28.693Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261119-001`
- 计划项：`plan-20261107-007`
- 分支：`feat/agent-chat-ui` → `main`
- 轮次：第 4 轮（diff）
- 结论：通过（approve）
- 开发会话：`session-b2a69179-5e60-4872-91f0-f30a7d3402d1`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 0 / P2 1

## 本轮结论

第 4 轮（scope=diff，a1e15d1→cb2931e，实质代码提交 fb185dc）复核结论：approve，openP0=0 / openP1=0，本轮不新增问题。

一、所审改动范围（已逐文件读 diff）
- astock/agent/chat.py（+50/-19）：ChatEvent 新增 `stopped: bool = False`（chat.py:631）；ask() 内新增 `released` 标志与内部 `finish(text, *, result=None, stopped=False)`（chat.py:751-763）；四处终止分支统一走 finish（模型不可用 chat.py:779、预运行中断 chat.py:789、worker 异常 chat.py:822、正常结束 chat.py:844）；分片循环后取 `stopped = self._stop_output.is_set()`（chat.py:836）并覆盖文案「⏹ 已停止输出：已生成的回答保留。」（chat.py:840-841）；finally 改为 `if not released: self._busy.release()`（chat.py:849-851）。
- astock/agent_chat_ui.py（+4/-1）：done 分支改为 `if result.answer and not event.stopped:`（agent_chat_ui.py:213-216）。
- tests/test_agent_chat.py（+56）：新增 test_done_event_releases_lock_before_generator_is_drained（tests/test_agent_chat.py:567-586）、test_stop_output_keeps_partial_answer_in_chat（tests/test_agent_chat.py:780-806），并给原停止输出用例补 `done.stopped` 断言（tests/test_agent_chat.py:665）。

二、逐项核对（iss-mwn7nzfl-0c196c 闭环判定）
1. 分片保留：UI 只在 `not event.stopped` 时用 result.answer 覆盖，停止输出后 chat[-1]["content"] 保持已 streamed 分片；UI 全链路用例断言 final[0][-1]["content"] 非空且短于完整回答，状态栏为「停止输出」——第 3 轮 P2 的核心矛盾（状态说已停止、界面显示全文）已消除。
2. 释放恰好一次：finish() 用 `nonlocal released` 幂等释放，finally 兜底；空问题（chat.py:741-743）与「上一轮仍在生成」（chat.py:744-746）两个分支都在 acquire 之前返回，不会误释放他人锁；GeneratorExit 关闭生成器时 finally 也不会二次释放。
3. 提前放锁的前提成立：done 之前 runtime.run 已返回（chat.py:818-820 break），worker 已在 finally 清 `_running`（chat.py:805-807），因此不存在「锁已释放但仍有 run 在途」导致两个 run 并发的窗口；interrupt() 的三态判断（chat.py:714-728）在锁释放后返回「当前没有正在生成的回答。」，与界面一致。
4. stopped 语义不会被下一问污染：stopped 在 finish 之前取值（chat.py:836 → 844），下一问的 clear（chat.py:747-748）发生在之后，无竞态。
5. 浏览器拿到 done 不再 next() 的场景：生成器被弃置时 busy 已在 yield done 之前释放，用例以「拿到 done 即 break」验证第二问不再被「上一轮仍在生成」拒绝，且第二问 COMPLETED——真机暴露的永久占用缺陷已修。

三、测试充分性
- 控制器层与 UI 层各有一条针对本缺陷的用例（断言 stopped 标记、界面分片保留、done 后不再消费仍可再问），覆盖了修复的三个失败面；测试执行由开发负责，本轮不代理执行。

四、无法独立验证的部分
本会话无 shell（只有 devloop_git/read/grep/glob），pytest 89 passed / 749 passed 79.09% / ruff / mypy 43 / import app 与 CI run 44482153964(fb185dc)、44495904965(cb2931e) 均依据 ho-20261119-019/020 与 planItem.links.test 记录，未复跑。

五、残存非阻断项（不记级别，供合并前留意）
1. 停止输出后引用面板仍以完整回答抽取（agent_chat_ui.py:212）：只在被隐藏尾部出现的引用卡片无法跳到界面上的锚点，但卡片自带文件名+页码/工作表定位，AC4 仍满足。
2. finish() 提前放锁后，done 之后的 `self.session.save()`（chat.py:845-848）落在锁外；生产入口未传 session_path（app.py:481-487 只传 context_provider 与 db_path），仅影响测试/嵌入式用法。
3. PR #32 仍未合并、.smoke-agent-ui 真机脚本与截图未入库（属交付流程事项，非代码缺陷）。
4. queue_.get() 阻塞收取 + 上游永不返回的极端场景仍依赖 httpx 超时兜底（第 3 轮已接受）。

六、结论与责任
本轮批准，plan-20261107-007 的 5 条验收标准在代码、测试与真机证据三方面均已覆盖，第 1–3 轮 7 条已修复问题无回归。剩余动作由开发执行：合并 PR #32（https://github.com/b199510027373-create/AStockAgent/pull/32）到 main，并在合并后确认 CI 仍绿；不需要再改代码。完成这两步即判定完全解决。若之后有人改动 ask()/interrupt() 的锁与事件时序（_busy/_cancelled/_stop_output/_running 的关系），须同时补一条同形态回归用例，否则视为回归风险。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P2 · 流式阶段点「停止输出」被 done 帧覆盖：状态栏说已停止输出，界面却一次性显示完整回答 · `astock/agent_chat_ui.py:208-214`（第 3 轮）

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
