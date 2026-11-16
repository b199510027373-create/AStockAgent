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

## 补充裁决（2026-11-14，D1）

用户明确 hfq 口径为“标准后复权，基准 1.0”。补充如下，不改变 qfq 仍为信号/展示默认：

1. **hfq 定义**：对每只证券，取该证券最早可用交易日的 `adj_factor` 作为 `base_factor`（令其为 1.0）；`hfq_price = raw_price * adj_factor / base_factor`。不得把最新因子当 1.0（那是 qfq），也不得全市场共用一个基准日。
2. **qfq 不变**：`qfq_price = raw_price * adj_factor / latest_adj_factor`，基准为最新复权因子；信号、因子、默认图表仍用 qfq。
3. **因子缺失/异常硬失败**：`adj_factor` 缺失、为空、`<= 0`、NaN、inf 一律抛 `DataContractError`；禁止静默回退 qfq、禁止用 1.0 填充、禁止跳过该标的继续算。
4. **分钟复权单列**：分钟线使用该交易日日线的 `adj_factor` 复权，metadata 必须写 `adj=hfq`、`adj_base=<最早交易日>`、`adj_source=daily`；分钟 hfq 不与日线 hfq 拼成连续序列，缺因子报错。
5. **raw 边界不变**：涨跌停、成交额/成交量、绝对价位判断仍用 raw；缓存 meta 的 `base_factor` 变化即失效。
6. **可追溯**：图表与行情工具的 metadata 暴露 `adj`/`adj_base`/`adj_source`；`ChartSpec.data.adj` 增加 `hfq` 枚举（默认仍 `qfq`），旧 `none` 保留给涨跌停/成交类图。落地项 `plan-20261114-003`（D1）与 `plan-20261114-002`（图注）。

## 落地（plan-20261114-003，2026-11-16）

1. **因子历史区间**：`astock/data.py` 新增 `ADJ_HISTORY_START = "19900101"`；`_fetch_daily` 与 `load_adj_factors` 均从该日起取 `adj_factor`，基准不再由请求区间推断。
2. **基准与比值**：`_adj_base(factors, adj)` 中 qfq 取最新因子、hfq 取最早可用交易日因子（`iloc[0]`）；`ratio = adj_factor / base_factor`，hfq 首日价格等于 raw，之后按累计因子放大。
3. **硬失败**：新增 `_clean_adj_factors` 与 `astock.contracts.DataContractError`（`ContractError` 子类）；空表、缺列、缺失/NaN/inf/`<=0` 一律抛错，禁止 ffill/bfill、禁止填 1.0、禁止回退 qfq；`BarFrame.validate(require_adj=True)` 同步加严。
4. **metadata**：日线与分钟线结果 `attrs["adjustment"]` 统一为 `{adj, adj_base, base_factor, adj_source="daily"}`；`load_daily`/`load_minute` 在 `validate_bars` 之后重新挂回 attrs，供行情工具与图表图注消费。
5. **缓存失效**：`_cache_base_valid` 校验 meta 的 `base_factor`/`adj_base`——qfq 要求缓存末行因子等于 `base_factor`；hfq 要求覆盖基准日且该日因子等于 `base_factor`；旧版无 `adj_base` 的 hfq meta 直接失效重拉。
6. **raw 边界不变**：涨跌停、成交额/成交量与 `raw_*` 列保持原始价；分钟线只改价格列与涨跌幅。
7. **测试**：`tests/test_data_adj_caliber.py` 覆盖 hfq/qfq/raw 对照（逐行比值恒定）、基准不受请求区间影响（断言查询起点为 `19900101`）、因子异常（空/`=0`/负/NaN/inf）与缺行、分钟线 attrs、缓存 `base_factor` 篡改与旧 hfq meta 失效。

## 证据

- `astock/data.py`（`raw_*`、`base_factor`）
- `astock/backtest.py`（限价、ST、IPO）
- 修复记录 D7/D8/D12
- 用户 2026-11-14 裁决（D1）
- `astock/contracts.py`（`DataContractError`、`BarFrame.validate` 的 `adj_factor` 硬校验）
- `tests/test_data_adj_caliber.py`（hfq/qfq/raw 对照、因子异常、分钟 attrs、缓存基准失效）
- `tests/test_data_pit_events.py`（hfq 最早因子基准、空因子抛 `DataContractError`）
