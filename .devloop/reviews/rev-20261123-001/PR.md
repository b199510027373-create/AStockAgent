# PR 评审：评审：板块/停牌/北交所交易规则补齐

> 自动生成于 2026-11-22T17:37:05.883Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261123-001`
- 计划项：`plan-20261103-009`
- 分支：`feat/plan-20261103-009-board-rules` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-9447de73-dfbd-4553-b66d-68047542f613`
- 本轮问题：2（P0 0 / P1 0 / P2 2）
- 线程未闭环：P0 0 / P1 1 / P2 9

## 本轮结论

第 3 轮（full，复检 65f14b2..1f7e124 与分支 head 156f962）评审完成。结论：approve，本轮 P0=0 / P1=0 / P2=2（均为提示级，不阻断合并）。前两轮 8 条问题（1 条 P1 + 7 条 P2）技术上全部落实并已复核。

【第 2 轮 3 条 P2 逐条复核（均已修复）】
1. iss-mws3oxhq-d35643（恒真用例）：tests/test_board_rules.py:323-343 改为 signals=[0,1,0,0]（第 3 根 bar 尝试买入、第 4 根卖出）并新增 pre_close=12.5 的对照断言（@:340-343 期望 1 笔成交）。按 sig[i-1] 语义，主用例在 offset=9（≥5，20% 限价、12.5 > 12.0 涨停价）处被一字板拦截→0 成交；若退回 bar 序号实现（listing_day=2）会放行→1 成交，断言可判别，dev 已用旧实现手工验证 failures=1。
2. iss-mws3oxip-3c3b84（退市/暂停上市静默失效）：astock/events.py:162-178 新增 _query_basic 查询 L/D/P（D/P 失败仅记录性跳过、L 失败仍抛出），:230-240 新增 stock_basic_all（独立缓存 stock_basic_all.parquet / _BASIC_ALL），:251-252 security_meta 改用它；stock_basic 仍只有 L，推荐/选股池不被退市股污染。tests/test_board_rules.py:441-523 StockBasicCoverageTests 覆盖三态合并、D 查询失败降级、L 池不含退市股。
3. iss-mws3oxjy-108c8a（旧缓存静默回退）：astock/events.py:154-159 新增 _cache_is_current（meta 内容须等于 _today_str()），:181-215 _load_basic 的 except 回退分支同样受该判定约束，更旧缓存 re-raise；用例 :503-523 覆盖 stale 抛 TushareError 与当天缓存回退。astock/scheduler.py:618-627 会把异常记为 failed 状态，硬失败可观测，可接受。

【四条验收复核（第 3 轮全量）】① 20%/30%/ST 5%：LimitPctTests + EndToEndRuleTests 覆盖；② IPO 无涨跌幅窗口：ipo_unbounded_days/limit_rule + README.md:166-170（含工作日计数、缺口说明）；③ 停牌/临停：0 量 bar、缺 bar、上市前 bar 不成交 + 无涨跌幅窗口振幅≥30% 保守近似（astock/backtest.py:239-256、:320-328）+ README.md:176/182-183 明确残余；④ README.md:151-186 四节差异表齐备，含 ST 非 PIT、D/P 覆盖、缓存新鲜度、退市整理期。ADR-008 与 adrs.json 同步登记第 2 轮收尾 3 条。代码层面仅 .devloop 记录变更（9342963、156f962），无未审代码。

【本轮新增 2 条 P2（开发处理，不阻断合并）】
- iss-mws4cwf7-d94964｜tests/test_board_rules.py:323-343：缺口用例时间夹具倒序（第 4 根 bar 仍是 05-09，早于买入的 05-17）。改法：补 df.loc[3,"trade_date"]=2024-05-20 或显式构造单调日期，并对对照用例断言卖出日期 > 买入日期。
- iss-mws4cwgj-54c06a｜README.md:184-185：新增的「更旧缓存显式报错、避免静默使用过期股票池」只约束文件缓存，进程内 _BASIC/_BASIC_ALL（astock/events.py:224-225、:237-238）无日期校验，长驻进程跨天仍静默复用。改法：README 限定为「离线回退」措辞，或给进程内缓存加同日校验并补跨天用例。

【闭环状态与责任】
技术上本轮 approve；但 ledger 里前两轮 issue 仍为 status=open（feedback=null），故 reviewer handoff 的 openP1 仍显示 1、resolved=false。责任人：开发会话——① 在第 2 轮 3 条 P2 上逐条填写反馈「已修复」（附本轮的代码/用例位置）；② 对第 1 轮 5 条同样补反馈；③ 处理或明确延后（写后续 plan 项）本轮 2 条 P2。上述三项完成后即可判定"完全解决"（resolved=true），PR #46 合并由用户决定；合并前建议 CI（当前 run 45049839619 head 9342963 全绿）在真正合并 head 上再确认一次。

## 本轮问题

### P2 · README 新增「更旧缓存显式报错、避免静默用过期股票池」但进程内 _BASIC/_BASIC_ALL 无日期校验，跨天仍静默复用

- 位置：`README.md:184-185`
- 状态：未闭环
- 置信度：0.7

**证据**

README.md:184-185 新增「离线时只有**当天**写出的基础信息缓存允许静默回退；更旧的缓存会显式报错，避免推荐/选股静默使用过期股票池」。该约束只作用于 **文件** 路径（astock/events.py:154-159 _cache_is_current、:189-205 _load_basic），而进程内 memo 仍然无日期判断：astock/events.py:224-225 `if _BASIC is not None and not refresh: return _BASIC.copy()`、:237-238 `if _BASIC_ALL is not None and not refresh: return _BASIC_ALL.copy()`。长驻进程（Gradio app / scheduler loop，astock/scheduler.py 的 loop）跨过零点后，:224 会继续返回前一天的股票池，不会走 _load_basic 的当天校验，也不会报错——与 README 让读者推断的「不会静默使用过期股票池」不一致。影响有限（进程重启即恢复，且不影响涨跌停口径），故 P2。

**建议**

二选一：(a) 文档限定——把 :184-185 改为「**离线回退**时只有当天写出的缓存文件允许静默使用；进程内缓存自首次调用起保留到进程重启」；(b) 代码对齐——给 `_BASIC` / `_BASIC_ALL` 各记一个写入日期（或复用 `_cache_is_current` 的同日判断），跨天时视为失效并重新走 `_load_basic`，并补一条用例：把 `_BASIC` 置为旧数据 + meta 为昨天，跨天后 `stock_basic()` 不再返回旧行。完成条件：README 表述与 `stock_basic()` / `stock_basic_all()` 在跨天场景下的实际行为一致（含用例或明确限定措辞）。

### P2 · 缺口用例的时间夹具倒序：卖出 bar 的日期（05-09）早于买入 bar（05-17）

- 位置：`tests/test_board_rules.py:323-343`
- 状态：未闭环
- 置信度：0.82

**证据**

tests/test_board_rules.py:324 `df = bars([10.0, 10.0, 12.5, 12.5], start="2024-05-06")` 生成 bdate_range 四根 bar = 05-06/05-07/05-08/05-09，然后 :325 把**第 3 根**覆盖成 `pd.Timestamp("2024-05-17")`，第 4 根仍是 05-09。于是 :328 注释里的「第 4 根 bar 卖出」实际发生在 2024-05-09——早于 :331 买入的 05-17，trade_date 序列非单调（offsets = [0, 1, 9, 3]）。对照用例 :340-343 断言 `len(control.trades) == 1` 恰好是这一对的成交，如果以后把断言加强为买入/卖出日期或持有天数（astock/backtest.py:430-454 的 _metrics 用 `trades["持有天数"]`），就会得到负的持有天数；同时「第 4 根 bar 卖出」的注释与实际日期不符，后来者容易误读缺口场景。断言本身当前仍具判别力（把 _listing_offsets 退回 bar 序号实现会得到 listing_day=2 而放行成交，dev 已手工验证 1 != 0），因此只记 P2。

**建议**

把夹具日期显式写成单调序列，例如 `df.loc[2, "trade_date"] = pd.Timestamp("2024-05-17")` 后补一行 `df.loc[3, "trade_date"] = pd.Timestamp("2024-05-20")`（offsets 变 [0,1,9,10]，第 4 根仍在窗口外=20% 限价，卖出不受影响），或直接 `dates = [pd.Timestamp("2024-05-06"), pd.Timestamp("2024-05-07"), pd.Timestamp("2024-05-17"), pd.Timestamp("2024-05-20")]` 构造 DataFrame 再跑。改完后对照用例额外断言 `control.trades[0]` 的卖出日期 > 买入日期，锁住时间方向。完成条件：用例仍为 0/1 成交断言且 trade_date 严格递增。

## 历史未闭环问题

- P1 · ST 5% 与 IPO 无涨跌幅规则未接入参数优化/策略实验室（run_backtest 未传 name/list_date） · `astock/optimize.py:83, 172, 187-195（另 astock/strategy_lab.p`（第 1 轮）
- P2 · 验收 3「临时停牌不产生虚假成交」与实现/README 口径不一致 · `README.md:171-175, 181`（第 1 轮）
- P2 · ST 判定使用当前证券名称，非历史 ST 状态（PIT），README 未标注 · `astock/backtest.py:78-90`（第 1 轮）
- P2 · _listing_offsets 用 bar 序号当交易日序号，停牌缺口会拉长 IPO 无涨跌幅窗口 · `astock/backtest.py:191-207`（第 1 轮）
- P2 · ST 5% 与旧规首日 44%/36% 只有纯函数用例，缺 run_backtest 端到端用例 · `tests/test_board_rules.py:110-190, 283-304`（第 1 轮）
- P2 · 缺口用例 test_suspension_gap_ends_ipo_window 实际从未下单，断言恒真 · `tests/test_board_rules.py:319-329`（第 2 轮）
- P2 · stock_basic 只查 list_status="L"，已退市/暂停上市个股的 ST 5% 与 IPO 窗口静默失效且未登记 · `astock/events.py:173-179, 215-217`（第 2 轮）
- P2 · stock_basic 网络失败时静默回退到任意日期的旧缓存，推荐/选股等调用方从失败变为拿到过期股票池 · `astock/events.py:173-187`（第 2 轮）

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
