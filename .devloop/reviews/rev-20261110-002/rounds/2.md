# PR 评审：评审：「无显著 alpha」实时横幅（ledger 驱动）

> 自动生成于 2026-11-10T08:17:09.657Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261110-002`
- 计划项：`plan-20261103-005`
- 分支：`—` → `main`
- 轮次：第 2 轮（full）
- 结论：通过（approve）
- 开发会话：`session-ae4db07f-a50d-4fc0-a44f-fabaf196353b`
- 本轮问题：2（P0 0 / P1 0 / P2 2）
- 线程未闭环：P0 0 / P1 0 / P2 2

## 本轮结论

第 2 轮全量复核（rev-20261110-002 / plan-20261103-005）。评审对象：修复提交 4d41512（PR #6，已 merge 到 main e5b98e8）。

结论：approve。openP0=0，openP1=0，openP2=2（本轮新增，均不阻断）。

第 1 轮 3 条问题逐条复核，均已真实闭环：
1) iss-mwadylup（横幅缺 load 刷新）已修：astock/recommend_ui.py:52-54 构建期取 banner 并加 elem_id="alpha-banner"，:103 返回组件，:106-112 `bind_banner_refresh` 注册 `demo.load(banner_markdown, outputs=banner)`；app.py:500 在 Blocks 构建后接线。测试新增 `test_builder_returns_banner_that_is_refreshed_on_load`（结构断言 load 依赖的 outputs 含 alpha-banner）与 `AppBannerWiringTests`（对真实 app 断言同一绑定）。「打开页面即最新」成立。
2) iss-mwadylv9（tracking 只靠手点跟踪页）已修：astock/track.py 抽 `_detail_rows` / `_save_tracking_rows`，新增 `refresh_tracking(horizon, limit=30, lookback_days=120)` 只补算「T+5 excess 仍为空」的近期批次；astock/scheduler.py:275-284 在推荐成功分支调用并以 try/except 隔离，stale 分支不调用（tests/test_hardening.py 两条测试分别锁定 called/not_called）。astock/ledger.py:342-367 新增 `tracking_run_ids(complete_only=True)`、`live_run_dates(500)`。横幅侧 `alpha_banner_snapshot` 携带 tracking_date/latest_live_date/pending_days，`_pending_note` 在落后时写明「跟踪数据截至 X，最新推荐日 Y，N 个推荐日尚无 T+5 结果（未到期或待结算）」，不再把手动未结算伪装成「样本不足」。tests/test_alpha_banner.py 的 TrackingSettlementTests 用真实 temp ledger 验证「结算一次后第二次无待结算」。
3) iss-mwadylvz（DB 异常被吞）已修：snapshot 不再吞异常（astock/track.py），astock/use_cases.py:578-587 捕获后用 `alpha_banner_error_markdown` 输出「⚠️ 统计读取失败…不代表样本不足」（html.escape 防注入），tests/test_use_cases.py 与 test_alpha_banner 的 `test_database_error_propagates_instead_of_looking_like_no_sample` 双向锁定。

验收标准对照：①数字来自 ledger/stats 实时聚合并每次页面加载重算；②样本数/独立窗口/p 或「样本不足」齐全；③样本到门槛后横幅切到 no_alpha/positive/negative 且调度任务会自动累积样本；④p 仅在 effective_n≥10 且 stats 可算时出现，DB 失败单独显示错误不输出结论。ADR-006/010、ARCH-01/02、STAT-01 均未被破坏（UI 仍只装配，p 仍只来自 stats.newey_west）。CI（PR #6 三次 + main merge 两次）与 pytest 239 passed / mypy 31 files / ruff / banner_smoke 证据齐全。

本轮新增两条 P2（不阻断，但需跟进）：
- iss-mwaf2gq4（astock/track.py:1172-1174）：compute_tracking 的写库块抽函数后不再有外层 try/except，save_tracking 抛错会让「推荐跟踪」页整体 failed、表格清空（改动前只丢持久化）。建议包 try/except 并在 status 里提示「本次仅页面展示」，补 sqlite 写失败用例。
- iss-mwaf2gr0（tests/test_alpha_banner.py:237-245）：AppBannerWiringTests.setUpClass 把任意 app 导入异常降级为 SkipTest，一旦 app.py:500 的接线被删，测试会 skip 而 CI 仍绿。建议只对 ModuleNotFoundError 跳过。

谁在什么条件下修复、何时算完全解决：两条 P2 由本分支开发者（或接手该模块的开发者）在工作分支上按建议修改并补测试；计划项可以现在置 done——因为 openP0=0、openP1=0 且三条验收要点均有测试与 CI 证据支撑；两条 P2 需在下一次触碰 astock/track.py 或 tests/test_alpha_banner.py 的迭代中关闭，若选择延后请在评审线程里明确「不采纳/延后」理由与责任人，否则下一轮 diff 复核会继续保留为 open。

## 本轮问题

### P2 · compute_tracking 的 ledger 写入失败不再被吞掉，会让「推荐跟踪」页整体失败

- 位置：`astock/track.py:1172-1174`
- 状态：未闭环
- 置信度：0.7

**证据**

本次提交把原来 compute_tracking 里 `try: ... save_tracking(...) except Exception: pass` 的写入块抽成 `_save_tracking_rows()`（astock/track.py:1044-1075，只 try 了 import），并在 astock/track.py:1174 直接调用、无异常保护。于是 SQLite 写失败（如调度线程正在写 ledger 时的 `database is locked`）会从 compute_tracking 抛出，被 astock/use_cases.py:470-480 转成 `failed` + `_empty_tracking_payload`，整个「推荐跟踪」页变成 ❌ 且表格为空；改动前同样的写入失败只会丢持久化，表格仍能正常渲染。这是本次修复引入的与横幅无关的行为回归。

**建议**

在 compute_tracking 里包一层 `try/except Exception as exc:` 并 `status += f" ⚠️ 跟踪结果未写入 ledger（{type(exc).__name__}），本次仅页面展示。"`，保持表格可用；或在 `_save_tracking_rows` 内逐个 batch try 并把失败数返回给调用方。补一条测试：patch `ledger.save_tracking` 抛 sqlite3.OperationalError 时 `compute_tracking()` 仍返回非空 detail 且 status 含提示。修复人：本分支开发者。完全解决判据：写库失败不再让跟踪页整体失败，且有测试锁定。

### P2 · app 导入失败被降级成 SkipTest，真实 app 的 load 绑定失效时 CI 仍会绿

- 位置：`tests/test_alpha_banner.py:237-245`
- 状态：未闭环
- 置信度：0.8

**证据**

AppBannerWiringTests.setUpClass 用 `except Exception as exc: raise unittest.SkipTest(...)`（tests/test_alpha_banner.py:241-245）把 app 模块的任意导入/构建异常都降级成 skip。它是唯一验证「真实 app 注册了 load→banner」的用例；一旦 app.py 的 `bind_banner_refresh(app, recommend_banner)`（app.py:500）被误删或 import 链断掉，该测试会静默 skip，CI 仍是绿的，正好放过第 1 轮 P1 要防的回归。

**建议**

只对确实可选的依赖缺失做跳过（如 `except ModuleNotFoundError`），其余异常直接抛出使测试失败；或者在 setUpClass 里只做 `import app`（不吞异常），把重依赖缺失用 `unittest.skipUnless` 显式声明。修复人：本分支开发者。完全解决判据：破坏 app.py 的 load 绑定时该测试为 fail 而非 skip。

## 变更文件

- `warning: in the working copy of '.devloop/reviews/rev-20261110-002/issues/iss-mwadylup-e7d967.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261110-002/issues/iss-mwadylv9-1f2a3a.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261110-002/issues/iss-mwadylvz-65b1af.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261110-002/thread.json', LF will be replaced by CRLF the next time Git touches it`

## 交给开发会话

1. 在分支 `当前分支` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
