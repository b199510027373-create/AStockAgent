# PR 评审：评审：推荐跟踪：Top5 等权组合净值 vs 沪深300

> 自动生成于 2026-11-09T01:25:18.913Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261109-001`
- 计划项：`plan-20261103-004`
- 分支：`feat/track-nav` → `main`
- 轮次：第 2 轮（diff）
- 结论：需要修改（request_changes）
- 开发会话：`session-d4244044-2816-4935-bf9d-2fa6f0a87d69`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 1 / P2 5

## 本轮结论

第 2 轮 diff 评审（scope=diff，范围 7aca362→00c6b05，astock/track.py / astock/track_ui.py / tests/test_track_nav.py）。开发交付 ho-20261109-003 与代码逐条核对结果如下。

【P1 已实质修复，但线程内仍未闭环】iss-mw8jo6dd-b0f30e：交易日判定改为 [track.py:348-355](astock/track.py#L348-L355) 的 `_trading_days()`（来自 `load_trade_cal()` 的 `is_open==1`，不再拿 000300.SH 当日 bar 冒充交易日历），[track.py:466-468](astock/track.py#L466-L468) 只在「入场日不在交易日历」时剔除并给出「不是交易日」；组合日历改为已纳入批次成分股 bar 并集 ∩ 交易日历（:502-511），不再被基准更新进度截断；基准末端缺 bar 保留 NaN（:532-533）；状态行区分「不是交易日 / 入场日缺 bar / 沪深300 数据截至 X，超额只算到该日」，超额改用共同有效日上的 `port_at_bench` 计算。对应三条新用例（test_batch_on_non_trading_day_is_dropped、test_missing_bench_bar_on_entry_keeps_batch、test_nav_extends_when_benchmark_lags_tail）+ 离线冒烟「dropped 2，最新批次保留」足以证明现场问题消失。技术上我认可这条 P1 已解决。

【4 条 P2 的处置核对】iss-mw8jodh9-e76932 已修（:555-560 值级 `ffill` + `ratio.fillna(1.0)`，test_nan_close_carries_previous_close_without_reweighting 断言 1.0 / 1.04）；iss-mw8jogea-70f60f 已修（[track_ui.py:27-35](astock/track_ui.py#L27-L35) 净值段单独 try/except，test_run_track_survives_nav_failure 断言 6 项输出 + 「失败/boom」+ 分组统计仍在）；iss-mw8jopos-909fec 已修（test_build_track_tab_folds_excess_tables_below_nav 断言净值 Plot 在两个 `open=False` Accordion 之前、折叠区含三张表标签）；上一轮未立单的文案矛盾也已修（`nav_status_text` 仅在 `has_nav` 时追加「原持仓延续到下一批」，并拆成两条用例）。iss-mw8joahc-4fc16e 只算部分修复，见下。

【仍存在的问题】
1. P2（本轮新立，iss-mw8kwygq-0717cf，[track.py:518-534](astock/track.py#L518-L534)）：基准缺入场日 bar 时的 base 兜底口径不一致——取「入场日之后第一根 bar 的收盘」做基，而入场日取 ffill 前收，导致要么整列退化成「沪深300 数据缺失，仅显示组合」（首批次入场日＝今天、指数未更新时的实际路径），要么凭空多出一日基准涨跌并把误差带进超额。需按「有入场日 bar / 其后有 bar / 其后无 bar」三分支处理，并补两条用例。
2. iss-mw8joahc-4fc16e 的残留（保持该条 open，不另立新单）：[track.py:378-393](astock/track.py#L378-L393) 只在「整批排名全为 NaN」时切换到综合分兜底、只在「入选后不足 5 只」时剔除；若一批 8 行里只有 2 行有排名，`sort_values("_order", na_position="last").head(5)` 会把 3 行无排名的按文件行序补进 Top5，`len(codes)==5` 通过校验，对外仍宣称「Top5 等权」。修法：`head()` 前先 `rows = rows[rows["_order"].notna()]`（无排名行不得占用 Top5 名额），或改成两键排序（有排名者按排名升序在前，无排名者按综合分降序排在其后且不进入前 top_n），不足则记 dropped。

【流程闭环缺口（不记 issue，但阻断 approve）】线程内 5 条 issue 的 `feedback` 全部为空（get 显示 feedback=null、fixed=0），评审侧没有「关闭 issue」的入口，所以工具仍统计 openP1=1 / openP2=4。请开发在 rev-20261109-001 上逐条填真实反馈（已修复并指向 00c6b05 / 换方案 / 延后 / 不采纳）；若当前工具不允许回填上一轮，就在下一次 update handoff 里给出「issue id → 状态 + 证据」对照表，我以该对照表判定闭环。否则即使代码全对，finish 也无法把线程判为完全解决。

【结论】verdict=request_changes（本轮 openP0=0、openP1=0（新增）、openP2=1）。P0 无；P1 技术层面已验证修复，不要求改代码。完全解决的判定条件：由开发在 feat/track-nav 上处理 iss-mw8kwygq-0717cf 与 iss-mw8joahc-4fc16e 的残留（含两条基准起算点用例、一条「部分行缺排名」用例），并在线程或 handoff 中回填 5 条 issue 的反馈；下一轮 diff 评审确认 openP1=openP2=0 后我给 approve 并标记完全解决。合并前请开 PR 跑远端 CI（本轮仅本地 120 passed，在线真数据冒烟因 Tushare 端点 read timeout 未执行，属环境限制，不作为问题记录）。

## 本轮问题

### P2 · 基准缺入场日 bar 时 base 兜底口径不一致：会退化成「数据缺失」或凭空多出一日基准涨跌

- 位置：`astock/track.py:518-534`
- 状态：未闭环
- 置信度：0.7

**证据**

P1 的修复主体成立（交易日判定改用 `_trading_days`、组合日历改成分股并集、基准末端留 NaN、状态行区分「不是交易日／缺 bar／数据截至 X」），但 [track.py:518-534](astock/track.py#L518-L534) 新增的 base 兜底口径有问题。`base = _frame_price(bench_bars, first_entry)`（开盘）与 `base = _frame_price(..., "close")` 都取不到时，代码用 `later = bench_close[bench_close.index >= first_entry]` → `base = float(later.iloc[0])`，即拿「入场日之后第一根基准 bar 的收盘」当起点，而 `aligned` 在入场日取的是 `ffill` 出来的前收。两个后果：(1) 最常见场景——首批次入场日就是今天、指数当日 bar 还没生成（就是本轮 P1 要解决的那个现场）：`later` 为空 → `base` 仍为 None → `bench_nav` 整体为 None → 全列 NaN，`nav_status_text` 走到 `parts.append("沪深300 数据缺失，仅显示组合")`（[track.py:620](astock/track.py#L620) 附近），把「指数落后一天」说成「数据缺失」，交付文档里「沪深300 最后有效 2026-11-06 并在状态行注明」在这一路径下不会出现；(2) 若入场日只是中间缺一根 bar、其后又有 bar（缓存空洞），则入场日基准净值 = 前收 / 后收 ≠ 1，凭空造出一段基准涨跌并直接进入 `超额`，而组合侧是以入场日开盘为基，两侧口径不一致。`test_missing_bench_bar_on_entry_keeps_batch` 用的 bench 从入场日+1 开始、入场日之前没有 bar，`aligned` 在入场日必然是 NaN，因此恰好绕过了这两条路径，没有覆盖「基准有更早数据但缺入场日」。

**建议**

把「基准起算点」单独算清楚，三种情况分别处理：(a) 入场日有基准 bar → 现状不变（open 为基、close 归一，与组合同口径）；(b) 入场日无 bar 但其后第一根 bar 存在 → 以该 bar 的**开盘**为基（或直接把该日净值设为 1.0），入场日到该日之间留 NaN，不要用前收除以基；(c) 入场日及其后都没有 bar → 用最后有效收盘做基，并对 `calendar > 最后有效日` 全部留 NaN（现状的 `.where` 已做到），状态行写「沪深300 数据截至 X，尚未覆盖入场日」，绝对不能落到「沪深300 数据缺失」。配套补两条用例：`bench` 覆盖到入场日前一天、入场日及其后无 bar → nav 的「沪深300净值」有值到该日、其后 NaN，状态行含「数据截至」且不含「数据缺失」；`bench` 在入场日缺一根、次日及以后有 bar → 入场日基准净值为 NaN（或 1.0）而**不是**前收/后收，且该日超额不被计入。验收标准：任何「基准缺入场日 bar」的组合下，状态行只会出现「数据截至／尚未覆盖」，不会出现「数据缺失」或凭空的一日基准涨跌。

## 历史未闭环问题

- P1 · 基准 bar 缺失被当成“非交易日”剔除最新批次，净值又被基准日历截断 · `astock/track.py:423-424,460-461`（第 1 轮）
- P2 · 批次成分数与排名缺失未校验，仍按“Top5 等权”对外 · `astock/track.py:354-356,441-445`（第 1 轮）
- P2 · 个股 close 为 NaN 时当日权重被静默重分配，与 docstring 的“收益记 0”不符 · `astock/track.py:494-500`（第 1 轮）
- P2 · run_track 无异常隔离，净值段失败会连带丢掉已算好的表格 · `astock/track_ui.py:25-44`（第 1 轮）
- P2 · 验收 #2（超额表降级到折叠区）缺少可复核证据 · `astock/track_ui.py:67-74`（第 1 轮）

## 变更文件

- `astock/track.py`
- `astock/track_ui.py`
- `tests/test_track_nav.py`

## 交给开发会话

1. 在分支 `feat/track-nav` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
