# ADR-014 模型网关：供应商可替换，Agent 不绑定 DeepSeek

- 状态：accepted
- 裁决：**accepted**（2026-11-10 架构契约评审门禁 plan-20261110-003；流式输出转二期非目标，见文末）
- 日期：2026-11-07
- 来源：用户 2026-11-07「开发自己的 Agent，而不是内接 DeepSeek 的 API」
- 关联：ADR-013（Agent 运行时）

## 背景

现状 `llm.py` 直接 `from openai import OpenAI` 并写死 `deepseek-chat`，业务代码与供应商 SDK 耦合。用户要求"自己的 Agent"，如果继续把 DeepSeek 的调用方式写进 Agent 循环，等于换了个壳。

需要明确的是：**自研 Agent ≠ 不需要大模型**。Agent 的推理仍需要一个 LLM，区别在于——模型只是可替换的推理后端，工具、循环、记忆、审计都由我们自己掌握。

## 决策

1. 新增 `astock/agent/gateway.py`，定义与供应商无关的接口：
   ```python
   class ChatModel(Protocol):
       def complete(self, messages, tools, *, stream=False, temperature=0.2) -> ChatResult: ...
   ```
   `ChatResult` 统一携带：文本、`tool_calls`、`finish_reason`、token 用量、模型标识。
2. 适配器：
   - `DeepSeekAdapter`：默认后端，OpenAI 兼容协议；
   - `OpenAICompatibleAdapter`：可指向 Ollama / vLLM / 其他兼容服务（本地优先）；
   - `RuleFallback`：无 key / 全部失败时返回规则化答复，流程不崩。
3. 配置全部走 `.env`，业务代码不出现供应商名：
   ```dotenv
   AGENT_PROVIDER=deepseek | openai_compatible | rule
   AGENT_MODEL=deepseek-chat
   AGENT_BASE_URL=
   AGENT_API_KEY=
   AGENT_MAX_STEPS=12
   AGENT_TOKEN_BUDGET=64000
   ```
4. 工具调用协议以 OpenAI `tools/tool_calls` 为准；对不支持 function calling 的模型，退化为 JSON 协议（要求输出 `{"tool": "...", "args": {...}}`），解析失败即重试一次。
5. 流式输出统一为生成器；UI 逐段渲染文本与步骤事件。
6. `llm.py` 保留给推荐文案（保持 ADR-009 的稳定路径）；Agent 侧只走 gateway。后续再收敛为一套。
7. 记录每次请求的 provider/model/参数/token 用量到 agent_runs，保证可追溯。

## 实现回写（2026-11-10）

对照 `astock/agent/gateway.py`（1,512 行）的实际实现。**只记录，不重写已通过评审的代码**：

1. **偏差 1（签名）**：冻结的接口是 `gateway.py:266-279`
   ```python
   class ChatModel(Protocol):
       provider: str
       model: str
       def complete(self, messages, tools, *, temperature: float = 0.2) -> ChatResult: ...
   ```
   原文的 `stream=False` 形参**不存在**——v1 不做流式（见「非目标」）。`messages`/`tools` 是必填位置参数，
   空 `tools` 表示本轮不给工具。详见 `docs/AStockAgent_interface_freeze_v1.md` §2。
2. `ChatResult`（`gateway.py:216-263`）11 字段冻结：`content`、`tool_calls: tuple[ToolCall, ...]`、
   `finish_reason="stop"`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`model`、`provider`、
   `protocol="tools"`、`degraded=False`、`raw`；`total_tokens == 0` 时由 `prompt+completion` 补齐；
   `tool_call` 属性取首个调用。
3. 适配器实现：`OpenAICompatibleAdapter`（`provider="openai_compatible"`，`gateway.py:509`）、
   `DeepSeekAdapter`（`provider="deepseek"`，`gateway.py:676`）、`RuleFallback`（`provider="rule"`，
   `model="rule-fallback"`，`gateway.py:736`）。装配类 `ChatGateway(primary, fallback, probe, usage)`
   与 `build_gateway()`；能力探测结果 `CapabilityStatus(provider, model, function_calling, degraded, reason, probe_passed)`。
4. **偏差 2（配置项）**：`GatewaySettings.from_env()` 实际读取 `AGENT_PROVIDER`（默认 `deepseek`）、
   `AGENT_MODEL`、`AGENT_BASE_URL`、`AGENT_API_KEY`（回退 `DEEPSEEK_API_KEY`）、`AGENT_MAX_STEPS`、
   `AGENT_TOKEN_BUDGET`、**`AGENT_TIMEOUT_SECONDS`**、**`AGENT_USAGE_LOG`**。原文 dotenv 示例缺后两项，
   此处补齐（冻结为 v1 配置面）。
5. 用量入账：`UsageRecord` + `UsageLogger` 以 JSONL 追加（`test_usage_logger_writes_jsonl`），
   路径由 `AGENT_USAGE_LOG` 决定；多工具调用被降级时记录 `dropped_tool_calls`
   （`test_multiple_tool_calls_record_dropped_count`）。
6. JSON 协议兜底已实现且更严格：不支持 function calling 时走 JSON 协议、失败重试一次、
   两次失败退回规则答复（`test_json_protocol_retries_once_after_invalid_output`、
   `test_json_protocol_twice_invalid_falls_back_without_crash`）；JSON 协议下历史消息不带原生 tool 字段
   （`test_json_protocol_wire_history_has_no_native_tool_fields`）。
7. 桥接：`AgentModelAdapter`（`gateway.py:1291`）把 `ChatModel` 适配为 ADR-013 的 `AgentModel`，
   因此运行时与网关各自冻结、互不依赖（`test_agent_model_adapter_drives_runtime`）。
8. 偏差小结：无阻塞偏差。三处偏差（无 `stream` 形参、补两个环境变量、新增 `AgentModelAdapter` 桥）
   已写入 `docs/AStockAgent_interface_freeze_v1.md` §2 并由 `tests/test_architecture_gate.py` 守住。

## 非目标（v1 明确不做）

- **流式输出**：`ChatModel.complete` 一次性返回，不预留 `stream` 形参；流式需要同时改网关、运行时、
  UI 三处的事件模型，列为二期项，届时必须单独 ADR 并升接口版本。
- 具体默认模型选型不冻结：`AGENT_PROVIDER` 默认 `deepseek`，模型由 `AGENT_MODEL` 决定，属配置而非接口。

## 后果

- 正面：不锁定供应商；可切本地模型做隐私/离线；DeepSeek 只是默认值而非架构。
- 负面：不同模型工具调用能力差异大，需要一致性测试；本地模型质量可能不足，需要明确降级提示。
- 风险控制：启动时做一次轻量能力探测（能否返回合法 tool_call），失败则标记为 `degraded` 并在 UI 提示
  （`test_probe_failure_marks_degraded_and_provides_prompt`）。
- 成本：token 用量入账，单轮预算硬限制，避免 Agent 循环烧钱。

## 证据

- `astock/agent/gateway.py:216-279`（`ChatResult` / `ChatModel`）、`gateway.py:509/676/736`（三个适配器）、
  `gateway.py:1291`（`AgentModelAdapter`）
- `tests/test_agent_gateway.py`（31 项）
- `astock/llm.py`（写死 `deepseek-chat` 与 base_url）、`astock/config.py`（环境变量加载方式）
- 用户 2026-11-07 消息
