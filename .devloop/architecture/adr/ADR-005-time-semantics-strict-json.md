# ADR-005 时间语义：data_as_of / tradable_from / 证据截止

- 状态：accepted
- 日期：2026-11-03
- 来源：评审 §2.1 D3/D4；修复记录 §1/§3

## 背景

原来只有一个 `as_of`，同时承担"数据截止"和"收益起算"两种含义，直接造成：

- 证据时间晚于 `as_of`：38 条证据中 23 条日期在 `2026-10-30` 之后（周末新闻），而 T+1 从 10-30 收盘起算 → 前视；
- 跟踪从 `as_of` 收盘买入，但那天根本无法成交；
- `latest.json` 含裸 `NaN`，严格 JSON 解析器直接报错，堵死迁移 SQLite 的路。

## 决策

1. 拆两个字段：
   - `data_as_of`：数据截止日（最后一根有效日线收盘）；
   - `tradable_from`：可成交起点，默认下一开市日；若生成时已开盘则顺延到再下一个开市日。
2. 证据必须满足 `pub_time <= generated_at`；超出的证据不进入推荐。
3. 推荐 JSON 升级到 `schema_version=2`，必须包含 `run_id/generated_at/data_as_of/tradable_from/source/params/diagnostics/market/ai/table/evidence`。
4. 序列化统一 `json.dumps(allow_nan=False)`，并递归清洗 `NaN/Inf/NaT`；CSV/JSON 原子写。
5. 跟踪从 `tradable_from` **开盘**入场，持有到 T+N 收盘；入场 bar 缺失返回空，**不静默后移**。
6. 旧批次没有新字段时，回退到 `as_of` 的下一开市日，保证兼容。

## 后果

- 正面：消除前视；推荐产物可被严格解析器读取；跟踪口径可审计。
- 负面：`tradable_from` 依赖交易日历，节假日/停牌边界需维护；旧批次与新批次口径并存一段时间。
- 验收：`json.loads(parse_constant=raise)` 通过；证据日期全部 ≤ `generated_at`；跟踪首日等于 `tradable_from`。

## 证据

- `astock/recommend.py`（run_id、严格 JSON、diagnostics）
- `astock/track.py`（`_forward_return` 精确 entry bar）
- 修复记录 §1 D3/D4、§3 行为变化
