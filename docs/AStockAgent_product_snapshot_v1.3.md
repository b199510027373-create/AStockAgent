# AStockAgent 产品快照 v1.3

> 更新：2026-11-03  
> 定位：本地单用户 A 股研究台；不是投顾产品，不构成投资建议。  
> 本文件只写“当前有什么、跑到什么状态”，不写架构决策和长期研究结论。

## 1. 当前入口

- Web：`http://127.0.0.1:7861`
- 启动：`start_app.bat` / `python app.py`
- 调度：`start_scheduler.bat` / `python -m astock.jobs`
- 自动推荐：交易日 16:40，补偿窗口至 23:00；单实例锁 `data/recommendations/scheduler.lock`

## 2. 13 个 Tab

| Tab | 状态 | 说明 |
|---|---|---|
| 🔍 单股 | 可用 | 基础/全指标视图、AI 点评、新闻引用 |
| 📊 组合 | 可用 | 代码:权重、净值、相关性、波动率 |
| 📰 简报 | 可用 | 自选池每日简报 |
| 🎯 选股 | 可用 | RSI/MACD/CCI/ATR/VR 等条件过滤 |
| 📋 自选 | 可用 | 本地 stocks.csv |
| 💬 AI 对话 | 可用 | 公告/研报/新闻/政策检索 + 来源 |
| 📈 多周期 | 可用 | 日/周/月/季 + 1/5/15/30/60 分钟；qfq |
| 📰 每日推荐 | 可用 | 技术 + 事件 + LLM 归因；run_id/diagnostics |
| 📈 推荐跟踪 | 可用 | SQLite ledger 读取；T+1/5/20；NW/BH |
| 🧪 参数优化 | 可用 | MA 网格 + 连续持仓 WF |
| 📊 因子 IC | 可用 | 11 因子；PIT 指数池；中性化 |
| 🧪 策略实验室 | 可用 | MACD/RSI/MA；多标的连续 WF |
| 🧪 回测 | 可用 | A 股规则、成本、原始价/ST/IPO |

## 3. 当前数据与运行状态

- 股票池：`stocks.csv` 49 只。
- 最新推荐：
  - `data_as_of=2026-11-02`
  - `tradable_from=2026-11-04`
  - 最新手动批次 2026-11-03 10:55 成功；16:40 调度任务将在收盘后运行
- 推荐台账：
  - SQLite `data/astock.db`
  - 55 runs / 263 items / 538 evidence / 585 tracking
- 跟踪：
  - 39 个批次 / 195 条记录
  - 实盘 20 条，技术分回填 175 条
  - T+5/T+20 有效独立样本 <10，p 值已隐藏
- PIT：
  - `index_weight` 历史成分
  - `stock_basic(D)` 退市过滤
  - `bak_basic` 逐日行业（2016-11+）
  - SW2014 L1 历史成员（2016 年前，7,558 条）
- 测试：39 个，全部通过。

## 4. 产物位置

```text
data/recommendations/   JSON/CSV、latest.json、调度日志
data/astock.db          SQLite ledger（运行时，不入库）
cache/                  日线/分钟/PIT 快照
deliverables/           产品文档/评审/修复记录
docs/                   产品快照/架构/研究/接口自查
```

## 5. 当前限制

- 数据源仍是单一 Tushare 代理；无自动降级。
- SW2014→SW2021 行业边界未精确校准。
- 推荐 UI 的“生成”走实时流水线；历史读取按钮走 ledger，二者尚未完全统一。
- 事件分是加权情绪均值，权重是人工设定，未用标注样本校准。
- LLM 只解释，不参与排序；没有 LLM 时走规则理由。

## 6. 验收口径

- 页面能打开，16:40 任务能成功/可重试，`latest.json` 合法。
- 推荐有 `run_id / data_as_of / tradable_from / diagnostics`。
- 跟踪优先读 ledger；T+5/T+20 不显示不可信 p。
- 回测涨跌停使用原始价；WF 跨窗口持仓连续。
