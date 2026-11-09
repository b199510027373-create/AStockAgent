# PR 评审：评审：推荐跟踪：Top5 等权组合净值 vs 沪深300

> 自动生成于 2026-11-09T02:08:34.350Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261109-001`
- 计划项：`plan-20261103-004`
- 分支：`feat/track-nav` → `main`
- 轮次：第 5 轮（diff）
- 结论：通过（approve）
- 开发会话：`session-d4244044-2816-4935-bf9d-2fa6f0a87d69`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 0 / P2 1

## 本轮结论

第 5 轮 diff 评审（13e5cea→538fd9b，仅 astock/track.py + tests/test_track_nav.py）。交付 ho-20261109-009 的 requirements/changes/tests/delivery 齐全，与 diff 一致。

iss-mw8m51bp-dd9930（第 4 轮唯一 open P2）已修复并逐条验证：
- case (b) 归一：track.py:634-642 在 `bench_anchor > start` 时取 `nav` 首个 `交易日 >= anchor` 的 `组合净值` 作 `port_base`，:646 `excess = (port_at_bench / port_base - 1.0 - (bench - 1.0)) * 100`，组合与基准同从 anchor 起算，与文案「此前不参与超额」一致。
- case (a) 边界：`port_base` 初始 1.0，仅 case (b) 覆盖 → 公式退化为 `port_at_bench - bench`，与原口径等价，入场日 close/open 收益不会被子二次剔除；test_status_text_reports_nav_and_caveat 的 `+8.00pp` 继续锁住该边界。
- 用例判别力：test_excess_excludes_pre_anchor_drift 中个股入场日 10→11、之后横盘，基准自 anchor 横盘；旧实现给 +10.00pp，新实现 +0.00pp，断言 `assertIn("超额 +0.00pp")` 能真实区分，不是同义断言。

其余 7 条历史 issue 代码与用例均成立，未回归（:390 无排名行过滤、:544-556 case (b) 收盘基、:654-662 三态文案、track_ui 净值段 try/except、折叠区结构断言）。测试证据：27 passed / 124 passed / mypy Success / 离线冒烟 41 批 44 日 +4.34% vs -11.13% 超额 +15.47pp dropped 2，与上轮数值一致（无口径漂移）。

无新增问题，verdict=approve，openP0=0/openP1=0 → 标记完全解决。

遗留事项（不影响本条结论，需开发在合并前后处理）：
1) 线程内 iss-mw8joahc-4fc16e 仍显示 deferred、iss-mw8m51bp-dd9930 仍显示 open，与 ho-20261109-009 的「已回填 fixed」不符。两者经我核对 commit 056c7de / 538fd9b 已修；请在下一份 handoff 中把状态改为 fixed，若确有延后项则须写明后续计划项，届时重开线程评审。
2) 合并前开 PR 并跑远端 CI（当前 delivery.status=branch-ready，仅本地 124 passed）。
3) 验收 #1/#2 的浏览器观感（首屏净值图、两折叠区默认收起）仍为 needed：结构断言已覆盖组件装配，但真实页面渲染未人工确认，请开发在合并前提供一次手工核对记录（或截图），作为本项完全解决的收尾证据。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P2 · case (b) 文案称「此前不参与超额」，但超额仍含入场日→anchor 的组合涨跌（两侧起点不同） · `astock/track.py:630-636, 656-660`（第 4 轮）

## 变更文件

- `astock/track.py`
- `astock/track_ui.py`
- `tests/test_track_nav.py`

## 交给开发会话

1. 在分支 `feat/track-nav` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
