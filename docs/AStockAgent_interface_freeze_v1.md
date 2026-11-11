# AStockAgent 接口冻结清单 v1（2026-11-10）

> 范围：ADR-002/010/013–018 的「接口级」评审结论与 6 个对外接口的冻结定义。
> 性质：本文件是 v1 的实现依据。任何签名、字段、枚举、表结构、目录布局的改动，
> 必须先提新 ADR 并把版本号推进到 v2，不允许在实现里「顺手改」。
> 关联文件：`docs/AStockAgent_contract_rule_map_v1.md`（18 条 contract 规则落点）、
> `.devloop/architecture/contract.json`（v1.1）、`tests/test_architecture_gate.py`（防漂移门禁）。

## 0. 评审记录

### 0.1 议程与时间盒

| 时段 | 内容 | 产出 |
| --- | --- | --- |
| D1 上午（11-11） | 追认已实现的 002/010/013/014：对照代码，记录偏差 | 4 条 ADR 的「实现回写」段落 |
| D1 下午（11-11） | 预评审 015/016/018：存储布局、ChartSpec、时点语义 | §3–§6 冻结草案 + 3 条 ADR 条件 |
| D2 上午（11-12） | 017 安全边界 + 18 条规则落点映射 + 冻结清单 + ADR-012 治理附录 | 本文件、规则落点表、ADR-012 附录 |

时间盒 6h，只做接口级评审：不重写已通过评审的代码（plan-20261103-004/005 的成果保持原样），
不做全面架构复审。

实际执行：2026-11-10 一次会话内完成（提前于 11-12 截止），三段议程按「已实现追认 → 未开工预评审 →
安全边界与落点映射」顺序压缩执行；上表日期为原排期，保留以便对照。

### 0.2 结论表

| ADR | 主题 | 原状态 | 结论 | 条件数 | 承接计划项 |
| --- | --- | --- | --- | --- | --- |
| ADR-002 | 六层分层与 contracts.py | proposed | **accepted** | 0 | — |
| ADR-010 | 研究语义与「无 alpha」 | proposed | **accepted-with-conditions** | 2 | plan-20261103-006 |
| ADR-013 | Agent 运行时 | proposed | **accepted** | 0 | — |
| ADR-014 | 模型网关 | proposed | **accepted** | 0（流式转二期） | — |
| ADR-015 | 文件导入与检索 | proposed | **accepted-with-conditions** | 4 | plan-20261107-003（C1–C3）+ plan-20261107-004（C4） |
| ADR-016 | 图表 ChartSpec | proposed | **accepted** | 0（任意 Python 转二期） | plan-20261107-005 |
| ADR-017 | 审计与安全边界 | proposed | **accepted-with-conditions** | 3 | plan-20261107-008 |
| ADR-018 | 实时口径 | proposed | **accepted-with-conditions** | 2 | plan-20261107-006 |

**无 deferred**：没有发现阻塞性接口问题，003（SQLite ledger）与 005（时间语义）可以在 11-15 照常开工。
判定依据：015/016/017/018 的接口草案在字段级可以确定，剩余问题都是「实现补齐」而非「设计未定」。

**2026-11-11 排期调整（承接项合并）**：上表「承接计划项」原为 2026-11-10 新建的
`plan-20261110-004~007`，与既有 `plan-20261107-003/004/005/006/008` 指向同一模块（文件导入、图表、
审计表、行情工具）。为避免同一模块并行两条计划线，已取消 `plan-20261110-004~007`，其验收条目并入：

| 原承接项 | 并入目标 | 理由 |
| --- | --- | --- |
| plan-20261110-004（files.py 布局 + FTS5） | plan-20261107-003（解析，C1–C3）+ plan-20261107-004（检索与表格查询，C4） | 布局与抽取同批落地；FTS5 在 `files.py` 抽取时写入，检索契约（单条 SELECT + LIMIT + 超时）属 07-004 |
| plan-20261110-005（ChartSpec v1） | plan-20261107-005（图表 spec 与渲染管线） | 同一交付物的规格与实现 |
| plan-20261110-006（agent_* 三表） | plan-20261107-008（审计表与安全限额） | 三表即审计表，且起始日提前到 11-18 |
| plan-20261110-007（data_time 生产者） | plan-20261107-006（行情/指标工具） | data_time 由行情工具产出 |

合并后 ADR-017 的条件最早 11-18 开工（原 11-29），仍在「冻结后 2 周内实现」的窗口内。

### 0.3 为什么这次只给 conditions 不给 deferred

deferred 的门槛是「接口设计本身还没定，先改下游设计」。四条未开工 ADR 都通过了这个检查：

- 015 的存储布局（内容寻址 + 三件产物）在 §4 可以逐字写死；
- 016 的 ChartSpec 在 §3 可以逐字写死，且 ADR-013 已决定「不执行任意代码」，不存在表达力回退风险；
- 017 的三张表在 §5 写死后不影响 003 的五表设计（同库不同表）；
- 018 的 `data_as_of`/`data_time` 是**已实现字段**（`ToolResult` 已带），缺的只是生产者，属实现缺口。

所以全部给 accepted-with-conditions，并把条件挂到具体计划项，避免「条件写在 ADR 里没人管」。

## 1. 冻结接口一：`ToolResult` / `ToolSpec`

代码锚点：`astock/agent/types.py:77-186`、`astock/agent/registry.py:260-305`。
状态：**已实现，追认**。

### 1.1 `ToolResult` 字段（9 个，顺序即字段顺序）

| 字段 | 类型 | 默认 | 不变量 |
| --- | --- | --- | --- |
| `text` | `str` | `""` | 进入 observation 时截断到 4000 字符（`MAX_TEXT_CHARS`），完整值保留在对象上 |
| `table` | `list[dict[str, Any]] \| None` | `None` | 只允许 `list[dict]`；超过 200 行（`MAX_TABLE_ROWS`）只保留前 200 行并置 `truncated` |
| `figure` | `str \| None` | `None` | artifact_id（`art_<16 hex>`），不是 HTML/路径 |
| `error` | `str \| None` | `None` | 非空即视为失败（`ok == False`），必须给可读原因 |
| `data_as_of` | `str \| None` | `None` | `YYYY-MM-DD`；行情/图表结果必填（§6） |
| `data_time` | `str \| None` | `None` | `YYYY-MM-DD HH:MM`；行情/图表结果必填（§6） |
| `source` | `str \| None` | `None` | 文件结论必填：`文件名 + 页码/工作表/单元格范围` |
| `truncated` | `bool` | `False` | 只由 `trim_table()` 置真；为真时 `metadata` 必须含 `dropped_rows` |
| `metadata` | `dict[str, Any]` | `{}` | 附加信息（`total_rows`/`dropped_rows`/`artifact_missing`/`session`/`freshness`） |

冻结的方法：

- `ok: bool`（property）：`not error`；
- `to_dict() -> dict`：空字段省略、**总是**带 `ok`；列表/字典字段深拷贝；
- `to_json() -> str`：`ensure_ascii=False, sort_keys=True, default=str`；
- `trim_table(limit: int = 200) -> ToolResult`：返回新对象，记录 `total_rows` 与 `dropped_rows`；
- `coerce(value: Any) -> ToolResult`（classmethod）：接受 `ToolResult/str/list[dict]/DataFrame/字段子集 Mapping`，
  其他退化为 `text=str(value)`。**这条是工具处理函数允许直接返回裸值的唯一理由，不得在工具里各写一套转换。**

### 1.2 `ToolSpec` 字段（5 个）

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `name` | `str` | 正则 `[a-z][a-z0-9_]{0,63}`，全局唯一 |
| `description` | `str` | 非空（模型靠它选工具） |
| `parameters` | `Mapping[str, Any]` | 必须是 JSON Schema `type: "object"`，注册期 `_check_schema` 校验 |
| `handler` | `Callable[..., Any]` | 必须可调用；返回值经 `ToolResult.coerce` 规范化 |
| `permissions` | `frozenset[Permission]` | 见 1.3；调用方 `allowed_permissions` 外的工具不可见 |

`json_schema() -> dict` 输出 `{"type": "function", "function": {name, description, parameters}}`。
`ArtifactWriter = Callable[[str, dict[str, Any], list[dict[str, Any]]], str]`（tool, arguments, rows → artifact_id）。
`ToolRegistry(*, allowed_permissions=None, table_row_limit=200, artifact_writer=None)`。

### 1.3 `Permission` 七标签（实现回写，替换 ADR-017 的三级模型）

```python
MARKET_READ = "market:read"        INDICATOR_COMPUTE = "indicator:compute"
CHART_RENDER = "chart:render"      FILE_READ = "file:read"
RESEARCH_READ = "research:read"    LEDGER_READ = "ledger:read"
USER_ASK = "user:ask"
```

冻结：**不存在 `write` 标签**。Agent 写自己的 run/step/artifact 由 `store` 内部控制（ADR-017 §5），
不作为工具权限暴露；工具侧永远拿不到写台账的能力（AGENT-03）。

## 2. 冻结接口二：`ChatModel.complete`

代码锚点：`astock/agent/gateway.py:266-279`（Protocol）、`gateway.py:216-263`（`ChatResult`）。
状态：**已实现，追认**。ADR-014 原文写的 `stream=False` 形参**不存在**，v1 不做流式（见 ADR-014 条件外的非目标）。

```python
@runtime_checkable
class ChatModel(Protocol):
    provider: str
    model: str
    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
    ) -> ChatResult: ...
```

- `messages` / `tools` 是**必填位置参数**；`tools` 为空序列表示「本轮不给工具」，不得传 `None`。
- `temperature` 是唯一的关键字参数，默认 `0.2`；新增参数只能以关键字参数追加，且必须有默认值。
- 实现方：`DeepSeekAdapter`（`provider="deepseek"`）、`OpenAICompatibleAdapter`（`"openai_compatible"`）、
  `RuleFallback`（`"rule"`）；装配走 `build_gateway()` / `GatewaySettings.from_env()`。
- `ChatResult` 冻结字段（11）：`content`、`tool_calls: tuple[ToolCall, ...]`、`finish_reason="stop"`、
  `prompt_tokens`、`completion_tokens`、`total_tokens`、`model`、`provider`、`protocol="tools"`、
  `degraded=False`、`raw`。`total_tokens == 0` 时由 `prompt+completion` 补齐；`tool_call` 属性取首个。
- **循环侧接口不同名**：`AgentRuntime` 用的是 `runtime.AgentModel`（`runtime.py:65-71`，
  关键字参数 `*, messages, tools`，返回 `ModelReply`），由 `gateway.py:1291 AgentModelAdapter` 桥接。
  两者都冻结；新增适配层必须走 `AgentModelAdapter`，不得让 runtime 直接依赖 `ChatModel`。

## 3. 冻结接口三：`ChartSpec` v1

代码锚点：待建 `astock/agent/chart_spec.py`（plan-20261107-005）。状态：**冻结草案，未实现**。

### 3.1 字段表（`additionalProperties: false`，未知键直接拒绝）

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `version` | `int` | ✓ | 必须等于 `1` |
| `kind` | `str` | ✓ | `candlestick`/`line`/`bar`/`scatter`/`heatmap`/`histogram` |
| `title` | `str` | ✗ | ≤ 120 字符，纯文本（禁 HTML/脚本） |
| `data` | `object` | ✓ | 见 3.2 |
| `encode` | `object` | ✓ | `x` 必填；`y` 为 `str` 或 1–4 项 `list[str]`；`color` 可选 |
| `transforms` | `list[object]` | ✗ | ≤ 8 项，`op` ∈ `filter`/`groupby`/`agg`/`sort`/`limit` |
| `layers` | `list[object]` | ✗ | ≤ 8 项，`type` ∈ `ma`/`boll`/`volume`/`macd`/`rsi`/`kdj` |
| `annotations` | `list[object]` | ✗ | ≤ 10 项，`type` ∈ `hline`/`vline`/`text` |
| `theme` | `str` | ✗ | `light`（默认）/`dark` |
| `meta` | `object` | ✗ | 透传元信息，JSON 序列化后 ≤ 1 KB |

### 3.2 `data` 子结构

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `source` | `str` | `market`/`artifact`/`inline` |
| `ref` | `str` | `source=market` 必填：个股 `^[0-9]{6}\.(SH\|SZ\|BJ)$`，指数 `^000[0-9]{3}\.SH$\|^399[0-9]{3}\.SZ$`；`source=artifact` 必填：`^art_[0-9a-f]{16}$` |
| `rows` | `list[dict]` | `source=inline` 必填，≤ 5000 行 |
| `fields` | `list[str]` | `source=inline` 必填，1–20 项，`^[a-z_][a-z0-9_]{0,31}$` |
| `period` | `str` | `day`（默认）/`week`/`month`/`1min`/`5min`/`15min`/`30min`/`60min`；分钟周期仅允许 `source=market` |
| `range` | `str` | `1mo`/`3mo`/`6mo`/`1y`（默认）/`3y`/`5y`/`ytd`/`max`，或 `YYYY-MM-DD:YYYY-MM-DD`（跨度 ≤ 10 年） |

**消歧（2026-11-11 评审）**：`period` 的分钟档一律带 `min` 后缀（`1min`…`60min`），`range` 的月档一律带 `mo` 后缀（`1mo`/`3mo`/`6mo`）。
两者**不共用 `1m`**：前者是「每根 bar 的粒度」，后者是「窗口长度」。schema 用例见 plan-20261107-005（`period=1min` 取 1 分钟 bar、`range=1mo` 取 1 个月窗口）。
| `adj` | `str` | `qfq`（默认）/`hfq`/`none`；涨跌停与成交判断固定 `none`（ADR-008） |

### 3.3 算子与图层参数（白名单，越界拒绝并返回字段级错误）

```text
filter    {field, cmp: gt|ge|lt|le|eq|ne|in, value: number|str|list}
groupby   {by: 1..3 field, metrics: {field: sum|mean|min|max|count|last}}
agg       {by: 1..3 field, metrics: {field: sum|mean|min|max|count|last}}
sort      {by: field, desc: bool}
limit     {n: 1..5000, from: head|tail}
ma        {window: 2..250}
boll      {window: 5..250, k: 1..3}
volume    {}
macd      {fast: 8..20, slow: 21..60, signal: 5..15}
rsi       {window: 2..60}
kdj       {n: 5..90, k: 3, m: 3}
hline     {y: number, label?: str ≤ 60}   vline {x: str|number, label?}   text {x, y, label ≤ 60}
```

### 3.4 渲染契约

- `render(spec) -> RenderedFigure{html: str, png_path: str|None, artifact_id: str, warnings: list[str]}`；
- 图题与角标同时展示 `data_as_of` 与 `data_time`；缺任一时点必须显式写 `时点未知`，不得留空；
- 产物落 `agent_artifacts`，`meta_json` 记录 `{data_as_of, data_time, source, params, spec, code_version}`；
- 无 `kaleido` 时只出 HTML（`png_path=None`），不抛异常；
- 渲染器**不得**执行 spec 之外的任何代码；`transforms` 只走白名单算子。

## 4. 冻结接口四：文件存储布局 v1

代码锚点：待建 `astock/agent/files.py`（plan-20261107-003）。状态：**冻结草案，未实现**。

```text
data/agent/files/<sha256>/            # sha256 为原文 64 位小写十六进制；内容寻址，不可变
  original.<ext>                      # 原样字节，扩展名取原文小写
  extracted.jsonl                     # 逐块抽取结果，一行一个 JSON
  tables/<sheet_slug>.parquet         # 每张工作表一个 parquet
  meta.json                           # 解析元数据
```

`file_id = "f_" + sha256[:16]`（与目录名解耦，便于 UI 短引用）。

`meta.json` 字段（全部必填，缺失视为解析失败）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `file_id` / `sha256` | `str` | 同上；`sha256` 全量 |
| `name` | `str` | 原始文件名（仅展示，不参与路径） |
| `ext` / `media_type` | `str` | 小写扩展名 / MIME |
| `size_bytes` | `int` | ≤ 20 MiB |
| `uploaded_at` | `str` | ISO 8601 带时区，如 `2026-11-10T10:32:00+08:00` |
| `pages` | `int \| null` | PDF/DOCX 页数；表格为 `null` |
| `sheets` | `list[object]` | `{name, slug, rows, cols, header_row}` |
| `text_chars` | `int` | 抽取文本总字符数 |
| `chunks_count` | `int` | 写入 FTS5 的块数 |
| `parser` / `parser_version` | `str` | 如 `pdfplumber` / `0.11.x` |
| `status` | `str` | `ok`/`empty_text`/`suspected_scan`/`encrypted`/`unsupported` |
| `warnings` | `list[str]` | 可读提示，直接回显给用户 |

`extracted.jsonl` 单行结构：
`{"file_id", "page": int|null, "sheet": str|null, "block": int, "kind": "text"|"table", "text": str, "bbox": [x0,y0,x1,y1]|null, "cells": int|null}`。

表格落盘规则：`sheet_slug` = 工作表名转小写、非 `[a-z0-9]` 替换为 `_`、截断 40 字符、重名后缀 `_2`；
parquet 列名取原始表头（去首尾空白），无表头时 `col_1..col_n`；数值列尽力转 `float`，转换失败**保留字符串**，
不静默丢弃行（丢弃必须在 `warnings` 里报数量）。

索引（与 ledger 同库 `data/astock.db`）：

```sql
CREATE TABLE IF NOT EXISTS file_chunks(
  chunk_id INTEGER PRIMARY KEY, file_id TEXT NOT NULL, page INTEGER,
  sheet TEXT, block INTEGER NOT NULL, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_fts USING fts5(
  text, content='file_chunks', content_rowid='chunk_id', tokenize='unicode61');
CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks(file_id, page);
```

`file_chunks_fts` 的 `ai`/`ad`/`au` 触发器与 `file_chunks` 同步（ADR-015 §5）。

约束（与 ADR-015 一致，此处只复述落点）：单文件 ≤ 20 MiB、单轮 ≤ 5 个文件、PDF ≤ 200 页；
仅允许 `data/agent/files/` 下的读取，拒绝路径穿越与符号链接；DuckDB 以 `read_only=True` 打开 parquet，
只允许单条 `SELECT`，强制 `LIMIT` 并 5s 超时；文件与索引 90 天留存（`AGENT_FILE_RETENTION_DAYS`），
GC 只删目录与索引行，不删 `agent_steps`/`agent_artifacts`。

## 5. 冻结接口五：`agent_*` 三表

代码锚点：待建 `astock/agent/store.py`（plan-20261107-008）。状态：**冻结草案，未实现**。
同库 `data/astock.db`，与既有五表（runs/items/evidence/jobs/tracking）共存；建表幂等。

```sql
CREATE TABLE IF NOT EXISTS agent_runs(
  run_id           TEXT PRIMARY KEY,
  session_id       TEXT NOT NULL,
  provider         TEXT NOT NULL,
  model            TEXT NOT NULL,
  temperature      REAL,
  seed             INTEGER,
  code_version     TEXT,
  status           TEXT NOT NULL CHECK(status IN ('running','success','partial','failed')),
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  steps            INTEGER NOT NULL DEFAULT 0,
  prompt_tokens    INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  error            TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps(
  run_id        TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  step          INTEGER NOT NULL,
  kind          TEXT NOT NULL CHECK(kind IN ('tool','model','final')),
  tool          TEXT,
  args_json     TEXT NOT NULL DEFAULT '{}',
  result_digest TEXT,
  latency_ms    INTEGER,
  created_at    TEXT NOT NULL,
  PRIMARY KEY(run_id, step)
);

CREATE TABLE IF NOT EXISTS agent_artifacts(
  artifact_id TEXT PRIMARY KEY,                 -- art_<16 hex>
  run_id      TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  step        INTEGER,
  kind        TEXT NOT NULL CHECK(kind IN ('figure','table','file','report')),
  path        TEXT NOT NULL,                    -- 相对 data/agent/ 的 POSIX 路径
  digest      TEXT NOT NULL,                    -- 文件内容 sha256
  meta_json   TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_session    ON agent_runs(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run       ON agent_steps(run_id, step);
CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run   ON agent_artifacts(run_id, step);
CREATE INDEX IF NOT EXISTS idx_agent_artifacts_digest ON agent_artifacts(digest);
```

相对 ADR-017 原始 DDL 的三处**实现回写**（评审发现，不重写已评审代码，只补 DDL）：

1. `agent_runs` 补 `temperature`/`seed`/`code_version`：ADR-017 §3 要求「记录 provider/model/temperature/seed
   与代码版本」，原 DDL 没有这些列，审计无法回放。
2. `agent_artifacts` 补 `digest`：§3 要求「按 hash 去重」，原 DDL 没有 hash 列，去重无从实现。
3. `agent_runs.status` 取值定为 `running/success/partial/failed`：与 `StopReason` 8 值映射
   （`completed → success`，其余 → `partial`），不再单列 `interrupted`。

artifact 路径与去重：`data/agent/artifacts/<digest[:2]>/<digest>/<filename>`；同 digest 复用目录、只插一行，
`digest` 索引保持非唯一（同一张图可被两个 run 引用）。删除 artifact 文件后 run/step 记录仍可查（审计不断链）。

## 6. 冻结接口六：`data_as_of` / `data_time`

代码锚点：`astock/agent/types.py:90-91`（已实现字段）、`astock/data.py`（缺生产者）。
状态：**字段已实现；生产者冻结，待 plan-20261107-006 落地**。

### 6.1 语义

| 字段 | 格式 | 定义 |
| --- | --- | --- |
| `data_as_of` | `YYYY-MM-DD` | 数据所属**交易日**，取最后一根有效 bar 所在交易日；必须能过 `trade_cal` 校验 |
| `data_time` | `YYYY-MM-DD HH:MM` | 最后一根 bar 的**时间戳**；日线固定写 `YYYY-MM-DD 15:00`，分钟写 bar 结束时间（`09:35` 表示 `09:30–09:35`） |

不变量：

1. 两者同一天：`data_time[:10] == data_as_of`，跨日 bar 不允许（盘中出现在下一交易日的 bar 视为数据错误，拒绝并回退）；
2. 行情与图表结果**两个都必填**；文件结论不需要这两个字段，但必须有 `source`（AGENT-04）；
3. 结果为 `error` 时可为空；
4. 回答与图题禁止只写「实时」，必须展示两个时点（ADR-018 §2）。

### 6.2 唯一生产者（条件 C1）

```python
# astock/data.py
def latest_bar_time(df: pd.DataFrame, *, freq: str = "D") -> tuple[str, str]:
    """返回 (data_as_of, data_time)，是全局唯一的时点格式化入口。"""
```

- 工具与渲染器**不得**自行 `strftime` 拼时点；所有 `ToolResult.data_as_of/data_time` 必须由它产出；
- `freq` ∈ `D`/`W`/`M`/`1min`/`5min`/`15min`/`30min`/`60min`，与 `ChartSpec.data.period` 同构（分钟档带 `min`，月档带 `mo`，不共用 `1m`）；
- 空 DataFrame 抛 `ValueError`（不允许返回空字符串骗过必填校验）。

### 6.3 降级标注（元数据键冻结）

`ToolResult.metadata` 中固定两个键，供 UI 与回答统一处理：

| 键 | 取值 | 含义 |
| --- | --- | --- |
| `session` | `pre`/`open`/`lunch`/`closed`/`non_trading_day` | 交易时段 |
| `freshness` | `live`/`delayed`/`eod`/`suspended` | 数据新鲜度；非交易时段必须是 `eod`，`rt_min` 超 2 分钟未更新必须 `delayed` |

## 7. 未决问题的去向

| 未决问题 | 结论 | 落点 |
| --- | --- | --- |
| 默认模型选谁（DeepSeek / 本地 / 其他） | 不冻结具体模型：`AGENT_PROVIDER` 默认 `deepseek`，模型走 `AGENT_MODEL`；无 key 走 `RuleFallback`。选型是配置不是接口 | ADR-014 §3 |
| tick / Level-2 | v1 非目标，单列 ADR 后才可能接入 | ADR-018 §6 |
| OCR / 扫描件 / 加密 PDF / 宏 | v1 非目标，识别为 `suspected_scan`/`encrypted` 并提示 | ADR-015 §1 |
| 任意 Python 执行 / 代码沙箱 | 二期项：只允许子进程 + 无网络 + 限额方案，不在主进程 `exec` | ADR-016 §6、ADR-013 §7 |
| 流式输出 | 二期：`ChatModel.complete` 保持一次性返回，不预留 `stream` 形参 | ADR-014 §5 |

## 8. 冻结流程（治理）

1. 本文件标 v1；`contract.json` 同步到 v1.1，18 条规则全部带「落点：」说明；
2. 改冻结接口 → 先写 ADR（含迁移影响）→ 状态 accepted → 再把本文件与 `contract.json` 升到 v2，
   同期更新 `tests/test_architecture_gate.py` 的断言；
3. `tests/test_architecture_gate.py` 是门禁：ADR 里出现 `proposed`、`adrs.json` 与 markdown 状态不一致、
   规则缺落点、冻结接口与代码不一致，任一成立即测试失败。
