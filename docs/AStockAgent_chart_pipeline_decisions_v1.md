# 图表 spec 与渲染管线：实现决策与风险清单（v1）

> 用途：交给架构角色评审/裁决的决策记录（decision log），不是验收文档。
> 关联：plan-20261107-005 · ADR-016（图表 spec 与渲染管线）· ADR-008（复权口径）· ADR-018 C1（时点唯一生产者）
> 冻结契约：`docs/AStockAgent_interface_freeze_v1.md` §3（ChartSpec v1）、§5（agent_* 三表 DDL）、§6（data_as_of / data_time）
> 代码：`astock/agent/chart_spec.py`、`astock/agent/chart_render.py`、`astock/agent/store.py`、`astock/data.py`
> 交付状态：PR #12，功能提交 `75c60e3`，CI run 43405286628 pass，计划项已合并（merge commit `351c66b`）
>
> **2026-11-14 已被取代**：Q1/Q2/Q3/Q4/Q5/Q6/Q7/D9 的最终裁决见
> `docs/AStockAgent_chart_audit_v2_change_spec.md` 与 ADR-016 补充裁决；本文件保留决策过程，
> 不再作为实现依据。D1/D10/D11 见 ADR-008/ADR-016 补充裁决。

---

## 0. 交付快照（用于定位评审对象）

| 项 | 值 |
|---|---|
| 计划项 | plan-20261107-005（P1，里程碑 ms-agent，原验收已并入 plan-20261110-005） |
| 分支 / 提交 | `feat/agent-chart-pipeline` / `75c60e3`（前序 `825b32c`、`e1c9e66`、`2eac348`） |
| PR | https://github.com/b199510027373-create/AStockAgent/pull/12 |
| CI | run 43405286628 → success（run 43402353579 首轮失败：plotly 7.x 的 `<!doctype html>` 前缀） |
| 测试 | 目标 113 passed；全量 `pytest tests -q` 471 passed, 1 skipped, 2 subtests；`mypy astock` 34 files clean；架构门禁 32 passed, 1 skipped |
| 交付文档 | `ho-20261113-018`（initial）、`ho-20261113-020`（update，含 8 条评审批次修复） |
| 评审线程 | `rev-20261113-003`（1 P0 + 2 P1 + 5 P2，已全部修复并附回归测试） |

本文档记录**代码里已经生效、但契约文本或架构图尚未完全覆盖**的决定，以及这些决定的已知风险。共 11 条（D1–D11），其中 7 条需要架构裁决（见 §3 的 Q1–Q7）。

---

## 1. 决策总览

| ID | 决策 | 影响面 | 需架构裁决 |
|---|---|---|---|
| D1 | 数据层真正实现 qfq / hfq / none，渲染层 `none`/`raw` → `adj=None` | 全局取数行为（`astock/data.py`） | 否（已有 ADR-008 授权），但需确认 hfq 基准 |
| D2 | `data_as_of` / `data_time` 按**transforms 后落图数据**重算 | 图注语义、下游新鲜度判断 | **是**（Q1） |
| D3 | artifact 回放按快照时间列重算，不信任旧 `meta_json` | 审计语义 | **是**（Q2） |
| D4 | `div_id` 固定（2026-11-16 已改为 spec+快照派生，见 §6） | 同页多图、跨环境复现 | **是**（Q3） |
| D5 | `panes`≡`layers`、`style`≡`theme`、`adj="raw"`≡`none` 兼容别名 | 冻结清单 §3.1「未知键拒绝」出现例外 | **是**（Q4） |
| D6 | `chart_render` 对 data/indicators 延迟 import，modules.json 不加边 | 架构图与代码一致性 | **是**（Q5） |
| D7 | kaleido 不进依赖，PNG 走降级 | 报告/PDF 场景、测试覆盖 | **是**（Q6） |
| D8 | `latest_bar_time` 提前落入 `astock/data.py`，分钟档只认两段交易时段 | plan-006 消费口径、数据源兼容 | **是**（Q7） |
| D9 | `agent/store.py` 三表最小实现 + 合成 run | `agent_runs` 审计语义、磁盘留存 | 部分（Q2 关联） |
| D10 | 无标准时间列时「猜」时间列（`_probe_time_frame`） | 时点正确性 | 建议确认 |
| D11 | transforms 在渲染帧上用 pandas 手写白名单算子 | 图/表数值一致性 | 建议确认 |

---

## 2. 逐项说明

### D1 · 复权口径：qfq / hfq / none 真正分开

**决策**
- `astock/data.py:192` 新增 `_normalize_adj(adj)`：`None`/`""` → `None`；只接受 `qfq` / `hfq`，其余抛 `ValueError`。
- `_fetch_daily` 复权基准：`qfq` 用**最后一根**因子（与历史行为一致），`hfq` 用**原始因子 1.0**（`close × adj_factor`），`none` 跳过复权。
- 分钟线 `_apply_minute_adj(df, factors, adj)` 同理按口径取基准。
- `astock/agent/chart_render.py` 取数前把 spec 的 `adj` 映射为 `{"qfq": "qfq", "hfq": "hfq", "none": None, "raw": None}`，未知值透传给 data 层报错。

**为什么**：评审 P0（iss-mwesogzh-14310b）。原实现 `if adj:` 对所有非空值都做前复权，`adj="none"` 仍输出 qfq 价格，而图注写「不复权（raw）」——图注与数据不一致，属于正确性缺陷。

**风险**

| 风险 | 说明 | 缓解 |
|---|---|---|
| hfq 基准假设 | 以 1.0 为基准等价于假设 `adj_factor` 首值≈1.0；若否则整条序列差一个固定倍数（收益率不变，绝对价位与涨跌停比较会偏） | 在 docstring 写明基准；如需严格对齐交易所口径，改为「以首个交易日因子为基准」 |
| 行为收紧 | 非常规 `adj` 值（`"raw"`、`"前复权"`、带空格）从「静默 qfq」变为抛错 | 已 grep 全部调用方（`backtest.py:429`、`optimize.py:44`、`track.py:730/1020`、`ui.py:101`）只传 `"qfq"`，现网无回归 |
| 分钟线样本 | 分钟因子来自当日 `adj_factor`，样本少，基准选择影响更明显 | 分钟场景仅用于盘中观察，ADr-008 允许 |

---

### D2 · 时点跟「图上最后一根 bar」走

**决策**：`chart_render.py:521` `_time_points()` 的优先级为：显式参数 → 对**transforms 之后**的 frame 调 `data.latest_bar_time()` → artifact 快照 attrs 兜底（附 warning）→ 空串 + warning。`render()` 中时点计算从 `apply_transforms` 之前移到之后。

**为什么**：评审 P1（iss-mwesok8w-692962）。原实现图注写源数据最新日（2024-01-04），但 `filter`/`limit` 后只剩 2024-01-03，快照与图注不一致。

**风险**
- **语义从「行情时点」变成「视图时点」**：若模型写了 `filter close > 10` 而最新一根不满足，图注日期会变旧。对「数据截止」这个词自洽，但下游若用它做新鲜度判断就错了；`ToolResult.metadata.freshness/session`（§6.3）仍是行情语义，两套时点并存。
- 显式传参仍优先，模型/工具可以覆盖，等于保留了一个「绕过一致性的口子」（但这是复审要求的优先级）。

**建议**：给 `RenderedFigure` 增加 `time_point_scope: "view" | "market"`，或在 caption 里区分「数据截止（视图）」。

---

### D3 · artifact 回放重算时点

**决策**：`source=artifact` 时优先用快照的时间列重算 `data_as_of/data_time`；只有快照缺时间列时才回退到 meta 里的旧时点，并记 warning「artifact 快照缺少可用时间列，回退到快照记录的时点」。回归用例会把 `meta_json.data_as_of` 篡改为 2020-01-01 后回放，断言仍得到 2024-01-03。

**为什么**：评审 P1。回放若信任 meta，快照被修正/篡改后图注会继续骗人。

**风险**
- **审计语义变化**：数据库里的 `meta_json` 不再是不可变事实，而是「渲染当时的快照记录」；内容真实性的锚点转移到文件 digest（`agent_artifacts.digest` = sha256）。若将来做审计报表，需要明确「以 digest 为准，不以 meta 为准」。
- 重算依赖快照里有可用时间列；`_probe_time_frame` 的猜测逻辑（D10）会放大误判影响。

---

### D4 · 固定 `div_id` 换确定性

**决策**：`chart_render.py:72` `FIGURE_DIV_ID = "astock-chart"`，`render()` 调 `figure.to_html(..., div_id=FIGURE_DIV_ID)`（`:1146`）。plotly 默认随机 uuid 会让同 spec 每次字节不同，固定后「同 spec → 同字节 → 同 artifact_id」。

**为什么**：验收 C3「凭 artifact_id 原样复现」；同时让 `put_content` 的内容寻址去重生效。

**风险（本轮最值得修）**
1. **同页多图 id 冲突**：一页嵌两张图表 artifact 时两个 div 都是 `astock-chart`，Plotly `newPlot` 会挂错/覆盖（表现为第二张空白或两图相同）。Gradio 同屏多 figure 是常态。
2. **跨环境字节不一致**：本机 plotly 6.7.0 输出 `<html>`，CI 的 7.1.0 输出 `<!doctype html>`（首轮 CI 即此失败）。**复现保证只在同 plotly 版本内成立**，而 `_params()` 没有记录 plotly 版本，`code_version` 仍是硬编码 `"astock-0.1.0"`，不随 commit 变化。

**建议**
- `div_id` 改为由「规范化 spec + 数据 digest」派生的稳定短哈希（`astock-<8hex>`）：同 spec 仍确定，不同 spec 不撞。
- `meta.params` 增加 `plotly_version` / `python_version` / 真实 `code_version`（包版本或 `git rev-parse --short HEAD`），并在冻结清单写明「复现以相同运行环境为前提」。

---

### D5 · panes / style / raw 兼容别名

**决策**
- spec 层：`panes` 折叠为 `layers`，`style` 折叠为 `theme`（`style` 可为字符串或 `{"theme": "light"|"dark"}`），`adj="raw"` 折叠为 `none`。
- 别名与规范名**同时出现**报字段级错误（`$.panes` / `$.style`）。
- JSON Schema 顶层同时声明 `panes` / `style`（带「别名」description），`data.adj` 的 enum 含 `raw`。
- 冻结清单新增 `§3.2.1 兼容别名` 记录三者语义，状态行改为「已实现（v1）」。

**为什么**：任务验收文字要求字段含 `panes` / `style`，而冻结清单 §3.1 与 ADR-016 只有 `layers` / `theme`（全仓库 docs 无 `panes`/`style` 字样）。两者都要满足，故选别名折叠而非改契约。

**风险**
- **契约出现例外**：§3.1 原写「未知键一律拒绝」，现在有三个合法别名；按 §3.1 独立实现的校验器（前端表单、外部 agent）会拒绝 `panes`。
- **模型侧双份心智**：schema 里同时有 `layers` 和 `panes`，模型容易两个都填，浪费一轮重试（好在是结构化错误）。
- **转正成本**：artifact 里存的是归一化后的 `layers`/`theme`，若将来把 `panes` 定为正式字段，需迁移或双读。

**建议**：架构在「承认别名（现状）」与「改验收文字、删别名」之间二选一。若保留别名，建议在 schema 描述里明确「新代码请只用 layers/theme」，把别名定位为兼容层。

---

### D6 · `chart_render` 延迟 import，modules.json 不加边

**决策**：模块图（`.devloop/architecture/modules.json`）没有 `chart_render → data / indicators` 的边；为避免整体重写图，`chart_render` 在函数内 `from astock import data as market` / `from astock import indicators`。

**为什么**：替换整张模块图风险大于收益；架构门禁只校验描述文本，不校验边与代码一致。

**风险**
- **架构图与代码不一致**：`chart_render`（layer 3）实际依赖 `data`（低层），图上隐形；分层违例不会被自动发现。
- **掩盖循环依赖**：若将来 `data` 反向依赖 `agent.*`，只会在运行时炸且只在特定分支触发。
- **对 plan-008 是负债**：补图的人要重新核对所有边。

**建议**：单独提一个「模块图补边」小项，把 `chart_render→data`、`chart_render→indicators`、`chart_render→agent_store` 写入 modules.json **并同时**把延迟 import 改回模块级——只做一半比不做更糟。

---

### D7 · kaleido 不进依赖，PNG 降级

**决策**：`_try_png()` 捕获 `ValueError/RuntimeError/ImportError/ModuleNotFoundError`，失败时 `png_path=None`、`degraded=True`、warning「PNG 导出降级：…（仅 HTML artifact）」，run 状态记 `partial`。符合 ADR-016「无 kaleido 不抛异常」。

**风险**
- **真实导出路径零覆盖**：测试全部用 monkeypatch 模拟缺失；CI 从不真正导出 PNG。kaleido 0.2（`scope` API）与 1.x（独立 chrome 进程）差异很大，装依赖时很可能「代码写了但跑不通」。
- 报告/PDF 场景拿不到位图，只能嵌 HTML。
- 无头环境缺中文字体时 PNG 中文变方块，只有真跑一次才暴露。

**建议**：把 kaleido 放进可选依赖组（`[project.optional-dependencies] png`）并加一个 `skipif` 的真导出用例；否则在 ADR-016 里明确「v1 不承诺 PNG」。

---

### D8 · `latest_bar_time` 提前落地与分钟时段收紧

**决策**：`astock/data.py:735` `latest_bar_time(df, *, freq="D")` 按冻结清单 §6.2 的唯一生产者签名提前实现（原状态行写「待 plan-20261107-006 落地」，已改为「已实现，plan-006 只消费」）。实现要点：`pd.Timestamp(series.max())`（不依赖行序）；分钟档按 `09:30–11:30` / `13:00–15:00` 两段**闭区间**校验，越界抛 `ValueError`；日/周/月固定 `15:00`；构造后断言 `data_time[:10] == data_as_of`。

**为什么**：渲染管线当下就需要时点，而冻结清单规定工具与渲染器不得自行 `strftime`。

**风险**
- **双生产者**：plan-006 若不知已存在，可能再写一个或自行格式化，时点口径分叉（正是 ADR-018 要禁止的）。
- **时段校验过严**：**09:25 集合竞价 bar**、部分数据源的盘后固定价 bar 会被判非法并**抛错**，整个渲染直接失败（不是降级），属线上偶发故障源。
- 只按裸时间戳比较，未处理时区（当前 tushare 为北京时间，一致）。

**建议**：越界改为「warning + 回退到边界内最后一根」或提供 `strict=False`；在 ADR-018 补一句 09:25 bar 的约定。

---

### D9 · `agent/store.py` 三表最小实现 + 合成 run

**决策**：逐字落 §5 冻结 DDL（含四索引）与 `agent_steps` 最小写入；`ensure_run()`（`store.py:149`）在无真实 run 时合成 `run_chart_<artifact_id>`，`session_id="chart"`、`provider="local"`、`model="chart_render"`；`status="running"` 时 `finished_at` 为 NULL；同 digest 内容寻址复用目录，只插一行 artifact。

**为什么**：验收 C3 要求每次渲染写 `agent_artifacts`，而真实 run 归属在 plan-20261107-008。

**风险**
- **审计语义稀释**：`agent_runs` 本应一行一次真实对话 run，现在混入「一次渲染一行」的合成 run；按 `run_id` 聚合 token/步数将不成立。
- **孤儿文件**：`run_id` 是 `ON DELETE CASCADE`，删 run 删行但**不删盘**；`data/agent/artifacts/` 单调增长（内容寻址能去重，无法回收）。
- **FK 依赖**：`PRAGMA foreign_keys=ON`，外部传 `run_id` 而不先建 run 会直接失败。
- `digest` 索引非唯一是刻意设计（同图可被多 run 引用），按 digest 查可能多行。

**建议**：plan-008 落地真实 run 时收敛 `ensure_run` 签名（`run_id` 必填或明确 `chart` 命名空间），补 `prune_artifacts(older_than, keep_digests)` 留存清理。

---

### D10 · 无标准时间列时「猜」时间列

**决策**：`chart_render.py:558` `_probe_time_frame()`：数据没有 `trade_date`/`trade_time` 时，把第一个「可被 `pd.to_datetime` 全解析的非数值列」临时补成 `trade_date` 交给唯一生产者；否则 warning + 空串（图注写「时点未知」）。

**为什么**：inline 数据常用 `day` / `date` 列名，若不认就会丢掉时点。

**风险**：`code="20240104"`、`label="2024-01-04 收盘"` 之类会被误判为时间列，导致 `data_as_of` 静默错一天，且图注看起来完全合理、难以发现。

**建议**：候选列限制在白名单（`day/date/trade_date/trade_time/time/datetime`）或要求列名含 `date`/`time`；其余一律「时点未知」——宁可缺，不要错。

---

### D11 · transforms 在渲染帧上用 pandas 手写

**决策**：`chart_render.py:302` `apply_transforms()` 显式实现白名单五算子：`filter`（gt/ge/lt/le/eq/ne/in）、`groupby`/`agg`（sum/mean/min/max/count/last，输出列名 `f"{field}_{func}"`）、`sort`、`limit`（head/tail）；缺列抛 `ChartSpecError(field="$.transforms[i].field")`；末尾 `out.attrs = dict(frame.attrs)` 供回放兜底。不执行任意 Python。

**为什么**：ADR-016 明确 v1 不支持任意 Python/join/pivot/window，且统计口径应复用既有输出而非自造。

**风险**
- **与既有统计口径的细微差异**：`groupby.mean()` 的 `ddof`、NaN 分组丢弃、排序稳定性等与 `track.py`/`factors` 的手工聚合未必逐位一致，可能出现「图上的均值 ≠ 表里的均值」。
- **列名冲突**：`field_func` 若与源数据列同名会覆盖，模型可能拿错列。
- 白名单无法表达窗口/连接，用户预期可能更高（ADR 已声明非目标）。

**建议**：把「聚合输出列名 = `<field>_<func>`」与 NaN 处理写进冻结清单 §3.3，并补一条「与 factors 同口径」的对齐用例。

---

## 3. 需要架构裁决的问题

| ID | 问题 | 选项 | 开发者倾向 |
|---|---|---|---|
| Q1 | `data_as_of` 到底是「行情时点」还是「视图时点」？ | A. 保持视图时点（现状，与快照一致）；B. 恒为行情时点，另加 `view_as_of` | A + 增加 `time_point_scope` 字段，让消费方不必猜 |
| Q2 | `agent_artifacts.meta_json` 是不可变审计记录还是可修正快照？ | A. 可修正（现状：回放按快照重算）；B. 不可变，另表记录修正 | A，但在 ADR 里写明「真实性以 digest 为准」 |
| Q3 | 渲染确定性 vs 同页多图 | A. 固定 `div_id`（现状，存在 id 冲突）；B. 由 spec+digest 派生短哈希 | B（改动小，风险更低） |
| Q4 | `panes` / `style` 是否转正为冻结字段？ | A. 保留兼容别名（现状）；B. 改验收文字删别名；C. 转正并弃用 `layers`/`theme` | A 或 B；不建议 C（迁移成本高） |
| Q5 | `chart_render → data/indicators` 的依赖边是否补进模块图？ | A. 本轮补边 + 改模块级 import；B. 留给 plan-008 | A（越晚补越贵） |
| Q6 | kaleido 是否进依赖？ | A. 可选组 + 真导出用例；B. ADR 明确 v1 不承诺 PNG | A |
| Q7 | 09:25 集合竞价 bar 如何处置？ | A. 越界抛错（现状，可能线上偶发失败）；B. warning + 回退边界内最后一根 | B |

---

## 4. 复现前提与证据

**测试命令与结果（本分支 `75c60e3`）**

| 命令 | 结果 |
|---|---|
| `pytest tests/test_agent_chart_spec.py tests/test_agent_chart_render.py tests/test_agent_store.py tests/test_data_pit_events.py -q` | 113 passed（spec 50 / render 30 / store 10 / data 23） |
| `pytest tests -q` | 471 passed, 1 skipped, 2 subtests passed（26.43s） |
| `mypy astock` | Success: no issues found in 34 source files |
| `pytest tests/test_architecture_gate.py -q` | 32 passed, 1 skipped |
| CI（GitHub Actions） | run 43402817240 / 43405286628 → success |

**注意**：根目录直接 `pytest` 会误收 `release/AStockAgent/test_ma.py`，造成同名模块冲突（`ModuleNotFoundError: No module named 'astock.agent'`），全量统一跑 `pytest tests`。

**artifact 复现的两个前提（现状）**
1. 相同 plotly 版本（6.7.0 与 7.1.0 输出的 doctype 前缀不同，字节即不同）；
2. 相同 `include_plotlyjs` 设置（`inline` 约 4.8 MB，`cdn` 依赖外网）。

---

## 5. 附录：相关代码位置

| 位置 | 内容 |
|---|---|
| `astock/data.py:192` | `_normalize_adj`（qfq/hfq/None 归一，未知值抛错） |
| `astock/data.py:735` | `latest_bar_time`（唯一时点生产者，两段交易时段校验） |
| `astock/agent/chart_spec.py:449` | `_parse_range_date`（非法日历日期 → `$.data.range` 结构化错误） |
| `astock/agent/chart_render.py:72` / `:1146` | `FIGURE_DIV_ID` 与 `to_html(..., div_id=…)` |
| `astock/agent/chart_render.py:302` | `apply_transforms`（白名单五算子） |
| `astock/agent/chart_render.py:521` | `_time_points`（显式参数 → 重算 → attrs 兜底） |
| `astock/agent/chart_render.py:558` | `_probe_time_frame`（时间列猜测，D10） |
| `astock/agent/chart_render.py:1019` / `:1030` | `_latest_position` / `data_snapshot`（截断保留最新 bar） |
| `astock/agent/chart_render.py:1091` | `render`（入口，落 artifact + 可选 PNG） |
| `astock/agent/store.py:149` | `ensure_run`（合成 run，running 不写 finished_at） |
| `docs/AStockAgent_interface_freeze_v1.md` §3.2.1 | 兼容别名（panes/style/raw） |

---

## 6. 按 Q1–Q7 倾向实施后的状态（2026-11-16 开发回填）

按本文档 §3 的开发者倾向落地，提交见 PR #12 后续 commit；架构裁决若与下表不同，以裁决为准再改。

| 问题 | 采纳 | 落地位置 |
|---|---|---|
| Q1 时点语义 | A + `time_point_scope` | `chart_render.py` `RenderedFigure.time_point_scope`（market/view/unknown），写入 `meta_json` 与 `ToolResult.metadata` |
| Q2 meta 真实性 | A（可修正，真实性看 digest） | 冻结清单 §5 已写明「产物真实性以 `digest` 为准」；回放按快照重算 |
| Q3 div id | B（spec + 快照派生） | `chart_render.py::figure_div_id()` → `astock-chart-<8 hex>`；`meta["div_id"]` 可核验 |
| Q4 panes/style | ~~A~~ → **架构裁决 2026-11-14：B（删除别名）** | 本轮保留别名（v1 现状）并注明「新写入用规范名」；删除动作在 plan-20261114-002 |
| Q5 模块图边 | ~~A~~ → **架构裁决 2026-11-14：C（完整版）** | 本轮已补 `chart_render→data/indicators` 边并删除延迟 import；`chart_render` 不写审计 + `chart_service`/`ArtifactReader` 在 plan-20261114-002 |
| Q6 kaleido | ~~A~~ → **架构裁决 2026-11-14：A（v1 支持）** | 本轮提供缺依赖即 skip 的真实导出用例 `tests/test_agent_chart_png_export.py`；版本锁定/Chrome/字体/CI 在 plan-20261114-001 |
| Q7 09:25 bar | B（默认跳过 + 说明） | `latest_bar_time(df, *, freq, strict=False, notes=None)`；跳过时往 `notes` 追加说明，`strict=True` 仍抛错 |

顺带落地：`store.prune_artifacts()`（留存清理，仅 `Path.unlink`/`Path.rmdir`）、`params` 记录 `plotly_version`/`python_version`、`_probe_time_frame` 时间列白名单（D10 收口）。

**ADR-016 补充裁决（2026-11-14）与本轮的关系**：Q4=B、Q5=C、Q6=A 的完整落地分别拆到 `plan-20261114-002`（别名清理 + 渲染/审计边界）与 `plan-20261114-001`（PNG 基础设施）。plan-20261107-005 只交付 v1 渲染管线，不在这条分支上改审计模型与依赖锁定，避免在无生产调用方时二次返工。

---

*本文档由开发角色整理：§1–§5 记录决策与风险，§6 回填实施状态。裁决结果请回写 ADR-016 / ADR-018 与冻结清单；若裁决与 §6 不一致，开发侧再出一次修复提交。*
