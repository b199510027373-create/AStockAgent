# PR 评审：评审：行情/指标工具：实时 K 线与指标

> 自动生成于 2026-11-18T16:02:59.572Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261118-001`
- 计划项：`plan-20261107-006`
- 分支：`feat/market-tools` → `main`
- 轮次：第 3 轮（diff）
- 结论：通过（approve）
- 开发会话：`session-604a4898-03c2-4f30-90ad-1559cb54410e`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 1 / P2 1

## 本轮结论

第 3 轮（scope=diff，base a0c8ecd → 266538d）复核结果：iss-mwma8thg 已实质修复，裁决 approve（openP0=0、openP1=0，余 1 条 P2 提示）。

一、iss-mwma8thg 复核通过（停牌改按开市日缺口判）
- 新增 `_missing_open_days(calendar, stamp, data_as_of) -> int`（astock/agent/tools/market_tools.py:407-432）：口径为 `last_bar < day < today` 的开市日计数，恰好满足 docs 里「真实停牌日没有行」的语义，并且在 docstring 里明确排除今天（收盘后当日日线常未发布，不能把正常股票判成停牌）。
- load_series 接线正确：`suspended = _is_suspended(frame)` 之后 `missing_days = 0 if is_index else _missing_open_days(deps.trade_cal(), stamp, data_as_of)`，`if missing_days > 0: suspended = True`（:520-525）；指数跳过停牌判据合理；生产依赖真实可用（MarketDeps.trade_cal 默认 market.load_trade_cal，:181；默认区间 20150101 起，能覆盖长停牌缺口）。
- 文案可审计：缺行分支写「停牌/无成交：最后一根 bar 为 {data_as_of}，其后 {missing_days} 个交易日无行情，指标按最后一根 bar 计算」，0 量分支保留原文案（:565-574）；`_freshness_label` 的 suspended →「停牌/无成交」（:297-298）不变，ADR-018 C2 的 freshness 冻结键与「回答写明停牌/无成交」均满足。
- 用例覆盖到位：test_quote_marks_suspended_when_bars_are_missing（收盘后 16:30、最后一根 bar vol>0 仍标 suspended）、test_suspended_missing_bars_intraday_is_not_just_delayed（盘中 10:00 不得退化成单纯 delayed、不得出现「盘中实时」）、test_closed_session_without_today_bar_is_eod_not_suspended（今天 bar 未发布 → eod 而非停牌）、test_non_trading_day_answer_never_claims_realtime 加断言 freshness=="eod" 且 suspended is False。正反两面都锁住了，不再依赖 0 量行这个假形态。
- 无回归面：本 commit 只改 `_missing_open_days` 与 load_series 的 suspended 分支，未触碰上轮已修好的 `_chart_range` / `_chart_layers` / `_render_kline` / `runtime._figure_handle`；已关闭的 4 条（iss-mwm6tovm、iss-mwm6towg、iss-mwm6tv83、iss-mwm6tv92）维持 fixed。

二、接受的已知取舍（不记问题）
- 数据源整体落后恰好 1 个完整开市日时会被标 suspended——与「停牌 1 天」在仅有日线的条件下本质不可区分，开发者已在 docstring 说明今天的排除规则，且 note 写的是「其后 N 个交易日无行情」，用户可据数字自行判断。
- 当日盘中临时停牌、且分钟取数失败回退日线时，因 today 被排除会判 eod/delayed 而不是 suspended：同为上述不可区分性，属可接受的上界，不追加要求。

三、CI 说明
ho-20261119-001 记录 png-export 首跑 10m14s 失败，原因是 Install CJK fonts 步骤 apt-get update 卡住并触发 job 的 timeout-minutes:10，`gh run rerun --failed` 后 pass；属流水线环境问题，与本 diff 无关，不作为评审问题。

四、唯一遗留 P2（iss-mwmb8h5m-ec84a1，不阻断）
test_data_time_semantics.py 的 `_trade_cal()` 默认从 2024-01-01 起，而 _missing_bar_frame() 的缺口从 2023-12-15 开始，导致用例里 note 只数到 2 天、真实应为 12 天，且断言只匹配「个交易日无行情」子串，不校验这行面向用户的审计数字。

五、完全解决判定
- 责任与条件：该 P2 由开发者在后续任意一次提交里补齐（给 _Deps 传 calendar=_trade_cal("2023-11-01","2024-01-31")，断言精确天数，收盘后与盘中两条用例同改）；补完并重跑 pytest tests -q 与 mypy astock 全绿、更新 links.test 即视为完全解决。
- 在此之前：本线程的验收项与 ADR-018 契约要求已全部满足，iss-mwma8thg 及前 4 条关闭项不再需要复核；PR #31 可以合并，P2 可作为独立小改动或下个迭代补上，不阻塞合并。

## 本轮问题

### P2 · 缺 bar 停牌用例的日历没覆盖真实缺口，审计句「其后 N 个交易日」的数字无人校验

- 位置：`tests/test_data_time_semantics.py:46-48, 735-760`
- 状态：未闭环
- 置信度：0.8

**证据**

_trade_cal() 默认 start="2024-01-01"（tests/test_data_time_semantics.py:46-48），而 _missing_bar_frame() 的最后一根 bar 是 2023-12-15（:738-741）；test_quote_marks_suspended_when_bars_are_missing 未传 calendar（:744），于是 _missing_open_days 只能数到 2024-01-01、2024-01-02 两天，note 实际写成「其后 2 个交易日无行情」（astock/agent/tools/market_tools.py:566-570），而真实缺口是 2023-12-18…2024-01-02 共 12 个交易日。测试只断言子串 "个交易日无行情"（:755），既不断言天数也不覆盖缺口区间，因此这行给用户看的审计数字若系统性少算（例如多算/少算一天、把 today 算进去），本用例仍然绿。

**建议**

在 test_quote_marks_suspended_when_bars_are_missing 里显式传覆盖缺口区间的日历：context = _Deps(daily=_missing_bar_frame(), clock="2024-01-03 16:30", calendar=_trade_cal("2023-11-01", "2024-01-31"))，并把断言改成精确天数，例如 expected = len(pd.bdate_range("2023-12-18", "2024-01-02"))，然后 assert any(f"其后 {expected} 个交易日无行情" in note for note in notes)（同时保留 text 含「停牌」「无成交」的断言）。test_suspended_missing_bars_intraday_is_not_just_delayed（:757-767）同样补 calendar 参数，使盘中与收盘后两条路径都验到同一数字。

## 历史未闭环问题

- P1 · 长停牌在真实数据上仍不会标 suspended：_is_suspended 只认 vol=0 行，而停牌是「缺 bar」（iss-mwm6toxk 残留） · `astock/agent/tools/market_tools.py:392-393,493-497,688-691`（第 2 轮）

## 变更文件

- `astock/agent/runtime.py`
- `astock/agent/tools/__init__.py`
- `astock/agent/tools/market_tools.py`
- `tests/test_agent_runtime.py`
- `tests/test_agent_tools.py`
- `tests/test_ci_quality_gates.py`
- `tests/test_data_time_semantics.py`

## 交给开发会话

1. 在分支 `feat/market-tools` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
