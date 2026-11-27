# PR 评审：评审：PR #9 调度 5 日生产验收与心跳告警观察

> 自动生成于 2026-11-27T16:33:17.061Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261128-001`
- 计划项：`plan-20261114-004`
- 分支：`main` → `main`
- 轮次：第 2 轮（full）
- 结论：需要修改（request_changes）
- 开发会话：`session-26fd9368-10b9-4097-8e89-204c2931b1c4`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 1 / P2 3

## 本轮结论

第 2 轮 full 复核完成（round 2）。开发者 d27f936 对三条问题的文档修复均已逐条核对：

1) P1（iss-mwz67nhp-876f04）：新增 §2.1（docs/AStockAgent_scheduler_5d_acceptance_2026-11-27.md L77-130），引用 scheduler_task.log L3379-3381（11-23 双 start、首跑无 exit）与 L3855-3895（11-25 16:45 TushareError token已过期、heartbeat_failed、report JSONDecodeError、17:16 重跑 exit=0），并说明失败发生在 scheduler.py:527 `last_completed_trade_date` 之前，早于 get_job(535)/save_job(547)，故无 jobs 行、attempts=1；token 已续期，11-23..11-27 5/5 success 结论不变。引用行号与日志一致，技术内容通过。

2) P2-a（iss-mwz67nj7-d58f04）：§1 L21-35 改为以 2026-11-28T00:06:01 手工快照为锚，给出 report/heartbeat sha256，并说明 acceptance_5d.json/.ok 每约 30 分钟被计划任务重写；快照 rows 与 23:47:05 留存件一致。通过。

3) P2-b（iss-mwz67nky-ab84ba）：§3 L151-160 改为只与最后一条 scheduler_heartbeat 签名比较、明确没有 24h 时间窗口，引 scheduler.py:344/236-262/232-233 与测试 L418/L268。通过。

4) 本轮新记 P2（iss-mwz7a1ls-b51941）：§2.1 L125 把 11-25 17:17/17:45 的 report 5 exit=2 解释为「窗口仍含 11-18/11-20 旧失败」；实际 `_recent_trading_days`（scheduler.py:114-127）在 17:17 的窗口是 11-19..11-25，11-18 不在内，且 §3 表 L148 表明 11-19 已 success，窗口内旧失败只有 11-20。需改文案或补当次 stdout。

阻塞不在技术内容：4 条 issue 在 .devloop/reviews/rev-20261128-001/issues/ 仍 status=open、feedback=null；devloop_review 无关闭入口，需开发侧回填 feedback=fixed（引用 d27f936 与具体行号），或由用户在 DevLoop UI 关闭。当前 openP1=1、openP2=4，故 verdict=request_changes，线程不标记完全解决。

完全解决条件：a) 开发者修正 L125；b) 将 4 条 issue 标记 fixed；c) 评审重跑 finish，openP0=0 且 openP1=0 后 approve，plan-20261114-004 方可置 done。无需再改代码；79 passed/mypy 45 files/CI 46031056050 采信 developer handoff，本会话无 shell 未独立执行。

## 本轮问题

### P2 · §2.1 对 11-25 report 5 失败窗口的说明不准确：11-18 不在窗口内，11-19 已 success

- 位置：`docs/AStockAgent_scheduler_5d_acceptance_2026-11-27.md:125`
- 状态：未闭环
- 置信度：0.86

**证据**

doc L125 称「17:17/17:45 的 report 5 exit=2 是窗口仍含 11-18/11-20 旧失败」。但 `astock/scheduler.py:114-127` 的 `_recent_trading_days(now, 5)` 以 `last_completed_trade_date(_due_reference(now))` 为 reference；`astock/data.py:87-101` 在 15:00 后返回当天，故 11-25 17:17 的 reference=11-25，窗口为 11-19..11-25，不含 11-18。又据同一文档 §3 表 L148「11-20 上一成功 11-19，streak=1」，11-19 已在盘后补跑成功；窗口内旧失败只有 11-20，不是 11-18/11-20。该错误只影响对 17:17/17:45 exit=2 原因的解释，不改变 11-23..11-27 5/5 success 的结论。

**建议**

将 L125 改为「窗口仍含 11-20 旧失败（11-19 已于盘后补跑成功）」，或直接引用 17:17/17:45 两次 `--report 5` 的 stdout 中 5 行 status 作为证据；不要写 11-18。若无法取回当次 stdout，则用 `_recent_trading_days` 的窗口推导 + §3 的 heartbeat 表说明，并注明 11-18 不在窗口内。

## 历史未闭环问题

- P1 · 五日验收遗漏 11-25 16:45 计划任务失败（token 过期），“11-24 起均一次成功”的结论不成立 · `docs/AStockAgent_scheduler_5d_acceptance_2026-11-27.md:55, 106`（第 1 轮）
- P2 · 留存报告时间与文档转录不一致，且 acceptance_5d.json/.ok 每 30 分钟被计划任务重写 · `docs/AStockAgent_scheduler_5d_acceptance_2026-11-27.md:13, 19-21, 36`（第 1 轮）
- P2 · 心跳去重描述“24h 内只落一条”与实现不符：仅比较最后一条签名，无时间窗口 · `docs/AStockAgent_scheduler_5d_acceptance_2026-11-27.md:77`（第 1 轮）

## 变更文件

未获取到变更文件清单；请用 `devloop_git files` 重新获取。

## 交给开发会话

1. 在分支 `main` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
