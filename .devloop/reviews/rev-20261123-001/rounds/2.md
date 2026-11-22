# PR 评审：评审：板块/停牌/北交所交易规则补齐

> 自动生成于 2026-11-22T17:18:27.074Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261123-001`
- 计划项：`plan-20261103-009`
- 分支：`feat/plan-20261103-009-board-rules` → `main`
- 轮次：第 2 轮（full_recheck）
- 结论：通过（approve）
- 开发会话：`session-9447de73-dfbd-4553-b66d-68047542f613`
- 本轮问题：3（P0 0 / P1 0 / P2 3）
- 线程未闭环：P0 0 / P1 1 / P2 7

## 本轮结论

第 2 轮（full_recheck，基于 54a3cd8 修复提交与当前分支 head 65f14b2）复核完成，对照 developer handoff ho-20261123-007/008/009、plan-20261103-009 links 与 ADR-008 更新。

结论：approve。上一轮 5 条问题全部闭环，openP0=0、openP1=0；本轮新增 3 条 P2（不阻断合并，建议在合并前或有后续项时处理）。

上一轮问题逐条核对（已关闭）：
- iss-mws2ln9l-646d4c（P1 接线）：astock/optimize.py:86、:178、:193-203 与 astock/strategy_lab.py:71、:136、:150-160 全部透传 name/list_date；astock/strategy_lab.py:261-285 经 meta[code] 逐只传入；astock/events.py:202-223 新增 security_meta，三个 UI（astock/backtest_ui.py:46、astock/optimize_ui.py:64、astock/strategy_ui.py:48）统一取数。grep 复核 astock/ 下 8 处 run_backtest 调用已无遗漏。tests/test_optimize.py:63-122 的 RuleWiringTests 用 spy 断言四条入口透传，属可判别用例。
- iss-mws2ltn4-c82470（P2 临停口径）：astock/backtest.py:239-256 新增 INTRADAY_HALT_AMPLITUDE=0.30 与 _suspected_intraday_halt，run_backtest 在 astock/backtest.py:327 并入 tradable 判定；README.md:176、182 改为"保守近似"并明确残余（振幅<30% 与常规限价日临停仍不可见）。验收 3 由"保守近似 + 文档登记"满足，不再与实现矛盾。
- iss-mws2lto3-f8ab4c（P2 ST 非 PIT）：README.md:183 登记当前名称限制，ADR-008 修复记录第 4 条同步。
- iss-mws2ltp0-6b9e10（P2 窗口计数）：astock/backtest.py:191-210 改 np.busday_count 工作日计数，停牌缺口不再拉长窗口；单测 tests/test_board_rules.py:313-317 断言 [0,1,9] 具判别力。
- iss-mws2ltpz-6f6abe（P2 端到端用例）：tests/test_board_rules.py:364-415 新增 ST 5% 一字 0 成交（并对照非 ST 可成交）、旧规首日 44% 一字 0 成交、首日低于 44% 可成交、次日高于 44% 可成交，四例语义均正确。

本轮新增（P2，开发处理）：
- iss-mws3oxhq-d35643 ｜ tests/test_board_rules.py:319-329：缺口用例 signals=[0,0,1] 且未传 prev_signal，按 sig[i-1] 语义三根 bar 都不下单，断言恒真，回退旧实现也照样通过。改法：signals 改 [0,1,0] 并加一条 pre_close=12.5 的对照断言。
- iss-mws3oxip-3c3b84 ｜ astock/events.py:173-179/215-217：stock_basic 只查 list_status="L"，已退市/暂停上市个股取不到名称与上市日 → ST 5%、IPO 窗口静默失效，README 未登记。改法：查询 L/D/P 后 concat，或 README 已知差异 + ADR-008 未覆盖项登记。
- iss-mws3oxjy-108c8a ｜ astock/events.py:173-187：网络失败时静默回退到任意日期旧缓存，其他调用方（recommend/universe/extract_entities/factors）从显式失败变成拿到过期股票池。改法：仅当天缓存允许静默回退，或记录 basic_as_of/warning。

测试充分性：开发给出的证据为 tests/test_board_rules.py+test_optimize.py 50 passed/26 subtests、7 文件相关集 136 passed、全量 917 passed/3 skipped、ruff/mypy/coverage 80.19% 通过，PR #46 在 54a3cd8 与 c07e020 上 CI（test + png-export）成功；本轮无 shell 工具，未独立复跑 pytest 与 gh，证据取自 plan links 与 ho-20261123-007/008/009。旁注：ho-20261123-009 写"最终 head c935687"，当前分支 head 为 65f14b2（仅 devloop 记录差异），不影响代码结论。

何时算完全解决：本线程判定 approve（openP0=0/openP1=0）即视为需求侧完全解决；3 条 P2 为开发侧收尾项，条件明确——(1) vacuous 用例改到可判别并可复现失败；(2) 退市股缺口二选一落地（代码补 L/D/P 或 README+ADR 登记）；(3) 旧缓存回退收紧到当天或输出 basic_as_of。若开发选择不改，须在 plan-20261103-009 记录显式延期理由，下一轮由 reviewer 复核确认。

## 本轮问题

### P2 · stock_basic 只查 list_status="L"，已退市/暂停上市个股的 ST 5% 与 IPO 窗口静默失效且未登记

- 位置：`astock/events.py:173-179, 215-217`
- 状态：未闭环
- 置信度：0.75

**证据**

astock/events.py:173-179 的 `stock_basic` 固定查询 `list_status="L"`；astock/events.py:215-217 的 `security_meta` 在 `basic["ts_code"] == code` 无匹配时返回 `("", None)`。因此对已退市（list_status="D"）或暂停上市（"P"）的个股，ST 5% 与 IPO 无涨跌幅窗口在回测中静默失效，按板块基准 10%/20% 处理——退市股历史里常含 ST 段与次新段，恰好是最容易踩 5% 一字板的场景。README.md:184 只说明"离线拿不到基础信息时退回常规限价"，未覆盖"在市列表里没有这只股票"的情况；README.md:183 只登记了 ST 非 PIT，没有登记这个缺口。

**建议**

二选一：(a) `stock_basic` 对 `list_status in ("L", "D", "P")` 分别查询后 concat 并缓存（每天 3 次调用，回测按需触发，可接受）；(b) 本轮不动代码，在 README「回测与实盘的已知差异」补一条："`stock_basic` 只含在市股票，已退市/暂停上市个股取不到名称与上市日，ST 5% 与 IPO 无涨跌幅窗口对其不生效"，并在 ADR-008 未覆盖项登记。

### P2 · stock_basic 网络失败时静默回退到任意日期的旧缓存，推荐/选股等调用方从失败变为拿到过期股票池

- 位置：`astock/events.py:173-187`
- 状态：未闭环
- 置信度：0.7

**证据**

astock/events.py:173-187：`get_client().query(...)` 抛异常时，只要 `path.exists()` 就 `pd.read_parquet(path)` 返回旧缓存，且不看 `meta` 里的日期——缓存可能已是任意多天前。此前该函数在网络失败时直接抛错，调用方（astock/recommend.py:79/698、astock/universe.py:54、astock/events.py:243 extract_entities、astock/factors.py:388）会显式失败；现在它们会在无提示的情况下拿到过期股票池。对回测本身影响小（名称/上市日本就按当前值近似，README.md:183 已登记），但推荐链路的股票池新鲜度被静默降级，且没有任何日志或 data_as_of 标记。

**建议**

离线回退仅在 `meta.exists() and meta.read_text() == _today_str()` 时启用（当天缓存才允许静默回退），否则按原行为抛错；若确实需要"任意旧缓存兜底"，至少在回退分支记一条 warning（含缓存日期）并把 `basic_as_of` 出现在返回值/缓存元数据里，供 UI 与推荐标注数据新鲜度。

### P2 · 缺口用例 test_suspension_gap_ends_ipo_window 实际从未下单，断言恒真

- 位置：`tests/test_board_rules.py:319-329`
- 状态：未闭环
- 置信度：0.9

**证据**

tests/test_board_rules.py:319-329 的 `df = bars([10.0, 10.0, 12.5], start="2024-05-06")` 后 `run_backtest(df, pd.Series([0, 0, 1]), ...)`。按 astock/backtest.py:322 的语义 `target = sig[i-1] if i>0 else prev_signal`，且此处未传 prev_signal：i=0 的 target 为 None，i=1 取 sig[0]=0，i=2 取 sig[1]=0 —— 三根 bar 都没有任何下单尝试，`len(result.trades)==0` 恒成立。把 `_listing_offsets` 退回到旧的 bar 序号实现（offsets [0,1,2]）时该用例依然通过，因此它没有验证"停牌缺口结束 IPO 窗口"这一修复。真正起回归保护作用的只有 tests/test_board_rules.py:313-317 的纯函数断言 [0,1,9]。

**建议**

把信号改为 `pd.Series([0, 1, 0])`，让买入尝试落在第 3 根 bar（trade_date=2024-05-17、listing_day=9、20% 涨停价 12.0、high==low==12.5）上；再补一条对照断言证明用例可区分：同一帧把 `df.loc[2, "pre_close"]` 改为 12.5（20% 涨停价 15.0，12.5 非一字）跑一次，断言 `len(trades)==1`。改后若把 `_listing_offsets` 还原成 bar 序号实现，第一条断言应当失败。

## 历史未闭环问题

- P1 · ST 5% 与 IPO 无涨跌幅规则未接入参数优化/策略实验室（run_backtest 未传 name/list_date） · `astock/optimize.py:83, 172, 187-195（另 astock/strategy_lab.p`（第 1 轮）
- P2 · 验收 3「临时停牌不产生虚假成交」与实现/README 口径不一致 · `README.md:171-175, 181`（第 1 轮）
- P2 · ST 判定使用当前证券名称，非历史 ST 状态（PIT），README 未标注 · `astock/backtest.py:78-90`（第 1 轮）
- P2 · _listing_offsets 用 bar 序号当交易日序号，停牌缺口会拉长 IPO 无涨跌幅窗口 · `astock/backtest.py:191-207`（第 1 轮）
- P2 · ST 5% 与旧规首日 44%/36% 只有纯函数用例，缺 run_backtest 端到端用例 · `tests/test_board_rules.py:110-190, 283-304`（第 1 轮）

## 变更文件

- `README.md`
- `astock/backtest.py`
- `astock/backtest_ui.py`
- `astock/events.py`
- `astock/optimize.py`
- `astock/optimize_ui.py`
- `astock/strategy_lab.py`
- `astock/strategy_ui.py`
- `tests/test_board_rules.py`
- `tests/test_optimize.py`

## 交给开发会话

1. 在分支 `feat/plan-20261103-009-board-rules` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
