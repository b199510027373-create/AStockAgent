# PR 评审：评审：根目录卫生与仓库单一真源校验

> 自动生成于 2026-11-21T09:22:25.751Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261121-001`
- 计划项：`plan-20261103-013`
- 分支：`—` → `main`
- 轮次：第 2 轮（full）
- 结论：通过（approve）
- 开发会话：`session-4106a9e9-99f3-4ef0-b0b5-fdc3b93c25db`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 0 / P2 4

## 本轮结论

第 2 轮复评（scope=full，分支 chore/repo-single-source 与 origin 同步，PR #44 open，HEAD=cd097ad）。本轮针对 ho-20261121-013 的修复提交逐条核实：256de94（fix(repo): harden source guard after review）、7808cdb（chore(devloop): record handoff and review history）、cd097ad（chore(devloop): update handoff for review fixes）。上一轮 4 条 P2 全部验证已修复，未发现 P0/P1，也未新增阻断项。

逐条核实结果：
1) 文档与 git status 一致性（原 iss-mwq6p6ia-79c506）已修复：docs/AStockAgent_repo_source_of_truth.md:81-82 改为「由开发在会话结束时随仓库提交；未提交前会短暂出现在 git status」；7808cdb 把 122 个 .devloop 正式记录入库（对 tools/ 等路径 diff 为空，确认该提交只含记录）；当前 `git status --short` 仅剩本轮评审自身产物（M .devloop/reviews/rev-20261121-001/thread.json、?? rounds/2.json），文档口径与实测一致。
2) pushurl 校验（原 iss-mwq6p6j3-9e6469）已修复：tools/check_repo_source.py:188-201 新增 `git remote get-url --push origin` 取值、normalize_remote 归一化并与 canonical_expected 比较，问题文案「origin.pushurl 指向 ...，期望 ...」；tests/test_repo_source_check.py:157-170 新增 `test_check_rejects_pushurl_to_other_remote`（设置 remote.origin.pushurl=https://github.com/other/repo.git 后断言 ok False）；文档 §2 第 3 项与 §4 失败排查表同步补充。
3) 测试强度（原 iss-mwq6pc1u-dcf6c2）已修复：tests/test_repo_source_check.py:51-64 新增 `_is_ignored()` 走 `git check-ignore -q`；:244-264 用 10 个正例（astock-chat-x/、.pytest-x/、pip-x/、tmp-x/、_archive/x、.devloop/drafts/x、handoffs/archive/x、reviews/archive/x、.tmp-tests/x、release/）与 3 个反例（astock/data.py、docs/repo_source.md、tests/test_repo_source_check.py）验证行为；:267-272 恒真断言改为 `tempfile.gettempdir()` 绝对相等 + `.tmp-tests/x` 被忽略。文件内 14 个 test 定义经参数化展开为 23 个用例，与 handoff 报告 23 passed 一致。
4) 一次性 ignore 规则（原 iss-mwq6pc31-f3713f）已修复：.gitignore 删除 /font_test*、/probe_*、/resume_*、/甲.txt、/01_test.ipynb、/my_stocks.csv、/.dsh_pytest.py、/.thesis_data/ 等逐文件名规则及与 L44/L46 的重复项，仅保留根锚定运行期目录、/coverage_*.txt、/.pr-*.md、/_archive/ 与 DevLoop drafts/archive；status 干净说明根目录无残留文件被 ignore 掩盖；docs 同步改为「一次性个人文件已移入 _archive/」。

架构与契约：本轮改动只涉及 tools/check_repo_source.py、tests/test_repo_source_check.py、.gitignore、真源文档与 .devloop 记录，未触及 astock/ 与 app.py；ci.yml 与 tests/conftest.py 相对 309591a 无变化；LEDGER-01 要求的数据忽略规则（data/*.db、data/agent/、data/recommendations/）保持。

验证边界：本会话无 shell/gh，未复跑 pytest/mypy/CI；采信 handoff 证据（test_repo_source_check 23 passed、全量 855 passed/3 skipped、mypy 44 files、ruff 通过、check_repo_source --any-branch exit 0、CI run 44888601333 test+png-export SUCCESS）。

结论：verdict=approve，openP0=0、openP1=0。完全解决条件：开发合并 PR #44，并在合并后提交本轮评审产物（thread.json、rounds/2.json 等）使 .devloop 工作树恢复干净，同时在 main 上以严格模式运行一次 `python tools/check_repo_source.py` 确认 exit 0；完成该步后 plan-20261103-013 可置为 done。随后由开发按计划把 plan 状态更新为 done 并写一条 merge 状态 update handoff。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P2 · 真源说明文档声称正式 handoff/review 记录“随仓库提交”，实际全部未跟踪 · `docs/AStockAgent_repo_source_of_truth.md:78-79`（第 1 轮）
- P2 · 校验脚本只看 remote.origin.url，pushurl 指向别处仍会通过 · `tools/check_repo_source.py:169-185`（第 1 轮）
- P2 · .gitignore 相关测试只做字符串包含判断，且临时目录断言恒真 · `tests/test_repo_source_check.py:212-234`（第 1 轮）
- P2 · 根目录卫生段把一次性个人文件写死进 .gitignore，且与既有规则重复 · `.gitignore:78-97`（第 1 轮）

## 变更文件

- `.github/workflows/ci.yml`
- `.gitignore`
- `README.md`
- `docs/AStockAgent_CI_work_calendar.md`
- `docs/AStockAgent_chart_pipeline_decisions_v1.md`
- `docs/AStockAgent_contracts_PR_handoff_2026-11-09.md`
- `docs/AStockAgent_contracts_delivery_2026-11-09.md`
- `docs/AStockAgent_development_status_2026-11-08.md`
- `docs/AStockAgent_repo_source_of_truth.md`
- `tests/conftest.py`
- `tests/test_repo_source_check.py`
- `tests/test_sw_crosswalk.py`
- `tools/check_repo_source.py`

## 交给开发会话

1. 在分支 `当前分支` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
