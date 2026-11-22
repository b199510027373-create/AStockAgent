# PR 评审：评审：修复 P2：AppBannerWiringTests 导入异常不得降级为 skip

> 自动生成于 2026-11-22T09:08:49.160Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261121-002`
- 计划项：`plan-20261110-002`
- 分支：`test/plan-20261110-002-app-banner-skip` → `main`
- 轮次：第 2 轮（full_recheck）
- 结论：通过（approve）
- 开发会话：`session-d4c01b00-1bac-4b27-8eab-1192501dc281`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 0 / P2 0

## 本轮结论

第 2 轮复核（full_recheck，rev-20261121-002 / plan-20261110-002）：按 developer 交付 ho-20261122-001 核对「合并后是否完全解决」。

结论：approve。openP0=0，openP1=0，openP2=0，0 issue——无新增可修项，第 1 轮给出的四项完全解决判据全部满足。

本轮独立核对的事实（不采信 handoff 结论本身）：
1) 合并已落地：本地 main 的 `devloop_git log` 显示 5a36c10「Merge pull request #45 from b199510027373-create/test/plan-20261110-002-app-banner-skip」，父提交为 1762b4d / 92489be，其上仅 b1bc904（.devloop 记录）。`devloop_git show base=5a36c10` 显示 tests/test_alpha_banner.py 82 行变更随合并进入 main。
2) main 内容与被评审实现逐字一致：`devloop_git diff base=3abe12b path=tests/test_alpha_banner.py` 输出为空，`devloop_git diff base=main path=tests/test_alpha_banner.py` 输出为空（工作区干净、当前分支 main）。即合并没有引入改动，评审过的实现就是线上实现。
3) 行为仍在位：main 上 tests/test_alpha_banner.py:283 `APP_OPTIONAL_DEPENDENCIES = frozenset({"gradio"})`；:286-300 `import_app_for_wiring_test(importer=importlib.import_module)` 只对 `ModuleNotFoundError` 且 `(exc.name or "").split(".")[0]` 命中白名单时 `raise unittest.SkipTest`，其余 `raise`；:306-308 setUpClass 只调用该 helper（宽泛 except 不存在）；:310-311 真实接线断言要求 load 输出含 alpha-banner，对应 app.py:505 `bind_banner_refresh(app, recommend_banner)`；:330-363 五条 GuardTests 覆盖 app/astock.* 缺失、ImportError、Blocks 构建异常与 setUpClass 不吞异常。
4) 计划项已收尾：plan-20261110-002 status=done、progress=100，acceptance 补齐 4 条（import 链断→fail / 删接线→FAIL / 仅 gradio 缺失允许 skip / 目标+全量+mypy+ruff 全绿且 PR #45 合并 5a36c10），links 新增 merge=5a36c10、reviewThread=rev-20261121-002，links.test 含变异测试与「合并后 main 复跑」两类证据。
5) 测试证据（developer 记录）：main @5a36c10 复跑 pytest tests/test_alpha_banner.py -q → 22 passed / 2 subtests；pytest tests -q -m "not png" → 860 passed, 3 skipped, 5 deselected, 20 subtests；mypy astock app.py → 44 files no issues；ruff check astock tests → All checks passed；main push CI run 45003030655 success 1m29s；PR #45 state=MERGED、合并前 mergeable/mergeStateStatus=CLEAN；合并前最后一轮 PR CI run 44891421054 test+png-export 全绿。

限制说明：本会话仍无 shell/run_code 工具，`gh auth status`、`gh pr checks 45`、`gh pr view 45` 无法由我执行；合并事实已由本地 git 独立确认（merge commit 与父提交可验），CI 与 PR 状态仅来自 ho-20261122-001 记录，属于「记录可信但未本轮重跑」。

无遗留问题与责任人：没有需要开发者再修改的代码、测试或计划数据。旧线程 rev-20261110-002 的 iss-mwaf2gr0 已是 fixed 且该线程 approved，无需在评审侧改写；plan.links.reviewThread=rev-20261121-002 已建立交叉引用，满足「关闭依据可回溯」。
边界提示（不构成 issue）：按 ho-20261122-001 next 第 3 条，后续若再改 tests/test_alpha_banner.py 的导入/跳过策略，应新开评审线程，不复用本条已 resolved 的 approve 结论。

## 本轮问题

本轮未记录问题。

## 变更文件

- `tests/test_alpha_banner.py`

## 交给开发会话

1. 在分支 `test/plan-20261110-002-app-banner-skip` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
