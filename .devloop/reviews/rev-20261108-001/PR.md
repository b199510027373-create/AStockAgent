# PR 评审：评审：契约层 contracts.py：DataFrame schema 校验

> 自动生成于 2026-11-08T16:33:29.848Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261108-001`
- 计划项：`plan-20261103-001`
- 分支：`feat/contracts` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：未关联
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 0 / P2 1

## 本轮结论

第 3 轮（scope=full；增量 diff 885c6d8..455f419：app.py +15、astock/contracts.py +103/−43、tests/test_contracts.py +96，另 docs/ 与 requirements-dev.txt 不计入代码）。

一、前两轮 9 条全部复核通过（逐条对码）
- iss-mw7wair1-e5fb1d（P1 引用块 KeyError）：app.py:113-121 改为 `raw_time = item.get("pub_time") or item.get("date")`，坏时间/NaT 走 try+`pd.isna` 降级为空串，kind/title 用 `.get`；新增 CitationBlockTests 4 例（pub_time 渲染、legacy date 兼容、缺键/坏日期不抛、空入参）。
- iss-mw7waptn-326bd5（P2 schema 双份真相）：contracts.py:199-209 BAR_SCHEMA 改为列元组且去掉 dtype 标注，:230-232 `required = tuple(c for c in BAR_SCHEMA if c != "adj_factor")`，:291-298 EVENT_SCHEMA、:315 `required = EVENT_SCHEMA`；新增 test_schema_is_single_source_of_truth 断言两边同源且 adj_factor 仍在列集合内。
- iss-mw7wawf3-843f9e（P2 时区 TypeError）：contracts.py:156-170 新增 `_naive_timestamp`（解析失败→ContractError，带时区 `tz_localize(None)`），:532/:575-582 用于 generated_at 与每条 evidence，比较处再包 `except TypeError` 转 ContractError；新增 tz 正例/反例两个用例（+08:00 过去时间通过且原表示保留、+08:00 未来时间报 DATA-02）。
- 更早 6 条（recommend 吞 ContractError、NaN 放过、AI-01、日期漂移、allow_empty、DATA-02 缺失）本轮重查仍在位：contracts.py:90-120 / :253-255 / :522-531 / :575-600，recommend.py:116/140/361、:749-754，events.py:111-133 与各 fetch_* 的 `except ContractError: raise`。
- 全仓无残留读取 evidence `["date"]` 的消费方（grep 12 处命中，app.py:115、contracts.py:380、events.py:763 均为显式兼容分支）。

二、验收标准 4 条全部满足：四契约有实现+docstring（contracts.py:218 BarFrame / :302 EventFrame / :359 Evidence / :480 RecommendationRun）、data/events/recommend 关键入口过校验、缺列与类型错误均抛 ContractError（测试断言 `assertRaises(ContractError)` 而非空结果）、测试覆盖远超 4 类（缺失列、坏日期、NaN code、非数值 close、require_adj 缺列/NaN、schema_version、越界证据、时区）。

三、本轮新开 1 条 P2：iss-mw81x690-cbec5c（backfill.py:120-130 写出的 recommend_*.json 缺 schema_version/run_id/tradable_from，仍会被 ledger.py:346-353 sync_json_dir 以 legacy run_id 收进 SQLite）。属交接文档 §8 已声明的后续项，不在本计划项验收范围内，不阻塞。

四、说明：本会话无命令执行能力，未运行 pytest，未独立复核交接文档所称「97 passed」；上述结论基于对 8c5e0e9/455f419 工作树与测试源码的静态核对。结论：契约层改造完成且质量达标，可以合并；backfill 迁移与 track 接入作为后续计划项跟进。

## 本轮问题

### P2 · backfill 写出的 recommend_*.json 绕过 RecommendationRun 校验，仍会被 ledger.sync_json_dir 收进 SQLite（schema_version/tradable_from 为 NULL）

- 位置：`astock/backfill.py:120-130`
- 状态：未闭环
- 置信度：0.8

**证据**

交接文档 §8 自述 backfill 未接入契约，但没说到下游后果：backfill.py:120-130 写出的 payload 只有 generated_at/as_of/source/market/ai/table/evidence，没有 schema_version、run_id、data_as_of、tradable_from、diagnostics；而 ledger.py:346-353 `sync_json_dir` 对 `recommend_*.json` 一视同仁 glob，缺 run_id 时补 `legacy_{stem}` 后直接 `save_run`，save_run(:141-144) 对缺失字段只写 NULL（`payload.get("schema_version")`、`payload.get("data_as_of") or payload.get("as_of")`）。于是 SQLite 里会混入 schema_version=NULL、tradable_from=NULL 的 run，DATA-01「推荐 JSON 必须 schema_version=2」在 ledger 侧无法自证；track.py:241-253 的 JSON 回退路径也要靠 `as_of` 兜底才不跳过。这类 payload 目前只是碰巧被下游的 `.get()` 容错吞下，不是契约把关。

**建议**

按交接文档的后续计划单独迁移时，至少做两件事：(1) backfill 组装完 payload 后先 `validate_recommendation_run(payload, source="backfill_technical")`，补齐 schema_version=2/run_id=f"backfill_{date:%Y%m%d}"/data_as_of/tradable_from（用 `next_open_date` 推导）/diagnostics；(2) 给 `ledger.sync_json_dir` 加一条断言或测试：缺 schema_version 的 payload 不得静默入库（或显式标注 legacy 并统计）。若本轮不迁移，建议在 ledger.sync_json_dir 里跳过无 schema_version 的文件，避免污染「运行事实以 ledger 为准」。这不阻塞本计划项，可挂到后续 backlog。

## 变更文件

- `app.py`
- `astock/contracts.py`
- `docs/AStockAgent_contracts_PR_handoff_2026-11-09.md`
- `docs/AStockAgent_contracts_delivery_2026-11-09.md`
- `requirements-dev.txt`
- `tests/test_contracts.py`

## 交给开发会话

1. 在分支 `feat/contracts` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
