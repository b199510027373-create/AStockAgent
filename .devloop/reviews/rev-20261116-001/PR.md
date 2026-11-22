# PR 评审：评审：PNG 导出基础设施：Kaleido/Chrome/字体与 CI

> 自动生成于 2026-11-16T02:33:41.027Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261116-001`
- 计划项：`plan-20261114-001`
- 分支：`feat/agent-chart-pipeline` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-a32e1f19-8de7-4baf-9957-8359ff6b3bbf`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 1 / P2 2

## 本轮结论

第 3 轮复核（full，705c88c..HEAD=e59961d+334c7fa）：round 2 的 3 条 issue 全部 fixed，无新增 P0/P1，验收 7 条全部有证据。

① P1 iss-mwimyt4m-96c199（CI 解耦）fixed：ci.yml:53-58 主 job 改 `coverage run -m pytest tests -q -m "not png"` 且删掉 fonts-noto-cjk 安装（diff 中 -29-33）；:122 png-export 改 `-m png --timeout=180` 并保留 CJK 字体（:79-85）与 Chrome 定位（:94-105）；新增 pytest.ini 注册 png marker；tests/test_agent_chart_png_export.py:20、test_agent_chart_png_zh.py:27 加 pytest.mark.png。CI run 43792183392：test 507 passed/2 skipped/5 deselected（无字体步骤、不跑真实导出）、png-export 5 passed in 11.91s（ASTOCK_REQUIRE_CJK=1）。仓库无 pyproject/setup.cfg 的 pytest 配置，新增 pytest.ini 不会遮蔽既有选项。ops 文档 §6 已同步改写。

② P2 iss-mwimyt6m-970024 fixed：store.py:201-219 新增 fail_run()（UPDATE status='failed', finished_at, error）；chart_render.py:1449-1455 在 require 失败、ensure_run 之后显式调用，覆盖"预建 running run"场景；tests/test_agent_chart_render.py:422-459 先 ensure_run(status="running") 再触发失败，断言 status/error/finished_at 且 artifacts=0。ops 文档 §4 同步。

③ P2 iss-mwimyt7o-da451d fixed：chart_render.py:1524-1529 meta 增加 png_reused=False / png_source_mode / png_last_attempt_error=None；store.py:308-313 复用旧 PNG 时置 png_reused=True、png_source_mode 取产出该 PNG 的模式（缺失时回退 png_mode）、本次原因落 png_last_attempt_error 且 png_error=None；tests/test_agent_store.py:489-557 覆盖 auto 失败复用场景（png_reused=True、degraded=False、png_error=None、png_last_attempt_error=本次原因）。ops 文档 §5 字段表更新。

剩余仅两件收尾事项（不构成新问题）：a) 开发者合并 PR #17 后将 plan-20261114-001 置 done 并补 merge 记录（当前 in_review/progress=95 符合"合并前不置 done"的约定）；b) 在评审工具里把本轮 3 条 issue 标为 fixed（开发者侧 feedback），使 handoff 的 openP1/openP2 归零、resolved=true；工具计数目前仍按 open 状态统计，不影响本次 approve 结论。合并后若 kaleido/Chrome 升级导致 png-export 变红，按 docs/AStockAgent_png_export_ops.md「版本升级检查清单」处理。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P1 · 主 test job 也被绑到真实 PNG 导出：无 per-test timeout，Chrome/kaleido 抖动会拖红整个主流水线 · `.github/workflows/ci.yml:16,29-33,38,62`（第 2 轮）
- P2 · require 失败时「run 记 failed」只对新建 run 生效：已有 run_id 因 ON CONFLICT DO NOTHING 不会被改成 failed · `astock/agent/chart_render.py:1437-1450`（第 2 轮）
- P2 · 复用旧 PNG 时 meta 留下矛盾的 provenance：degraded=False + 旧 png_path，却保留本次的 png_mode/png_error · `astock/agent/store.py:255-288`（第 2 轮）

## 变更文件

未获取到变更文件清单；请用 `devloop_git files` 重新获取。

## 交给开发会话

1. 在分支 `feat/agent-chart-pipeline` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
