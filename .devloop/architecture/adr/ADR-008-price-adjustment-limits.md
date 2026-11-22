# ADR-008 价格口径：qfq 信号 + raw 限价 + ST/IPO

- 状态：accepted
- 日期：2026-11-03
- 来源：评审 §2.2 D7/D8/D12；修复记录 §2

## 背景

同一只股票在不同 Tab 可能得到不同的 RSI/MA，因为 `app.py` 内置了一套 121 行的指标实现，而 `astock/indicators.py` 是另一套；部分遗留 Tab 还没做前复权。回测的涨跌停判断用复权价，等于用"调整后的价格"去撞"原始的涨跌停价"，规则失真。

## 决策

1. 指标唯一实现：`astock/indicators.add_all`；删除 `app.ind()`，旧字段用别名适配。
2. 信号、展示、因子统一使用**前复权（qfq）**，基准为最新复权因子（等价于最新日线收盘）。
3. 数据层同时保留 `raw_open/raw_high/raw_low/raw_close/raw_pre_close`；成交与涨跌停判断**只用原始价**。
4. `limit_pct(code, name)` 支持 ST 5% 与普通 10%/20%（创业板、科创板）；IPO 首日无涨跌幅与临时停牌细则后续补齐。
5. 缓存 meta 记录 `base_factor`；基准变化即视为缓存失效，禁止跨日混用。
6. 分钟线按每个交易日的 `adj_factor` 前复权，成交量/成交额不复权，单位统一为手/千元。

## 后果

- 正面：六个 Tab 口径一致；涨跌停规则正确；复权基准可追溯。
- 负面：Parquet 体积增加（qfq + raw 双份）；板块规则会随交易所调整，需要维护。
- 现状：qfq/raw、ST 5%、Beta ddof 已落地；不同板块与停牌细则已由 plan-20261103-009 补齐（2026-11-23），盘中临停/封单排队仍不建模。

## 补充裁决（2026-11-14，D1）

用户明确 hfq 口径为“标准后复权，基准 1.0”。补充如下，不改变 qfq 仍为信号/展示默认：

1. **hfq 定义**：对每只证券，取该证券最早可用交易日的 `adj_factor` 作为 `base_factor`（令其为 1.0）；`hfq_price = raw_price * adj_factor / base_factor`。不得把最新因子当 1.0（那是 qfq），也不得全市场共用一个基准日。
2. **qfq 不变**：`qfq_price = raw_price * adj_factor / latest_adj_factor`，基准为最新复权因子；信号、因子、默认图表仍用 qfq。
3. **因子缺失/异常硬失败**：`adj_factor` 缺失、为空、`<= 0`、NaN、inf 一律抛 `DataContractError`；禁止静默回退 qfq、禁止用 1.0 填充、禁止跳过该标的继续算。
4. **分钟复权单列**：分钟线使用该交易日日线的 `adj_factor` 复权，metadata 必须写 `adj=hfq`、`adj_base=<最早交易日>`、`adj_source=daily`；分钟 hfq 不与日线 hfq 拼成连续序列，缺因子报错。
5. **raw 边界不变**：涨跌停、成交额/成交量、绝对价位判断仍用 raw；缓存 meta 的 `base_factor` 变化即失效。
6. **可追溯**：图表与行情工具的 metadata 暴露 `adj`/`adj_base`/`adj_source`；`ChartSpec.data.adj` 增加 `hfq` 枚举（默认仍 `qfq`），旧 `none` 保留给涨跌停/成交类图。落地项 `plan-20261114-003`（D1）与 `plan-20261114-002`（图注）。**落地位置（2026-11-20 评审 iss-mwojg80u-28e11e）**：`astock/agent/tools/market_tools.py` 的 `MarketSeries.metadata()` 与 `kline` 工具 meta 固定输出 `adj_base`/`base_factor`/`adj_source`（数据层没给就 `None`，不伪造基准）；`_render_kline` 把同三键写进 `ChartSpec.meta`（自由对象，不占 `data` 白名单），图注文本另由 `chart_render._caption`/`charts._adj_caption` 渲染。
7. **盘中当日 bar 例外（2026-11-16 评审补充）**：Tushare 当日 `adj_factor` 收盘后才发布，而 `rt_min` 的当日 bar 盘中已存在。仅当"缺因子的行都是今天、且因子表末行早于今天"时，用最近已发布因子复权，并在 metadata 标 `partial=true` 与 `factor_as_of=<该因子交易日>`，供图注提示"盘中近似"；其它任何缺因子（含非当日历史缺失）仍按第 3 条硬失败。禁止 ffill/bfill。该例外只放宽"当日尚未发布"，不放宽"因子表缺行"。
8. **全历史因子必须分页；覆盖校验落在日线路径**：单次 `query` 受服务端行数上限截断。实测 000001.SZ 自 `19900101` 起：单次查询 6000 行、最早 20020304（该股 19910403 上市，最早段被截断）；`query_all` 分页 8692 行、最早 19910403。因此日线与分钟线的因子历史一律优先走 `query_all`（`_query_adj_history`，测试替身只有 `query` 时回退单次查询）。**覆盖校验的分工**：日线路径用 `_check_factor_coverage` 显式校验——hfq 要求因子表首日 <= 行情首日，qfq 要求因子表末日 >= 行情末日，不满足抛 `DataContractError(code="AdjFactor")`；分钟路径**不设**覆盖断言，由缺因子硬失败兜底（盘中当日 bar 按第 7 条例外），因为因子截断若落在分钟窗口之内必然表现为窗口内某天缺因子，而窗口内每天都有因子时首末比较恒为真、断言不可达（评审 iss-mwixznmz-2f393e 指出该死调用已删除）。**可观测边界**：截断边界恰好早于窗口起点、且窗口内每天都有因子的情形无法在分钟内自证，只能靠 `query_all` 分页完整性——因此两条路径都不得把单次 `query` 作为常态，回退仅用于没有 `query_all` 的测试替身。
9. **缓存基准可自证才命中**：任何复权口径的缓存 meta 必须带 `adj_base`（旧版缺失一律失效）；qfq 用请求 `end` 对应的最后一行因子作基准（不是缓存末行，否则同一请求随缓存窗口漂移）；hfq 要求缓存覆盖基准日，否则视为"基准不可证"直接失效重拉（CACHE-01）。

## 落地（plan-20261114-003，2026-11-16）

1. **因子历史区间**：`astock/data.py` 新增 `ADJ_HISTORY_START = "19900101"`；`_fetch_daily` 与 `load_adj_factors` 均从该日起取 `adj_factor`，基准不再由请求区间推断。
2. **基准与比值**：`_adj_base(factors, adj)` 中 qfq 取最新因子、hfq 取最早可用交易日因子（`iloc[0]`）；`ratio = adj_factor / base_factor`，hfq 首日价格等于 raw，之后按累计因子放大。
3. **硬失败**：新增 `_clean_adj_factors` 与 `astock.contracts.DataContractError`（`ContractError` 子类）；空表、缺列、缺失/NaN/inf/`<=0` 一律抛错，禁止 ffill/bfill、禁止填 1.0、禁止回退 qfq；`BarFrame.validate(require_adj=True)` 同步加严。
4. **metadata**：日线与分钟线结果 `attrs["adjustment"]` 统一为 `{adj, adj_base, base_factor, adj_source="daily"}`；`load_daily`/`load_minute` 在 `validate_bars` 之后重新挂回 attrs，供行情工具与图表图注消费。
5. **缓存失效**：`_cache_base_valid` 校验 meta 的 `base_factor`/`adj_base`——任何口径缺 `adj_base` 直接失效；qfq 按请求 `end_eff` 取缓存中 `trade_date <= end_eff` 的最后一行因子比较；hfq 要求覆盖基准日且该日因子等于 `base_factor`，基准日不在缓存窗口内（生产常见：缓存按请求区间切片）按失效重拉。
6. **raw 边界不变**：涨跌停、成交额/成交量与 `raw_*` 列保持原始价；分钟线只改价格列与涨跌幅。
7. **测试**：`tests/test_data_adj_caliber.py` 覆盖 hfq/qfq/raw 对照（逐行比值恒定）、hfq 收益率与已知因子序列、基准不受请求区间影响（断言查询起点为 `19900101` 且走 `query_all`）、因子异常（空/`=0`/负/NaN/inf）与缺行、因子表首/末被截断时 hfq/qfq 报错、分钟 attrs、盘中当日 bar 的 `partial`/`factor_as_of`、非当日缺因子仍报错、缓存 `base_factor`/`adj_base` 篡改与基准日不在窗口内、qfq 基准随请求 `end` 变化、旧 qfq meta 无 `adj_base` 失效。
8. **评审修复（rev-20261116-002）**：新增 `_query_adj_history`（优先 `query_all`，测试替身只有 `query` 时回退）与 `_check_factor_coverage`（仅日线 `_fetch_daily` 调用）；`_cache_base_valid` 增 `end_eff` 形参与 `adj_base` 前置校验；`_apply_minute_adj` 增盘中 `partial` 分支；`load_adj_factors` 改走分页助手。第 3 轮复核（iss-mwixznmz-2f393e）：曾短暂在 `_apply_minute_adj` 增加 `covered` + `_check_factor_coverage` 调用，经复核该断言在缺因子硬失败之后恒为真（不可达），已删除，分钟路径改由缺因子分支兜底并在用例中断言异常文案来自该分支（第 8 条与此保持同步）。

## 落地（plan-20261120-001，2026-11-20）：标注必须活着走到展示层

2026-11-20 实跑回测发现：口径本身对，但标注在派生帧上会消失，图注也会说错话。

1. **attrs 随派生帧传播**：`astock.data.enrich()`（内部 `indicators.add_all`）与 `resample_ohlcv()` 都会新建 frame，必须显式带回 `attrs["adjustment"]`；`period_frame()` 经二者组合后同样保留。否则 `run_strategy()` 返回帧、UI 图表的周/月/季线与 `adj_base`/`base_factor` 全部丢失。
2. **图注区分三种口径**：`astock.charts.summary_markdown()` 不再用 `'前复权' if adj == 'qfq' else '不复权'` 的三元表达式，改为按 `attrs["adjustment"]["adj"]` 取 `qfq`/`hfq`/`none`/未知；`hfq` 必须显示"后复权（hfq）"与"基准 `<adj_base>` 起 1.0"，`qfq` 显示"基准 `<adj_base>`，因子 `<base_factor>`"，任何口径都不得把 hfq 标成不复权。
3. **测试**：`tests/test_data_adj_caliber.py` 新增 `AdjustmentMetadataPropagationTests`（`enrich`/`resample_ohlcv`/`period_frame`/`load_daily→enrich` 全链路保留 attrs）；`tests/test_charts_summary.py` 覆盖 hfq/qfq/none/未知/无 attrs 回退与 `summary_markdown` 实际图注。

## 落地（plan-20261120-001 评审 P2，2026-11-20）：partial 必须被消费，空帧也要带标注

评审 rev-20261116-002 第 5 轮（iss-mwoit6on-7d19f2 / iss-mwoit6pr-6c8c9f / iss-mwoit6qf-174fb1）指出三处标注"写得出、看不到"的残余：

1. **`partial`/`factor_as_of` 被消费**（选 (a)，未拆后续计划项）：`astock/agent/tools/market_tools.py` 的 `MarketSeries` 增加 `partial`/`factor_as_of` 字段，`load_series()` 从 `frame.attrs["adjustment"]` 读并追加一条 notes「盘中近似：当日 bar 按最近已发布因子（截至 X）复权」；`metadata()` 固定输出这两个键；`_render_kline()` 把它们写进 `ChartSpec.meta`（自由对象，不占 `data` 白名单）；`astock/agent/chart_render.py:_caption()` 与 `astock/charts.py:_adj_caption()` 据此追加「盘中近似（因子截至 X）」。因子已发布时不得出现该提示。
2. **空帧也带标注**：`astock/data.py` 新增 `_empty_with_attrs()`，`enrich()`/`resample_ohlcv()` 的空帧快速返回改为回填输入 `attrs`（停牌整段、窗口内无交易日时图注/回测不再退回调用方 adj）。
3. **周期线回归**：`period_frame` 用例的 subTest 覆盖 `week`/`month`/`quarter`（`data.py` 有 `QE` 规则），季线标注通道有回归保护。

## 落地（plan-20261103-009，2026-11-23）：板块 / IPO 窗口 / 停牌

决策第 4 条遗留的「IPO 首日无涨跌幅与临时停牌细则」在本项补齐，`limit_pct` 之外新增规则层：

1. **板块识别**：`board_of(code)` 按后缀/前缀分类（`.BJ`、43/83/87/92 → 北交所，88 开头为同花顺行业指数不按股票处理；300/301 → 创业板；688/689 → 科创板；其余主板），限价 `_BOARD_LIMIT` 为 10%/20%/20%/30%；ST 仍全局 5%（契约 DATA-04 不变，创业板/科创板 ST 现实 20%，取保守侧并在 README 说明）。
2. **日期切换**：`limit_rule()` 以 `trade_date` 处理创业板 2020-08-24 前 10%（`CHINEXT_REFORM_DATE`）。
3. **IPO 窗口**：`ipo_unbounded_days()` 给出无涨跌幅交易日数——科创板/创业板 5 日、北交所 1 日、2023-04-10 起主板注册制新股 5 日（`MAIN_BOARD_REG_IPO_DATE`）；窗口内 `LimitRule(None, None)`，旧规则首日 44%/36%，次日起常规限价。窗口判定依赖 `list_date` 且日线必须从上市日起完整覆盖（`_listing_offsets`），覆盖不足时返回 `None` 走常规限价，不乐观假设「新股无涨跌停」。
4. **停牌**：`_has_trading_volume()` 0 量（无 vol 时看 amount）视为不可成交；`run_backtest` 对上市前 bar（负偏移）与停牌 bar 都不产生买卖；缺 bar 时只在实际存在的 bar 上成交，不补虚假行。
5. **测试**：`tests/test_board_rules.py` 覆盖板块限价、创业板日期切换、IPO 窗口与旧规则、一字板按板块判定、停牌/复牌/缺 bar/上市前 bar、以及「数据未覆盖上市日不假设无涨跌幅」。
6. **文档**：README「回测口径与限制」新增板块限价表、IPO 窗口、停牌与「回测 vs 实盘已知差异」小节。
7. **未覆盖（显式标注）**：盘中临时停牌（日线不可见，见下方保守近似）、涨跌停价逐步取整、退市整理期、ST 板块感知、封单量与排队。

### 评审 rev-20261123-001 修复（2026-11-23）

1. **规则接线（P1）**：`optimize.grid_search` / `walk_forward`、`strategy_lab.grid_strategy` / `walk_forward_strategy` / `portfolio_walk_forward` 增加 `name` / `list_date`（组合入口用 `meta[code]`）并透传 `run_backtest`；三个 UI 统一用 `events.security_meta()` 取名称与上市日（`stock_basic` 字段补 `list_date`，旧缓存缺列时尝试重拉，离线退回旧缓存）。ST 5% 与 IPO 窗口不再只在单票回测生效。
2. **停牌口径（P2）**：新增 `_suspected_intraday_halt()`——无涨跌幅窗口内振幅 `high/low-1 ≥ 30%` 视为可能盘中临停、不成交；README 同步为「保守近似」，振幅更小的临停仍不可见。
3. **IPO 窗口计数（P2）**：`_listing_offsets` 改用工作日计数（`np.busday_count`），停牌缺口不再让「上市后第 N 个交易日」偏小、把已恢复限价的 bar 错放进无涨跌幅窗口；节假日计入只会收紧窗口。
4. **ST 非 PIT（P2）**：README「回测与实盘的已知差异」登记 ST 判定取 `stock_basic` **当前名称**，未接 `namechange` 按交易日取历史名称。
5. **测试**：新增端到端用例（ST 5% 一字 0 成交、旧规首日 44% 一字 0 成交、次日非一字可成交）、停牌缺口结束 IPO 窗口、临停振幅判定、优化/策略实验室透传规则与 `security_meta` 用例。

### 评审 rev-20261123-001 第 2 轮收尾（2026-11-23）

1. **缺口用例可判别**：`test_suspension_gap_ends_ipo_window` 改为 `signals=[0,1,0,0]`（第 3 根 bar 尝试买入、第 4 根卖出），断言停牌缺口后 `trades==0` 且 `shares==0`；对照用例把 `pre_close` 改为 12.5 时 `trades==1`。已手工验证：把 `_listing_offsets` 还原为 bar 序号实现后该用例失败。
2. **退市/暂停上市覆盖**：`events.stock_basic_all()` 查询 `list_status ∈ {L,D,P}` 并独立缓存（`_BASIC_ALL` / `stock_basic_all.parquet`），`security_meta()` 改用它，退市股的 ST 5% 与 IPO 窗口不再静默失效；`stock_basic()` 仍只含 L，推荐/选股池不会被退市股污染。D/P 查询失败时退回仅 L（不阻断主流程）。
3. **缓存新鲜度**：新增 `_cache_is_current()`；网络失败时只有当天缓存允许静默回退，更旧缓存按原行为抛出异常，避免推荐/选股等调用方无提示地使用过期股票池。

### 评审 rev-20261123-001 第 3 轮收尾（2026-11-23）

1. **缺口用例夹具单调**：`test_suspension_gap_ends_ipo_window` 的第 4 根 bar 显式设为 2024-05-20（原为 05-09，早于买入日 05-17），offsets 变为 `[0,1,9,10]`；对照用例额外断言卖出日期晚于买入日期，避免持有天数等派生断言失真。
2. **进程内缓存按天校验**：`stock_basic()` / `stock_basic_all()` 的 memo 增加 `_BASIC_DAY` / `_BASIC_ALL_DAY`，只有当天写入才复用，长驻进程跨天会重新读盘/回源；README 的「新鲜度」表述同步覆盖文件缓存与进程内缓存，并补 `test_in_process_memo_is_refreshed_across_days` / `test_in_process_memo_reused_within_day` 两条用例。

## 证据

- `astock/data.py`（`raw_*`、`base_factor`）
- `astock/backtest.py`（限价、ST、IPO）
- 修复记录 D7/D8/D12
- 用户 2026-11-14 裁决（D1）
- `astock/contracts.py`（`DataContractError`、`BarFrame.validate` 的 `adj_factor` 硬校验）
- `tests/test_data_adj_caliber.py`（hfq/qfq/raw 对照、因子异常、分钟 attrs、缓存基准失效）
- `tests/test_data_pit_events.py`（hfq 最早因子基准、空因子抛 `DataContractError`）
