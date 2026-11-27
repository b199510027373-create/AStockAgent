# 调度 5 日生产验收与心跳告警观察（plan-20261114-004）

本文件留存 PR #9（plan-20261103-007，合并 `a9c1248`）落地后的真实交易日验收结果。
2026-11-21 复核时窗口内 11-16/17/18/20 均为 failed，根因是推荐流水线抛
`ContractError: [EventFrame] 列 code 存在空值`（研报 `report_rc` / `research_report`
混入无 `ts_code` 的市场级行），不是调度状态机失败。根因修复随 plan-20261121-001
（PR #43，`442fbb9`）合入：`astock/events.py::_drop_unattributed_reports()` 过滤空
`ts_code` 行并写回 `code`，列缺失仍硬失败；回归用例
`tests/test_data_pit_events.py::ReportEventContractTests`（3 条）。

## 1. 验收窗口与报告

命令（2026-11-27 23:17 由收盘计划任务执行，23:23 人工复跑一致）：

```powershell
.\.venv\Scripts\python.exe -m astock.jobs --report 5
```

结果：`exit 0`，`all_success=true`，`passed=5/5`。报告留存于
`data/recommendations/acceptance_5d.json` + `acceptance_5d.ok`（`2026-11-27 23:17:05`，
data/ 不入库，故在此转录）：

```json
{
  "days": 5,
  "requested": 5,
  "all_success": true,
  "passed": 5,
  "rows": [
    {"trade_date": "2026-11-23", "job_key": "2026-11-23:close", "status": "success", "attempts": 2, "late_by_min": 110.8, "as_of": "2026-11-23", "finished_at": "2026-11-23T18:31:50", "error": null},
    {"trade_date": "2026-11-24", "job_key": "2026-11-24:close", "status": "success", "attempts": 1, "late_by_min": 18.3, "as_of": "2026-11-24", "finished_at": "2026-11-24T16:59:44", "error": null},
    {"trade_date": "2026-11-25", "job_key": "2026-11-25:close", "status": "success", "attempts": 1, "late_by_min": 36.2, "as_of": "2026-11-25", "finished_at": "2026-11-25T17:16:57", "error": null},
    {"trade_date": "2026-11-26", "job_key": "2026-11-26:close", "status": "success", "attempts": 1, "late_by_min": 115.7, "as_of": "2026-11-26", "finished_at": "2026-11-26T18:36:17", "error": null},
    {"trade_date": "2026-11-27", "job_key": "2026-11-27:close", "status": "success", "attempts": 1, "late_by_min": 157.3, "as_of": "2026-11-27", "finished_at": "2026-11-27T19:18:20", "error": null}
  ],
  "generated_at": "2026-11-27T23:23:43"
}
```

`late_by_min` 口径为「实际开跑时刻 − 当日 16:40」，与 jobs 表逐行核对一致
（例如 11-24 `started_at=16:58:21` → `18.3`；11-27 `started_at=19:17:18` → `157.3`）。

## 2. jobs 表状态机现场数据

`data/astock.db` → `jobs`：

| job_key | status | attempts | started_at | finished_at | late_by_min |
| --- | --- | --- | --- | --- | --- |
| 2026-11-23:close | success | 2 | 2026-11-23T18:30:47 | 2026-11-23T18:31:50 | 110.8 |
| 2026-11-24:close | success | 1 | 2026-11-24T16:58:21 | 2026-11-24T16:59:44 | 18.3 |
| 2026-11-25:close | success | 1 | 2026-11-25T17:16:11 | 2026-11-25T17:16:57 | 36.2 |
| 2026-11-26:close | success | 1 | 2026-11-26T18:35:46 | 2026-11-26T18:36:17 | 115.7 |
| 2026-11-27:close | success | 1 | 2026-11-27T19:17:18 | 2026-11-27T19:18:20 | 157.3 |

- 11-23 首次尝试失败后第 2 次成功，`attempts=2` 如实落库；11-24 起均一次成功。
- 11-23 之后连续 5 个交易日 `as_of` 与 `trade_date` 相等，无 stale 误记 success。
- `--once --slot close` 在已 success 的交易日再次执行会被幂等跳过（11-24 实测 exit 0，
  attempts/latest.json 均未变化）。

## 3. 心跳告警现场证据

`python -m astock.jobs --heartbeat`（2026-11-27 23:23）：

```json
{"ok": true, "initialized": true, "threshold_days": 2, "reference_trade_date": "2026-11-27", "streak": 0, "missed_days": [], "last_success": {"job_key": "2026-11-27:close", "trade_date": "2026-11-27", "finished_at": "2026-11-27T19:18:20"}, "alerted": false, "alert_path": null}
```

历史窗口（`data/recommendations/scheduler_task.log` 与 `scheduler_alerts.jsonl`）：

| 日期 | 上一成功 | streak | alerted | 结论 |
| --- | --- | --- | --- | --- |
| 2026-11-18 | 2026-11-06 | 8 | true（`signature=2026-11-18:8:2`） | 连续 ≥2 个交易日未 success，告警落盘 |
| 2026-11-19 | 2026-11-06 | 9 | true（`signature=2026-11-19:9:2`） | 仍在停摆，按日签名各记一条 |
| 2026-11-20 | 2026-11-19 | 1 | false | 仅 1 日失败，不触发 |
| 2026-11-23 起 | 当日 success | 0 | false | 恢复后不再告警 |

签名去重：同一 `reference_trade_date:streak:threshold` 24h 内重复检查只落一条；
自动化用例 `tests/test_scheduler_ledger.py::test_heartbeat_dedup_uses_persisted_alert_file`
与 `test_heartbeat_alerts_only_after_two_missing_trading_days` 覆盖「1 日不告警、
2 日告警、同日重复不重复写」。

## 4. 自动化验证（2026-11-27，main `84f459c`）

| 命令 | 结果 |
| --- | --- |
| `python -m pytest tests/test_scheduler_ledger.py tests/test_hardening.py -q` | **50 passed**, 1 warning, 6.92s |
| `python -m pytest tests/test_data_pit_events.py -q` | **29 passed**（含空 code 回归 3 条） |
| `python -m pytest tests -q` | **956 passed, 3 skipped, 46 subtests passed**, 176.48s |
| `python -m mypy astock app.py` | Success: no issues found in 45 source files |

注：在 DSH workspace-write 沙箱下，pytest 新建临时目录后写入会被拒绝（`WinError 5`），
表现为 12 failed/3 errors 的假失败；切换到 danger-full-access 后同一命令全绿。此为
运行环境限制，与代码无关，已在交付 handoff 的 permissions 中记录。

## 5. 验收结论（对照 plan-20261114-004）

| 验收项 | 结论 |
| --- | --- |
| EventFrame 空 code 根因修复 + 回归 | 通过：`_drop_unattributed_reports` + `ReportEventContractTests`（过滤空行、全空返回空帧、缺列仍 ContractError） |
| `test_scheduler_ledger.py` + `test_hardening.py` | 通过：50 passed |
| 修复后连续 5 个交易日全 success + 留存报告 | 通过：11-23..11-27 `passed=5/5`，报告已留存 |
| 心跳 2 日触发 / 1 日不触发 / 签名去重 | 通过：11-20 streak=1 不告警；11-18/19 告警；单测覆盖去重 |
| running 超时恢复 failed、到期登记 pending、late_by_min | 通过：`test_recover_interrupted_running_job`、`test_sync_due_job_marks_missing_pending_only_after_run_time`、生产 5 行 late_by_min 与 started_at 对账 |
| 空台账 / 长期停摆不误报 | 通过：`test_startup_maintenance_marks_due_job_without_empty_history_alert`（空台账不告警）、`test_heartbeat_detects_long_outage_beyond_lookback_window`（长期停摆正确告警且 streak 封顶） |

剩余风险：窗口内仅 11-23 出现一次失败重试（attempts=2），11-24..11-27 均一次成功；
若后续再次出现供应商字段漂移，需按 `ContractError` 文案定位并补充回归。
