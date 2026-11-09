# PR 评审：评审：推荐跟踪：Top5 等权组合净值 vs 沪深300

> 自动生成于 2026-11-09T00:51:08.551Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261109-001`
- 计划项：`plan-20261103-004`
- 分支：`feat/track-nav` → `main`
- 轮次：第 1 轮（full）
- 结论：需要修改（request_changes）
- 开发会话：`session-d4244044-2816-4935-bf9d-2fa6f0a87d69`
- 本轮问题：5（P0 0 / P1 1 / P2 4）
- 线程未闭环：P0 0 / P1 1 / P2 4

## 本轮结论

第 1 轮全量评审（scope=full）完成，范围 astock/track.py、astock/track_ui.py、tests/test_track_nav.py 三个文件的 diff 与 14 个新增用例、links.test 全部证据。架构侧无违规：ARCH-01/ARCH-02 未被破坏（track_ui 只做装配与渲染，指标与统计全部在 track.py/ stats 内），STAT-01 未新增 p 值路径，DATA-03 的入场口径经核对成立——astock/track.py:255 的 `tradable = pd.Timestamp(tradable_raw) if tradable_raw else next_open_date(as_of_ts)` 保证 detail 的入场日恒为 tradable_from 或 next_open_date，不存在用推荐日开盘入场的前视；批次链式拼接（nav_base 传递、ratio 以入场日开盘为基）数学正确，首行非 1.0 系与沪深300同口径所致，非遗漏。首屏结构符合 ADR-010 决策 3：nav_chart 首屏第一块、平均超额表与显著性、逐条记录进入两个默认收起的 Accordion。

结论：不通过（request_changes）。openP0=0，openP1=1，openP2=4。

必须修复（P1，iss-mw8jo6dd-b0f30e，astock/track.py:423-424、460-461）：基准 bar 缺失被当作「非交易日」处理，直接导致最新批次被剔除、且净值被基准日历截断。开发自己的真数据冒烟（42 批次/443 日、剔除 1 批 2026-11-09）就是这条缺陷的现场证据——2026-11-09 是周一交易日，只是当日基准 bar 尚未生成，文案「不是沪深300交易日」是事实错误；同时首屏最新批次永远不会出现，净值也止于基准最后一天，与验收 #1/#3「净值随批次更新、缺 bar 的批次剔除并提示」的意图冲突。修法：交易日判定改用交易日历（data 层的交易日序列）或成分股 bar 是否存在的组合，而不是「是否在 bench_dates 里」；bench 在区间末端缺 bar 时保留批次、沪深300 列末端留 NaN 并在状态行注明「沪深300 数据截至 X 日」，超额只在共同有效日计算。需补两条测试：基准日历落后于个股日历（末段缺 bar）时批次仍保留且组合线延续到最新交易日；基准缺失区间内仍能算出组合净值。

应修（P2，4 条，下一轮请逐条回应）：
- iss-mw8joahc-4fc16e（astock/track.py:354-356,441-445）批次成分数与排名缺失未校验却仍按「Top5 等权」对外；
- iss-mw8jodh9-e76932（astock/track.py:494-500）close 为 NaN 时当日权重被静默重分配，与 docstring「收益记 0」不符；
- iss-mw8jogea-70f60f（astock/track_ui.py:25-44）run_track 无异常隔离，净值段异常会连带丢掉已算好的 summary/significance/detail；
- iss-mw8jopos-909fec（astock/track_ui.py:67-74）验收 #2「折叠但可查看」缺少可复核证据（结构断言或手工验证记录二选一）。

另有两点本轮因「同一文件 P2 上限 2 条」未能立单，请在修 P1 时一并处理：(1) nav_status_text 在「全部批次被剔除 / nav 为空」或「首个批次被剔」时仍无条件输出「原持仓延续到下一批」，与 :525-526 的「暂无可拼接的批次」自相矛盾，且括注固定写「入场日缺 bar」与 :424 的「不是沪深300交易日」是两套说法，test_status_text_mentions_dropped_batches 把该文案固定住了；(2) 建议把该用例拆为「nav 为空 + dropped 非空」（断言不出现「原持仓延续」）与「nav 非空 + dropped 非空」两条。

何时判定完全解决：由开发在 feat/track-nav 上修完上述 P1 并回应 4 条 P2（至少给出处置结论：修或说明不修的理由），下一轮 diff 评审中 P1 的修复被两条新测试覆盖、真数据冒烟不再出现「最新批次因基准未生成被剔除」且状态行正确区分缺 bar 与非交易日，openP1 与 openP2 归零后，我才会给出 approve 并标记完全解决。

## 本轮问题

### P1 · 基准 bar 缺失被当成“非交易日”剔除最新批次，净值又被基准日历截断

- 位置：`astock/track.py:423-424,460-461`
- 状态：未闭环
- 置信度：0.85

**证据**

第 423-424 行用沪深300 当日 bar 充当交易日历：`if len(bench_dates) and entry not in bench_dates:` → `reason = f"入场日 {entry:%Y-%m-%d} 不是沪深300交易日（缺基准 bar）"`；随后第 460 行 `calendar = bench_dates[bench_dates >= first_entry]` 把所有行都限制在基准有 bar 的日期上。该路径在开发自己的冒烟里已经触发：links.test 第 5 条写着“42 批次/443 日 … 剔除 1 批（2026-11-09 基准 bar 未生成）”，而 2026-11-09 是周一、非节假日，正常情况下就是交易日——也就是说文案给出的“不是沪深300交易日”是事实错误，真实原因是当日指数 bar 尚未生成。后果有两层：(1) 最新一批的 Top5 从未进入首屏净值，用户盘中刷新永远看不到最近一次推荐的跟踪结果；(2) 组合净值被基准日历截断在最后一个有基准 bar 的日期，即使个股 bar 已更新。ADR-010 把这条曲线定为对外唯一指标，验收 #1「首屏为组合净值曲线」和 #3「缺 bar 的批次被剔除并提示」针对的是成分股 bar，不应由基准数据滞后决定。

**建议**

把三件事拆开：(a) “是否交易日”用交易日历（existing `next_open_date`/`last_open_date`）或成分股 bar 判断，不要拿 000300.SH 当日 bar 当交易日历；(b) 基准当日 bar 未生成时仍把该批次纳入 nav——`沪深300净值` 在该日留 NaN（或从最后有效日 ffill 并明示），`超额` 只在双方都有值的最后共同交易日计算，并在 `nav_status_text` 单独提示“基准数据截至 X，超额仅算到 X”；(c) 只有成分股在入场日缺 bar 时才按验收 #3 整批剔除，并保留每批具体原因。配套在 tests/test_track_nav.py 补两条用例：入场日为交易日但基准缺该日 bar → 批次仍进入 nav、组合净值覆盖该日、提示文案不含“不是交易日”；基准整体滞后于个股 → nav 最后交易日等于个股最后交易日。验收标准：2026-11-09 那批出现在曲线上，nav 覆盖到个股最后交易日，且不存在把“数据未更新”写成“非交易日”的提示。

### P2 · run_track 无异常隔离，净值段失败会连带丢掉已算好的表格

- 位置：`astock/track_ui.py:25-44`
- 状态：未闭环
- 置信度：0.7

**证据**

astock/track_ui.py:25-44 里 `nav, dropped = (portfolio_nav(detail) if not detail.empty else (…))`、`nav_status_text(nav, dropped)`、`nav_figure(nav)` 都是裸调用，没有任何异常隔离。`portfolio_nav` 内部只在第 409-413 行给 `load_index_daily` 和第 430-435 行给单只 `load_daily` 包了 try/except，仍有多处可抛出：第 465 行 `bench_bars["close"]`、第 494 行 `frame["close"]`（列缺失即 KeyError）、第 414 行 `_bar_frame(bench)` 里 `pd.to_datetime(out["trade_date"])`。一旦抛出，`track_recommendations()` 已经算好的 summary／significance／detail 全部作废，整个「推荐跟踪」Tab 只剩 Gradio 报错；而同文件 run_backfill（第 21-22 行）是包了 try/except 的，行为不一致。对于“首屏加点东西”的改动，不应让新增的净值段把原本可用的表一起拖垮。

**建议**

在 `run_track` 把净值段单独包起来：nav、dropped 计算失败时置空 DataFrame，`nav_status` 写成“净值计算失败：{exc}（下方表格不受影响）”，其余 5 个输出照常返回；或在 `portfolio_nav` 内部把 `bench_bars["close"]` / `frame["close"]` 的读取也纳入容错。补一条测试：`patch.object(track_ui, "portfolio_nav", side_effect=RuntimeError("boom"))` → `run_track()` 仍返回 6 个输出，第 1 个含“失败”字样，第 3 个仍是分组统计表。验收标准：净值段任一异常都不再导致整 Tab 空白。

### P2 · 验收 #2（超额表降级到折叠区）缺少可复核证据

- 位置：`astock/track_ui.py:67-74`
- 状态：未闭环
- 置信度：0.75

**证据**

验收 #2「平均超额表降级到折叠区，仍可查看」在代码里只体现为 astock/track_ui.py:67-72 的 `with gr.Accordion("平均超额表 / 显著性检验（重叠窗口，仅供参考）", open=False)` 与 `with gr.Accordion("逐条记录", open=False)`。links.test 对本 Tab 的证据只有第 5 条“gr.Blocks 构建 track tab 通过”，这是构建冒烟、不含内容断言；TrackUiWiringTests.test_run_track_puts_nav_first_and_keeps_excess_table 只断言 `len(outputs) == 6`、outputs[0] 含“组合净值/状态”、outputs[2] 含“Top2”，没有断言首屏第一个可视块是 nav_chart、也没有断言超额表与显著性表位于默认收起的 Accordion 内。因此验收 #2 目前既无自动化断言，也无明确的手工验证记录，无法复核“折叠但可见”。

**建议**

二选一即可，由开发在下一轮提交时落地：(a) 在 tests/test_track_nav.py 增加 build_track_tab() 的结构断言——遍历 blocks，断言 `Plot(label="Top5 等权组合净值 vs 沪深300")` 的 index 小于两个 Accordion，且两个 Accordion 内分别包含 summary/significance 与 detail、`open` 为 False；(b) 若认为该断言过脆，则在 links.test 或交付文档里写明手工验证步骤与结果：「打开推荐跟踪 Tab → 首屏只有净值图 + 一行口径文案 → 平均超额表/显著性/逐条记录默认收起 → 展开后三张表可见且有数据」。验收标准：验收 #2 有一条可复核的证据（测试断言或手工验证记录）落在下一轮 links.test 中。

### P2 · 批次成分数与排名缺失未校验，仍按“Top5 等权”对外

- 位置：`astock/track.py:354-356,441-445`
- 状态：未闭环
- 置信度：0.7

**证据**

第 354-356 行 `rows = rows.sort_values("_rank", na_position="last").drop_duplicates("代码")` 之后直接 `rows = rows.head(top_n)`，没有任何“是否拿到满 5 只 Top5”的校验：若某批 `排名` 整列为 NaN（旧 JSON 缺字段或回填批次），`na_position="last"` 会退回原始行序，选出的 5 只并不是排名前 5；若该批只有 3 条记录，`codes` 只有 3 只。第 441-445 行的 `if missing and require_full_batch` / `elif not frames` 只校验“已选成分在入场日有没有 bar”，不会因为不足 5 只而拒绝，于是 3 只等权被当成 Top5 等权继续拼接。对外文案在 nav_status_text 第 536 行固定输出“Top5 等权累计”，第 546 行固定写“等权买入 Top5”，用户无从知道某一批其实是 3 只、或者 5 只是按文件行序取的。tests/test_track_nav.py 的 `detail_rows` 永远给 1..5 的完整排名，没有任何“排名缺失”或“批次不足 5 只”的用例。

**建议**

在 `_batch_plans` 增加显式守卫：(1) 没有可用 `排名` 时按 `综合分` 降序兜底，两者都不可用的批次直接跳过并计入 dropped；(2) `len(codes) < top_n` 时按“成分不足 Top5（仅 N 只）”记入 dropped 并从净值剔除，或在 nav 中标记 partial 并把文案改成“等权 N 只”，不要继续宣称 Top5；(3) 在 `included` 里记录实际成分数，`nav_status_text` 输出实际范围（如“每批 5 只”或“存在 1 个 3 只批次”）。补两条测试：批次只有 3 只 → 被剔除且 `原因` 含“3 只”；`排名` 全为 NaN 但有 `综合分` → 按综合分取前 5。

### P2 · 个股 close 为 NaN 时当日权重被静默重分配，与 docstring 的“收益记 0”不符

- 位置：`astock/track.py:494-500`
- 状态：未闭环
- 置信度：0.75

**证据**

第 494-497 行 `closes = pd.to_numeric(frame["close"].reindex(segment, method="ffill"), errors="coerce")`：`reindex(..., method="ffill")` 只能补“索引里缺这一行”，对“索引在、close 值是 NaN”无效。第 500 行 `pd.concat(ratios, axis=1).mean(axis=1)` 会按行跳过 NaN 列，等于当天把该股权重设为 0、在其余 4 只上重新归一；次日该股 close 恢复，ratio 又从入场开盘价起算，权重回到 1/5。结果是累计净值不是 5 只等权组合的真实市值序列，而是权重逐日漂移的混合体。这与函数 docstring 第 393 行“成分股当日缺 bar（停牌）按上一收盘持有，当日收益记 0”的承诺不一致。tests/test_track_nav.py 的 `test_equal_weight_and_suspension_carry_forward` 只覆盖“整行缺 bar”，没有 close 为 NaN 的用例（`bars()` 构造的值全部非空）。

**建议**

改成值级填充再对齐：`closes = pd.to_numeric(frame["close"], errors="coerce").reindex(segment).ffill()`（先按 segment 对齐索引，再对数值做 ffill），这样“索引缺行”和“值为 NaN”都归为“持有上一收盘、当日收益 0”。若确实希望某只股票当天无有效价格时权重为 0，则要显式实现并写进口径文案与 docstring，不能依赖 `mean` 的 NaN 跳过语义。补一条用例：5 只中 A5 在 2026-01-06 的 close 为 NaN（行存在），断言当日组合净值等于前一交易日净值、次日仍按 5 只等权计算。

## 变更文件

- `astock/track.py`
- `astock/track_ui.py`
- `tests/test_track_nav.py`

## 交给开发会话

1. 在分支 `feat/track-nav` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
