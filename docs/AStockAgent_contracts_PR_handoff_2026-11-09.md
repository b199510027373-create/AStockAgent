# 契约层 PR 交接手册（给 PR / 评审会话）

日期：2026-11-09  
分支：`feat/contracts` → `main`  
任务：`plan-20261103-001` · 契约层 contracts.py：DataFrame schema 校验  
评审线程：`rev-20261108-001`（第 1 轮 6 条 + 第 2 轮 3 条）  
配套文档：[AStockAgent_contracts_delivery_2026-11-09.md](AStockAgent_contracts_delivery_2026-11-09.md)（契约语义与接入细节）

## 1. 一句话现状

契约层已实现并接入行情/事件/推荐三条主链路；两轮评审共 9 条问题全部有对应提交；`pytest`（本次新装）与 unittest runner 双通道均为 **97 passed / 97 tests OK**；代码已推送到 `feat/contracts`，等待开 PR 与第 3 轮复核。

## 2. 仓库与提交

```text
仓库      https://github.com/b199510027373-create/AStockAgent.git
基线      main @ 885c6d8
提交      8c5e0e9  fix(contracts): 修复第 2 轮评审三项并补交付文档
分支      feat/contracts（已推送，tracking origin/feat/contracts）
开 PR     https://github.com/b199510027373-create/AStockAgent/pull/new/feat/contracts
```

`feat/contracts` 相对 `main` 的代码改动：

```text
 app.py                                             |  15 ++-
 astock/contracts.py                                | 103 ++++++++++-------
 docs/AStockAgent_contracts_delivery_2026-11-09.md  | 128 +++++++++++++++++++++
 tests/test_contracts.py                            |  96 +++++++++++++++++++
 4 files changed, 299 insertions(+), 43 deletions(-)
```

另含 `.devloop/` 的计划项、worklog、评审轮次与 issue 状态同步（不含业务代码）。

> 如果评审需要「仅契约层首次引入」的完整 diff，用 `git diff d756231..2fce08f -- astock tests`（6 文件 +1417/−61，即 PR #2 合并前的契约层快照）；本轮增量 diff 用 `git diff 885c6d8..8c5e0e9`。

## 3. 在 PR 会话里跑测试

### 3.1 首选：pytest（本次已安装）

```powershell
cd C:\Users\b1995\Documents\AStockAgent
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # 含 pytest
.\.venv\Scripts\python.exe -m pytest tests -q
```

本次实测环境与结果：

```text
pytest 9.2.0（pluggy 1.7.0 / iniconfig 2.3.1）
Python 3.13 / pandas 3.0.3
97 passed, 1 warning in 15.84s
```

唯一警告来自第三方插件：`anyio/pytest_plugin.py:14 PytestRemovedIn10Warning: CallSpec2 → CallSpec`，与本仓库代码无关。

注意：

- 必须带 `tests` 参数。仓库根目录还有 `release/AStockAgent/tests` 发布副本，直接 `pytest` 会重复收集并且可能因路径差异失败。
- `CitationBlockTests` 首次 setup 约 6.5s，是在 `import app`（gradio/plotly UI 模块），属预期。

### 3.2 备选：unittest runner（无 pytest 时）

```powershell
$env:TMPDIR="$PWD\.tmp-tests"; $env:TEMP="$PWD\.tmp-tests"; $env:TMP="$PWD\.tmp-tests"
.\.venv\Scripts\python.exe .tmp-tests\run_tests.py
# Ran 97 tests in 16.397s / OK
```

`.tmp-tests/run_tests.py` 的存在原因：在受限沙箱里 `tempfile.mkdtemp` 用 `0o700` 建目录会被拒写，runner 把它替换为 `0o777` 实现。**全权限环境直接用 pytest 即可，不需要这个 runner。**

## 4. 交付范围（行锚点）

| 文件 | 关键位置 | 内容 |
| --- | --- | --- |
| `astock/contracts.py` | `_naive_timestamp` :156、`BAR_SCHEMA` :199、`BarFrame` :218、`EVENT_SCHEMA` :291、`EventFrame` :302、`Evidence` :359、`RecommendationRun` :480 | 4 契约 + 5 个模块级校验函数 + `ContractError` |
| `astock/data.py` | :220、:257、:305、:357、:392、:409、:647 | `_fetch_daily` / `load_daily` / `load_index_daily` / `load_minute` 进出口校验 |
| `astock/events.py` | `_require_source_columns` :111、`_require_parseable_dates` :124、`_normalize_news` :232、`fetch_news` :269、`fetch_announcements` :378、`fetch_reports` :439、`evidence_for_stock` :621、`score_events` :719、`build_ai_context` :775 | 事件帧校验、日期漂移报错、证据键 `date`→`pub_time`、D18 代码补全 |
| `astock/recommend.py` | `ContractError` 穿透 :116/:140/:361、`_save` 校验 :558、AI-01 注释 :749 | 推荐 run 校验、异常不降级、LLM 不得改 action |
| `app.py` | `_citation_block` :109-133 | 引用块消费 `pub_time` 并兼容 `date` |
| `astock/ledger.py` | 证据落库 | 读取 `pub_time` |
| `tests/test_contracts.py` | 契约单测 :110-:252、入口测试 :267-:427、回归 :428、引用块 :534、LLM 契约 :618 | 40 条契约/回归用例 |

## 5. 两轮评审 9 条问题状态

| # | 级别 | issue id | 问题 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | P1 | `iss-mw7mbatu-070b43` | `_load_one`/`load_many` 吞掉 `ContractError` | fixed |
| 2 | P1 | `iss-mw7mbo6x-d66364` | `require_code`/`non_empty` 放过 NaN | fixed |
| 3 | P1 | `iss-mw7mbrki-aaeb66` | LLM `action` 覆盖规则「建议」（AI-01） | fixed |
| 4 | P2 | `iss-mw7mby2q-01b8d2` | 事件日期格式漂移被静默清空 | fixed |
| 5 | P2 | `iss-mw7mc0x0-405224` | `allow_empty` 放行 0 行缺列帧 | fixed |
| 6 | P2 | `iss-mw7mc3r9-8689f2` | RecommendationRun 缺 DATA-02 校验 | fixed |
| 7 | P1 | `iss-mw7wair1-e5fb1d` | `app._citation_block` 读 `item["date"]` 触发 `KeyError` | fixed（待复核） |
| 8 | P2 | `iss-mw7waptn-326bd5` | `BAR_SCHEMA`/`EVENT_SCHEMA` 与 `required` 双份真相 | fixed（待复核） |
| 9 | P2 | `iss-mw7wawf3-843f9e` | DATA-02 时区比较抛 `TypeError` | fixed（待复核） |

> 表中状态以 `.devloop/reviews/rev-20261108-001/issues/*.json` 为准；第 7-9 条为第 2 轮新开，仍标记 `open`，需要评审方复核后自行置位——开发侧不代写评审结论。

## 6. 复核检查清单（评审方可直接照做）

```powershell
git fetch origin
git switch feat/contracts            # 或 git switch -c review/contracts origin/feat/contracts
git diff 885c6d8..8c5e0e9 -- app.py astock/contracts.py tests/test_contracts.py
.\.venv\Scripts\python.exe -m pytest tests -q
```

逐条验证点：

1. **P1 引用块**：`python -c "import app; print(app._citation_block([{'kind':'news','pub_time':'2025-01-05T09:00:00','title':'t','source':'s','url':''}]))"` 应输出含 `2025-01-05` 的行；旧 `date` 键同样能渲染；坏日期不抛异常。
2. **P2 schema 单一来源**：`grep -n "^BAR_SCHEMA\|^EVENT_SCHEMA" astock/contracts.py`，确认 `BarFrame.required` / `EventFrame.required` 均由常量派生，且无 dtype 标注残留在常量里。
3. **P2 时区**：`python -m pytest tests/test_contracts.py -q -k tz_aware`，正例（+08:00 过去时间通过）与反例（+08:00 未来时间报 DATA-02）都应通过。
4. **AI-01**：`recommend.py` 中不再有 `ai_action` 映射；`_llm_rank` prompt 不索取 action/confidence；`LlmActionContractTests` 注入 `"action":"回避"` 后 `建议` 仍等于规则值。
5. **不再静默降级**：`DataEntryContractTests` / `EventsEntryContractTests` / `RecommendEntryContractTests` 覆盖缺列、类型错误、NaN 代码、日期漂移、证据越界，均断言抛 `ContractError` 而非返回空结果。

## 7. 关键设计决策（复核时的判断依据）

- **失败即抛错**：跨模块 DataFrame 进出统一校验，`ContractError` 消息带契约名、缺失/非法列名与实际列；`fetch_*`、`_load_one`、`load_many`、`_market_snapshot` 一律 `except ContractError: raise` 置于宽泛 `except` 之前，schema 漂移不再伪装成「单票缺数据」。
- **0 行哨兵**：只有「连列都没有」的 `pd.DataFrame()` 在 `allow_empty=True` 时放行（离线测试会显式 patch 空帧）；有列但缺必需列的 0 行帧仍报错——这是第 1 轮 P2 的裁决结果。
- **dtype 宽松**：契约保证「列齐全 + 可解析 + 无脏值」，不强制改写 dtype（供应商 float32/int 差异不应让整条链路失败）；`_to_numeric` 仅在无损时写回。
- **时间键迁移**：证据统一 `pub_time`，读取侧（ledger、app、`score_events`）保留 `date` 兼容，避免旧缓存与旧测试断裂。
- **DATA-02**：`pub_time ≤ generated_at` 逐条硬校验，比较前统一去时区；校验成功后才整体替换 `evidence`，不改写调用方 payload。
- **AI-01**：`action` 只由规则（`_action`）决定，LLM 仅补充理由/催化/风险/引用。

## 8. 已知边界与后续

- `astock/backfill.py` 自行拼装历史 payload 且不调 `_save`，未接入 RecommendationRun 校验（其结构缺 `schema_version`/`run_id`，需单独迁移）。
- `track.py` 尚未接入契约。
- 根目录 `release/AStockAgent/` 为发布副本，未同步本次修改。
- `requirements-dev.txt` 已加入 `pytest`；CI 建议直接跑 `python -m pytest tests -q` 并固定 pandas/pyarrow 版本。
- 建议后续给 `ContractError` 增加诊断码（如 `BAR_MISSING_COLUMN`），供上层分类告警。

## 9. 本次会话产生的新增/变更（相对上次交接）

- 安装 `pytest 9.2.0` 到项目 `.venv`，并写入 `requirements-dev.txt`。
- 以 pytest 复跑全量：`97 passed, 1 warning in 15.84s`。
- 新增本交接手册，整合上次会话（两轮评审、9 条问题、测试排障、分支历史）以便 PR 会话独立运行。
