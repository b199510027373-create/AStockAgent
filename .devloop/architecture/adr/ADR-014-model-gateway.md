# ADR-014 模型网关：供应商可替换，Agent 不绑定 DeepSeek

- 状态：proposed
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

## 后果

- 正面：不锁定供应商；可切本地模型做隐私/离线；DeepSeek 只是默认值而非架构。
- 负面：不同模型工具调用能力差异大，需要一致性测试；本地模型质量可能不足，需要明确降级提示。
- 风险控制：启动时做一次轻量能力探测（能否返回合法 tool_call），失败则标记为 `degraded` 并在 UI 提示。
- 成本：token 用量入账，单轮预算硬限制，避免 Agent 循环烧钱。

## 证据

- `astock/llm.py`（写死 `deepseek-chat` 与 base_url）
- `astock/config.py`（环境变量加载方式）
- 用户 2026-11-07 消息
