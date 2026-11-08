# ADR-002 六层分层与契约层（contracts.py）

- 状态：proposed
- 日期：2026-11-03
- 来源：评审 §4.1 / §4.2；修复记录 §6；2026-11-02 评审会话

## 背景

现有 26 个模块在功能上已经闭环，但边界是"隐式"的：

- UI 直接调数据/领域函数，推荐生成与历史读取存在两套路径；
- DataFrame 的列约定没有校验，供应商缺列时靠 `try/except` 静默吞掉（评审 D18 的 `ts_code` KeyError 就是实例）；
- 用例返回值不统一：有的返回 DataFrame，有的返回 dict，诊断信息散落在页面文案里。

评审给出的目标分层是"原地归位，不是重写"：交互 → 用例 → 领域 → 适配 → 契约 → 平台。其中**契约层是新增的**，也是当前唯一完全缺失的一层。

## 决策

1. 采用六层分层：交互层（Gradio Tabs）、用例层（recommend/track/research 编排）、领域层（纯函数、无 IO）、适配层（Tushare/LLM/cache）、契约层（新增）、平台层（ledger/scheduler）。
2. 新增 `astock/contracts.py`，定义并校验：
   - `BarFrame`：`ts_code/trade_date/open/high/low/close/vol/amount/adj_factor`；
   - `EventFrame`：`code/kind/pub_time/title/source/url`；
   - `Evidence`：证据必须带 `pub_time/title/url/source`；
   - `RecommendationRun`：`schema_version=2` 的推荐批次结构。
3. 用例层统一返回 `RunResult(run_id, status, payload, diagnostics)`；`status ∈ {success, stale, failed}`。
4. 所有跨模块的 DataFrame 进出必须过契约校验；校验失败抛出显式异常，不再静默降级。

## 后果

- 正面：字段缺失从"静默空结果"变成"带列名的异常"；测试可在 30 秒覆盖全部接口；UI 与逻辑解耦后可独立替换。
- 负面：多一层校验成本；旧模块迁移期需要兼容包装；`contracts.py` 本身需要维护 schema 版本。
- 迁移顺序：先 `contracts.py` + `ledger.py` 双写一版，确认无回归后再切读 SQLite（评审 §4.6 P0.5）。

## 证据

- 当前 `astock/contracts.py` 不存在（已核验）
- `deliverables/AStockAgent_代码与架构评审_v1.md` §4.1/§4.2
- `deliverables/AStockAgent_评审修复记录_v1.1.md` §6
