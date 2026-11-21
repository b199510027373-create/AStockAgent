# Agent 会话真实模型 E2E（v1）

状态：已落地，2026-11-20。对应 `plan-20261119-002`；实现位于 `tests/test_agent_live_e2e.py`。

## 1. 目标与边界

用真实 DeepSeek（或任意 OpenAI-compatible 网关）跑通三轮端到端会话，验证：

1. 流式分片可见，生成中停止后已输出分片不被完整回答覆盖；
2. 至少一次工具调用出现在时间线，且工具结果正文对用户可见；
3. 引用可定位到上传文件的具体页/表，内嵌图可渲染；
4. 停止后下一问不受中断标记污染；
5. 记录首字延迟（TTFT）、总耗时、token 用量与可选费用估算；
6. 失败时有明确降级提示；无 key 时自动 skip，且不把未验证说成已验证。

边界：`ChatModel.complete()` 按 ADR-014 是**非流式**冻结接口；这里的“流式”指 UI 侧按标点增量渲染（`iter_answer_chunks`），不是模型侧 token 流。真实 token 流属于二期，不在本任务范围。

## 2. 开关与密钥

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `AGENT_LIVE_E2E` | 是 | 必须显式设为 `1`；缺失时整组 live 用例 skip |
| `AGENT_PROVIDER` | 否 | `deepseek`（默认）或 `openai_compatible` |
| `AGENT_MODEL` | 视网关 | 兼容网关必填 |
| `AGENT_BASE_URL` | 视网关 | 兼容网关必填；DeepSeek 默认官方地址 |
| `AGENT_API_KEY` | 否 | 首选；缺省回退 `DEEPSEEK_API_KEY`（deepseek）或 `OPENAI_API_KEY`（兼容网关） |
| `AGENT_PRICE_INPUT_PER_M` | 否 | 输入价格（每百万 token），配合下一项生成费用估算 |
| `AGENT_PRICE_OUTPUT_PER_M` | 否 | 输出价格（每百万 token） |
| `AGENT_PRICE_CURRENCY` | 否 | 费用币种标签，仅用于报告展示 |
| `AGENT_LIVE_E2E_WORKDIR` | 否 | 报告与索引的落盘根目录，默认 `.tmp-tests/live-e2e` |
| `AGENT_TIMEOUT_SECONDS` | 否 | 单轮运行时上限（秒），默认 120；会被透传给 `AgentRuntime` |
| `AGENT_MAX_STEPS` | 否 | 单轮最大工具步数，默认 8 |
| `AGENT_TOKEN_BUDGET` | 否 | 单轮 token 预算，默认 64000 |

`AGENT_PRICE_*` 写错（非数字）时不会静默当作“未配置”：报告里 `cost` 变成带 `error` 字段的结构并说明哪个变量非法。`AGENT_*` 上限类变量同时会覆盖本测试内三层（网关/运行时/控制器绑定）的默认值。

密钥只从环境变量读取：`.env` 由 `astock/config.py` 的 `load_dotenv` 载入，文件本身在 `.gitignore` 中；测试与报告都不打印密钥，只记录 provider/model/protocol。

## 3. 运行方式

```powershell
# 默认（CI 或本地无 key）：live 用例 skip，其余语义用例照常运行
.venv\Scripts\python.exe -m pytest tests\test_agent_live_e2e.py -q

# 真实模型三轮 E2E（需 key）
$env:AGENT_LIVE_E2E = "1"
.venv\Scripts\python.exe -m pytest tests\test_agent_live_e2e.py -m live -q -s
```

用例划分：

- 语义用例（默认运行）：`test_live_config_requires_explicit_opt_in`、`test_live_config_skips_without_api_key`、`test_live_config_skips_incomplete_compatible_gateway`、`test_model_failure_surfaces_clear_degradation`，以及报告/费用口径的纯函数用例（`test_cost_estimate_uses_price_env`、`test_cost_without_price_env_is_unknown`、`test_cost_invalid_price_reports_error_instead_of_none`、`test_round_report_records_timing_usage_and_cost`）。
- 真实用例（`-m live`）：`test_round1_real_answer_streams_with_tool_step`、`test_round2_stop_keeps_partial_and_citations_resolve`、`test_round3_next_question_clean_and_chart_renders`。

产物写入 `.tmp-tests/live-e2e/<时间戳>/`（gitignored）：`report.json`、`files/`（上传件索引）、`index.db`、`session.json`。`report.json` 含每轮 TTFT、耗时、stop_reason、步骤、token 用量、费用估算、引用与图表摘要；`probe_usage` 单列探测调用的 token，`totals` 的口径只含三轮对话。

## 4. Skip 语义

- 未设 `AGENT_LIVE_E2E=1`，或没有可用 key，或兼容网关缺 `AGENT_BASE_URL`/`AGENT_MODEL` 时，live 用例 `pytest.skip`，消息明确写“未运行，不能视为已验证”。
- R2 若模型完整回答不足一个分片（`CHUNK_CHARS=40`），停止后“分片短于完整回答”无法验证，该用例 `pytest.skip` 并打印完整回答，而不是以断言失败报红。
- skip 不是通过：CI 日志里 live 用例永远是 skipped，真实证据只能来自显式开启的运行结果。

## 5. 2026-11-21 实测记录

环境：DeepSeek 官方接口，`deepseek-chat`，probe `function_calling=true`、`degraded=false`、`probe_passed=true`。三轮对话共 6 次模型调用、21,622 tokens（prompt 20,506 / completion 1,116）；probe 探测另有 1 次调用、343 tokens，单列在报告的 `probe_usage` 中（不计入 `totals`）。未配置价格，`cost=null`。报告：`.tmp-tests/live-e2e/20261121-031843/report.json`。

| 轮次 | 目标 | TTFT | 总耗时 | 回答字符 | 结果 |
| --- | --- | --- | --- | --- | --- |
| R1 | quote 查询 600519.SH | 6,364.3 ms | 6,369.3 ms | 282 | completed，9 个流式分片，quote 步骤 1 个（5,126.1 ms），2 次调用 |
| R2 | 附件 PDF + file_search，首个分片后停止 | 2,651.0 ms | 2,655.1 ms | 40 | stopped=true，运行已完成、仅截断输出；file_search 1 个步骤，3 条引用全部定位 |
| R3 | kline 画图并验证下一问无污染 | 3,761.1 ms | 3,765.2 ms | 676 | completed，19 个分片，1 张内嵌图（figure HTML 4,862,205 字符 / 嵌入后 5,550,370 字符），无中断标记 |

R2 本次 `file_search("Revenue")` 首次即命中 3 页；上一轮实测出现过模型先传文件名导致失败、时间线以 `data-status="error"` 展示并以“改用 `file_list`”恢复的路径，测试对成功与失败恢复两种路径都做了断言（失败步骤必须给出可执行的下一步提示）。

R1/R2 的首字延迟包含首轮模型思考与工具调用时间，因此高于纯吐字场景；比较跨轮延迟时应结合步骤数与网络状况看。

## 6. 失败与降级

- 模型调用抛异常：运行时返回 `stop_reason=model_error`，回答区追加“模型调用失败……已保留此前产出的内容”并附错误摘要，`done` 正常释放 busy 锁；由 `test_model_failure_surfaces_clear_degradation` 覆盖。
- 工具失败：步骤保留错误信息与 `data-status="error"`，模型可继续下一步；R2 真实跑通该路径。
- 缺 key：`create_agent_model` 只在没有 key 时换成规则兜底并 `mark_degraded`（`astock/agent/gateway.py:1413-1425`）；E2E 不会因为没有 key 而假装通过，会直接 skip。
- 有 key 但网关不通：不会退规则兜底。probe 把状态置为 `degraded` 并降级为 JSON 工具协议（`astock/agent/gateway.py:1003-1008`），主适配器仍是 DeepSeek；运行时调用抛异常后走 `stop_reason=model_error`，回答区追加“模型调用失败……已保留此前产出的内容”。

## 7. 已知限制

- 模型侧无 token 流（ADR-014），TTFT 实际是“首个 UI 分片时间”。
- 图表 `figure` HTML 体积大（本次约 4.86 MB），报告只记录长度与关键标记，不落库。
- 真实 E2E 不进普通 CI，由维护者显式配置运行；费用估算依赖人工填写价格变量，未配置时 `cost=null`，写错时 `cost` 带 `error` 提示而不会静默消失。
