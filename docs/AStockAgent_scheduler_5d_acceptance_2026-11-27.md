# 调度 5 日生产验收与心跳告警观察（plan-20261114-004）

本文件留存 PR #9（plan-20261103-007，合并 `a9c1248`）落地后的真实交易日验收结果。
2026-11-21 复核时窗口内 11-16/17/18/20 均为 failed，根因是推荐流水线抛
`ContractError: [EventFrame] 列 code 存在空值`（研报 `report_rc` / `research_report`
混入无 `ts_code` 的市场级行），不是调度状态机失败。根因修复随 plan-20261121-001
（PR #43，`442fbb9`）合入：`astock/events.py::_drop_unattributed_reports()` 过滤空
`ts_code` 行并写回 `code`，列缺失仍硬失败；回归用例
`tests/test_data_pit_events.py::ReportEventContractTests`（3 条）。

## 1. 验收窗口与报告

命令：

```powershell
.\.venv\Scripts\python.exe -m astock.jobs --report 5
```

结果：`exit 0`，`all_success=true`，`passed=5/5`。

时间口径（评审 P2 更正）：11-27 的 jobs 行由 19:16:52 的 close 运行产生
（`19:18:22 exit=0`）；`19:18:34` 起计划任务每约 30 分钟幂等重跑一次 `report 5`
（19:18:34、19:47:14、20:17:10 … 23:17:05、23:47:05），每次都会重写
`data/recommendations/acceptance_5d.json` 并在通过时重写 `acceptance_5d.ok`
（`data/recommendations/run_daily_job.py` 先 `MARKER.unlink()` 再写 REPORT/MARKER），
所以这两个文件的 mtime 不是稳定锚点。本文以 **2026-11-28T00:06:01 手工运行快照**
为准，快照固化在：

- `docs/AStockAgent_scheduler_5d_report_2026-11-28.json`，SHA256
  `E21FD988F785FD0EEE64CE4340B8BB0949C50FC6B56B518F05CEAEBDE898D121`
- `docs/AStockAgent_scheduler_5d_heartbeat_2026-11-28.json`，SHA256
  `13098E3F316925A6B643EFBD8CBAB5CDF761B8715BB34D0C7E83E1BDC7A9AEE7`

11-27 23:17:05、23:23:43、23:47:05 是同一日多次等价运行（仅 `generated_at` 不同），
不是独立的验收证据。以下为 2026-11-28T00:06:01 快照（rows 与 23:47:05 留存件一致）：

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
  "generated_at": "2026-11-28T00:06:01"
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

- 11-23 的 `attempts=2` 来自 18:24:23 与 18:30:39 两次启动：首跑在写入
  `running(attempts=1)` 后进程中断（无 exit/traceback），次跑读到 attempts=1 后置 2 并成功。
- 11-25 16:45 计划任务在写 jobs 行之前因 `token已过期` 崩溃，故 11-25 仍为
  `attempts=1`；失败细节与影响见 §2.1。
- 11-23 之后连续 5 个交易日 `as_of` 与 `trade_date` 相等，无 stale 误记 success。
- `--once --slot close` 在已 success 的交易日再次执行会被幂等跳过（11-24 实测 exit 0，
  attempts/latest.json 均未变化）。

### 2.1 窗口内异常（评审 P1 补充）

行号对应 `data/recommendations/scheduler_task.log`（UTF-8）。

**（1）11-23 18:24:23 首跑无 exit/traceback（L3379-L3381）：**

```text
3379: [2026-11-23 18:24:23] close job start
3380: [2026-11-23 18:30:39] close job start
3381: [2026-11-23 18:31:52] close job exit=0
```

判定：18:24 的 `run_once` 已写入 `running(attempts=1)`，随后进程被中断（没有 Python
traceback，`run_daily_job.py` 的 subprocess 也没有 exit 行）；18:30:39 重跑读到
`attempts=1` → 2 并成功。`attempts=2` 与日志双 start 互证，不是“首次尝试失败”，
而是进程中断后重跑。对验收的影响：当日 job 仍 success，未产生 failed 行。

**（2）11-25 16:45 计划任务失败：Tushare token 过期（L3855-L3894）：**

```text
3855: [2026-11-25 16:45:00] close job start
3856: [2026-11-25 16:45:14] close job exit=1
3857: [2026-11-25 16:45:14] Traceback (most recent call last):
...
3866:   File "...\astock\scheduler.py", line 527, in run_once
3867:     trade_date = last_completed_trade_date(now)
...
3878:   File "...\astock\tushare_client.py", line 104, in query
3879:     raise TushareError(f"{api_name}: {message}")
3880: astock.tushare_client.TushareError: trade_cal: token已过期
3881: [2026-11-25 16:45:19] maintenance exit=0
3882: { "ok": false, "initialized": false, "error": "heartbeat_failed" }   # L3882-L3890
3892: [2026-11-25 16:45:24] report 5 exit=1
3893: [2026-11-25 16:45:24] report failed: JSONDecodeError: Expecting value: line 1 column 1 (char 0)
3894: [2026-11-25 17:16:00] close job start
3895: [2026-11-25 17:16:58] close job exit=0
```

说明与影响：

- 异常发生在 `scheduler.py:527` 的 `last_completed_trade_date()` →
  `data.py::load_trade_cal()` → `tushare_client.py:104`，早于 `run_once` 的
  `ledger.get_job()`（`scheduler.py:535`）与 `save_job()`（`scheduler.py:547`），
  所以 jobs 表没有 11-25 的 running/failed 行，`attempts` 仍为 1。
- 16:45 的 maintenance 因同一异常返回 `heartbeat_failed`（L3882-L3890），该次心跳
  没有评估、也没有告警；随后 17:16 之前的 maintenance 正常。
- report 在 16:45 的 `JSONDecodeError` 来自 `run_daily_job.py` 对空 stdout 做
  `json.loads`（close 已崩），不是 `--report` 自身缺陷；17:17/17:45 的
  `report 5 exit=2` 是窗口仍含 11-18/11-20 旧失败（预期），11-27 窗口干净后 exit 0。
- token 在 17:16 前已恢复（17:16:00 重跑 success），根因是外部凭证过期并已续期，
  不属于本任务代码缺陷；本次 `--report 5`（2026-11-28T00:06:01）再次成功，token 当前有效。
- 验收结论不变：5 个交易日的 close job 均为 success。但暴露两个观察盲区：
  ① pre-ledger 失败不计入 attempts/late_by_min；② 同一时段心跳返回 error 而非告警。
  若 token 再次过期，应按“窗口内异常”重开 5 日窗口。

## 3. 心跳告警现场证据

`python -m astock.jobs --heartbeat`（2026-11-28T00:06 快照，
`docs/AStockAgent_scheduler_5d_heartbeat_2026-11-28.json`，
SHA256 `13098E3F316925A6B643EFBD8CBAB5CDF761B8715BB34D0C7E83E1BDC7A9AEE7`）：

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

签名去重（与实现对齐，评审 P2 更正）：`heartbeat_check()` 只与告警文件**最后一条**
`scheduler_heartbeat` 记录的 signature 比较（`astock/scheduler.py:344`
`if signature != _last_alert_signature()`；`_last_alert_signature()` 见
`scheduler.py:236-262`），相同则跳过。**没有 24h 时间窗口**：若中间写入其他签名，
旧签名再次出现仍会落盘。签名格式为
`f"{reference}:{streak}:{threshold}"`（`scheduler.py:232-233`）。
自动化用例 `tests/test_scheduler_ledger.py:418
test_heartbeat_dedup_uses_persisted_alert_file` 覆盖“最后一条为同签名时不再写”，
`test_heartbeat_alerts_only_after_two_missing_trading_days`（L268）覆盖
“1 日不告警、2 日告警、同进程重复检查不重复写”。

## 4. 自动化验证（main `4a09291`，2026-11-28 复跑）

| 命令 | 结果 |
| --- | --- |
| `python -m pytest tests/test_scheduler_ledger.py tests/test_hardening.py tests/test_data_pit_events.py -q` | **79 passed**（50 + 29），1 warning，22.60s |
| `python -m mypy astock app.py` | Success: no issues found in 45 source files |

2026-11-27 在 `84f459c` 上的全量结果（此后代码未变，仅文档/plan 元数据提交）：
`tests/test_scheduler_ledger.py tests/test_hardening.py -q` → 50 passed；`tests -q` →
956 passed, 3 skipped, 46 subtests passed（176.48s）。本轮评审只改文档，未改代码，
因此没有新增代码回归；空 code 回归 3 条仍由 `tests/test_data_pit_events.py` 覆盖。

注：在 DSH workspace-write 沙箱下，pytest 新建临时目录后写入会被拒绝（`WinError 5`），
表现为 12 failed/3 errors 的假失败；切换到 danger-full-access 后同一命令全绿。此为
运行环境限制，与代码无关，已在交付 handoff 的 permissions 中记录。

## 5. 验收结论（对照 plan-20261114-004）

| 验收项 | 结论 |
| --- | --- |
| EventFrame 空 code 根因修复 + 回归 | 通过：`_drop_unattributed_reports` + `ReportEventContractTests`（过滤空行、全空返回空帧、缺列仍 ContractError） |
| `test_scheduler_ledger.py` + `test_hardening.py` | 通过：50 passed |
| 修复后连续 5 个交易日全 success + 留存报告 | 通过：11-23..11-27 `passed=5/5`，快照固化于 `docs/AStockAgent_scheduler_5d_report_2026-11-28.json`；11-25 16:45 的 pre-ledger token 失败见 §2.1，不改变按交易日 success 判定 |
| 心跳 2 日触发 / 1 日不触发 / 签名去重 | 通过：11-20 streak=1 不告警；11-18/19 告警；单测覆盖去重 |
| running 超时恢复 failed、到期登记 pending、late_by_min | 通过：`test_recover_interrupted_running_job`、`test_sync_due_job_marks_missing_pending_only_after_run_time`、生产 5 行 late_by_min 与 started_at 对账 |
| 空台账 / 长期停摆不误报 | 通过：`test_startup_maintenance_marks_due_job_without_empty_history_alert`（空台账不告警）、`test_heartbeat_detects_long_outage_beyond_lookback_window`（长期停摆正确告警且 streak 封顶） |

剩余风险：
- 11-25 16:45 的 `token已过期` 发生在写 jobs 行之前，因此不会被 `attempts` /
  `late_by_min` / `--report 5` 反映，也不会触发心跳告警（返回 `heartbeat_failed`）。
  需要日志巡检或对 `heartbeat_failed` 增加告警才能覆盖这一类 pre-ledger 失败。
- 11-23 18:24 首跑进程中断同样没有 exit/traceback；`recover_interrupted_jobs`
  会在下次启动时把超时 running 置 failed，但本次因 20 分钟内重跑未触发。
- 若后续再次出现供应商字段漂移，需按 `ContractError` 文案定位并补充回归。
