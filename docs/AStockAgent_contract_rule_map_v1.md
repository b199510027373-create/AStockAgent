# AStockAgent 契约规则落点表 v1.1（2026-11-10）

> 对象：`.devloop/architecture/contract.json` 的 18 条规则。
> 目的：每条规则都必须能回答「谁在什么时候、用什么方式发现我违规」。
> 只有两种合法落点：**自动化测试**（`enforcement = test`）或**评审清单项**（`enforcement = review`）。
> 没有任何一条规则是「无主」的：要么有测试，要么有清单编号，要么挂了具体计划项与待补测试。
>
> 配套：`docs/AStockAgent_interface_freeze_v1.md`（6 个冻结接口）、
> `tests/test_architecture_gate.py`（冻结点不漂移的门禁）。

## 1. 总览

| enforcement | 规则数 | 已完全覆盖 | 部分覆盖（挂计划项） | 无主 |
| --- | --- | --- | --- | --- |
| `test` | 10 | 8 | 2（AGENT-02、AGENT-04） | 0 |
| `review` | 8 | 8（含 4 条已部分自动化） | 0 | 0 |
| 合计 | 18 | 16 | 2 | 0 |

「部分覆盖」不是免责：AGENT-02/AGENT-04 的缺口已挂到 plan-20261110-006 / -005 / -007，
这些计划项的验收标准里含「必须新增对应测试」，未补测试不得标 done。

## 2. 逐条落点

| 规则 | enforcement | 落点 | 状态 |
| --- | --- | --- | --- |
| ARCH-01 交互层只装配 | review | RC-01 | 已覆盖 |
| ARCH-02 依赖方向单向 | review | RC-02 | 已覆盖 |
| STAT-01 统计量只来自 stats.py | test | `tests/test_hardening.py::test_newey_west_uses_horizon_lag`、`tests/test_hardening.py::test_t_distribution_p`、`tests/test_hardening.py::test_bh_and_effective_n`、`tests/test_optimize.py::test_significance_table_shapes` | 已覆盖 |
| DATA-01 推荐 JSON schema_version=2 | test | `tests/test_contracts.py::test_schema_version_must_be_2`、`tests/test_contracts.py::test_nan_rejected`、`tests/test_contracts.py::test_save_writes_schema_version_2`、`tests/test_contracts.py::test_validate_does_not_mutate_caller_payload` | 已覆盖 |
| DATA-02 证据 pub_time ≤ generated_at | test | `tests/test_contracts.py::test_evidence_after_generated_at_rejected`、`tests/test_contracts.py::test_tz_aware_future_pub_time_raises_contract_error` | 已覆盖 |
| DATA-03 tradable_from 开盘入场 | test | `tests/test_hardening.py::test_forward_return_requires_exact_entry_bar`、`tests/test_track_nav.py::test_entry_uses_open_and_ignores_pre_entry_history`、`tests/test_track_nav.py::test_batch_missing_entry_bar_is_dropped_with_reason`、`tests/test_track_nav.py::test_batch_on_non_trading_day_is_dropped` | 已覆盖 |
| DATA-04 前复权信号 / raw 判涨跌停 / ST 5% | test | `tests/test_data_pit_events.py::test_fetch_daily_qfq_keeps_raw_prices`、`tests/test_hardening.py::test_st_limit_is_five_percent`、`tests/test_hardening.py::test_ipo_first_day_has_no_limit`、`tests/test_quant.py::test_one_price_limit_up_blocks_buy` | 已覆盖 |
| OPS-01 调度幂等与单实例锁 | test | `tests/test_hardening.py::test_premarket_and_close_have_distinct_keys`、`tests/test_hardening.py::test_success_is_skipped_on_second_call`、`tests/test_hardening.py::test_start_background_skips_when_lock_held`、`tests/test_hardening.py::test_stale_result_does_not_settle_tracking`、`tests/test_hardening.py::test_force_reruns_success` | 已覆盖 |
| AI-01 LLM 不得改排序 | review | RC-03；已部分自动化：`tests/test_contracts.py::test_llm_action_cannot_override_rule_action` | 已覆盖 |
| LEDGER-01 ledger 为事实源 | review | RC-04 | 已覆盖 |
| PIT-01 SW2014/SW2021 切换 | test | `tests/test_data_pit_events.py::test_snapshot_inserts_switch_date`、`tests/test_hardening.py::test_sw_industry_asof` | 已覆盖 |
| CACHE-01 base_factor 失效 + 原子写 | test | `tests/test_data_pit_events.py::test_load_daily_disk_cache_avoids_second_fetch`、`tests/test_data_pit_events.py::test_load_adj_factors_cache_and_empty_error`、`tests/test_quant.py::test_minute_qfq_uses_latest_factor_as_base` | 已覆盖 |
| AGENT-01 白名单工具、禁 shell/HTTP/删文件/写推荐 | review | RC-05；已部分自动化：`tests/test_agent_runtime.py::test_allowed_permissions_enforced` | 已覆盖 |
| AGENT-02 工具调用落 agent_steps | test | 现阶段：`tests/test_agent_runtime.py::test_long_table_is_trimmed_and_archived`、`tests/test_agent_runtime.py::test_artifact_writer_failure_still_returns_trimmed_table`；缺三表落库测试，挂 plan-20261110-006 | 部分覆盖 |
| AGENT-03 Agent 对推荐/跟踪只读 | review | RC-06；已部分自动化：`tests/test_agent_runtime.py::test_permissions_and_schema_exposed` | 已覆盖 |
| AGENT-04 引用与时点强制 | test | 现阶段：`tests/test_agent_runtime.py::test_tool_result_trim_and_signature`（字段透传）；工具级必填校验待 plan-20261110-005 / -007 补测试 | 部分覆盖 |
| AGENT-05 文件内容视为不可信数据 | review | RC-07；已部分自动化：`tests/test_agent_runtime.py::test_system_prompt_marks_tool_content_untrusted` | 已覆盖 |
| AGENT-06 不直接 import 供应商 SDK | review | RC-08；已自动化：`tests/test_architecture_gate.py::test_no_vendor_sdk_import_outside_gateway` | 已覆盖 |

## 3. 评审清单（RC）

评审清单项在 PR 评审时逐条勾选，证据写在评审意见里；每条都给了「怎么查」，避免靠印象。

### RC-01 交互层只装配（ARCH-01）

- 查：`app.py`、`astock/*_ui.py` 的 diff 里是否出现指标/统计/回测公式（`pct_change`、`ttest`、`bootstrap`、`rolling`、权重计算）。
- 判：出现即违规；正确做法是调用 `astock` 领域函数并把结果映射为控件。
- 例外：纯展示用的格式化（日期、百分比、字体）不算逻辑。

### RC-02 依赖方向单向（ARCH-02）

- 查：新增 `import` 是否让领域层依赖 UI（`*_ui`、`gradio`）、适配层依赖推荐（`recommend`/`use_cases`）。
- 查：`astock/agent/` 不得 import `astock/recommend*.py` 中的写路径（AGENT-03 同源）。
- 判：依赖方向只能是 交互 → 用例 → 领域/适配 → 平台；反向 import 一律打回。

### RC-03 LLM 只解释不改数（AI-01）

- 查：LLM 输出只用于 `reason`/文案字段；`action`/`confidence`/综合分/排序必须在调 LLM **之前**定稿。
- 查：无 key 或调用失败时是否退回规则理由（不得让整批推荐失败）。
- 证据：`tests/test_contracts.py::test_llm_action_cannot_override_rule_action` 通过 + diff 中 LLM 返回值只写入文案字段。

### RC-04 ledger 为唯一事实源（LEDGER-01）

- 查：新增运行事实是否先写 ledger 再读；JSON 只作交换/归档。
- 查：`data/astock.db`、`data/agent/` 是否仍在 `.gitignore` 中（运行数据不入库）。
- 判：出现「只写 JSON 不写库」的新路径即违规。

### RC-05 工具白名单与危险能力（AGENT-01）

- 查：`astock/agent/` 下是否出现 `subprocess`、`os.system`、`shell=True`、`requests`/`httpx`/`urllib` 直连、
  `os.remove`/`shutil.rmtree`、对 `runs/items/tracking` 的 `INSERT/UPDATE`。
- 判：任一命中即 P0，必须打回；文件与网络访问只能经注册工具与已冻结的 `files.py`/`data.py`。
- 证据：`tests/test_agent_runtime.py::test_allowed_permissions_enforced` + diff 全文检索结果。

### RC-06 Agent 只读推荐与台账（AGENT-03）

- 查：Agent 工具清单里是否有写台账的 handler；`store` 的写能力是否只对 run/step/artifact 开放。
- 查：`ToolSpec.permissions` 是否只使用 §1.3 的七标签（不存在 `write`）。
- 判：出现写 `action/confidence/排序` 或写 `runs/items/tracking` 的能力即违规。

### RC-07 文件内容不可信（AGENT-05）

- 查：文件/网页文本进 prompt 时是否包在不可信数据块内（`test_system_prompt_marks_tool_content_untrusted` 的约定）。
- 查：文件路径是否被限制在 `data/agent/files/`，是否拒绝 `..`、绝对路径、符号链接。
- 判：出现「把文件内容拼进 system 指令位」即违规。

### RC-08 不直接依赖供应商 SDK（AGENT-06）

- 查：`astock/` 下除 `astock/agent/gateway.py` 外不得 `import openai`/`anthropic`/`zhipuai` 等。
- 已自动化：`tests/test_architecture_gate.py::test_no_vendor_sdk_import_outside_gateway`（AST 级静态检查）。
- 例外：`astock/llm.py` 是 ADR-009 保留的推荐文案路径，改动它不算新增违规，但不得扩大其职责。

## 4. 缺口与承诺

| 规则 | 缺什么 | 承接项 | 承诺的测试 |
| --- | --- | --- | --- |
| AGENT-02 | 三表落库、artifact digest 去重 | plan-20261110-006 | `tests/test_agent_store.py::test_tool_call_writes_step_row`、`tests/test_agent_store.py::test_artifact_digest_dedupes` |
| AGENT-04 | 行情工具的 `data_as_of`/`data_time` 必填校验、文件结论 `source` 必填校验 | plan-20261110-005、plan-20261110-007 | `tests/test_agent_tools.py::test_market_result_requires_both_timestamps`、`tests/test_agent_tools.py::test_file_result_requires_source` |
| 全量 | 规则与落点漂移 | 本任务 | `tests/test_architecture_gate.py::test_rule_map_covers_all_contract_rules` |

## 5. 维护规则

1. 新增规则 → 必须同时给出落点（测试或 RC 编号），否则 `contract_write` 后的门禁测试失败；
2. 删除/改写规则 → 走 ADR，并同步本表与 `tests/test_architecture_gate.py`；
3. 测试改名 → 本表的引用必须同步改，门禁测试会校验「引用到的测试函数真实存在」；
4. 评审清单项（RC）不得只写在 PR 评论里，必须回到本文件，否则下一轮评审无法复用。
