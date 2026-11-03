# AStockAgent 架构说明 v1.3

> 更新：2026-11-03  
> 范围：当前代码的稳定边界、数据契约、运行架构与 ADR。  
> 不写每日推荐批次和收益数字，那些在「产品快照」「研究结论」里。

## 1. 分层

```text
交互层    Gradio 13 Tab（app.py 只做装配）
用例层    recommend / track / research 编排
领域层    indicators / backtest / factors / stats / pit
适配层    tushare_client / llm / parquet cache
契约层    recommendation JSON schema（v2）+ ledger tables
平台层    SQLite ledger / scheduler + jobs / logging
```

原则：领域层不依赖 UI；适配层不依赖推荐；LLM 只做解释；统计只从 `stats.py` 出。

## 2. 新增/关键模块

| 模块 | 职责 |
|---|---|
| `astock/ledger.py` | SQLite `runs/items/evidence/jobs/tracking`；推荐双写、跟踪读取、历史导入 |
| `astock/pit.py` | `index_weight` 历史成分 + `stock_basic(D)` + `bak_basic` + SW2014 L1 历史行业 |
| `astock/stats.py` | Newey-West、Student-t p、有效独立样本、BH |
| `astock/jobs.py` | 独立调度入口、`--once` 任务 |
| `astock/backtest.py` | 原始价限价、ST/IPO、跨窗口 `state` |
| `astock/recommend.py` | run_id、data_as_of/tradable、证据时间、严格 JSON、diagnostics |

## 3. 数据流

```text
Tushare
  -> tushare_client（trust_env=False）
  -> data.py（qfq + raw_* + parquet）
  -> indicators / factors / backtest

公告/研报/新闻/政策
  -> events.py
  -> evidence（pub_time/title/source/url）
  -> recommend.py（事件分 + 技术分 + LLM）
  -> data/recommendations/*.json + SQLite runs/items/evidence
  -> track.py（从 tradable_from 开盘）-> SQLite tracking

index_weight / stock_basic(D) / daily_basic / bak_basic / index_member(SW2014)
  -> pit.py 快照
  -> factors.py（PIT 指数模式）
```

## 4. 数据契约

推荐 JSON（`schema_version=2`）：

```text
run_id, generated_at, data_as_of, tradable_from,
source, params, diagnostics, market, ai, table, evidence
```

SQLite：

```sql
runs(run_id PK, schema_version, kind, data_as_of, tradable_from,
     generated_at, source, status, error, params_json, diagnostics_json,
     json_path, csv_path)
items(run_id, rank, code, name, industry, composite, technical, event,
      action, reason, PK(run_id, rank))
evidence(run_id, code, kind, pub_time, title, url, source, content,
         PK(run_id, code, kind, pub_time, title))
jobs(job_key PK, trade_date, slot, status, attempts, started_at,
     finished_at, error, late_by_min, run_id)
tracking(run_id, code, horizon, entry_date, exit_date, ret, excess,
         computed_at, PK(run_id, code, horizon))
```

规则：

- `data_as_of` 是数据截止；`tradable_from` 是可成交起点。
- 证据 `pub_time <= generated_at`。
- 推荐 JSON 必须 `allow_nan=False`。
- 跟踪 entry bar 缺失返回空，不后移。

## 5. 调度与运行

- 单实例锁：`scheduler.lock`（msvcrt/fcntl）。
- 幂等键：`(last_completed_trade_date, slot)`，`slot ∈ {premarket, close, manual}`。
- 墙钟循环 + 16:40–23:00 补偿；`late_by_min` 落库。
- `StaleDataError` 不发布推荐，不覆盖 `latest.json`。
- `start_scheduler.bat` 调 `python -m astock.jobs`。

## 6. ADR

| ADR | 决策 | 理由 | 代价 |
|---|---|---|---|
| ADR-01 | 本地 Gradio 单用户 | 快速迭代、无前端成本 | 多用户/鉴权弱 |
| ADR-02 | Parquet 行情 + SQLite ledger | 简单、可审计、可迁移 | 未上分布式 |
| ADR-03 | LLM 只解释 | 可复现、避免幻觉进回测 | 解释价值受证据限制 |
| ADR-04 | TushareClient 强制 trust_env=False | 绕过系统代理 502 | 依赖代理地址 |
| ADR-05 | PIT 优先于扩因子 | 避免幸存者偏差 | 数据成本/版本校准 |
| ADR-06 | 每次实验只从 stats.py 出 p 值 | 防止各模块自算口径 | 旧模块需迁移 |

## 7. 未完成/风险

- 推荐 UI 的“生成”与“历史读取”尚未统一到同一 service。
- SW2014→SW2021 行业边界未校准。
- 事件分权重人工设定，未做标注校准。
- 全库 mypy/覆盖率未开；CI 只检查新模块类型。
- 多用户/远程、FastAPI、任务队列均未做，也不建议现在做。
