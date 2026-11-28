# 产品 × 论文协同交接说明（panel v2 / 因子方向 / 结果引用）

> 版本：v1.0 · 日期：2026-11-28 · 主导计划：plan-20261128-006
> 论文对照计划：plan-20261123-005（正式版）、plan-20261123-006（论文交付）
> 产品对照计划：plan-20261128-002（证据链）、plan-20261128-003（组合层）、plan-20261103-014（实盘复核）
> 机器可读事实源：[docs/thesis_product_fact_source_v1.json](thesis_product_fact_source_v1.json)（本页所有数字由它派生，冲突时以它为裁决输入）
> 验证命令：`python tools/check_thesis_sync.py --check`（论文数据未挂载时返回 `unavailable`，不是失败）

---

## 1. 唯一事实源

| 对象 | 唯一来源 | 指纹 / 口径 | 谁可以写 |
|---|---|---|---|
| 月度面板 | `D:\毕业论文\论文需要\data\panel_monthly_v2.parquet` | 671,631 行 × 40 列、5,431 只、192 个月（2010-01-29..2025-12-31）；contract v2、gate=passed；sha256 `28d06335…` | 论文侧数据构建；产品只读 |
| 因子方向表 | `D:\毕业论文\M1_合成诊断代码\compose\types.py::DIRECTIONS` → `factor_directions.csv`（40 行，`论文需要\M1_合成诊断缩小版_v3_rerun_20261125\` 为规范副本，另 2 份副本逐字节一致） | 12 个基础因子方向必须与 `factor_def_v2.csv` 逐行一致；另 3 个补充字段方向均为 −1 | 论文侧；产品只读 |
| 正式结果 | 论文 005 正式版结果目录（待产出，`gate-thesis-005-formal` 未关闭） | 哈希清单 + 表格映射 | 论文侧 |
| 产品样本 | `data/astock.db` 台账（LEDGER-01） | run_id / evidence_id | 产品侧；论文不得并入 |

**面板 v1（`panel_monthly.parquet`，60 个月 / 989 只）只服务缩小版诊断，不得当正式版或产品面板。**
**产品不得复制方向表、不得按自己的理解改 `direction`；需要新因子时先改论文侧 `DIRECTIONS` 并升 `direction_table.version`。**

---

## 2. 输入输出边界

| 环节 | 输入（只读） | 输出（谁写） | 明确禁止 |
|---|---|---|---|
| 论文 005 正式版 | panel v2、方向表、日线缓存、compose 代码（`DEFAULT_PANEL` 需切到 v2） | 正式结果目录、中性化/bootstrap 口径、可复现哈希（论文侧写） | 沿用缩小版数字冒充正式版；改方向表不升版本 |
| 论文 006 论文交付 | 005 产物、论文框架 v1.0、局限分析 v1.0 | 正文/表/图/附录、口径对照、open items（论文侧写） | 表图与代码版本脱钩；正文与附录两套数字 |
| 产品 002 证据链 | panel v2（经 `astock/thesis_sync.py` 校验）、台账、既有 run | run_id/evidence_id 追溯、TopN 复盘、证据包（产品侧写） | 重跑论文实证或另建面板；改排序后宣称 alpha |
| 产品 003 组合层 | 002 证据链、panel v2 波动率/流动性字段、成本模型 | 组合权重与约束、换手/成本报告（产品侧写） | 均值-方差优化器；用组合层结果反推因子有效性 |
| 产品 014 实盘复核 | 台账实盘批次（live 20 条）、002 统计口径 | 实盘显著性复核（`product-observation` 口径） | 当论文实证；与论文数字合并；n<10 显示 p |

一句话：**论文侧产出“口径与数字”，产品侧产出“可追溯的产品证据”；两者不互相代跑。**

---

## 3. 引用白名单 / 黑名单

### 3.1 可对外引用（必须带标签）

| id | 能引用什么 | 必须带的标签 |
|---|---|---|
| `panel-v2-caliber` | 面板 v2 行/列/只数/月数/合约版本/hash | 数据口径 |
| `factor-direction-base12` | 12 个基础因子方向 + 3 个补充字段 | 因子口径 |
| `reduced-v3-method` | 缩小版方法链路闭合、54/54 独立复算 | 缩小版，不可外推 |
| `reduced-v3-results` | 五层 IC 全正但不显著、四负一正、无显著超额；lgbm_40 **双口径** | 缩小版；缺失口径两侧同时给 |
| `random-label-null` | 10 seeds 随机标签 \|z\|<1 | 缩小版 |
| `synthetic-recovery` | 衰减估计 RMSE 1.2–1.9 月；零信号 81.5% 不误报 | 合成数据，不是 A 股实证 |
| `limitation-set` | 单基准/单市场/近似池/未中性化/free_float_mv 缺失/SW2021 前视/成本简化/未校正多重比较 | 必须随任何结果引用一起出现 |

### 3.2 不可引用（对外与论文正文都禁止）

| id | 禁止的说法 | 原因 |
|---|---|---|
| `lgbm-22pct` | “lgbm_40 有效 / 年化 22%” | t=1.87、z=0.94，符号随缺失口径从 +1.84% 翻到 −9.83%/月 |
| `ridge12-reverse` | “ridge_12 反向有效” | t=−2.15 未校正多重比较，且该层 IC 为正 |
| `smallcap-beta-tilt` | “模型偏好小市值 / 高 beta” | v3 主口径相反（偏大市值 1.3–3.6 倍） |
| `half-life-2m` | “半衰期约 2 个月” | 五层均不可估，估计器 RMSE 与真值同量级 |
| `alla-180m-live-extrapolation` | 把缩小版外推到全 A / 180 个月 / 实盘 | 样本与口径都不同，等 `gate-thesis-005-formal` 关闭 |
| `product-live-as-thesis-evidence` | 产品 195 条（实盘 20）当论文实证样本 | T+5/T+20 有效独立样本约 6.8/1.0；回填批次事件分固定 50 |
| `synthetic-as-ashare-evidence` | 用合成面板证明“事件驱动因子在 A 股有效” | 合成只验证管道 |
| `sw-cross-version-ic` | 跨 2021-12-13 直接比较行业 IC | 全 A 31.14% 股票、沪深300 14.70% 权重换标签 |

### 3.3 只允许产品口径引用

产品样本数字（`live-sample-observation`：39 批次 / 195 条，Top2−其余 T+1 +0.34pp、p≈0.197，p 隐藏规则；`product-walkforward`：连续 WF 跑输沪深300、11 因子无 BH 显著项）**只能**以 `product-observation` 标签出现在产品文档、UI 与面试材料里，不得写入论文结果章，也不得与论文数字拼成一句话。

---

## 4. 冲突裁决与变更流程

1. **权威顺序**：论文 006 定稿 > 论文 005 正式版产物 > 缩小版 v3 交付说明 > 产品文档。冲突时以论文口径为准，产品改文案而不是改数字。
2. **数字只出现一次**：同一指标在产品文档里必须直接引用论文数并注明“论文口径 + 样本期”；不允许四舍五入后再造一个数。
3. **面板或方向表变更**：改 `panel.sha256` / `direction_table.version`（`gate-panel-hash` 触发）→ 冻结一切既有引用 → 论文侧重跑 → 更新 `citable.evidence` → 产品侧重新跑 `tools/check_thesis_sync.py --check`。未走完流程前不发布数字。
4. **闸门**：`gate-thesis-005-formal`（正式版未落地）与 `gate-thesis-006-final`（论文未定稿）未关闭时，对外材料标“工作稿”，只引用 §3.1。
5. **论文侧计划 links 闸门** `gate-thesis-plan-links`：已关闭（2026-11-28）。plan-20261123-005/006 均已写入 `fact_source` / `handoff` / `sync_check` / `data` / `counterpart_plan` 五键（写前备份 `*.json.bak`），复跑 `--dry-run` 返回 SKIP，`tools/check_thesis_sync.py` 仍为 OK。后续论文侧若改 links，只需重跑同一命令（只增不改）。

---

## 5. 两边计划怎么引用

产品侧已写入（本次改动，`devloop_plan update`，只增不改）：
`plan-20261128-006` 的 `links` 增加 `fact_source`、`handoff`、`module`、`checker`、`tests`、`test`、`thesis_plans`；
`plan-20261128-002/003/014` 与 `plan-20261128-001/005` 增加 `fact_source`、`handoff`（002/003/014 另加 `sync_check`）。

论文侧（`D:\毕业论文\.devloop`）：**已合并（2026-11-28）**，`plan-20261123-005` 与 `plan-20261123-006` 的 `links` 已写入下面五个键，写前各留一份 `*.json.bak`；复跑 `--dry-run` 返回 SKIP（幂等）。脚本内容来自 manifest 的 `thesis_link_patch`，只增不改：

```powershell
cd C:\Users\b1995\Documents\AStockAgent
python tools/apply_thesis_plan_links.py --root D:\毕业论文 --dry-run   # 先看差异
python tools/apply_thesis_plan_links.py --root D:\毕业论文             # 写入，备份 *.json.bak
```

合并进 `plan-20261123-005` 与 `plan-20261123-006` 的 `links`：

```json
{
  "fact_source": "C:\\Users\\b1995\\Documents\\AStockAgent\\docs\\thesis_product_fact_source_v1.json",
  "handoff": "C:\\Users\\b1995\\Documents\\AStockAgent\\docs\\AStockAgent_thesis_product_handoff_v1.md",
  "sync_check": "C:\\Users\\b1995\\Documents\\AStockAgent\\tools\\check_thesis_sync.py",
  "data": "D:\\毕业论文\\论文需要\\data\\panel_monthly_v2.parquet",
  "counterpart_plan": "plan-20261128-006"
}
```

冲突规则：已存在同名 key 且值不同 → 报 `links.<key> 已存在且不同` 并退出 1，不覆盖；先人工对齐再重跑。

写入权限提示：本仓库 `.venv` 里的 python 写 `D:\毕业论文` 会被系统拒绝（`PermissionError: [Errno 13]`），而同一目录用 `C:\Anaconda\python.exe` 或 PowerShell 可写；论文会话在自己环境执行即可，无需改脚本。

---

## 6. 自检

```powershell
python tools/check_thesis_sync.py --check          # 有论文数据：校验 hash/行数/列/方向表
python tools/check_thesis_sync.py --check --json   # 机器可读
python tools/apply_thesis_plan_links.py --root D:\毕业论文 --dry-run   # 论文侧 links 差异
pytest tests/test_thesis_sync.py -q                # 契约与文档一致性测试
```

检查项：manifest 结构合法 → 面板 sha256 / 行数 / 列数 / 月份起止一致 → 12 个基础方向与 `factor_def_v2.csv` 一致 → 40 行方向表与导出 CSV 一致（多副本逐一比对，任一漂移即失败）→ 五个计划角色齐全、citable 与 not_citable 不交叠 → 交接文档覆盖全部条目标签。
数据未挂载时输出 `unavailable`（退出码 2），这是环境事实，不是口径失败。
