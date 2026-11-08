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
- 现状：qfq/raw、ST 5%、Beta ddof 已落地；不同板块与临时停牌细则仍未完全覆盖。

## 证据

- `astock/data.py`（`raw_*`、`base_factor`）
- `astock/backtest.py`（限价、ST、IPO）
- 修复记录 D7/D8/D12
