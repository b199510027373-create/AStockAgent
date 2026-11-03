# AStockAgent

A 股智能投顾本地系统，基于 Gradio + Tushare + DeepSeek。在原有「单股体检 / 组合 / 选股 / AI 对话 / 简报」之上，新增了**多周期 K 线**、**策略回测**与**每日股票推荐**。

> 仅供学习研究，不构成任何投资建议。

## v1.1 代码加固（2026-11-02）

按代码/架构评审的 P0 清单修复：

- **调度幂等**：任务键改为 `(last_completed_trade_date, slot)`，盘前运行不再吃掉 16:40 收盘槽；`scheduler_state.json` 记录 `jobs` 状态机（running/success/stale/failed）。
- **墙钟调度**：循环每次用 `datetime.now()` 重算剩余时间，机器休眠后不会按旧倒计时漂移；16:40–23:00 补偿窗口。
- **单实例锁**：`data/recommendations/scheduler.lock`；独立入口 `python -m astock.jobs`。
- **`as_of` 拆分**：`data_as_of`（数据截止）与 `tradable_from`（可成交起点，下一开市日；若已开盘则顺延）；证据 `pub_time <= generated_at`。
- **合法 JSON**：递归清洗 NaN/Inf/NaT，`json.dumps(allow_nan=False)`；新增 `run_id`、`schema_version`、`params`、`diagnostics`。
- **失败可诊断**：`load_many` 回传失败代码/原因；推荐状态显示失败/剔除数量；`diagnostics.failed_codes` 落盘。
- **缓存**：Parquet/meta 原子写（唯一临时文件）、日历加载锁、读侧容错、内存缓存上限。
- **统计**：Newey-West `lag=horizon-1`、Student-t p 值、有效独立样本 <10 隐藏 p、BH 多重检验；跟踪改为 `tradable_from` 开盘入场。
- **口径**：遗留 Tab 统一 `astock.indicators.add_all` + qfq；ST 5% 涨跌停；Beta ddof 统一；`load_many(years=...)`；PIT union 按出现频次取前 400；`parse_universe` 支持别名。
- **台账**：新增 SQLite `data/astock.db`，推荐/证据/调度任务双写；`python -m astock.ledger stats` 可查。
- **PIT 指数池**：新增 `astock/pit.py`，用 `index_weight` 历史成分 + `stock_basic(D)` 退市过滤 + `bak_basic` 历史行业；因子 IC 支持 PIT沪深300/中证500/中证1000。

测试从 18 个增加到 39 个（`tests/test_hardening.py`）。

文档分工：

- 产品快照：[docs/AStockAgent_product_snapshot_v1.3.md](docs/AStockAgent_product_snapshot_v1.3.md)
- 架构说明：[docs/AStockAgent_architecture_v1.3.md](docs/AStockAgent_architecture_v1.3.md)
- 研究结论：[docs/AStockAgent_research_conclusions_v1.3.md](docs/AStockAgent_research_conclusions_v1.3.md)


## 功能

- **单股体检**：基础 K 线、全指标视图、AI 技术面点评
- **组合体检**：权重配置、净值曲线、相关性热力图、年化波动率
- **策略选股**：基于自选池的多条件过滤，导出 CSV
- **多周期 K 线**（新）
  - 单图叠加：日线 K 线 + MA5/20/60/120/250 + 周线 MA20 + 月线 MA12 + 布林带
  - 四宫格：日 / 周 / 月 / 季线并排，各自带均线与成交量；可只勾选 2 个做对比
  - 区间对比：近 1 月 / 3 月 / 6 月 / 1 年 / 3 年归一化到同一张图，按区间进度对齐
  - **分钟周期**：1 / 5 / 15 / 30 / 60 分钟；当日用 `rt_min`，近 5/20/60 日走 `stk_mins` 并补当日实时
- **每日推荐**（新）
  - 技术面 65%：趋势、动量、RSI、MACD、量能、ATR/回撤，并混入横截面排名
  - 事件面 35%：公告、研报评级、新闻快讯、政策库；同行业最多 2 只，避免推荐过于集中
  - DeepSeek 只做归因/风险表述，不改变量化排序；输出 CSV/JSON 与证据链
- **策略回测**（新）
  - 策略模板：MA 双均线、MACD、RSI 反转、布林突破、买入持有
  - A 股规则：T+1、一字涨停不买 / 一字跌停不卖、100 股整数倍
  - 成本：佣金万 2.5（最低 5 元）、印花税卖出万 5、滑点单边万 5
  - 指标：累计/年化收益、最大回撤、夏普、卡玛、胜率、盈亏比、换手/仓位暴露、Beta/Alpha/信息比率、相对沪深 300 超额
  - 图表：K 线买卖点、策略净值 vs 沪深 300、回撤曲线；交易明细表
- **AI 对话 / 每日简报**：上下文接入自选股、组合数据；AI 对话还会按问题检索公告/研报/新闻/政策并附来源

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# 编辑 .env，填写 TUSHARE_TOKEN、TUSHARE_API_URL、DEEPSEEK_API_KEY
python app.py
```

或双击 `start_app.bat`，启动后访问 http://127.0.0.1:7861 。

## 配置

`.env`（已在 `.gitignore` 中）：

```dotenv
TUSHARE_TOKEN=your_tushare_token
TUSHARE_API_URL=http://your-tushare-proxy:8020
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE=https://api.deepseek.com
```

- `astock.config` 启动时自动加载 `.env`。
- **不要提交 `.env`**；如果 token 曾经出现在公开仓库，请立即在数据商后台重置。
- 旧版 `ts_init.py` 里的硬编码 token 已移除；兼容入口改为读环境变量。

## 网络与数据源

- Windows 上 Clash 等系统代理会拦截 `127.0.0.1` 和 Tushare 请求，表现为 **502**。
  - `astock.tushare_client` 使用 `requests.Session(trust_env=False)` 绕过系统代理。
  - `app.py` 启动时设置 `NO_PROXY=127.0.0.1,localhost,::1`，避免 Gradio 启动自检 502。
- 日线、复权因子、周/月线、指数、交易日历已验证可用。
- **分钟线**：当日 `rt_min`，历史 `stk_mins`（1/5/15/30/60 分钟），已开通；历史分钟按 10–80 天分块拉取，避免单次行数上限；默认按日复权因子做**前复权**。
- **事件数据**：`anns_d` 公告、`report_rc` 研报评级、`research_report` 研报正文标题、`npr` 政策库、`news` 快讯（新浪/同花顺/东财）、`major_news` 长篇新闻。
- `rt_min` / `stk_mins` 原始 `vol` 单位是股、`amount` 是元；已统一转成手 / 千元，和日线口径一致。
- 行情缓存位于 `cache/`（parquet），事件缓存 5–10 分钟，已在 `.gitignore` 中。

## 多周期 K 线的口径

- 日线默认**前复权**（qfq）；周/月/季线由日线重采样，`W-FRI` / `ME` / `QE`。
- 分钟线：当日走 `rt_min`；近 5/20/60 日走 `stk_mins`，再补当日 `rt_min`；按每日 `adj_factor` 以最新因子为基准做**前复权**，和历史日线价格可对齐。
- 分钟横轴用 `trade_time`，午休和隔夜用 `rangebreaks` 隐藏；多日图按 `%m-%d %H:%M` 显示。
- 周期标签取该周期内**最后一个实际交易日**，不会出现 10-31 这种未来日期。
- K 线图用 `rangebreaks` 去掉周末和节假日，长历史不会出现大片空白。
- 均线在对应周期上计算；周线 MA20 / 月线 MA12 映射回日线时用 `ffill`，不会提前看到未来值。

## 每日推荐口径

- 股票池取 `stocks.csv`，或在页面输入逗号分隔的代码/名称；默认最多 60 只。
- **技术分**：MA20/60/120、MACD、RSI、量比、20/60 日动量、ATR 与回撤；再混入横截面排名，避免绝对分在 100 附近饱和。
- **事件分**：近 30 天公告、7 天研报评级/研报标题、3 天新闻、7 天政策；按关键词情感、评级方向、公告正负事件聚合为 0–100。
- **综合分 = 0.65 × 技术分 + 0.35 × 事件分**；同行业最多输出 2 只。
- 数据截止时间写在页面状态里；`data_as_of` 取有效日线的最新收盘，`tradable_from` 是下一开市日（若生成时已开盘则再顺延），证据必须早于 `generated_at`。
- DeepSeek 仅根据给定证据写「逻辑/催化/风险」，不改排序；没有证据时会明确说资料不足。
- 结果保存到 `data/recommendations/*.csv|json`（含 `run_id` / `data_as_of` / `tradable_from` / `diagnostics`），同时原子更新 `latest.json` 供 AI 对话引用。
- 首次运行需要为股票池拉 3 年日线（50 只约 30–60 秒），之后走 parquet 缓存会快很多。
- **自动任务**：应用运行时每个交易日 **16:40** 自动生成一次（`ASTOCK_AUTO_RECOMMEND=0` 关闭，`ASTOCK_RECOMMEND_TIME` 改时间）；也可单独运行 `start_scheduler.bat`（`python -m astock.jobs`），或用 Windows 任务计划调用 `python -m astock.jobs --once --slot close`。日志在 `data/recommendations/scheduler.log`。调度器有单实例锁；盘前/收盘任务键不同。

> 这是量化初筛 + 文本证据的工具，不是投资建议；不要用单一分数直接下单。

## 推荐跟踪

- 「📈 推荐跟踪」Tab 读取 `data/recommendations/recommend_*.json`，优先使用带 `data_as_of` / `tradable_from` 的新格式；旧文件回退到 `as_of` 的下一开市日。
- 从 `tradable_from` **开盘**买入，计算持有到 **T+1 / T+5 / T+20** 收盘的收益，并减去同期沪深300 收益得到超额；入场日无 bar 则返回空，不静默后移。
- 按 `Top2`、`其余`、`综合分≥75`、`综合分<75`、`全部` 分组统计：平均超额、胜率、有效样本数。
- 显著性在**日期层面**：先按推荐日求组合/分组的平均超额，再对日期序列做 t 检验、Newey-West（`lag=horizon-1`）、block bootstrap 与 **BH 多重检验**；`Top2 - 其余` 同样按日期差序列检验。
- 明示「独立窗口(约)」和「有效独立样本」：有效独立样本 <10 时隐藏 p 值（当前 T+5/T+20 就是这种情况），不把重叠窗口包装成显著。
- 样本逐日积累；至少 20 个实盘批次后再看显著性。
- 「回填近30个交易日（仅技术分）」会用历史日线回填 30 个技术分批次（事件分固定 50），用于在等不到 20 个自然日时先检验技术分排序；回填不等于完整推荐。
- 跟踪只做研究，不构成投资建议；如果推荐生成后发生停牌/退市/复权因子变化，前向收益可能缺失。
- 注意：多批次、多周期的前向收益存在重叠，t/p 值会偏乐观；正式判断要结合批次独立性/市场环境。

## AI 对话的新闻数据源

- 从问题中识别股票代码/名称（如「茅台」「600519.SH」），检索：
  - 近 30 天公告（`anns_d`）
  - 近 7 天研报评级与研报标题（`report_rc` / `research_report`）
  - 近 3 天新闻快讯（`news`）
  - 近 7 天长篇新闻与政策（`major_news` / `npr`）
- 命中的证据以 `[1] [2]` 注入 system prompt，回答末尾自动附「数据来源」；模型被要求资料不足时不要编造。
- 同一轮对话如果提到多只股票，会分别检索；未提股票时返回市场要闻与政策。

## 回测口径与限制

- 信号在 **t 日收盘**生成，**t+1 日开盘**成交，避免未来函数。
- T+1：当日买入不能当日卖出。
- 一字涨停不能买、一字跌停不能卖；停牌（成交量为 0）不交易。
- 满仓、long-only；按 100 股整数倍；初始资金默认 50 万（高价股一手可能超过 10 万）。
- 指标包含未平仓浮盈亏；交易明细只列已平仓交易。
- 基准为沪深 300（`000300.SH`）。
- **回测结果不等于未来收益**；参数寻优容易过拟合，建议看样本外区间与参数平原，而不是单一最优值。

## 参数优化与 Walk-forward

- 「🧪 参数优化」Tab 对 MA 的 `fast × slow` 做网格，指标可选夏普/年化；输出参数热力图与全样本前 5 组。
- Walk-forward：滚动训练窗口（默认 1.5 年）选最优参数，下一个测试窗口（默认 3 个月）做样本外验证，滚动前进。
- 跨窗口**连续持仓**：现金、持股、成本价、T+1 状态和最后信号延续到下一个测试窗口；窗口边界不强制平仓。
- 输出每个窗口的最优参数、测试收益/夏普/回撤/超额，以及连续拼接后的样本外净值 vs 沪深300。
- **全样本最优只是 in-sample 参考**；判断策略要看 walk-forward 样本外与参数平原，而不是单个最高分。

## 策略实验室（MACD/RSI/多标的）

- 「🧪 策略实验室」支持 MA双均线、MACD、RSI反转 的固定参数网格。
- 每只股票独立做 walk-forward，选择训练窗口最优参数，在下一个测试窗口做样本外；持仓跨窗口连续，再把各标的样本外收益**等权拼接**成组合。
- 输出分标的样本外指标、每个窗口的最优参数/测试收益/超额，以及等权组合净值 vs 沪深300。
- 最多 10 只股票；MACD 27 组、RSI 27 组、MA 12 组参数，首次运行约 20 秒。
- 这只是研究工具，不代表实盘可复制。

## 多因子 IC

- 「📊 因子 IC」Tab 在股票池横截面上计算 RankIC：20/60 日动量、5 日反转、20 日波动、量比、成交额比、Amihud 非流动性、MA20 乖离、RSI14、MACD 柱、ATR 占比。
- 指标：IC 均值、IC 标准差、ICIR、IC>0 占比、t 值、p 值、有效日数；另给 Q1–Q5 分层平均收益、Q5-Q1 与单调性。
- 不使用未来数据：因子取当日及以前数据，前向收益从当日收盘往后 T+5/T+20。
- 股票池支持：自选池、按最新总市值排序的前 100 / 前 300、**动态前 100/300（PIT）**、**PIT 沪深300/中证500/中证1000**（`index_weight` 历史成分 + 退市过滤 + 历史行业）；也可手填代码。
- 默认做**行业 + 市值中性化**：每日横截面对行业哑变量 + log(总市值) 回归，取残差；PIT 指数模式用历史成分/历史行业/历史市值，动态市值模式仍只含当前上市公司。
- 行业版本：**2021-12-13 之前用 SW2014 L1，之后用 SW2021 L1**（`index_classify` + `index_member` 历史成员）；`bak_basic` 只补历史名称/ST，缺失时用 `stock_basic` 行业兜底。
- 日期少时 IC 噪声大；扩到前 100 后约 12k 行、130+ 个 IC 日，PIT 指数模式约 2.4k 行/18 个 IC 日（1 年），结论仍只作研究。

## 项目结构

```text
app.py                 Gradio 入口（原有 Tab + 新 Tab）
astock/
  config.py            .env / 路径 / 常量
  tushare_client.py    无系统代理的 Tushare HTTP 客户端（重试、权限提示）
  data.py              日线/分钟/复权/缓存/交易日历/重采样
  indicators.py        向量化指标（MA/EMA/RSI/MACD/BOLL/KDJ/ATR/OBV/CCI/VR/PSY）
  charts.py            日线/分钟 K 线、区间对比
  ui.py                「📈 多周期」Tab
  backtest.py          回测引擎、策略信号、指标/基准
  backtest_chart.py    回测图与指标表
  backtest_ui.py       「🧪 回测」Tab
  events.py            公告/研报/新闻/政策/股票基础信息
  recommend.py         技术分 + 事件分 + 每日推荐流水线
  recommend_ui.py      「📰 每日推荐」Tab
  track.py             T+1/T+5/T+20 跟踪、分组超额与显著性检验
  track_ui.py          「📈 推荐跟踪」Tab
  backfill.py          历史技术分回填（事件分固定 50）
  optimize.py          MA 参数网格、walk-forward、样本外净值
  optimize_ui.py       「🧪 参数优化」Tab
  strategy_lab.py      MACD/RSI/MA 网格 + 多标的 walk-forward
  strategy_ui.py       「🧪 策略实验室」Tab
  factors.py           多因子面板、RankIC、行业/市值中性化、分层收益
  factor_ui.py         「📊 因子 IC」Tab
  universe.py          市值前 N / PIT 动态市值股票池、市值快照
  pit.py               PIT 指数成分/退市/历史行业快照
  ledger.py            SQLite runs/items/evidence/jobs/tracking
  stats.py             NW / Student-t / 有效独立样本 / BH
  scheduler.py         交易日 16:40 自动推荐（单实例锁、幂等任务）
  jobs.py              独立调度进程 / --once 任务入口
  llm.py               DeepSeek 客户端（直连/代理自动切换）
start_scheduler.bat    单独启动自动推荐调度器
tests/
  test_quant.py        T+1、涨跌停、成本、重采样、指标边界、分钟复权、前向收益
  test_recommend.py    技术分、事件情感、别名、行业分散、JSON 解析
  test_optimize.py     参数网格、walk-forward、策略实验室、显著性、因子 IC
  test_hardening.py    调度幂等、严格 JSON、ST、行业上限、NW/BH、t 分布、ledger、PIT 映射
```

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前覆盖：信号次日成交、资金不足一手、一字涨停不可买、T+1、成本侵蚀、平价数据指标无 `inf`、周线聚合、分钟前复权、前向收益；技术分、事件情感、别名、行业分散、LLM JSON 解析；参数网格、walk-forward、连续持仓、策略实验室、显著性、因子 IC；调度幂等、严格 JSON、ST 5%、行业上限、NW/BH、t 分布、SQLite ledger、PIT 快照。共 39 个测试。

## 已知待办

- 用回填批次先看技术分，再等实盘批次积累 20+ 天，比较“技术分回填”和“完整推荐（技术+事件）”的差异。
- PIT 动态市值模式仍只含当前上市公司；要完全消除幸存者偏差，请使用 PIT 指数模式（`index_weight` + 退市过滤）。
- 因子池继续扩到前 300/全市场，增加质量、估值、资金流因子；PIT/统计修好前不继续扩因子。
- 策略实验室扩到布林/多策略组合，做交易成本敏感性、参数平原稳定性与多市场环境分层；当前 WF 是独立窗口重开仓，需注明。
- 组合页把「算收益」与「调 LLM」拆开，避免重复调用 API；自动任务挂 Windows 任务计划并加失败告警。

## 免责声明

本项目仅用于技术学习与研究，所有输出均不构成投资建议。股市有风险，投资需谨慎。
