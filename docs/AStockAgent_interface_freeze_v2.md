# AStockAgent 接口冻结清单 v2（2026-11-19，plan-20261114-002 生效）

> 范围：在 v1（`docs/AStockAgent_interface_freeze_v1.md`）之上，冻结 2026-11-14 裁决的
> 图表时点语义（Q1）、meta/provenance 与 correction（Q2）、artifact 逻辑身份与 `div_id`（Q3）、
> 别名收口（Q4）、审计写入唯一入口（Q5/D9）、集合竞价与盘后 bar（Q7）、
> 时间列契约（D10）、transforms 统计口径（D11），以及 `table_chart` 表格图表的
> 权限/引用/PNG/UI 导出契约（§3.5，plan-20261124-001）。
> 设计全文：`docs/AStockAgent_chart_audit_v2_change_spec.md`（724 行）。
> 性质：本文件自 2026-11-19 起是图表/审计链路的**实现依据**；v1 正文对未涉及条目继续有效。
> 任何签名、字段、枚举、表结构、目录布局的改动必须提新 ADR 并推进到 v3。
>
> 与 v1 的关系：v1 §3.4（`render(spec) -> RenderedFigure{...}`、`meta_json` 单版本、
> `time_point_scope`）由本文件 §3.4 取代；v1 §5 的 `agent_artifacts.run_id NOT NULL`
> 与合成 run 模型已由 plan-20261107-008 的 v2 DDL 取代（本文件 §5 记口径）。

## 3.4 图表 artifact 的渲染、meta 与时点

- **唯一入口**：`astock/agent/chart_service.py::render_chart(spec, *, frame=None, resolver=None,
  conn=None, db_path=None, artifact_root=None, run_id=None, step=None, session_id=None,
  code_version=None, data_as_of=None, data_time=None, view_as_of=None, view_time=None,
  include_plotlyjs="inline", export_png=True, png_mode=None, source=None, reason=None,
  actor=None, created_at=None) -> RenderedFigure`；
  `astock/agent/chart_render.py::render_figure(...)` 只产出字节与 meta 草稿，
  **不 import `astock.agent.store` 的写路径**（只允许 `PNG_DEGRADED_WARNING_PREFIX` 与
  `compute_env_fingerprint` 两个纯函数）。
- `RenderedFigure` 字段：`html, png_path, artifact_id, warnings, path, digest, data_as_of,
  data_time, view_as_of, view_time, session, source, spec, params, degraded, run_id, code_version,
  artifact_version, meta_digest, meta, snapshot, png_bytes, legacy`；不再有 `time_point_scope`。
- `data_as_of`/`data_time` 固定为**行情时点**（transforms 之前，`astock/data.py::latest_bar_time`）；
  `view_as_of`/`view_time` 为 transforms 之后图上最后一根，可为空。图注同时展示两对时点：
  `行情 2024-01-04 15:00 · 视图截止 2024-01-03` / `行情 2024-01-04 15:00（视图一致）` /
  `行情 2024-01-04 15:00 · 视图时点未知（聚合后无时间列）` / `行情时点未知 · 视图 2024-01-03`；
  v1 旧记录写作 `旧版记录时点 2024-01-04 15:00（语义未区分）`。
  「视图时点未知」的括号原因由 `RenderedFigure.view_unknown_reason` 显式区分：只有 transforms
  真的丢了时间列才写「（聚合后无时间列）」；`data.time_semantics="none"` 写
  「（声明 time_semantics=none）」；其余情况不带括号原因。
- `run_id` 只接受**已存在**的 `run_kind='agent'` run（由 `AgentRuntime` 先 `ensure_run`），
  不存在直接 fail-fast——`chart_service` 不创建 run；成功渲染不改 run 状态（生命周期归 runtime
  的 `finish_run`），只有 PNG 硬失败才把该 run 标 `failed`。Agent 循环通过
  `astock/agent/registry.py::tool_context(run_id, step)` 把 `(run_id, step)` 传给工具，
  `kline` 等工具据此落 `source='agent'` 的 link。
- `ToolResult.data_as_of`/`data_time`、`metadata["freshness"]`/`["session"]` 仍然只读行情时点；
  `runtime.py`/`gateway.py` 的文案不变。视图时点只出现在 `metadata["view_as_of"]`/`["view_time"]`。
- `meta_json` 只存小型 provenance（`schema_version="chart_meta_v2"`、`spec`、`params`、
  `data_*`、`view_*`、`session`、`source`、`code_version`、`plotly_version`、`python_version`、
  `degraded`、`warnings`、`png_*`、`data_digest`、`data_blob`、`data_rows`、`data_truncated`）；
  最多 5000 行数据快照落独立 `data.json`，**不进 meta**；列级时间语义由
  `data.time_field` / `data.time_semantics`（`bar_time`/`trade_date`/`none`）显式声明。
- 复现前提：同一 `spec`、同一份数据快照、同一 `plotly`/`python`/`code` 版本与
  `include_plotlyjs` 设置。规范 HTML 的 `div_id` 固定为 `astock-chart`；
  `chart_render.embed_html(html, div_id)` 为每个嵌入实例分配唯一 id（正则
  `^[A-Za-z][A-Za-z0-9_-]{0,63}$`，替换不到目标 id 直接报错），UI 侧用
  `astock-chart-<8 hex>`；同一 artifact 重复嵌入必须是两个不同 id。
- 同一 `artifact_id` 重复渲染**不覆盖 meta**：追加 `agent_artifact_versions`（`meta_digest` /
  `reason` / `actor`）；修正只能追加 `corrections`（actor / 理由 / 旧值 / 新值 / 时间），
  且只允许维护脚本 `tools/artifact_admin.py` 或管理员执行，Agent/UI 没有入口。
  回放 `as_recorded` 与 `recomputed` 分离：不一致只给 warning，不静默覆盖记录；
  回放同时用库内 `meta_digest` 校验 `meta_json`（绕过 correction 直接改库会被发现并拒绝回放，
  抛 `ArtifactReplayUnavailable`）。

## 3.5 `table_chart` 表格图表契约（plan-20261124-001，2026-11-24 冻结）

> 性质：`table_chart` 的权限、聚合口径、引用、artifact 审计与 UI 导出语义以本节为准；
> v1 §3.4 的权限表继续有效，但 metadata 以本节字段为准（新增 `png_path`/`warnings`/`title`）。

- **签名**：`astock/agent/tools/file_tools.py::table_chart(file_id, sheet, columns,
  group_by, metric, *, agg="sum", chart_type="bar", title=None, limit=50, root=None,
  audit_db_path=None, artifact_root=None, timeout_seconds=TABLE_TIMEOUT_SECONDS)
  -> ToolResult`。
- **权限**：只申请 `file:read` + `chart:render`（`build_file_registry` 的 ToolSpec 冻结），
  不新增写权限；工具不能写 Excel 源文件，也不能绕过 `ArtifactReader` 读其他文件。
  `columns` 必须 1–20 个、`group_by`/`metric` 必须在 `columns` 内；`agg` ∈
  `sum|avg|count|min|max`，`chart_type` ∈ `bar|line`，`limit` ∈ 1..`MAX_QUERY_LIMIT`。
- **聚合 SQL**：只允许 SELECT，强制 `LIMIT` 与 5s 超时（`sql_guard`）；聚合表达式为
  `agg(CAST(metric AS DOUBLE))`，按 `group_by` 分组并按分类升序，输出
  `[{"category": str, "value": float|int|None}]`；空结果返回成功但不生成 figure/artifact，
  文案明确「没有可绘制的数据」。
- **ChartSpec**：内联 `data.source="inline"`，`data.rows` 即聚合结果，`encode={"x":"category",
  "y":"value"}`，`data.period="day"`、`data.range="1y"`、`data.adj="none"`、
  `data.time_semantics="none"`（分类轴不是时间轴），`annotations=[]`、`meta` 记录
  `file_id`/`sheet`/`citation`/`group_by`/`metric`/`agg`。`title` 截断到 120 字符。
- **引用**：`citation` 固定为 `《文件名》工作表「sheet」A1:B<n>`（含表头与全部数据行），
  `citations=[citation]`，`source="file:<文件名>"`；回答必须原样带该引用，审计可回查
  `file_id`/`sheet`/聚合口径。
- **审计**：`table_chart` 只调用 `chart_service.render_chart` 落 artifact；meta 走
  `chart_meta_v2`，`agent_artifacts` / `agent_artifact_versions` / `agent_run_artifacts`
  由 chart_service 写。Agent 循环内经 `ToolContext` 落 `source="agent"` + 真实
  `run_id`/`step`；直接调用落 `source="render"`、`run_id=NULL`。`db_path=None` 时用
  `:memory:`，不触碰 `DEFAULT_DB_PATH`。artifact 行 append-only，修正只走
  `tools/artifact_admin.py correct`。
- **PNG**：固定 `export_png=True, png_mode="auto"`；有 kaleido/Chrome 时 metadata
  `png_path` 为真实绝对路径、版本 meta 带 `png_path`/`png_digest`；不可用时
  `png_path=""` 且 `warnings` 说明降级，工具仍成功、HTML artifact 仍可审计。
- **ToolResult.metadata 冻结字段**：`file_id`、`file`、`sheet`、`citation`、`citations`、
  `title`、`chart_spec`、`artifact_id`、`artifact_version`、`artifact_path`、`png_path`、
  `data_as_of`、`data_time`、`warnings`。`figure` 为 chart_service 产出的完整 HTML。
- **会话 UI**：`collect_figures` 同时识别 `metadata["chart"]`（K 线）与
  `metadata["chart_spec"]`（table_chart）；图表区展示 iframe、spec/来源/PNG 名称；
  「图表导出」区提供本轮每个 figure 的 HTML（从内存写入 `%TEMP%/astock-agent-exports`）与真实 PNG 文件下载；
  该目录必须通过 `agent_export_allowed_paths()` 加入 Gradio `allowed_paths`，否则 `/file=` 下载返回 403；
  PNG 缺失时只给降级说明，不让整轮回答失败。
- **评测**：xlsx-005 保持 `figure_contains`（`plotly-xlsx-005`、`bar 2026 monthly sales`）
  与 `chart_spec`（`kind=bar`、`data.source=inline`、`encode.x=category`、
  `encode.y=value`）断言，不得退化为纯文本断言。

## 5. `agent_*` 表结构口径（v2 实现口径，plan-20261107-008 已落地）

- `agent_artifacts`（artifact 身份 / path / content_digest / env_fingerprint / created_at）；
- `agent_artifact_versions`（`PK(artifact_id, version)`，`meta_json` / `meta_digest` / `reason` /
  `actor` / `created_at`，append-only）；
- `agent_run_artifacts`（`link_id` 自增、`run_id` 可空且 `ON DELETE CASCADE`、`source` ∈
  `agent|render|import|legacy`）；`agent_artifact_links` 仅作为只读兼容视图；
- `corrections`（`artifact_id` / `artifact_version` / `field` / `old_value` / `new_value` /
  `reason` / `actor` / `created_at`）；
- `agent_runs.run_kind` ∈ `agent|render|legacy`；直接渲染**不建合成 run**（无 `run_chart_*`），
  link 的 `run_id` 为 `NULL`、`source='render'`；真实 Agent run 由 `ensure_run`/`finish_run` 成对维护；
- 触发器 `trg_agent_artifacts_no_update` / `trg_agent_artifact_versions_no_update` 拒绝 UPDATE。

## 7. 留存清理与真实性口径

`prune_orphan_blobs` 只删**没有被任何 `agent_artifacts.path` 引用**、且超过保留期（默认 90 天）
的 blob 文件与空目录，默认 dry-run；`agent_artifacts` / `agent_artifact_versions` /
`agent_run_artifacts` 行**永久保留**（仅随 run 删除而删 link）。产物真实性以
`content_digest + meta_digest` 为准。

## 8. 时间列契约（D10）

`encode.x` 或 `data.time_field` 必须指向**可证明**的时间列：datetime，或全部取值匹配
`YYYY-MM-DD[ HH:MM[:SS]]` 的字符串（Asia/Shanghai）。字符串代码（`20240104`）、文本标签
（`2024-01-04 收盘`）与纯数字永不被猜成时间；无法证明时抛 `ChartSpecError`，不出图。
声明 `time_semantics="none"` 表示确实没有时间轴：图注写「视图时点未知」且**不编造** `data_as_of`。

## 9. transforms 的统计口径（D11）

`filter/groupby/sort/limit` 不重写统计语义：分组聚合统一走
`astock/stats.py::aggregate_groups`（与表口径同源，`docs/AStockAgent_chart_audit_v2_change_spec.md`
§15.2）。空组、全 NaN 组、NaN 分组键都保留并给 warning；排序稳定；同名输出加数字后缀并 warning；
图上均值必须与表一致。

## 10. 集合竞价与盘后 bar（Q7）

`astock/data.py` 新增 `classify_bar_time` / `bar_sessions` / `continuous_bars` / `validate_bar_time`；
`latest_bar_time` 接受 09:25 集合竞价与 15:00 后固定价 bar，并在 `notes` 标注 `session=pre|post`，
`strict=True` 只在需要时硬失败。K 线照常画这些 bar，`meta["session"]` 与
`ToolResult.metadata["session"]` 如实标注；MA/BOLL/MACD/RSI/KDJ 在**分钟周期**只用连续竞价 bar，
日/周/月线（`session="close"`）全量参与计算——指标样本为空时必须写
「连续指标无可用 bar」warning，不允许静默全 NaN；
只有竞价数据时指标为空、图注写「盘前，暂无连续竞价数据」，**不回退前一日**。
