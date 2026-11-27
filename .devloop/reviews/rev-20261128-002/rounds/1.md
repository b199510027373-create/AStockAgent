# PR 评审：评审：观察期后收口：Ruff 豁免移除与文档基线守护加固

> 自动生成于 2026-11-27T18:43:16.504Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261128-002`
- 计划项：`plan-20261117-001`
- 分支：`feat/plan-20261117-001-ruff-docs-guard (merged)` → `main`
- 轮次：第 1 轮（full）
- 结论：需要修改（request_changes）
- 开发会话：`session-c468f8e2-c39d-4e4d-a7fb-ebb0adfeb569`
- 本轮问题：3（P0 0 / P1 1 / P2 2）
- 线程未闭环：P0 0 / P1 1 / P2 2

## 本轮结论

## 第 1 轮（full）结论：request_changes

范围：plan-20261117-001 / 分支 feat/plan-20261117-001-ruff-docs-guard（已 squash 合并 e73c49c）/ base main。评审对象 = e73c49c 提交的 3 个文件（astock/scheduler.py、ruff.toml、tests/test_ci_quality_gates.py）+ 被守护的 5 个文档 + .coveragerc/.mypy.ini/CI 配置，已核对 ho-20261128-012/013/014。

### 已确认通过的部分
- **A1 满足**：ruff.toml 的 [lint.per-file-ignores] 整节已删除，无任何文件级豁免；test_ruff_config_has_no_per_file_exemptions 断言 `per_file == {}` 且新增豁免会红。
- **A2 代码行为等价**：8 处 except 收窄仅覆盖解析/IO 类异常（ValueError/TypeError/OSError/json.JSONDecodeError），_log、_recent_trading_days、_release_lock 的 try/except/pass → contextlib.suppress 改写语义与原文一致（scheduler.py:104/179/485/495）；幂等键、单实例锁、stale 结算路径未动；heartbeat_check 告警写盘收窄为 OSError 属正确收窄（磁盘满/无权限）。scheduler.py 现存 15 处 noqa: BLE001 均带非空理由。
- **A3 的变异自证成立**：test_docs_baseline_guard_goes_red_on_wrong_numbers 对 5 个文档逐一变异 + 注入「覆盖率门槛 50%」，并验证「单票仓位上限 50%」不误报。
- **A4 满足**：STALE_MARKERS 已去掉裸 "50%"，换成「覆盖率门槛 50%」等带语境的标记。
- 无 P0。

### 未通过：1 条 P1 + 2 条 P2（均 open）
1. **P1 iss-mwzbx2ma-bd56b1（.coveragerc:15-24,41,44）** 文档与守护基线仍锁 2026-11-17 快照，与本项自己 CI run 46047574586 的实测（12474/2425 = 80.56%、952 passed、mypy 45 files；本地 953 passed）矛盾；5 个受守护文档仍写 9248/2178=76.45%、76.68%、610 passed、mypy「36 个源文件」（实测 53-9+1=45）。后果：谁按实测把文档改对，守护反而变红；A3 点名的 architecture_v1.3.md:107 里 mypy 数字不在任何 required 片段内，改错不红。附带 tests/test_ci_quality_gates.py 中 52/43/44 的注释算术与实测 53/44/45 不符。
2. **P2 iss-mwzbx2no-cbaa41（ruff.toml:16-19）** 新写的「BLE001 必须写理由」被表述为全仓规则并由 test_blind_except_exemptions_carry_reasons 守护，但该用例只扫 scheduler.py/jobs.py（tests/test_ci_quality_gates.py:349）；全仓另有 11 处无理由 noqa: BLE001（ui.py:217、backtest_ui.py:57、factor_ui.py:29、optimize_ui.py:113、strategy_ui.py:66、track_ui.py:13、llm.py:72、recommend.py:125/:148、agent/runtime.py:703、use_cases.py:773）。
3. **P2 iss-mwzbx91j-da1a81（ho-20261128-012.md:22,52）** A2 交付证据与代码不符：handoff 称「保留 6 处 BLE001」，scheduler.py 实测 15 处，另有 9 处站点是否经过逐条审计无从查证。

### 谁在什么条件下修复 / 何时算完全解决
- 由**开发**在后续 PR（或本项 follow-up）内修复 3 条：按 CI 实测刷新 .coveragerc 基线注释与 fail_under（升到 80.56，或在 :41 写明保留 76.45 的理由与复核时点）+ 同步 5 文档数字并把 mypy 文件数/用例数纳入 _doc_baseline_problems；让 ruff.toml 的 BLE001 措辞与守护范围自洽（限缩措辞或扩扫描范围并补齐 11 处理由）；在下一份 update handoff 补全 15 处 BLE001 审计表。
- **完全解决的判定**：openP0=0、openP1=0（即 iss-mwzbx2ma-bd56b1 关闭），且 `grep -rn "36 个源文件" docs/ README.md` 为空、docs 任一受守护数字改错仍变红、`ruff check astock tests` 为 0、mypy/pytest 全绿。两条 P2 可在同一轮或后续质量项关闭，但必须有一份写明结论的 update handoff。

### 局限（需下一轮注意）
本会话无 shell 工具，无法执行 `gh auth status`、本地 pytest/ruff/mypy，也无法直接调 GitHub API：CI run 46047574586 / 46048029684 / 46049387833 的绿状态仅与 handoff ho-20261128-012/013/014 及 docs 交叉核对（数字互相自洽，但未独立复验 run 结论）。下一轮如有环境，请优先独立确认这 3 个 run 的结论与 coverage TOTAL 行。

## 本轮问题

### P1 · 文档/守护基线仍锁 2026-11-17 快照，与本项自己 CI 实测矛盾（覆盖率 76.45% vs 80.56%、mypy 36 vs 45 files、610 vs 952 passed），且 A3 点名的 architecture:107 里 mypy 数字未被守护

- 位置：`.coveragerc:15-24,41,44`
- 状态：未闭环
- 置信度：0.85

**证据**

.coveragerc:16「本 PR CI Linux（2026-11-17, run 43935079016）：9248 stmts / 2178 miss = 76.45%（门禁权威值）」、:17「本地 Windows（…610 passed）：9248 / 2157 = 76.68%」、:41「P1（当前）：76.45% —— 等于 CI 实测显示值」、:44 fail_under = 76.45，全部是 2026-11-17 的数字。而本项交付 handoff ho-20261128-012 记录的 CI run 46047574586（同一分支）：952 passed / 4 skipped、coverage TOTAL 12474 stmts / 2425 miss = 80.56%，mypy「Success 45 source files」，本地 953 passed。
由此 5 个受守护文档全部与实测脱节：docs/AStockAgent_architecture_v1.3.md:107「覆盖全部非 UI 模块（36 个源文件 Success）…覆盖率门槛 76.45%（=CI Linux 实测显示值，本地 76.68%）」；docs/AStockAgent_CI_work_calendar.md:11、:144「610 passed, 1 skipped, 5 deselected」、:146「Success: no issues found in 36 source files」、:187「CI Linux 76.45%（9248/2178）本地 76.68%（9248/2157）」；docs/AStockAgent_development_status_2026-11-08.md:142/:145；docs/AStockAgent_product_snapshot_v1.3.md:51；README.md:23。
事实核对：astock 下 53 个 .py，mypy.ini:4 exclude 9 个 UI 模块 ⇒ 44 受检 + app.py = 45，与 handoff 的「45 files」自洽；docs 的「36 个源文件」是 plan-20261103-012 时期的旧计数。tests/test_ci_quality_gates.py 的 _doc_baseline_problems 只断言从 .coveragerc 注释解析出来的 (stmts, miss, pct, passed)，因此：(a) 谁按实测把 docs 改成 952 passed / 80.56% / 45 files，守护立刻变红；(b) architecture_v1.3.md:107 恰好是 A3 点名要守的行，而这一行的「36 个源文件」不在任何 required 片段里，改错也不会红（同行的「覆盖率门槛 76.45%」「本地 76.68%」才被守）。另 tests/test_ci_quality_gates.py 的 EXPECTED_CHECKED_MODULES=44 上方注释写「astock 下共 52 个 .py…= 43 个受检 + app.py = CI 日志里的 44 files」，52/43/44 三个数与实测 53/44/45 全部对不上。

**建议**

由开发在同一个 PR 内三件事一起做：(1) 用本项 CI run 46047574586 的真实值重写 .coveragerc:15-28 的基线注释（CI 12474/2425=80.56%、952 passed；本地 953 passed 及本地覆盖率实测值），并按 .coveragerc:28「以 CI 实测为准、只升不降」处理 fail_under：要么升到 80.56，要么在 :41 显式写「本项未同步 ratchet，保留 76.45 的理由 + 下次复核时点」——现在 :41 的「等于 CI 实测显示值」已是假命题，不能两种都不做；(2) 同步 5 个文档里被守护的数字（mypy 45 files、952 passed、80.56%），尤其把 architecture_v1.3.md:107、CI_work_calendar.md:146 的「36 个源文件」改为与 CI 日志一致的 45 files；(3) 把 mypy 文件数与 pytest 用例数也纳入 _doc_baseline_problems（mypy 数从 EXPECTED_CHECKED_MODULES+1 推导、用例数从 .coveragerc 解析），并修正 EXPECTED_CHECKED_MODULES 上方 52/43/44 的注释。判定完全解决：`grep -rn "36 个源文件" docs/ README.md` 为空；docs 里任一受守护数字（含 mypy 文件数、passed 数、覆盖率）被改错时 test_docs_baseline_guard_goes_red_on_wrong_numbers 变红；ruff/mypy/pytest 仍全绿。

### P2 · A2 交付证据与代码不符：handoff 称「保留 6 处 BLE001」，astock/scheduler.py 实测 15 处，另外 9 处是否经过逐条审计无从查证

- 位置：`.devloop/handoffs/ho-20261128-012.md:22,52`
- 状态：未闭环
- 置信度：0.88

**证据**

handoff ho-20261128-012 两处写「保留的 6 处 BLE001 边界兜底（_is_trading_day、startup_maintenance×3、run_once 外层、_acquire_lock）全部补写理由」（.devloop/handoffs/ho-20261128-012.md:22），并把评审动作写成「复核 A2 的 6 处保留 BLE001」（.devloop/handoffs/ho-20261128-012.md:52）。实测 `grep -n "noqa: BLE001" astock/scheduler.py` 返回 15 处：scheduler.py:80、:162、:419、:424、:429、:434、:473、:514、:528、:548、:606、:610、:618、:632、:661（每条理由均非空，代码本身没问题）。差额 9 处（162 迁移单条脏数据、514 台账预检、528 读 job 状态、548 写 running、606 跟踪结算、618 写 failed 状态、632 loop 启动自检、661 loop 单轮异常）不在 handoff 清单里，无法据交付文档判断这些站点是否经过 A2 要求的「逐条审计、能收窄则收窄」判断，评审只能重新做一遍审计。

**建议**

下一份 update handoff 里把 A2 的审计表补全为 scheduler.py 全部 15 处 noqa: BLE001 的逐条清单：行号 + 站点 + 结论（保留/收窄/改 suppress）+ 保留理由是否已在代码注释里。同时注明 8 处收窄与 3 处 suppress 改写的对应行（_log/_recent_trading_days/_release_lock），以及 heartbeat_check 告警写盘由 Exception 收窄为 OSError 的语义变化。判定完全解决：handoff 清单行号与 `grep -n "noqa: BLE001" astock/scheduler.py` 输出一一对应，且 scheduler.py:462 附近 migrate/read/write 各站点的收窄理由与代码注释一致（不接受「6 处」这类与实测不符的计数）。

### P2 · ruff.toml 新写的「BLE001 必须写理由」策略句声称由测试守护，但该用例只扫 scheduler.py/jobs.py，全仓 11 处无理由 noqa 未被覆盖

- 位置：`ruff.toml:16-19`
- 状态：未闭环
- 置信度：0.9

**证据**

ruff.toml 新增的策略段（:16-19）把约定写成全仓规则：「BLE001 只允许出现在兜底处且必须写理由」「tests/test_ci_quality_gates.py::test_blind_except_exemptions_carry_reasons 守护这条约定」。但该用例的 FILES 只含 ("astock/scheduler.py", "astock/jobs.py")（tests/test_ci_quality_gates.py:349），全仓另有 11 处 `# noqa: BLE001` 完全不带理由：astock/ui.py:217、astock/backtest_ui.py:57、astock/factor_ui.py:29、astock/optimize_ui.py:113、astock/strategy_ui.py:66、astock/track_ui.py:13、astock/llm.py:72、astock/recommend.py:125、astock/recommend.py:148、astock/agent/runtime.py:703、astock/use_cases.py:773。策略句与守护范围不一致，读 ruff.toml 的人会误以为「noqa 一定有理由」已被 CI 强制。

**建议**

二选一，二选一后必须自洽：(a) 把 ruff.toml:16-19 的策略句限定为实际范围——「调度/任务边界（astock/scheduler.py、astock/jobs.py）的 BLE001 必须写理由，其余模块暂不强制」；(b) 把 test_blind_except_exemptions_carry_reasons 的遍历范围扩到 astock/ 全量（rglob("astock/**/*.py")），接受上述 11 处立即转红，逐条补「 - 理由」或删除多余 noqa（UI 模块的理由可按「UI 事件回调边界，异常不应打断界面」统一模板）。判定完全解决：ruff.toml 的策略措辞与用例实际扫描范围一致，`ruff check astock tests` 仍为 0，且人为删掉 scheduler.py 任一 noqa 理由时用例变红。

## 变更文件

未获取到变更文件清单；请用 `devloop_git files` 重新获取。

## 交给开发会话

1. 在分支 `feat/plan-20261117-001-ruff-docs-guard (merged)` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
