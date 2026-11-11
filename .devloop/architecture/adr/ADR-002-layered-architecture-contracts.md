# ADR-002 六层分层与契约层（contracts.py）

- 状态：accepted
- 裁决：**accepted**（2026-11-10 架构契约评审门禁 plan-20261110-003，对照实现追认，无附加条件）
- 日期：2026-11-03
- 来源：评审 §4.1 / §4.2；修复记录 §6；2026-11-02 评审会话

## 背景

现有 26 个模块在功能上已经闭环，但边界是"隐式"的：

- UI 直接调数据/领域函数，推荐生成与历史读取存在两套路径；
- DataFrame 的列约定没有校验，供应商缺列时靠 `try/except` 静默吞掉（评审 D18 的 `ts_code` KeyError 就是实例）；
- 用例返回值不统一：有的返回 DataFrame，有的返回 dict，诊断信息散落在页面文案里。

评审给出的目标分层是"原地归位，不是重写"：交互 → 用例 → 领域 → 适配 → 契约 → 平台。其中**契约层是新增的**，也是当前唯一完全缺失的一层。

## 决策

1. 采用六层分层：交互层（Gradio Tabs）、用例层（recommend/track/research 编排）、领域层（纯函数、无 IO）、适配层（Tushare/LLM/cache）、契约层（新增）、平台层（ledger/scheduler）。
2. 新增 `astock/contracts.py`，定义并校验：
   - `BarFrame`：`ts_code/trade_date/open/high/low/close/vol/amount/adj_factor`；
   - `EventFrame`：`code/kind/pub_time/title/source/url`；
   - `Evidence`：证据必须带 `pub_time/title/url/source`；
   - `RecommendationRun`：`schema_version=2` 的推荐批次结构。
3. 用例层统一返回 `RunResult(run_id, status, payload, diagnostics)`；`status ∈ {success, stale, failed}`。
4. 所有跨模块的 DataFrame 进出必须过契约校验；校验失败抛出显式异常，不再静默降级。

## 实现回写（2026-11-10）

对照 `astock/contracts.py`（637 行）与 `astock/use_cases.py` 的实际实现。**只记录，不重写已通过评审的代码**：

1. 对外面冻结（`__all__`）：`ContractError`、`BAR_SCHEMA`、`EVENT_SCHEMA`、`EVIDENCE_REQUIRED_KEYS`、
   `RECOMMENDATION_REQUIRED_KEYS`、`BarFrame`、`EventFrame`、`Evidence`、`RecommendationRun`、
   `validate_bars` / `validate_events` / `validate_evidence` / `validate_evidence_list` / `validate_recommendation_run`。
2. 异常模型：`ContractError(contract, detail, *, missing, invalid)` 继承 `ValueError`，消息格式 `[{contract}] {detail}`，
   并结构化携带 `missing` / `invalid`。用例层据此把契约错误与未知异常分开（`test_use_cases.py::test_contract_error_is_failed`）。
3. **偏差 1**：`BarFrame.required` 由 `BAR_SCHEMA` 派生并排除 `adj_factor`，只有 `require_adj=True` 时才要求
   `adj_factor` 且非缺失。原文把 `adj_factor` 与 OHLC 并列，实现降为条件必填——这是正确收敛（原始价路径不读复权因子），
   冻结为 v1 行为，不修改代码。
4. `price_columns = ("open","high","low","close","vol","amount")`；`trade_date` 统一归一化为 `datetime64`；
   `allow_empty` 只放过"零行但列齐全"的哨兵，空表无列仍报错
   （`test_contracts.py::test_allow_empty_requires_columns_on_zero_row_frame`）。
5. **偏差 2**：`EventFrame` 列固定为 `(code, kind, pub_time, title, source, url)`，但市场级新闻允许 `code` 为空
   （`test_contracts.py::test_market_news_allows_empty_code`）。原文未区分个股事件与市场事件，此处补齐口径。
6. `RecommendationRun` 强制 `schema_version == 2`（`test_contracts.py::test_schema_version_must_be_2`），
   并要求 `data_as_of` / `tradable_from` 在场（`contracts.py:533` 循环校验）；校验不修改调用方 payload
   （`test_contracts.py::test_validate_does_not_mutate_caller_payload`）。
7. 用例层：`RunResult` 位于 `astock/use_cases.py:67`，`RUN_STATUSES` 仅 `success/stale/failed`；
   `stale`/`failed` 必须在 `diagnostics["message"]` 给可读诊断（`test_use_cases.py::test_stale_and_failed_require_message`）；
   推荐渲染唯一出口 `_render_recommendation(payload, status=, diagnostics=)`
   （`test_use_cases.py::test_generate_and_history_share_one_renderer`）。
8. 偏差小结：无阻塞偏差，仅上述 2 处口径收敛。两处已写入
   `docs/AStockAgent_interface_freeze_v1.md`，并由 `tests/test_architecture_gate.py` 守住不再漂移。

## 后果

- 正面：字段缺失从"静默空结果"变成"带列名的异常"；测试可在 30 秒覆盖全部接口；UI 与逻辑解耦后可独立替换。
- 负面：多一层校验成本；旧模块迁移期需要兼容包装；`contracts.py` 本身需要维护 schema 版本。
- 迁移顺序：先 `contracts.py` + `ledger.py` 双写一版，确认无回归后再切读 SQLite（评审 §4.6 P0.5）。

## 证据

- `astock/contracts.py`（已实现，637 行）、`astock/use_cases.py:67`
- `tests/test_contracts.py`（39 项）、`tests/test_use_cases.py`
- `deliverables/AStockAgent_代码与架构评审_v1.md` §4.1/§4.2
- `deliverables/AStockAgent_评审修复记录_v1.1.md` §6
- 评审门禁：plan-20261110-003（2026-11-10）
