# 图表时点、artifact 审计与渲染事件模型：v2 改造说明

> 状态：设计定稿，**只写文档，未改代码**  
> 日期：2026-11-14  
> 范围：Q1（时点语义）、Q2（meta 不可变）、Q3（artifact 身份与 div_id）、Q7（09:25 集合竞价）、D9（合成 run / 审计污染）  
> 关联：`docs/AStockAgent_chart_pipeline_decisions_v1.md`、`docs/AStockAgent_interface_freeze_v1.md`、ADR-016、ADR-017、ADR-018  
> 代码基线：PR #12 / `75c60e3`，`pytest tests -q` 471 passed，`mypy astock` 34 files clean

---

## 0. 一句话结论

把「图上看到的时点」和「行情数据截止时点」拆成两组字段；把 artifact 的**内容**、**元数据版本**、**被谁引用**拆成三张表；渲染不再伪造 Agent run；同页多图在展示层分配唯一 `div_id`；09:25 集合竞价 bar 从「抛错」改为「接受 + 标注 + 连续指标排除」。

这次改造不改变主业务（数据、推荐、回测、跟踪、调度）的取数与计算逻辑，影响集中在 `astock/agent/store.py`、`astock/agent/chart_render.py`、`astock/data.py` 的时点函数，以及 UI 的图注/嵌入层。

---

## 1. 背景与问题

v1 的图表管线已经能出图、落 artifact、按内容寻址去重，但有五个语义问题：

| 问题 | 现状 | 后果 |
|---|---|---|
| Q1 时点双义 | `data_as_of` 按 transforms 后的图数据重算 | `filter` 掉最新 bar 后，图注写旧日期；Agent 的「数据截至」也跟着错 |
| Q2 meta 可变 | `agent_artifacts.meta_json` 可被 UPDATE；digest 只锚定 HTML | 审计回答不了「当时图注写了什么」；改 meta 无法发现 |
| Q3 div_id 固定 | 所有图都用 `astock-chart` | 同页两张图冲突；跨 plotly 版本字节不同，但 meta 不记录环境 |
| Q7 时段过严 | 09:25 / 盘后固定价 bar 直接 `ValueError` | 整张图失败，不是降级 |
| D9 合成 run | 每次渲染造 `run_chart_<artifact_id>` | `agent_runs` 指标被零步/零 token 的假 run 污染；删 run 不删盘；artifact 复用丢归属 |

v1 文档中的 D2、D3、D4、D8、D9 与本文件冲突时，以本文件为准；D1、D5、D6、D7、D10、D11 不在本次范围内（D5/D6/D7 仍是 v1 的待裁决项）。

---

## 2. 目标与非目标

### 2.1 目标

1. `data_as_of` / `data_time` 永远是**行情时点**，不受 transforms 影响。
2. 图上实际展示的最后一根用 `view_as_of` / `view_time` 表达，图注同时展示两者。
3. `meta_json` 成为不可变 provenance：修正只能追加版本，不能 UPDATE。
4. artifact 的逻辑身份、HTML 内容、DOM 实例三者解耦。
5. 直接渲染不创建 `agent_runs`；内容与「被哪个 run/会话引用」分开记录。
6. 09:25 集合竞价、盘后固定价 bar 可渲染，标注 session，连续指标默认排除。
7. 旧库可迁移；迁移可回滚；旧 artifact 的语义不被伪装成新语义。

### 2.2 非目标

- 不做 tick 级实时；实时口径仍是 `rt_min` 最新一根 + 30s 缓存（ADR-018）。
- 不做扫描件 OCR；文件导入仍只支持文本层 PDF / XLSX / XLS / CSV / DOCX / TXT。
- 不执行任意 Python；图表仍走声明式 ChartSpec。
- 不重构图表渲染引擎，不引入 pandas 之外的统计框架。
- 不解决 Q4（panes/style 别名）、Q5（模块图补边）、Q6（kaleido 依赖）——这三项仍按 v1 文档单独裁决。
- 不删除旧数据；迁移以保留、标注、可回放为优先。

---

## 3. Q1：时点语义

### 3.1 字段定义

| 字段 | 含义 | 计算位置 | 是否可空 |
|---|---|---|---|
| `data_as_of` | 行情数据的交易日 | transforms **之前**的源 frame，调 `latest_bar_time()` | 否；源数据无时间列时为空串并 warning |
| `data_time` | 行情数据的时刻 | 同上 | 否；日线固定 `15:00` |
| `view_as_of` | 图上实际展示的最后一根的交易日 | transforms **之后**的落图 frame | 是；聚合掉时间列时为空 |
| `view_time` | 图上实际展示的最后一根的时刻 | 同上 | 是 |

**不再引入** `time_point_scope`。一个字段只表达一件事，消费方不需要猜。

### 3.2 计算顺序

```text
源数据 frame
  └─ latest_bar_time(source)        -> data_as_of / data_time      （行情时点）
       └─ apply_transforms(frame)   -> chart frame
            └─ latest_bar_time(chart frame) -> view_as_of / view_time（视图时点）
```

显式参数仍然优先，但只覆盖对应的一组：

- 显式 `data_as_of` / `data_time`：只覆盖行情时点。
- 显式 `view_as_of` / `view_time`：只覆盖视图时点。
- 不允许用一组显式参数同时改两组语义。

### 3.3 图注

`RenderedFigure.caption` 统一生成，UI 和 Agent 文本都复用它：

| 场景 | 图注 |
|---|---|
| 两者都有且不同 | `行情 2024-01-04 15:00 · 视图截止 2024-01-03` |
| 两者相同 | `行情 2024-01-04 15:00（视图一致）` |
| 视图时点未知 | `行情 2024-01-04 15:00 · 视图时点未知（聚合后无时间列）` |
| 行情时点未知 | `行情时点未知 · 视图 2024-01-03` |
| 旧版 artifact | `旧版记录时点 2024-01-04 15:00（语义未区分）` |

### 3.4 下游语义

- `ToolResult.data_as_of` / `data_time`：保持**行情时点**。
- `ToolResult.metadata["freshness"]`、`["session"]`：继续用行情时点。
- 新增 `ToolResult.metadata["view_as_of"]`、`["view_time"]`：可选，仅用于展示；不改 `ToolResult` 的冻结字段。
- `astock/agent/runtime.py:502`、`astock/agent/gateway.py:789` 拼接「数据截至」时仍用 `data_as_of`，不需要改语义。
- `RenderedFigure` 新增 `view_as_of`、`view_time`；`run_id` 改为可选（直接渲染为 `None`，不再合成）。
- `RenderedFigure.metadata()` 增加 `view_as_of`、`view_time`、`artifact_id`、`artifact_version`、`meta_digest`；不把 `run_id` 放进 UI metadata。

### 3.5 artifact 回放

回放分两种模式，不能混：

| 模式 | 数据来源 | 时点来源 | 用途 |
|---|---|---|---|
| `as_recorded` | 该 link 指向的 HTML + meta version | 原样读取 meta，不重算 | 审计「当时用户看到什么」 |
| `recomputed` | 快照数据 + 当前代码 | 重算 `view_*`；`data_*` 仍取 meta | 检查代码/数据变化后的差异 |

**关键约束**：`recomputed` 不能用 transforms 后的快照反推行情时点。快照是落图数据，filter 可能已经丢掉最新 bar；行情时点是 provenance，只能来自渲染时记录的 meta。这是对 v1 D3 的修正：

- v1 D3：回放按快照时间列重算 `data_as_of`，不信任旧 meta。
- v2：`view_*` 按快照重算；`data_*` 信任 meta，缺失时 warning，不静默编造。
- 如果快照缺少时间列，`recomputed` 的 `view_*` 为空并 warning；`as_recorded` 不受影响。

### 3.6 旧 meta 的兼容

旧 artifact 的 `data_as_of` 是 v1 的「视图时点」，不能直接当行情时点：

- `meta_json.schema_version` 缺失 → 标 `legacy`。
- `data_as_of` 保留原值，图注标「旧版记录时点（语义未区分）」。
- `view_as_of` 不伪造；需要时由 `recomputed` 从快照算。
- 新写入的 meta 一律 `schema_version="chart_meta_v2"`。

### 3.7 meta 字段（v2）

```json
{
  "schema_version": "chart_meta_v2",
  "spec": { "...": "规范化后的 ChartSpec" },
  "params": {
    "adj": "qfq",
    "range": "1y",
    "interval": "1d"
  },
  "data_as_of": "2024-01-04",
  "data_time": "2024-01-04 15:00",
  "view_as_of": "2024-01-03",
  "view_time": "2024-01-03 15:00",
  "source": "market",
  "data_digest": "sha256:...",
  "data_rows": 245,
  "data_truncated": false,
  "code_version": "0.1.0+git.75c60e3",
  "plotly_version": "6.7.0",
  "python_version": "3.11.9",
  "degraded": false,
  "warnings": [],
  "session": "continuous"
}
```

- 不再把 5000 行 `data` 快照放进 `meta_json`；快照落 `data.parquet`，meta 只存 `data_digest`、行数、是否截断。
- `meta_json` 必须 canonical JSON（键排序、无多余空格），否则 `meta_digest` 不稳定。

---

## 4. Q2：artifact 不可变审计

### 4.1 模型：内容 / 版本 / 引用分离

```text
agent_artifacts          内容身份（一行一个逻辑 artifact，不可 UPDATE）
  └─ agent_artifact_versions   元数据版本（v1 创建，修正追加 v2/v3，不可 UPDATE）
       └─ agent_artifact_links 被谁引用（run / 直接渲染 / 导入；run_id 可空）
```

`agent_runs` 只保留真实 Agent 会话 run；直接渲染不再造 run。

### 4.2 DDL

```sql
CREATE TABLE agent_artifacts (
    artifact_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('chart','table','file','image')),
    path            TEXT NOT NULL,
    content_digest  TEXT NOT NULL,
    env_fingerprint TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX ix_agent_artifacts_digest
    ON agent_artifacts(content_digest);
CREATE INDEX ix_agent_artifacts_created
    ON agent_artifacts(created_at);

CREATE TABLE agent_artifact_versions (
    artifact_id  TEXT NOT NULL
                 REFERENCES agent_artifacts(artifact_id) ON DELETE RESTRICT,
    version      INTEGER NOT NULL CHECK (version >= 1),
    meta_json    TEXT NOT NULL,
    meta_digest  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (artifact_id, version)
);

CREATE TABLE agent_artifact_links (
    link_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id      TEXT NOT NULL
                     REFERENCES agent_artifacts(artifact_id) ON DELETE RESTRICT,
    artifact_version INTEGER NOT NULL,
    run_id           TEXT
                     REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    step             INTEGER,
    source           TEXT NOT NULL
                     CHECK (source IN ('agent','render','import','legacy')),
    session_id       TEXT,
    created_at       TEXT NOT NULL,
    FOREIGN KEY (artifact_id, artifact_version)
        REFERENCES agent_artifact_versions(artifact_id, version)
        ON DELETE RESTRICT
);

CREATE INDEX ix_artifact_links_run
    ON agent_artifact_links(run_id);
CREATE INDEX ix_artifact_links_artifact
    ON agent_artifact_links(artifact_id);
```

说明：

- `agent_artifacts` 没有 `run_id`、没有 `meta_json`，因此不存在「同一 artifact 被第二次引用时覆盖第一次归属」的问题。
- `agent_artifact_links.run_id` 可空：直接渲染 `NULL, source='render'`；文件导入 `NULL, source='import'`；真实 Agent 调用填 `run_id, step, source='agent'`。
- 删 run 只级联删 link，artifact/version/文件保留；指标查询不会被渲染记录污染。
- `agent_artifact_links` 不设 `UNIQUE` 约束（NULL 在 SQLite 唯一索引中不互相冲突），幂等由写入函数保证。

### 4.3 不可变触发器

```sql
CREATE TRIGGER trg_agent_artifacts_no_update
BEFORE UPDATE ON agent_artifacts
BEGIN
    SELECT RAISE(ABORT, 'agent_artifacts is immutable');
END;

CREATE TRIGGER trg_agent_artifact_versions_no_update
BEFORE UPDATE ON agent_artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'agent_artifact_versions is append-only');
END;
```

- 允许 DELETE 仅用于显式的保留期清理；默认不自动删 artifact/version 行。
- 测试必须覆盖：`UPDATE agent_artifacts SET ...` 和 `UPDATE agent_artifact_versions SET ...` 都抛 `sqlite3.IntegrityError`。
- 现有 `tests/test_agent_chart_render.py:504` 直接改 meta 的测试要改成「改 meta 必须走 correction，直接 UPDATE 失败」。

### 4.4 身份与 digest

```text
canonical_spec = canonical_json(spec)
env_fingerprint = sha256(
    f"plotly={plotly.__version__}\n"
    f"python={platform.python_version()}\n"
    f"code={CODE_VERSION}"
).hexdigest()[:16]

artifact_id = "art_" + sha256(
    canonical_spec + "\n" + data_digest + "\n" + env_fingerprint
).hexdigest()[:16]

content_digest = "sha256:" + sha256(canonical_html.encode("utf-8")).hexdigest()
meta_digest    = "sha256:" + sha256(canonical_json(meta).encode("utf-8")).hexdigest()
```

- `artifact_id` 保持 `art_[0-9a-f]{16}`，与 `chart_spec.py:489` 的校验一致。
- `env_fingerprint` 进 artifact id：plotly 6.7 与 7.1 输出不同，本来就该是两个 artifact，不能假装同 id。
- `content_digest` 锚定**规范 HTML**，不是嵌入后的 HTML（见 Q3）。
- `CODE_VERSION` 不再硬编码 `"astock-0.1.0"`；从包版本或 `git rev-parse --short HEAD` 生成，取不到时写 `"unknown"`。

### 4.5 修正（correction）

```python
correct_artifact(
    conn,
    artifact_id: str,
    patch: dict,
    reason: str,
    actor: str,
) -> int  # 返回新 version
```

规则：

- 只允许修正 provenance 字段：`source`、`data_as_of`、`data_time`、`view_as_of`、`view_time`、`session`、`degraded`、`warnings`。
- 不允许修正 `spec`、`data_digest`、`content_digest`、`artifact_id`；这些变了就是新 artifact，走「新增 artifact + link 指向新 id」。
- 实现：读当前最大 version 的 meta → 应用 patch → 写 `version+1`，`reason` 必填、`actor` 必填；原 version 不动。
- `as_recorded` 用 link 上的 `artifact_version`，所以修正不会改写历史回放；只有显式选择新 version 才看到修正。

### 4.6 回放 API

```python
load_artifact(conn, artifact_id, *, version=None, mode="as_recorded")
```

| 模式 | 行为 |
|---|---|
| `as_recorded` | 取 link 指定 version（无 link 取最新）；读 HTML；校验 `content_digest`；返回原图 + 原 meta。文件缺失/digest 不符直接报错，不降级。 |
| `recomputed` | 需要 `data_digest` 快照；用当前代码重渲染；返回新图 + 差异 warning；不覆盖原 artifact。快照缺失时报 `replay_unavailable`。 |

`as_recorded` 是审计口径；`recomputed` 是诊断口径。UI 默认 `as_recorded`，修复/对比工具用 `recomputed`。

### 4.7 快照与文件布局

```text
data/agent/artifacts/blobs/<sha256>.html       # 规范 HTML，内容寻址
data/agent/artifacts/blobs/<sha256>.parquet    # 落图数据快照（可选）
data/agent/artifacts/blobs/<sha256>.png        # 可选 PNG
```

- 多个 `agent_artifacts` 行可以共享同一个 blob path（内容相同），`content_digest` 索引非唯一。
- meta 只存 `data_digest` / `data_rows` / `data_truncated`，不存 `data` 大数组。
- 快照保留最新 5000 行；`view_*` 在渲染时从完整 frame 算，所以截断不影响图注。
- GC 只删「没有任何 artifact 行引用」的 blob；默认保留 90 天，先 dry-run。

---

## 5. Q3：artifact 身份与 div_id

### 5.1 三层身份

| 层 | 标识 | 作用 |
|---|---|---|
| 逻辑 artifact | `artifact_id` | 同 spec + 同数据 + 同环境 → 同一逻辑图 |
| 内容 | `content_digest` | 规范 HTML 的字节哈希，用于去重/完整性 |
| DOM 实例 | `div_id` | 同页每张图唯一，只影响展示，不影响 artifact 身份 |

### 5.2 规范 HTML 与嵌入

- `render()` 仍用固定 `FIGURE_DIV_ID = "astock-chart"` 生成**规范 HTML**，保证同环境同 spec 字节稳定、`content_digest` 可复现。
- 新增展示层函数：

```python
embed_html(html: str, div_id: str) -> str
```

  - 校验 `div_id` 匹配 `^[A-Za-z][A-Za-z0-9_-]{0,63}$`。
  - 把 HTML 中所有 `astock-chart` 替换为 `div_id`；替换数为 0 时抛错。
  - UI 每次嵌入生成 `astock-chart-<8hex>`，同一 artifact 两次嵌入得到两个不同 id。

- **禁止**把 artifact HTML 原样重复插入同一页面；Gradio 多图必须走 `embed_html`。需要强隔离时用 `iframe srcdoc`，但默认走 id 重写。
- `as_recorded` 回放、下载、审计比较的是规范 HTML；页面上的 HTML 是展示副本，不写回 artifact。

### 5.3 版本记录

`meta.params` 增加：

```json
{
  "plotly_version": "6.7.0",
  "python_version": "3.11.9",
  "code_version": "0.1.0+git.75c60e3"
}
```

- 环境变化 → `env_fingerprint` 变 → `artifact_id` 变，不覆盖旧图。
- 复现声明改为：「同 spec + 同数据 + 同 plotly/python/code 版本 → 同 `content_digest`」。
- 需要 pin plotly 版本（`requirements.txt` 当前全是 `>=`，至少 agent 图表依赖要锁）。

### 5.4 已知代价

- `include_plotlyjs="inline"` 每图约 4.8 MB；同页多图会重复。v2 先保证正确性，不解决体积；后续可做「页面级共享 plotly bundle」。
- `embed_html` 是字符串替换，必须测试：两张图、id 含特殊字符、HTML 中没有目标 id 的报错路径。
- Plotly 若在某些 trace 上生成随机 uid，`content_digest` 会不稳定；实现时先跑两次同 spec 比对字节，若有随机量则规范化或固定 seed。

---

## 6. Q7：09:25 与盘后 bar

### 6.1 行为

`latest_bar_time(df, *, freq="D")` 保持返回 `(data_as_of, data_time)` 的签名，但：

- 分钟档不再对 `09:25`、盘后固定价 bar 抛 `ValueError`。
- 所有合法时间都接受；新增 `classify_bar_time(ts, freq)` 返回 `session`：
  - `pre`：09:15–09:29（集合竞价）
  - `continuous`：09:30–11:30、13:00–15:00
  - `post`：15:00 之后（盘后固定价等）
  - `closed`：其他/无法判断
- 时区统一 `Asia/Shanghai`；naive 时间按本地时间处理，aware 时间转换后再判断。
- `data_time[:10] == data_as_of` 的断言保留。

### 6.2 严格模式

数据质量/研究口径另提供：

```python
validate_bar_time(df, *, freq="D", strict=True)
```

- `strict=True` 时，`pre`/`post`/`closed` 抛 `ValueError`；渲染层不调用它。
- 保留给数据质量任务、回测/研究入口，避免把集合竞价混进连续竞价统计。

### 6.3 指标与图注

- K 线可以画 09:25 bar；`session=pre` 写入 meta 和 `ToolResult.metadata`。
- MA/BOLL/MACD/RSI/KDJ 默认只使用 `continuous` bar；只有 auction bar 时指标为空，图注写「盘前，暂无连续竞价数据」。
- 不回退到前一日收盘；不静默替换。
- 日线/周线/月线不受影响，`session="close"`。

### 6.4 plan-006 的边界

- plan-006 只消费 `latest_bar_time` / `classify_bar_time`，不重写时点逻辑。
- 如果 plan-006 需要「最后一根连续竞价 bar」，调用方显式过滤 `session=="continuous"`，而不是让渲染层猜。

---

## 7. D9：渲染事件模型

### 7.1 不再合成 run

删除 `chart_render` 中 `run_chart_<artifact_id>` 的创建逻辑：

- 直接渲染：`record_artifact(..., run_id=None, source="render")`，只写 artifact/version/link。
- Agent 调用：`record_artifact(..., run_id=run_id, step=step, source="agent")`，link 指向真实 run。
- 文件导入：`source="import"`，`run_id=None`。
- 旧合成 run：迁移时标 `run_kind="render"`，保留历史，但所有指标查询排除。

### 7.2 `agent_runs` 变更

```sql
ALTER TABLE agent_runs
ADD COLUMN run_kind TEXT NOT NULL DEFAULT 'agent'
CHECK (run_kind IN ('agent','render','legacy'));
```

- 新 run 默认 `agent`。
- 旧 `run_chart_%` 迁移为 `render`。
- 指标查询统一加 `WHERE run_kind='agent'`。
- `ensure_run` 只服务真实 Agent：默认 `status="running"`，不再默认 `success`；新增 `finish_run(run_id, status, finished_at, token_usage, step_count)`，禁止留下永久 `running`。
- `ON CONFLICT(run_id) DO NOTHING` 保留用于幂等启动，但冲突后必须校验 `run_kind`/`session_id` 一致，不一致报错。

### 7.3 写入 API

```python
record_artifact(
    conn,
    *,
    artifact_id: str,
    kind: str,
    path: str,
    content_digest: str,
    env_fingerprint: str,
    meta: dict,
    run_id: str | None = None,
    step: int | None = None,
    source: str = "render",
    session_id: str | None = None,
    reason: str = "render",
    actor: str = "system",
) -> tuple[str, int]:
    """写 artifact（若不存在）+ version（若 meta 新）+ link；返回 (artifact_id, version)。"""
```

行为：

1. 查 `artifact_id`；不存在则插入 artifact 行；存在则校验 `content_digest` / `env_fingerprint` 一致，不一致抛 `ArtifactIdentityConflict`。
2. 计算 `meta_digest`；查同 artifact 是否已有同 digest 的 version：
   - 有 → 复用该 version，不重复写。
   - 无 → 插入 `version = max+1`，`reason` 记录。
3. 插入 link；同一 `(artifact_id, version, run_id, source, step)` 重复调用时返回已有 link，不重复插。
4. 返回 version，供 `RenderedFigure` / ToolResult 使用。

### 7.4 删除与 GC

- 删 run：`ON DELETE CASCADE` 只删 link；artifact/version/blob 保留。
- `prune_runs(older_than_days)`：按保留期删旧 run；先删 link 再删 run，或依赖 cascade。
- `prune_orphan_blobs(older_than_days, dry_run=True)`：扫描 blob 目录，删除没有被任何 `agent_artifacts.path` 引用的文件；默认 90 天，先 dry-run。
- 不自动删 artifact/version 行；如要做保留期，另开策略，先导出审计清单。

### 7.5 指标

- `agent_runs` 总数、成功率、平均步数/token：`run_kind='agent'`。
- `agent_steps` 只对真实 run 写入；渲染不写 step。
- 旧 `run_chart_*` 保留可查，但默认报表排除；需要历史对比时显式 `include_render=True`。

---

## 8. 迁移

### 8.1 前置

1. `sqlite3 data/astock.db "PRAGMA wal_checkpoint(TRUNCATE);"`。
2. 复制备份：`data/astock.db.bak-YYYYMMDD-HHMMSS`。
3. 迁移在单事务内；`PRAGMA foreign_keys=OFF`，结束前 `foreign_key_check`。
4. 迁移脚本必须能在「没有 agent_* 表」和「v1 三表」两种库上跑。

### 8.2 步骤

```text
1. 建 agent_artifacts_v2 / agent_artifact_versions_v2 / agent_artifact_links_v2
2. agent_runs 增加 run_kind（默认 agent）
3. 旧 agent_artifacts 逐行：
   - artifact_id 保留旧 id（格式已是 art_...）
   - kind='chart'
   - path=旧 path
   - content_digest=旧 digest
   - env_fingerprint='legacy'
   - 写 version=1：
       meta = 旧 meta + schema_version='legacy'
       reason='migration', actor='migration'
       若旧 meta 含 data 大数组：抽到 data.parquet，写 data_digest/data_rows；
       否则 data_digest=NULL（只能 as_recorded）
   - 写 link：run_id=旧 run_id，artifact_version=1，source='legacy'
4. 旧 run_chart_% 的 run_kind='render'
5. 校验行数：artifacts/versions/links == 旧 artifacts 行数
6. drop 旧表，rename v2 表
7. 建索引/触发器
8. PRAGMA foreign_key_check; PRAGMA integrity_check
```

### 8.3 旧数据语义

- 旧 meta 的 `data_as_of` 标 `legacy`，不冒充行情时点。
- 旧 artifact 的 `env_fingerprint='legacy'`，不参与新 artifact 的 dedupe。
- 旧合成 run 保留，`run_kind='render'`，指标排除；不删除历史。
- 旧 artifact 无快照时，`recomputed` 返回 `replay_unavailable`；`as_recorded` 仍可用。

### 8.4 回滚

- 迁移前备份整库；迁移失败回滚事务。
- 已迁移后回滚：停写 → 用备份覆盖 → 重启；不提供自动 down migration（避免二次损坏）。

---

## 9. 代码改动清单

| 文件 | 改动 | 规模 |
|---|---|---|
| `astock/agent/store.py` | 新 schema、迁移、`record_artifact`、`correct_artifact`、`load_artifact`、link/GC、`finish_run` | 大 |
| `astock/agent/chart_render.py` | 删除合成 run；Q1 双时点/ caption；Q3 canonical HTML + embed；Q7 session；`RenderedFigure` 字段 | 中 |
| `astock/data.py` | `latest_bar_time` 放宽；新增 `classify_bar_time` / `validate_bar_time` | 小 |
| `astock/agent/runtime.py` / `gateway.py` | 不改 `data_as_of` 语义；可选把 `view_*` 放进 metadata | 小/无 |
| UI 图表嵌入处 | 用 `embed_html` 分配唯一 div_id；图注用 `caption` | 小 |
| `tests/test_agent_store.py` | 重写 schema/不可变/correction/link/GC/迁移 | 大 |
| `tests/test_agent_chart_render.py` | 双时点/caption/双图/embed/无合成 run | 中 |
| `tests/test_data_pit_events.py` | 09:25/盘后/严格模式 | 小 |
| `tests/test_architecture_gate.py` | 冻结 DDL 断言升级 | 小 |
| `docs/AStockAgent_interface_freeze_v1.md` | 新增 v2 章节或升 v2：三表→五表、时点字段、replay | 中 |
| `docs/AStockAgent_chart_pipeline_decisions_v1.md` | 加「v2 裁决记录」链接，标注 D2/D3/D4/D8/D9 已被取代 | 小 |
| ADR-016 / ADR-017 / ADR-018 | 补 Q1/Q2/Q3/Q7/D9 的最终口径 | 小 |

---

## 10. 测试与验收

### 10.1 Q1

- `filter` 掉最新 bar：`data_as_of=2024-01-04`，`view_as_of=2024-01-03`，caption 同时出现两者。
- `groupby` 聚合掉时间列：`view_*` 为空，caption 写「视图时点未知」，`data_*` 仍在。
- `ToolResult.data_as_of` 仍是行情时点；runtime 文本说 01-04，不说 01-03。
- 显式 `data_as_of` 只改行情，不改视图。

### 10.2 Q2

- 直接 `UPDATE agent_artifacts` / `agent_artifact_versions` 抛错。
- `correct_artifact` 追加 v2；v1 内容不变；`as_recorded` 旧 link 仍读 v1。
- 篡改 HTML 文件：`as_recorded` 校验 digest 失败并报错。
- `recomputed` 与 `as_recorded` 不一致时给 warning，不覆盖原图。
- 旧 meta 无 schema_version：标 legacy，不冒充行情时点。

### 10.3 Q3

- 同 spec + 同数据 + 同环境：两次渲染 `artifact_id`、`content_digest` 相同。
- 同页两张图：`embed_html` 生成两个不同 div_id，两张都渲染，无空白/覆盖。
- HTML 中没有 `astock-chart`：`embed_html` 抛错。
- meta 记录 plotly/python/code 版本；换 plotly 版本后 artifact_id 变。

### 10.4 Q7

- 09:25 bar：渲染成功，`session=pre`，caption 标「集合竞价」。
- 15:05 盘后 bar：渲染成功，`session=post`。
- 只有 auction bar：连续指标为空，caption「盘前，暂无连续竞价数据」。
- `validate_bar_time(strict=True)` 对 09:25 抛错。
- 不出现「静默回退到前一日」。

### 10.5 D9

- 直接渲染：`agent_runs` 数量不变；artifact 1 行、version 1 行、link 1 行，`run_id IS NULL, source='render'`。
- Agent 渲染：link 指向真实 run；删 run 后 link 消失，artifact/version/文件仍在。
- 旧 `run_chart_*` 迁移为 `run_kind='render'`，指标查询排除。
- 同 artifact 被两个 run 引用：两条 link，artifact/version 不重复。
- `prune_orphan_blobs` 只删无引用文件，dry-run 不删。

### 10.6 迁移

- 用 v1 fixture 建库、插 2 个 artifact、1 个合成 run，跑迁移：
  - 行数守恒；
  - `foreign_key_check` 为空；
  - 旧 meta 标 legacy；
  - 旧 run 标 render；
  - `as_recorded` 可读；
  - 无快照时 `recomputed` 报 `replay_unavailable`。

---

## 11. 实施顺序与工作量

| 阶段 | 内容 | 预估 |
|---|---|---|
| 0 | 本文件评审；ADR-016/017/018 补丁；冻结清单 v2 草案 | 0.5 天 |
| 1 | `store.py` 新 schema + 迁移 + 单测 | 1 天 |
| 2 | `chart_render.py` 接线（Q1/Q2/Q3/Q7/D9） | 1 天 |
| 3 | `data.py` 时点函数 + 测试 | 0.25 天 |
| 4 | UI caption / embed / 双图验收 | 0.5 天 |
| 5 | 全量回归、文档、迁移演练 | 0.5–1 天 |
| 合计 |  | **2.5–3.5 天** |

最小止血（只做 `run_kind` + `meta_digest` + 孤儿 GC，不改 link）约 1 天，但 artifact 复用归属仍然错，不建议单独上线。

建议一次 migration 把 Q1/Q2/Q3/D9 做完，避免同一张表迁两遍；Q7 可以独立先合，风险最低。

---

## 12. 风险与待确认

| 风险 | 影响 | 处理 |
|---|---|---|
| SQLite 不能直接改 NOT NULL | 迁移必须重建表 | 新表 + 拷贝 + rename；备份；fixture 测试 |
| 旧 artifact 的 `data_as_of` 语义不清 | 不能冒充行情时点 | 标 legacy；只保证 `as_recorded` |
| Plotly 输出含随机量 | `content_digest` 不稳定 | 同 spec 跑两次比字节；必要时规范化/固定 |
| `embed_html` 字符串替换 | 漏替换/误替换 | 全量替换 + 替换数校验 + 双图用例；必要时 iframe |
| inline plotly 4.8 MB/图 | 多图页面过大 | v2 先正确；后续页面级 bundle |
| 旧合成 run 指标 | 历史报表偏差 | `run_kind='render'` 排除；保留可查 |
| correction 权限 | 谁能改 provenance | 先限定本地单用户；actor 必填；审计日志 |
| `data_digest` 快照缺失 | 旧 artifact 不能 recomputed | 明示 `replay_unavailable`，不静默 |
| 环境版本进 artifact id | 升级 plotly 后 id 变化 | 预期行为；文档写明复现前提 |

**已定稿（2026-11-14）**：

1. artifact/version 行**永久保留**，不按保留期删行；保留期只 GC 无引用的 HTML/PNG 文件。理由：审计要能回答“当时是什么”，行删了 provenance 就断；文件可重建/可清理，行不可。
2. correction **只允许维护脚本/管理员**操作；普通 Agent/UI 不提供入口。每次 correction 必须写 actor、理由、时间、旧值/新值；本地单用户也不例外。理由：provenance 的修改权限必须比写入更窄。

---

## 13. 与 v1 文档的关系

| v1 条目 | v2 处理 |
|---|---|
| D2 时点跟图走 | 被 Q1 取代：`data_*` 行情，`view_*` 视图 |
| D3 回放重算时点 | 被 Q2/Q1 取代：`view_*` 可重算，`data_*` 用 meta provenance |
| D4 固定 div_id | 被 Q3 取代：规范 HTML 固定，展示层 `embed_html` 分配唯一 id |
| D8 09:25 抛错 | 被 Q7 取代：接受 + session 标注 + 严格模式另置 |
| D9 合成 run | 被本文件 D9 取代：内容/版本/引用分离，渲染不造 run |
| Q4/Q5/Q6 | Q4/Q5 并入 plan-20261114-002；Q6=A 由 plan-20261114-001 落地 |

---

## 14. 附：关键代码位置（改造前）

| 位置 | 内容 |
|---|---|
| `astock/agent/store.py:58-101` | v1 三表 DDL |
| `astock/agent/store.py:122` | `artifact_id_for(digest, filename)` |
| `astock/agent/store.py:149` | `ensure_run`，默认 `status="success"` |
| `astock/agent/store.py:178` | `ON CONFLICT(run_id) DO NOTHING` |
| `astock/agent/store.py:217` | `ArtifactRecord`（含 `run_id`） |
| `astock/agent/store.py:261` | `record_artifact`，`ON CONFLICT DO NOTHING` |
| `astock/agent/chart_render.py:63` | `CODE_VERSION` 硬编码 |
| `astock/agent/chart_render.py:72` | `FIGURE_DIV_ID = "astock-chart"` |
| `astock/agent/chart_render.py:86-102` | `RenderedFigure`，`run_id` 在 `:101` |
| `astock/agent/chart_render.py:521` | `_time_points`，transforms 后重算 |
| `astock/agent/chart_render.py:558` | `_probe_time_frame` 猜时间列 |
| `astock/agent/chart_render.py:1146` | `to_html(..., div_id=FIGURE_DIV_ID)` |
| `astock/agent/chart_render.py:1172` | 合成 `run_chart_<artifact_id>` |
| `astock/agent/chart_render.py:1185-1196` | meta 含 `data` 快照 |
| `astock/data.py:735` | `latest_bar_time`，`series.max()` |
| `astock/data.py:764` | 分钟时段硬校验抛 `ValueError` |
| `astock/agent/runtime.py:502` | 用 `data_as_of` 拼「数据截至」 |
| `astock/agent/gateway.py:789` | 用 `data_as_of` 做提示 |
| `docs/AStockAgent_interface_freeze_v1.md:311` | v1 三表 DDL |

---

## 15. 2026-11-14 补充裁决（D1 / D10 / D11 / §12）

### 15.1 D10：时间列不再猜

- 现状：`astock/agent/chart_render.py:558` 的 `_probe_time_frame` 把“第一个可解析的非数值列”当时间列；`code="20240104"`、`label="2024-01-04 收盘"` 会被误判，`data_as_of` 静默错一天。
- 定稿：`encode.x` 或显式 `data.time_field` 必须指向可证明的时间列；只接受真实 datetime、`YYYY-MM-DD`/`YYYY-MM-DD HH:MM[:SS]` 且能按 `Asia/Shanghai` 解析的列。字符串代码、带文字标签、纯数字代码一律不猜。
- 无显式时间列、无法证明时区/交易日语义：抛 `ChartSpecError`，不生成图；inline 数据必须自带 `time_field` 或声明 `time_semantics=none`（此时图注写“视图时点未知”，且不得伪造 `data_as_of`）。
- 验收：`code=20240104`、`label=2024-01-04 收盘`、`trade_date=20240104` 三个反例都必须报错或要求显式字段；显式 datetime 列正常出图。

### 15.2 D11：transforms 统一到 stats 口径

- 现状：`transforms` 手写 pandas 五算子（filter/groupby/agg/sort/limit），与 `astock/stats.py` 的 NaN/空组/排序/聚合语义可能不同，图上均值≠表里均值。
- 定稿：统计口径复用 `astock/stats.py` 或同一实现，不另写一套；`groupby` 默认 `dropna=False` 保留空组；`agg` 的 `mean/sum/min/max/count/last` 与 stats 对齐；空组/全 NaN 组保留并标 warning，不静默丢行；排序稳定（`mergesort`），同值不换序；字段同名覆盖时加后缀并报警。
- 验收：同一组数据经 `transforms` 与 `stats` 计算结果逐值一致；含 NaN/空组/重复值/同名覆盖的对照测试；图上标注的均值与表一致。

### 15.3 D1：复权口径（详见 ADR-008 补充裁决）

- hfq = 标准后复权，基准 1.0：`base_factor` 取该证券**最早可用交易日**的 `adj_factor`；`hfq_price = raw * adj_factor / base_factor`。不得把最新因子当 1.0（那是 qfq），不得全市场共用基准日。
- `adj_factor` 缺失/空/`<=0`/NaN/inf 一律抛 `DataContractError`；禁止回退 qfq、禁止填 1.0。
- 分钟线用当日日线因子，metadata 写 `adj=hfq`、`adj_base=<最早交易日>`、`adj_source=daily`；分钟 hfq 不与日线 hfq 拼连续序列。
- `ChartSpec.data.adj` 增加 `hfq`，默认仍 `qfq`；`none` 保留给涨跌停/成交类图。落地项 `plan-20261114-003`（D1）与 `plan-20261114-002`（图注）。

### 15.4 §12 定稿

- artifact/version 行永久保留，只 GC 无引用文件；correction 仅维护脚本/管理员，actor/理由/时间/旧值/新值必填。
