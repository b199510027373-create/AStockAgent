# ADR-004 调度器：独立进程 + 墙钟 + 幂等 + 补偿窗口

- 状态：accepted
- 日期：2026-11-03（2026-11-12 补充状态机迁移与心跳）
- 来源：评审 §2.1 D1/D2/D3；修复记录 §1；plan-20261103-007

## 背景

修复前的调度器有三个互相放大的缺陷：

- 幂等键用"日历日"，盘前任务把 16:40 收盘槽吃掉（10-30 的 `last_run_date` 已写入，当天收盘任务被跳过）；
- 倒计时用"剩余 60 秒再算"，机器休眠后整点漂移，实际 21:00 才跑，晚 4 小时 20 分；
- 跑在应用内线程里，没有单实例锁，重复启动会重复生成推荐。

评审结论：调度必须从"应用内线程"拿出来，变成**有锁、幂等、可重试、可观测的 job**。

## 决策

1. 独立入口 `python -m astock.jobs`；`start_scheduler.bat` 只负责启动它。Web 应用不再承担调度职责。
2. 单实例锁 `data/recommendations/scheduler.lock`（Windows `msvcrt` / POSIX `fcntl`）。
3. 幂等键 `(last_completed_trade_date, slot)`，`slot ∈ {premarket, close, manual}`；盘前与收盘互不占用。
4. 每次循环用 `datetime.now()` 重算墙钟剩余时间，禁止依赖上一次的倒计时。
5. 补偿窗口 16:40–23:00；补跑记录 `late_by_min`，随任务状态落库。
6. `StaleDataError` 不发布推荐、不覆盖 `latest.json`，任务状态记 `stale`。
7. 状态机：`pending → running → success/stale/failed`；失败保留 `attempts` 与错误文本。
8. `jobs` 表是调度状态唯一权威：每次尝试先写 `running`（含 `attempts`、`started_at`、`late_by_min`），结束写 `success/stale/failed` 与 `as_of/finished_at/error`。`scheduler_state.json` 降级为**只读迁移源**，不再写入；启动时只补缺失行、绝不覆盖已有行。
9. 启动自检：`migrate_legacy_state()` → `recover_interrupted_jobs()`（running 超过 180 分钟或无 `started_at` 标记 failed）→ `sync_due_job()`（交易日过 run_time 仍无 close 行时插入 `pending`，表达“该跑但没跑”）→ `heartbeat_check()`。各步骤异常独立记录日志，不阻断其余步骤。
10. 心跳：以“已到期”的最近收盘交易日为参照（run_time 之前用当天 00:00 计算，避免把当天未到期的 close 算作漏跑），倒序统计连续未 success 的 close 交易日；连续缺失达到阈值（默认 2，`ASTOCK_HEARTBEAT_THRESHOLD_DAYS`）写 `data/recommendations/scheduler_alerts.jsonl` 并记 `scheduler.log`。告警签名持久化在 JSONL 中用于跨进程去重；历史存在性按全表判断，台账只有当天 `pending`、尚无任何历史记录时返回 `initialized=false` 且不告警；窗口内无任何行但全表存在更早历史时视为停摆超过窗口，`streak` 取窗口长度并照常告警。
11. 验收：`python -m astock.jobs --report 5` 输出最近 5 个交易日 close 状态与 `all_success`，退出码 0/2；`--heartbeat` 输出心跳结果，退出码 0/2。连续 5 个交易日成功即可复现 §4.6 P0 验收。

## 后果

- 正面：休眠/重启不再漏跑；重复启动不会重复生成；失败可定位、可重试；状态机与 attempts 可从 SQLite 查询；漏跑有告警和可复现验收报表。
- 负面：需要一个常驻进程或 Windows 任务计划；锁文件残留需人工清理策略；旧 `scheduler_state.json` 不再更新，外部若直接读该文件会看到冻结状态。
- 当前状态（2026-11-12）：锁、幂等、墙钟、补偿、jobs 表状态机、legacy 状态只读迁移、心跳告警与 5 日验收报表均已实现；`tests/test_scheduler_ledger.py` 覆盖。

## 证据

- `astock/scheduler.py`、`astock/jobs.py`、`astock/ledger.py`（jobs 表 `as_of` 迁移、`update_job`、`list_jobs`、`latest_success_job`）
- 修复记录：2026-11-03 16:40 任务 17:43 补跑成功，`late_by=63.3m`
- 测试：`tests/test_scheduler_ledger.py`（状态机、迁移、中断恢复、心跳阈值、5 日验收）；`tests/test_hardening.py`（盘前/收盘双键、stale 不写成功、失败累计 attempts）
