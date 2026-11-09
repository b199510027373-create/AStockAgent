# PR 评审：评审：推荐跟踪：Top5 等权组合净值 vs 沪深300

> 自动生成于 2026-11-09T01:59:30.690Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261109-001`
- 计划项：`plan-20261103-004`
- 分支：`feat/track-nav` → `main`
- 轮次：第 4 轮（diff）
- 结论：需要修改（request_changes）
- 开发会话：`session-d4244044-2816-4935-bf9d-2fa6f0a87d69`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 0 / P2 2

## 本轮结论

第 4 轮 diff 评审（056c7de→13e5cea，仅 astock/track.py + tests/test_track_nav.py）。

交付覆盖核对：ho-20261109-007 的 requirements/changes/tests/delivery 四项齐全，与 commit 13e5cea 实际改动一致（track.py 三分支收紧 + nav_status_text 三态口径句；tests 两例更新）。links.test 6 条与交付表一致：26 passed / 123 passed / mypy Success / 离线冒烟 41 批 44 日 dropped 2 / gr.Blocks 15 组件两 Accordion open=False / case(b)(c) 文案断言。

已修并逐条验证：
- iss-mw8lei8u-2f0ca8（第 3 轮唯一 open P2）——已修复。track.py:516-518/608 写 nav.attrs["bench_anchor"]；:531-536 case (a)/(b) 分派，:544-550 case (b) 先 aligned.where(calendar >= anchor) 再以 anchor 收盘为基，anchor 日净值 = 1.0（旧实现 1.2 的口径偏差消失）；:654-662 口径句三态化，case (c) 走「本次无可用数据，不展示基准与超额」，case (b) 走「自 X 收盘起算，此前不参与超额」，case (a) 保留「从首个入场日开盘买入持有」。tests/test_track_nav.py:218-235、:237-251 的断言已能区分三态（1.0 vs 1.2、assertIn 起算句、assertNotIn 旧口径句），不再是只断言「不含数据缺失」的空断言。请在 handoff 或线程 feedback 里把 iss-mw8lei8u-2f0ca8 置为 fixed，评审侧无关闭入口。
- 其余 6 条（含 iss-mw8joahc-4fc16e）代码与用例仍成立，未回归；iss-mw8joahc 线程内状态仍显示 deferred，请开发在下一份 handoff 里一并改回 fixed（本轮已核对 :390 过滤 + 对应用例，判定已修）。

本轮新增 1 条 P2：iss-mw8m51bp-dd9930——case (b) 下超额两侧起点不同（bench 从 anchor 收盘重置为 1.0，组合仍从首个入场日起算），把 start→anchor 的组合涨跌算进了超额，而新增文案恰恰承诺「此前不参与超额」；现有用例个股价格恒为 10，测不出该偏差。修法：nav_status_text 按 bench_values.index[0] 归一组合净值后再算超额，并补 test_excess_excludes_pre_anchor_drift。

结论：仍有 1 条 P2 未闭环，本轮 request_changes。由开发在 feat/track-nav 按 iss-mw8m51bp-dd9930 修改 astock/track.py:630-636 的超额归一逻辑并补 1 条用例、把 iss-mw8lei8u/iss-mw8joahc 状态回填为 fixed 后，进入第 5 轮 diff 评审；第 5 轮确认 openP2=0（或仅剩已书面延后的项）且 openP0=0/openP1=0 时我再 approve 并标记完全解决。合并前仍需开 PR 跑远端 CI（当前 delivery.status=branch-ready，仅本地 123 passed）。

## 本轮问题

### P2 · case (b) 文案称「此前不参与超额」，但超额仍含入场日→anchor 的组合涨跌（两侧起点不同）

- 位置：`astock/track.py:630-636, 656-660`
- 状态：未闭环
- 置信度：0.75

**证据**

track.py:630-636 超额 = port_at_bench − bench，其中 port_at_bench = nav.loc[last_idx, "组合净值"] 是自首个入场日起的累计值；而 case (b) 的 bench 在 anchor 日被重置为 1.0（:552-556 `bench_nav = aligned / base`，base 取 anchor 收盘）。因此当 anchor > start 时，bench 只度量 anchor→末日，组合却度量 start→末日，两者之差把 start→anchor 的组合涨跌算进了超额。:656-660 新增文案「沪深300 自 X 收盘起算（Y 缺基准 bar），此前不参与超额」正好承诺了相反的语义，数字与说明不一致。现有用例 test_bench_missing_entry_bar_uses_later_close_as_anchor（tests/test_track_nav.py:218-235）中个股用 flat() 全 10 元，anchor 日组合净值恰为 1.0，所以超额恒为 0，测不出这段偏差。

**建议**

在 nav_status_text 里按基准首个有效日归一组合：`first_idx = bench_values.index[0]; port_base = float(nav.loc[first_idx, "组合净值"]); 超额 = (port_at_bench / port_base - 1 - (bench - 1)) * 100`（anchor == start 时 first_idx=0、port_base=1.0，退回现公式，无需再分支）。并补一条 `test_excess_excludes_pre_anchor_drift`：个股在 start→anchor 期间从 10 涨到 11（anchor 之后横盘），基准从 anchor 起横盘，断言超额约 0.00pp 而非 +10pp；再加 `assertIn("此前不参与超额")` 与数值一致性断言，避免文案与数字再次脱钩。仅改 track.py 的这一段与本测试文件，不影响 case (a)/(c)。

## 历史未闭环问题

- P2 · 基准起算分支改了，但状态行与口径句没有跟着改：case (b)/(c) 下「沪深300 从首个入场日开盘买入持有」与实际相反 · `astock/track.py:552-555,637-648`（第 3 轮）

## 变更文件

- `astock/track.py`
- `astock/track_ui.py`
- `tests/test_track_nav.py`

## 交给开发会话

1. 在分支 `feat/track-nav` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
