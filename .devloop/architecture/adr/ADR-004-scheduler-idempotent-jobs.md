# ADR-004 调度器：独立进程 + 墙钟 + 幂等 + 补偿窗口

- 状态：accepted
- 日期：2026-11-03
- 来源：评审 §2.1 D1/D2/D3；修复记录 §1

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

## 后果

- 正面：休眠/重启不再漏跑；重复启动不会重复生成；失败可定位、可重试。
- 负面：需要一个常驻进程或 Windows 任务计划；锁文件残留需人工清理策略。
- 当前状态：锁、幂等、墙钟、补偿已实现；调度状态仍主要写 `scheduler_state.json`，完整迁移到 `jobs` 表与心跳告警尚未完成。

## 证据

- `astock/scheduler.py`、`astock/jobs.py`
- 修复记录：2026-11-03 16:40 任务 17:43 补跑成功，`late_by=63.3m`
- 测试：`tests/test_hardening.py` 覆盖盘前/收盘双键、stale 不写成功
