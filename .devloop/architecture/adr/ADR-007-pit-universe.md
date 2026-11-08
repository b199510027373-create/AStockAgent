# ADR-007 PIT 股票池：历史成分 + 退市 + 行业版本

- 状态：accepted
- 日期：2026-11-03
- 来源：2026-10-27 构建会话 PIT 扩展；PIT 接口自查；研究结论

## 背景

用"今天的市值前 N / 今天的指数成分"回看历史，会系统性高估收益：活到今天的公司天然是赢家。评审复核发现，"PIT 后动量 IC 从 −0.0207 掉到 −0.0078"方向可信，说明幸存者偏差确实是重要来源。

原有接口权限曾被误判为不足；实际核验后确认，现有 Tushare 权限已能拼出 2015 年以来的 PIT 数据，不需要外部采购。

## 决策

1. 指数池成分用 `index_weight` 历史快照（沪深300/中证500/中证1000 等），不取当前成员。
2. 用 `stock_basic(list_status="L"/"D")` 过滤：`list_date <= as_of < delist_date`。
3. 历史市值用 `daily_basic`（2015+）；历史名称/ST 用 `bak_basic` + `namechange`；`bak_basic` 只补名称/ST，不作为行业主来源。
4. 行业用申万 L1 历史成员 `index_member`：
   - `date >= 2021-12-13` 用 SW2021；
   - `date < 2021-12-13` 用 SW2014；
   - 缺失时用 `stock_basic` 当前行业兜底；
   - 快照落 `industry_version` 字段用于审计。
5. 快照落 Parquet/SQLite 缓存，按 `as_of` 查询，不每次重拉。
6. 明确限制：没有官方 SW2014→SW2021 crosswalk，跨版本行业名称不完全可比；2016 年前的行业完整性略低。

## 后果

- 正面：消除幸存者偏差；因子 IC 与回测口径一致；数据可离线复现。
- 负面：数据成本与缓存体积上升；跨版本行业边界需要单独校准（见计划项）；早期年份覆盖有限。
- 现状：`pit.py` 已接入因子 IC；1 年沪深300 快照 20 期 / 331 只 / 18 个 IC 日；结论仍是"无 BH 后显著因子"。

## 证据

- `astock/pit.py`
- `docs/AStockAgent_PIT_data_interfaces.md`
- `docs/AStockAgent_research_conclusions_v1.3.md` §2
