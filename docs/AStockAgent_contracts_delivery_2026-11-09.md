# AStockAgent 契约层交付文档（contracts.py）

日期：2026-11-09  
任务：`plan-20261103-001` · 契约层 contracts.py：DataFrame schema 校验  
架构依据：ADR-002（六层分层 + 跨模块 DataFrame 必须过契约校验）  
评审线程：`rev-20261108-001`（第 1 轮 6 条、第 2 轮 3 条，均已修复待复核）

## 1. 交付摘要

- 新增 `astock/contracts.py`：把散落在 `data.py` / `events.py` / `recommend.py` 的隐式列约定，收敛为 4 个显式契约（BarFrame、EventFrame、Evidence、RecommendationRun）。
- 契约失败一律抛 `ContractError`（`ValueError` 子类），消息带契约名、缺失/非法列名与实际列，不再静默返回空 DataFrame 或降级。
- 已接入行情、事件、推荐三条主链路的全部关键入口，并把推荐 JSON 的 `schema_version=2`、`allow_nan=False`、证据时间不晚于生成时间（DATA-02）变成硬校验。
- 修复评审 D18：`fetch_announcements` 在供应商不返回 `ts_code` 时，用请求代码补全 `code`，不再留下空串导致按代码过滤后证据为空。
- 测试：`Ran 97 tests … OK`（57 条原有 + 40 条契约/回归），命令见第 6 节。

## 2. 变更清单

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `astock/contracts.py` | 新增 | 4 个契约 + 5 个模块级校验函数 + `ContractError` |
| `tests/test_contracts.py` | 新增 | 契约单测、评审回归用例、引用渲染用例（共 40 条） |
| `astock/data.py` | 修改 | `_fetch_daily` / `load_daily` / `load_index_daily` / `load_minute` 出口校验 |
| `astock/events.py` | 修改 | 各 `fetch_*` 入口与归一化函数校验；日期解析漂移显式报错；证据键 `date`→`pub_time` |
| `astock/recommend.py` | 修改 | `_save` 写入前校验 RecommendationRun；`ContractError` 穿透 `_load_one`/`load_many`/`_market_snapshot`；删除 LLM 覆盖 action |
| `astock/ledger.py` | 修改 | 证据落库读取 `pub_time` |
| `app.py` | 修改 | `_citation_block` 兼容 `pub_time` / 旧 `date` 键，坏日期不再抛异常 |

## 3. 四个契约

| 契约 | 必需列/键 | 校验语义 |
| --- | --- | --- |
| `BarFrame` | `ts_code, trade_date, open, high, low, close, vol, amount`（`require_adj=True` 时加 `adj_factor`） | 列齐全；`ts_code` 非空字符串（NaN 与空串同罪）；`trade_date` 必须可解析为时间；价量列必须数值或可无损转数值；`require_adj` 时 `adj_factor` 不得缺失或为 NaN |
| `EventFrame` | `code, kind, pub_time, title, source, url` | 列齐全；`kind`/`title` 非空；`source`/`url` 允许空串但必须是字符串；`require_code=True` 时 `code` 不得为空/NaN；`pub_time` 必须可解析 |
| `Evidence` | `pub_time, title, source, url, kind` | 单条证据字典；保留调用方原始时间表示（字符串或 Timestamp 均可）；兼容迁移期旧键 `date`，但只读入不写回调用方 |
| `RecommendationRun` | `schema_version, run_id, generated_at, data_as_of, tradable_from, source, market, table, evidence, params, diagnostics` | `schema_version == 2`；`run_id` 非空；`table` 每行含 7 个中文列（排名/代码/名称/综合分/技术分/事件分/建议）；`evidence` 为 `{代码: [证据]}` 且逐条过 Evidence；每条证据 `pub_time ≤ generated_at`（DATA-02，时区统一后再比较）；最后做 JSON 安全扫描，拒绝 NaN/Inf 与不可序列化值 |

`BAR_SCHEMA` / `EVENT_SCHEMA` 是列名的唯一来源：`BarFrame.required` 由 `BAR_SCHEMA` 排除 `adj_factor` 派生，`EventFrame.required` 直接取 `EVENT_SCHEMA`，并有测试防止两者漂移。

## 4. 接入点

### 4.1 行情（astock/data.py）

- `_fetch_daily`：供应商原始帧返回后立刻校验（缺列在此暴露），复权处理完再校验出口；空帧仍返回空帧（由上层决定是否报错）。
- `load_daily`：磁盘缓存读入后校验（旧缓存缺列会显式失败），返回前再校验一次；`require_adj` 跟随 `adj` 参数。
- `load_index_daily`：缓存命中与新拉取两条路径都校验。
- `load_minute`：供应商分钟线不带 `ts_code`，先补列再校验，复权后作为出口校验。

### 4.2 事件（astock/events.py）

- 新增 `_require_source_columns`：供应商帧缺关键列时抛 `ContractError`，消息含实际列。
- 新增 `_require_parseable_dates`：`ann_date` / `pubtime` 等日期字段格式漂移（如 `"2026-11-06"` 落进 `%Y%m%d` 解析）不再被 `errors="coerce"` + `dropna` 静默吞掉，而是统计坏行并报错；源数据本身缺日期的行仍按缺失处理。
- 所有 `fetch_*` 与 `build_ai_context` 在 `except Exception` 之前加 `except ContractError: raise`，契约错误穿透重试/降级分支。
- `evidence_for_stock` 产出统一为 `pub_time` 键；`score_events` 兼容读取旧 `date` 键（迁移期）。

### 4.3 推荐（astock/recommend.py）

- `_save`：先校验 `RecommendationRun`，再 `json.dumps(..., allow_nan=False)`，最后落 CSV/JSON/latest；校验失败不产生半成品文件。
- `_load_one` / `load_many` / `_market_snapshot`：`ContractError` 直接抛出，坏票不再悄悄退出候选池、run 不再带病标 success。
- AI-01：删除 LLM 返回 `action` 覆盖规则「建议」的逻辑，prompt 也不再索取 `action`/`confidence`；LLM 只补充理由、催化、风险与引用。

### 4.4 展示与存储

- `astock/ledger.py`：证据写入读取 `pub_time`。
- `app.py::_citation_block`：优先 `pub_time`、兼容旧 `date`；时间不可解析时降级为空日期，不再 `KeyError`。

## 5. 两轮评审问题对照

第 1 轮（request_changes）：

| 级别 | 问题 | 处理 |
| --- | --- | --- |
| P1 | `_load_one`/`load_many` 的 `except Exception` 吞掉 `ContractError` | 前置 `except ContractError: raise`；补 `test_load_many_propagates_contract_error` |
| P1 | `require_code`/`non_empty` 放过 NaN（`astype(str)` → `"nan"`） | `_check_strings` 改为 `isna() \| strip()==""`；删除重复不可达块；补 NaN 用例 |
| P1 | LLM 的 `action` 覆盖规则「建议」，违反 AI-01 | 删除覆盖映射与 prompt 字段；补注入相反 action 的端到端用例 |
| P2 | 事件日期格式漂移被静默清空 | `_require_parseable_dates` 统计坏行抛错；补漂移用例 |
| P2 | `allow_empty` 在列校验前返回，0 行缺列帧被放行 | 只放过「连列都没有」的无数据哨兵；补 0 行缺列用例 |
| P2 | RecommendationRun 未校验 DATA-02 | 逐条比较 `pub_time ≤ generated_at`；校验成功后才整体替换，且不改写调用方 payload |

第 2 轮（request_changes）：

| 级别 | 问题 | 处理 |
| --- | --- | --- |
| P1 | `app.py::_citation_block` 仍读 `item["date"]`，有引用即 `KeyError` | 改读 `pub_time` 并兼容 `date`；补 `CitationBlockTests` 4 条 |
| P2 | `BAR_SCHEMA`/`EVENT_SCHEMA` 与 `required` 双份真相且已漂移 | 常量为唯一来源，`required` 由其派生；补防漂移断言 |
| P2 | tz-aware `pub_time` 与 naive `generated_at` 比较抛 `TypeError` | 统一去时区后比较，`TypeError` 兜底转 `ContractError`；补 `+08:00` 正/反用例 |

## 6. 测试与验证

本机未安装 pytest，测试为 unittest 风格。受工作区临时目录权限影响（`tempfile.mkdtemp` 以 `0o700` 建目录会被拒写），使用仓库内 runner 运行：

```powershell
$env:TMPDIR="$PWD\.tmp-tests"; $env:TEMP="$PWD\.tmp-tests"; $env:TMP="$PWD\.tmp-tests"
.\.venv\Scripts\python.exe .tmp-tests\run_tests.py
```

结果：

```text
Ran 97 tests in 16.397s

OK
```

覆盖分布：

- 57 条原有测试（行情 PIT、事件、推荐、回测、硬化用例）全部保持通过，说明 `date`→`pub_time` 迁移与异常传播未破坏既有行为。
- 40 条新增：四契约的缺列、类型错误、空帧/0 行帧、NaN 代码、日期漂移、证据时间越界、payload 不可变、ContractError 穿透、LLM 不得改 action、引用块渲染。

## 7. 行为变化与兼容性

- **异常而非降级**：缺列/类型错误以前可能表现为空结果或静默跳过，现在抛 `ContractError`，消息含列名。依赖「拿不到就空」的调用方需要显式处理。
- **证据时间键**：`evidence_for_stock` 与推荐 JSON 统一用 `pub_time`；读取侧（ledger、app、`score_events`）保留旧 `date` 兼容。
- **公告代码补全**：供应商缺 `ts_code` 时用请求代码填充，按代码过滤不再得到空证据集（D18）。
- **0 行哨兵**：完全无列的 `pd.DataFrame()` 在 `allow_empty=True` 时放行（离线用例会显式 patch 成空帧）；有列但缺必需列的 0 行帧仍报错。

## 8. 已知边界

- `require_adj` 由调用方决定：指数日线与分钟线不强制 `adj_factor`。
- dtype 不做强制转换：契约保证「可解析、无脏值」，不保证列 dtype 与 schema 完全一致（避免把供应商的 float32/int 强行改写）。
- `astock/backfill.py` 自行拼装历史 payload，绕过 `_save`，本任务未接入校验（其结构缺 `schema_version`/`run_id`，接入需单独迁移）。
- 根目录 `release/AStockAgent/` 是发布副本，未同步修改。
- 评审 issue 的「已修复」状态由评审方填写，开发侧不代写评审结论。

## 9. 后续建议

1. 将 `backfill.py` 与 `track.py` 的产出也接入契约，补齐「所有跨模块 DataFrame 必须校验」的最后一环。
2. 为 `ContractError` 增加诊断码（如 `BAR_MISSING_COLUMN`），便于上层按码分类告警。
3. CI 增加 `python -m unittest` 步骤，并固定 pandas/pyarrow 版本，避免 dtype 行为随版本漂移。
