# PR 评审：评审：SW2014→SW2021 行业边界校准与 crosswalk

> 自动生成于 2026-11-16T17:53:43.354Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261116-003`
- 计划项：`plan-20261103-008`
- 分支：`feat/sw2014-sw2021-crosswalk` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-7bc34221-e10d-4cf7-8884-b65970d84206`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 0 / P2 0

## 本轮结论

第 3 轮全量复核（base d3ef09a → head bfd2b63，改动仅 docs/sw2014_sw2021_crosswalk.md 与 tests/test_sw_crosswalk.py，代码与两个入库 CSV 零改动）：第 2 轮 2 条问题均已修好且证据充分，历史 8 条 issue 全部 status=fixed，openP0=0 / openP1=0，四条验收标准逐条满足。verdict=approve，本轮 P0=0 / P1=0 / P2=0，判定完全解决。

【第 2 轮问题复核】
1. P1 iss-mwjjtisr-009abf（§2.6 敏感性结论错误）——已按实测改写并自洽：doc:29 现为「结构性边 32→34，新增 传媒→社会服务（旧侧 10.73%）与 化工→美容护理（新侧 46.15%）；一对多 4→5、社会服务结构性来源 2→3、美容护理获得首个结构性旧来源、new_without_structural_source 变空」。我用未改动的 docs/sw2014_sw2021_crosswalk.csv 逐边重判完全一致（:10 `传媒,社会服务,19,0.107345,0.256757`、:34 `化工,美容护理,12,0.030151,0.461538`，全表再无落在旧侧 [0.10,0.20) / 新侧 [0.40,0.50) 的边）。切点限定同步到位：§1:15「默认 20%/50% 切点」、§3.1/§3.2 标题、§3.4 美容护理条目、§5.2 新增段与「无结构性旧来源（默认切点）」条目。新增 test_sensitivity_at_10_40_changes_one_to_many（tests:246-264）我独立复算其 fixture：默认切点 8 条结构性边、one_to_many={化工,综合}、new_without_structural_source=["美容护理"]；10/40 下 10 条、one_to_many={传媒,化工,综合}、来源清单为空、share 断言 19/177 与 12/26 均正确——该用例正好锁死文档数字与代码口径。
2. P2 iss-mwjjtiu2-739bf2（分布缓存回归缺口）——两条用例到位：tests:525-555 同窗口 refresh=False 只在首次取快照（calls==1）并 assert_frame_equal，等价于证明 meta 的 edges_fingerprint 跨 Parquet 往返稳定（我第 2 轮无法执行的验证，由此闭环）；tests:557-594 阈值改 0.60/0.90 且窗口不变时 calls==2、meta thresholds 随之更新、metrics 非空，证明 distribution meta 失效重建。目标文件 25 个 test_ 方法与交付声明一致（22→25）。

【验收标准状态】
- AC-1 可审计映射表 + 未匹配清单：docs/sw2014_sw2021_crosswalk.csv 200 边/32 结构性边与 doc §3 逐边一致；§3.4 未匹配清单（仅旧 1 只、仅新 4 只、无旧行业无后继）齐备。
- AC-2 前后各 6 快照量化：docs/sw2014_sw2021_distribution_metrics.csv 13 行与 doc §4.1 逐格一致，前/后 6 月末均值 13.11%/.077599/6.59% 与 12.81%/.074713/1.92% 复核无误；§6 说明 2021-12-01 为桥接参照日不入 6+6 表。
- AC-3 落缓存/Parquet 且可复现：7 件产物（edges/summary/distribution/metrics/meta/bridge + docs CSV）命名带 index+start+end 与 meta 校验，`python -m astock.sw_crosswalk --refresh --export-docs` 重导出两 CSV 零 diff，§6 命令与过滤规则完整。
- AC-4 研究结论影响范围：docs/AStockAgent_research_conclusions_v1.3.md §4.1 已同步两侧闭合、非 SW 成员 8→11 只（标签 5→6）、行业对级 join 与回映口径，并与 crosswalk 文档口径一致。

【非阻断备注（不占 issue 配额，合并后择机处理）】
- active_industry_conflicts（astock/sw_crosswalk.py:155-205）截断到 20 条且未在 summary 标注截断，极端脏数据下审计可能误以为并列代码只有 20 个。
- distribution_table 的 sw_weight_share 对非 SW 行仍给非零值（分母为该日 SW 子集权重），按全行求和会 >1；使用方需按 is_sw 过滤，建议后续在 parquet 文档串或列名上再强调一次。
- cache/pit 下旧命名产物（sw_crosswalk_{distribution,metrics}_000300.SH.parquet、sw_crosswalk_bridge_000300.SH.json）仍在，cache 不入库，建议本地清理以免与新窗口产物混淆。

【后续动作】开发侧合并 PR #20 并把 plan-20261103-008 置 done（交付声明 CI run 43922165135 @1e24be4 与 43922508546 @bfd2b63 双 job SUCCESS）；PIT 主流程未改动，无需额外回归。本轮 approve 后线程标记为完全解决，后续若有新需求另开评审线程。

## 本轮问题

本轮未记录问题。

## 变更文件

- `astock/sw_crosswalk.py`
- `docs/AStockAgent_PIT_data_interfaces.md`
- `docs/AStockAgent_research_conclusions_v1.3.md`
- `docs/sw2014_sw2021_crosswalk.csv`
- `docs/sw2014_sw2021_crosswalk.md`
- `docs/sw2014_sw2021_distribution_metrics.csv`
- `tests/test_sw_crosswalk.py`

## 交给开发会话

1. 在分支 `feat/sw2014-sw2021-crosswalk` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
