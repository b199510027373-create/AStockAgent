# AStockAgent 接口冻结清单 v2 草案（图表 meta / 文件 meta / `agent_*` 留存）

> **状态：草案、未实现。** 目标 plan-20261114-002（审计入口与 v2 模型迁移），
> **不作为当前实现依据**；`main` 上的实现与验收一律以
> `docs/AStockAgent_interface_freeze_v1.md` 的 v1 正文为准。
>
> 设计全文见 `docs/AStockAgent_chart_audit_v2_change_spec.md`（Q1/Q2/Q3/Q5/D9 模型，706 行）；
> 本文件只抽出与该变更规格冲突的**接口冻结条目**，供 v2 升版时整段替换。
> 2026-11-14 曾把这些条目就地写进 v1 冻结清单，导致「冻结清单声明 v2、代码仍是 v1」的口径冲突
> （评审 rev-20261113-003 第 5 轮 P1）。现在它们只存在于本草案文件里。

## 3.4 图表 artifact 的 meta（v2 草案）

- `render(spec) -> RenderedFigure{html: str, png_path: str|None, artifact_id: str, warnings: list[str]}`（签名不变）；
- 产物落 `agent_artifacts`，`meta_json` 只存小型 provenance：`{data_as_of, data_time, view_as_of, view_time, source, params, spec, code_version, plotly_version, python_version, degraded, warnings, png_path, data_digest}`；5000 行数据快照落独立 `data.json`，不进 meta；
- `data_as_of`/`data_time` 固定为**行情时点**（transforms 前），`view_as_of`/`view_time` 为 transforms 后图上的最后一根；图注同时展示，transforms 聚合掉时间列时 `view_*` 可空、写“视图时点未知”，但行情时点必须保留；不再使用 `time_point_scope` 单字段双义；
- 复现前提：同一 `spec`、同一份数据快照、**同一 plotly/python 版本与 `include_plotlyjs` 设置**。`params` 记录版本；规范 HTML 的 div id 固定，`embed_html` 为每个嵌入实例分配唯一 `astock-chart-<8 hex>`，不同图/重复嵌入不共享 id；
- 同一 `artifact_id` 重复渲染时**不覆盖 meta**：追加 `agent_artifact_versions`（`meta_digest`/`env_version`），修正走 `corrections`（actor/理由/旧值/新值/时间），行数不合并；旧 `as_recorded` 与 `recomputed` 回放分离，不一致只给 warning。

## 4. 文件 `extracted.jsonl` 的 `bbox` 语义

**不属于 v2 草案**：`bbox` 的两类坐标（PDF 页面坐标 / 表格行列坐标）已在 `main` 的
`astock/agent/files.py` 与 v1 冻结清单 §4 实现，是 v1 生效口径；本文件不再重复声明，
v2 只保留下方真正未实现的 DDL / meta 变更。

## 5. `agent_*` 三表 DDL（v2 覆盖草案）

> 以下取代 v1 的 `agent_artifacts.run_id NOT NULL` / 合成 run 模型：artifact 去 `run_id`，
> 新增 `agent_artifact_versions`、`agent_run_artifacts` 引用表与 `corrections`；
> 直接渲染 `link.source=render`、`run_id` 可空；行永久保留、只 GC 无引用文件。
> 实现以 plan-20261107-008 的 v2 acceptance 为准（当前 `main` 仍是 v1 DDL）。

## 7. 留存清理与真实性口径（v2 草案）

`prune_artifacts` 只删 `created_at < before` 且**无引用**的 HTML/PNG 文件与空目录（只用 `Path.unlink`/`Path.rmdir`）；
`agent_artifacts`/`agent_artifact_versions`/`agent_run_artifacts` 行**永久保留**，不按保留期删行。
`meta_json` 不可变，修正只追加 `corrections`；产物真实性以 `content_digest + meta_digest` 为准。
