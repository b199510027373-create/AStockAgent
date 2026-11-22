# PR 评审：评审：调度状态机迁移 jobs 表 + 心跳告警

> 自动生成于 2026-11-12T18:52:48.400Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261113-001`
- 计划项：`plan-20261103-007`
- 分支：`feat/plan-20261103-007-scheduler-ledger` → `main`
- 轮次：第 2 轮（diff）
- 结论：通过（approve）
- 开发会话：`session-680f1ea7-de6d-4144-b50d-61c8204ae454`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 2 / P2 4

## 本轮结论

第 2 轮 diff 复审（base=e2e4146 → aabc2cb，scope=diff）。复核开发者交付 ho-20261113-004 声称的 5 项修复，逐条对照当前工作树代码与测试，结论：第 1 轮 2 条 P1 + 3 条 P2 的实现与测试均已落实，未发现 P0；本轮新增 1 条 P2（长期停摆盲区，不阻断）。

【第 1 轮问题复核（逐条，均认可）】
1. P1 iss-mwdsn7s3-6954f5（15:00–16:40 把当天未到期 close 计为漏跑）→ 已修复。新增 `_due_reference()`（scheduler.py:53-63），run_time 之前取当天 00:00；`_recent_trading_days`（116）、`heartbeat_check`（273）、`acceptance_report`（379 经 _recent_trading_days）统一使用。测试补齐 test_report_at_1530_excludes_not_yet_due_today、test_heartbeat_at_1530_does_not_count_today、test_report_at_1530_returns_zero_for_completed_days、test_heartbeat_cli_at_1530_returns_zero_when_only_yesterday_failed（tests/test_scheduler_ledger.py:584-615 等），CLI 级验证 15:30 退出码 0。
2. P1 iss-mwdsn7t0-8b6b2b（台账异常导致调度线程静默退出）→ 已修复。run_once 内 `_prepare_ledger`、`ledger.get_job`、起始 save_job、失败分支 update_job 全部纳入 try/except 并 `_log`；startup_maintenance 四步各自隔离（scheduler.py:407-444）；loop 的启动自检与 while 体整体 try/except + 600s 重试（约 629-668）。测试 test_run_once_survives_ledger_write_failure、test_startup_maintenance_survives_ledger_write_failure、test_loop_survives_startup_failure 覆盖 sqlite3.OperationalError 路径。
3. P2 iss-mwdsns2o-7c2e72（去重只在进程内）→ 已修复。删除模块全局，新增 `_alert_signature()`（232-233）与 `_last_alert_signature()`（236-262），写盘记录带 `signature` 字段并从 JSONL 尾部回读；test_heartbeat_dedup_uses_persisted_alert_file 覆盖“重启后同签名仍只一行”（含无 signature 旧行的兼容重建）。
4. P2 iss-mwdsnve8-4643e3（空台账首启即告警）→ 已修复。heartbeat_check 增加 `initialized` 判定与 `no_history` 早退（308-328），missed 以窗口内最早 job 为下界截断（330-333）；test_startup_maintenance_marks_due_job_without_empty_history_alert 断言空库 16:45 → due 已登记、ok=True、initialized=False、alerted=False、ALERTS_PATH 不存在。
5. P2 iss-mwdsnvf7-7056ec（loop 无测试）→ 已修复。新增 test_loop_runs_due_job_once_and_records_success、test_loop_waits_600_after_stale_and_does_not_rerun、test_loop_survives_startup_failure，用 FrozenDateTime + 预置 Event 驱动 loop，断言 pending→running→success、run_recommendation 仅一次、stale 后 600s 不重跑。手工推演这些用例与现有实现一致（`_due_reference` 16:40 起生效、周末/盘前不变；FrozenDateTime 与 last_completed_by_clock 配对正确）。
- 全仓已无 `_LAST_ALERT_SIGNATURE` 残留；tests/test_hardening.py 仍以 jobs 表断言双键/attempts/stale，未受本次改动破坏；ADR-004 §9/§10 同步更新，与实现口径一致。开发者证据：49 passed（两个文件）、319 passed 1 skipped、mypy 31 files、ruff E9,F63,F7,F82、compileall exit 0；测试执行属开发者职责，本轮不重复执行。

【本轮新增（不阻断）】
- P2 iss-mwdwnlvb-1e70b4（astock/scheduler.py:29,114-127,308-334）：`initialized` 只在 60 个交易日窗口内判断，若最近 60 个交易日无任何 close 行但更早有历史（停机 >3 个月、或从旧备份恢复 db），会被判为 no_history → `--heartbeat` 退出 0 且不告警，与 ADR-004 §10“尚无任何历史记录才 initialized=false”不符。修复建议：initialized 改为全表存在性判断，窗口内无行时把 streak 截断为 60 并照常告警；补一条“仅有 2026-06-01 success、now=2026-11-06 16:45 → streak=60、alerted=True”的用例。归开发者，可在下一次 scheduler.py 改动时一并提交，不要求阻塞本次合并。

【已知小权衡（不记问题）】台账不可用时 run_once 返回 None，`python -m astock.jobs --once` 因此仍以 0 退出，仅 scheduler.log 留痕；--once 属人工调试入口，主路径 loop 已在日志中体现失败并有重试，可接受。README 未补充 --heartbeat/--report/--maintenance 与 scheduler_alerts.jsonl 用法，属文档增强项。

【结论与完全解决判定】本轮无 P0、无未关闭 P1（20261113-001 的 2 条 P1 实现与测试均已落实，待工具状态同步后 openP1 应为 0）；不再要求返工。完全解决条件：①开发者把第 1 轮 5 条 issue 状态更新为 fixed（本轮已完成复核）；②合并本分支到 main，并在真实环境跑 `python -m astock.jobs --report 5`，连续 5 个交易日 all_success=True、退出码 0；③观察一个失败周期确认 scheduler_alerts.jsonl 出现 kind=scheduler_heartbeat 且同一签名不重复。满足后由评审将线程标记 resolved=true。P2 iss-mwdwnlvb-1e70b4 可作为后续小项跟进，不阻塞解决判定。

## 本轮问题

### P2 · 长期停摆（>60 个交易日无任何 close 行）会被判成“无历史”，心跳永远不告警

- 位置：`astock/scheduler.py:29,114-127,308-334`
- 状态：未闭环
- 置信度：0.8

**证据**

`initialized` 只在 `_recent_trading_days(now, HEARTBEAT_LOOKBACK_DAYS)` 返回的窗口内判断（scheduler.py:308-313），而 `_recent_trading_days` 固定截断到 60 个交易日（scheduler.py:127 `return days[:count]`，HEARTBEAT_LOOKBACK_DAYS=60 见 29 行）。因此当台账里最近 60 个交易日一行 close 记录都没有、但更早存在历史行时（例如机器停机 3 个月以上后重启、或从旧备份恢复 astock.db），循环拿到的 `records` 全是 `job is None`（284-291），`initialized` 为 False，直接走 314-328 返回 `{"ok": True, "initialized": False, "reason": "no_history"}`，`python -m astock.jobs --heartbeat` 退出码 0、不写 scheduler_alerts.jsonl。这与 ADR-004 §10 的措辞不符：文档写的是「台账只有当天 pending、**尚无任何历史记录**时才 initialized=false 且不告警」，而实现把“窗口内没有记录”等同于“没有历史记录”。后果恰好落在心跳最该报警的场景：长时间完全停跑时监控静默通过。现有测试也未覆盖（test_startup_maintenance_marks_due_job_without_empty_history_alert 只测空库，tests/test_scheduler_ledger.py:350-370 附近）。

**建议**

把“是否有历史”与“窗口”解耦：`initialized` 改为查全表是否存在任意 close 行（如 `ledger.list_jobs(slot="close", limit=1)` 非空即视为已初始化），窗口内没有行时把 `streak` 截断为 `HEARTBEAT_LOOKBACK_DAYS` 并照常告警（signature 用 reference+streak+threshold 即可，天然去重）。补一条用例：库里只有 2026-06-01 一条 success、now=2026-11-06 16:45 → `initialized=True`、`ok=False`、`alerted=True`、`streak==60`。此项为 P2，不阻断本次 approve，可与下一次 scheduler.py 改动一起提交。

## 历史未闭环问题

- P1 · 心跳/验收报表在 15:00–16:40 把当天未到期的 close 任务计为漏跑 · `astock/scheduler.py:229-239, 298-303`（第 1 轮）
- P1 · 台账异常会让调度线程静默退出（save_job/_prepare_ledger 在 try 之外） · `astock/scheduler.py:417, 435-443, 514-536`（第 1 轮）
- P2 · 心跳告警去重只在进程内生效，重启后同一签名重复落盘 · `astock/scheduler.py:33, 260-277, 330-336`（第 1 轮）
- P2 · 空台账首次启动即产生 60 日连续漏跑告警 · `astock/scheduler.py:29, 233-257, 330-336`（第 1 轮）
- P2 · 调度执行链 loop() 无测试覆盖（pending→running→success 与失败重试） · `tests/test_scheduler_ledger.py:127-358`（第 1 轮）

## 变更文件

- `README.md`
- `astock/jobs.py`
- `astock/ledger.py`
- `astock/scheduler.py`
- `tests/test_hardening.py`
- `tests/test_scheduler_ledger.py`
- `tools/build_product_doc.py`

## 交给开发会话

1. 在分支 `feat/plan-20261103-007-scheduler-ledger` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
