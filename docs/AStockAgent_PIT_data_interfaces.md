# AStockAgent PIT 数据接口自查

日期：2026-11-02  
目的：确认 `daily_basic`、退市名单、历史行业分类能否支撑真正的 point-in-time（PIT）股票池。  
结论：**不依赖外部采购，现有 Tushare 权限已能拼出 PIT 所需的主要数据。**

## 1. `daily_basic`：可用，历史覆盖足够

实测按 `trade_date` 拉全市场：

| 日期 | 行数 |
|---|---:|
| 2026-10-30 | 5,571 |
| 2025-12-31 | 5,458 |
| 2022-12-30 | 5,061 |
| 2015-12-31 | 2,545 |

可用字段：

```text
ts_code, trade_date, total_mv, circ_mv, pe_ttm, pb, ps_ttm,
dv_ratio, turnover_rate, volume_ratio
```

请求：

```python
client.query(
    "daily_basic",
    trade_date="20261030",
    fields="ts_code,trade_date,total_mv,circ_mv,pe_ttm,pb,ps_ttm,dv_ratio,turnover_rate,volume_ratio",
)
```

用途：

- PIT 市值前 N；
- 历史市值中性化；
- 估值因子（EP/BP/股息率）；
- 流动性/换手率因子。

注意：`total_mv` 单位是万元；北交所代码也会返回，按需过滤 `.SH/.SZ`。

## 2. `index_weight`：可用，历史成分可回溯

实测：

| 指数 | 2026-10-30 行数 | 权重合计 |
|---|---:|---:|
| `000300.SH` 沪深300 | 300 | 99.99 |
| `000905.SH` 中证500 | 500 | 100.00 |
| `000852.SH` 中证1000 | 1000 | 99.99 |
| `399006.SZ` 创业板指 | 100 | 100.00 |
| `000016.SH` 上证50 | 50 | 100.00 |

历史覆盖：`000300.SH` 从 **2005-12-30** 到 **2026-10-30** 可逐月取；`20260101–20261030` 区间返回 **17 个快照 / 5,100 行**。

请求：

```python
client.query(
    "index_weight",
    index_code="000300.SH",
    trade_date="20261030",
    fields="index_code,con_code,trade_date,weight",
)
```

区间：

```python
client.query(
    "index_weight",
    index_code="000300.SH",
    start_date="20260101",
    end_date="20261030",
    fields="index_code,con_code,trade_date,weight",
)
```

注意：

- `weight` 是**百分比**，合计约 100，不是 0–1 小数。
- 返回的是**指数成分快照**，不是全市场；做全市场 PIT 仍需叠加 `daily_basic` + `stock_basic`。
- 之前评审里“`index_weight` 返回 0”的结论已过期。

## 3. 退市名单：`stock_basic(list_status="D")`

实测：

```python
client.query(
    "stock_basic",
    exchange="",
    list_status="D",
    fields="ts_code,name,industry,market,list_date,delist_date",
)
```

结果：**343 只退市股票**，字段含 `delist_date`。当前上市 `list_status="L"` 为 5,577 只，`P` 为 0。

用途：

- `security_master` 的 `list_date / delist_date`；
- PIT 过滤：`list_date <= as_of < delist_date`；
- 识别“当时在指数里、后来退市”的股票。

## 4. 历史名称 / ST：`namechange` + `bak_basic`

### 4.1 `namechange`

```python
client.query_all(
    "namechange",
    fields="ts_code,name,start_date,end_date,ann_date,change_reason",
    limit=5000,
    max_rows=20000,
)
```

实测：**7,312 条 / 4,707 只**，最早 `start_date=20100629`，最晚 `20261029`。

用途：

- 还原历史证券名称；
- 根据 `*ST` / `ST` 名称区间识别风险警示期；
- 补 `bak_basic` 缺失的早期名称。

注意：有重复记录（同一名称多条），需要按 `ts_code + start_date + name` 去重。

### 4.2 `bak_basic`：按日的历史名称 + 行业

```python
client.query(
    "bak_basic",
    trade_date="20261030",
    fields="trade_date,ts_code,name,industry,area,pe,float_share,total_share",
)
```

实测：

| 日期 | 行数 |
|---|---:|
| 2016-11-30 | 3,014 |
| 2016-12-30 | 3,071 |
| 2022-12-30 | 4,909 |
| 2026-10-30 | 5,579 |

它返回的是**当日快照**，`industry` 和 `name` 会随日期变化：

- 2016-12-30 与 2026-10-30 的共同股票中，**791 只行业不同**、**693 只名称不同**；
- 例如 `000010.SZ` 从“美丽生态 / 建筑施工”变为“*ST美丽 / 建筑工程”。

用途：

- 历史行业分类（2016-11 起）；
- 历史 ST/名称（2016-11 起）；
- 市值/股本辅助校验。

限制：2016-01、2016-06 返回 0；**实际起点约 2016-11**。

## 5. 历史行业分类：`index_member` 可按申万行业回溯

`index_classify` 实测：

- 无参数：359 行；
- `src="SW2021"`：511 行；
- 只传 `level="L"`：0 行（参数组合不对）。

`index_member` 实测（`801010.SI` 农林牧渔）：

| `is_new` | 行数 | `out_date` 非空 |
|---|---:|---:|
| `Y` | 104 | 0 |
| `N` | 80 | 80 |
| 不传 | 184 | 80 |

所以：

```python
# 当前成分
client.query("index_member", index_code="801010.SI", is_new="Y", ...)

# 历史剔除成分
client.query("index_member", index_code="801010.SI", is_new="N", ...)

# 当前 + 历史（推荐）
client.query("index_member", index_code="801010.SI", ...)
```

`index_member_all` 实测：

- 5,922 行，但全部 `is_new=Y`、`out_date` 全空；
- 2000 行/页，offset 0/2000/4000 可取全；
- **它只是当前申万成员，不是历史成员**。

因此历史行业分类建议：

1. 用 `index_classify(src="SW2021")` 取行业指数列表；
2. 对每个行业指数调用 `index_member`（不传 `is_new`），得到 `in_date/out_date`；
3. 对 2016-11 之后的日期，用 `bak_basic.name` 补历史名称/ST；行业仍以申万成员为准。

### 5.1 已接入的 SW2014 / SW2021 L1 历史成员

`astock/pit.py` 已用：

```python
client.query("index_classify", src="SW2014"/"SW2021", fields=...)
# 过滤 level == "L1"
client.query("index_member", index_code=..., fields="con_code,in_date,out_date,...")
```

实测：

- SW2014：28 个 L1，**7,558 条**历史成员；
- SW2021：31 个 L1，**7,740 条**历史成员；
- 切换日：**2021-12-13**；
- 2015 年沪深300 快照：13 个日期、341 只代码、3,900 行；
- 2021-11～2022-01 快照：SW2014 900 行、SW2021 1,200 行；补了 2021-12-13 切换日快照；
- 缓存：`cache/pit/sw2014_l1_members.parquet`、`sw2021_l1_members.parquet`。

优先级：

1. `date >= 2021-12-13`：SW2021 L1；
2. `date < 2021-12-13`：SW2014 L1；
3. 行业缺失时用 `stock_basic` 当前行业兜底；
4. `bak_basic` 只补历史名称/ST，不再作为行业主来源。

剩余限制：没有官方 crosswalk，旧 `801020` 采掘拆分为煤炭/石油石化等，跨版本行业名称不完全可比；快照里保留 `industry_version`（SW2014/SW2021/BAK）用于审计。

## 6. 能拼出的 PIT 数据模型

```text
security_master(code, name, list_date, delist_date, board)
index_membership(date, index_code, code, weight)
industry_membership(industry_code, industry_name, code, in_date, out_date)
name_history(code, name, start_date, end_date, is_st)
daily_basic(date, code, total_mv, pe_ttm, pb, dv_ratio, ...)
```

构建顺序：

1. 每月末取 `index_weight` 成分；
2. 用 `stock_basic L/D` 过滤上市/退市；
3. 用 `daily_basic` 取当日市值；
4. 用 `index_member`（2021-12-13 前 SW2014、之后 SW2021）取行业；`bak_basic.name` 只补名称/ST；
5. 用 `bak_basic.name` / `namechange` 取 ST 状态；
6. 落 SQLite/Parquet，按 `as_of` 查询。

## 7. 结论

| 需求 | 数据源 | 状态 |
|---|---|---|
| 历史市值 | `daily_basic` | ✅ 2015+ |
| 指数成分 | `index_weight` | ✅ 2005+ |
| 退市名单 | `stock_basic(D)` | ✅ 343 只 |
| 历史名称/ST | `namechange` + `bak_basic` | ✅ 2010+ / 2016-11+ |
| 历史行业 | `index_member` SW2014/SW2021 | ✅ 2021-12-13 前 SW2014、之后 SW2021 |
| 全市场股票池 | `stock_basic` + `daily_basic` | ✅ |

**不需要再向外部采购数据**，现有权限足够做 3–5 年 PIT；更早年份（2010–2016）的行业分类需要用 `index_member` 拼接，完整性略低。

## 8. 注意事项

- `index_weight` 的 `weight` 是百分比，不是小数。
- `bak_basic` 起点约 2016-11，不能用于 2015 年因子。
- `index_member_all` 只有当前成员；历史回溯必须逐行业调用 `index_member`。
- `namechange` 有重复记录，需要去重；ST 判断建议同时看 `bak_basic.name` 和 `namechange`。
- 这些接口都有频率限制，PIT 快照应落库/缓存，不要每次研究都重拉。
- 本文只做接口核验，没有改业务代码。
