# PR 评审：评审：「无显著 alpha」实时横幅（ledger 驱动）

> 自动生成于 2026-11-10T07:46:09.469Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261110-002`
- 计划项：`plan-20261103-005`
- 分支：`—` → `main`
- 轮次：第 1 轮（full）
- 结论：需要修改（request_changes）
- 开发会话：`session-ae4db07f-a50d-4fc0-a44f-fabaf196353b`
- 本轮问题：3（P0 0 / P1 2 / P2 1）
- 线程未闭环：P0 0 / P1 2 / P2 1

## 本轮结论

第 1 轮全量评审（rev-20261110-002 / plan-20261103-005「无显著 alpha 横幅」）。

结论：request_changes。openP0=0，openP1=2，openP2=1。实现本身在统计口径上是对的（p 全部来自 astock/stats.py 的 newey_west/effective_n，NW lag=horizon−1、Student-t、有效独立样本 <10 不显示 p；每日 Top2−其余配对差与 astock/track.py:122-198 significance_table 的「Top2 - 其余」行同口径；recommend_ui 只做组装，符合 ARCH-01/STAT-01/ADR-006；live-only 过滤有 astock/ledger.py:315-339 与 tests/test_core_paths.py:120-186 覆盖；10 条横幅单测覆盖 insufficient/positive/no_alpha/negative/unavailable/ledger 失败/无 rank 等状态）。因此 P0 不成立。

未通过的原因集中在「实时性」这一验收要点：
1) P1（iss-mwadylup，astock/recommend_ui.py:52-54,88-101）：banner 的值在 gr.Blocks 构建期算一次就固定，仓库内 astock/ 下 grep `\.load(`/`gr.Timer` 零命中，app.py:374/487/502 也没有 load 绑定，只有三个手工点击事件会刷新 → 进程内 16:40 调度（astock/scheduler.py:38 默认 16:40，app.py:502 启动）写入的 ledger 数据不会反映到已打开或新打开的页面，横幅长期停在启动时的状态，与 README.md:108「实时显示」及验收 1/3 不符。
2) P1（iss-mwadylv9，astock/track.py:319-327,944-1073）：横幅唯一数据源 tracking 表只由 compute_tracking 写（astock/track.py:1070），调用链 astock/use_cases.py:467-470 ← astock/track_ui.py:17-19 ← track_ui.py:59 按钮；调度路径不刷新。用户不手工点开「推荐跟踪」页时横幅永远「实盘样本 0 · 样本不足」，验收 2/3 在常规路径不可达，README 的「直接聚合 tracking 表」也隐瞒了这个前置手动步骤。
3) P2（iss-mwadylvz，astock/track.py:319-327）：ledger 异常被 `except Exception: rows = []` 吞成「样本不足」，DB 锁/损坏与「样本真的不足」不可区分，astock/use_cases.py:578-587 的 error 分支在真实故障下不可达。

修复责任与完成判据（开发者在本分支 feat/plan-20261103-005-alpha-banner 上修复，修复后进入第 2 轮全量复核）：
- 谁：本分支开发者（不改变对外契约，无需新 ADR）。
- 修什么：① 页面加载即刷新横幅（app.load / Tab.select / Timer，让 build_recommend_tab 暴露 banner 组件），并加结构断言测试锁定该绑定；② 让 daily 调度成功后增量刷新 tracking（或横幅明确显示「跟踪数据截至 X、落后 N 个交易日」并提供补算入口），消除「样本恒为 0」；③ snapshot 区分 error 与 insufficient，补 sqlite3.OperationalError 用例。
- 何时算完全解决：上述 3 条全部落地且有对应测试/手工验证记录（重启应用后刷新页面能看到最新数字；不点「推荐跟踪」也能在隔日看到样本增长或明确的 stale 提示），且 openP0=0、openP1=0；仅 openP2 残留时仍可 approve，但要保留 P2 的跟进记录。

## 本轮问题

### P1 · 横幅只在页面构建时计算一次，缺少 load 刷新，定时任务产出不会反映到页面

- 位置：`astock/recommend_ui.py:52-54,88-101`
- 状态：未闭环
- 置信度：0.85

**证据**

astock/recommend_ui.py:54 `banner = gr.Markdown(value=banner_markdown())` 在 `with gr.Blocks()` 构建期调用一次即固定；随后仅 :92/:96/:101 三个手工事件 `.then(banner_markdown, outputs=banner)` 会刷新。app.py:374 `with gr.Blocks(...) as app:`、:487 build_recommend_tab()、:502 start_recommend_scheduler()：全仓 grep `\.load` 无任何 load/Tab.select 绑定。进程内调度器（16:40 自动生成推荐）写 ledger 后不会触发上述任一事件，用户刷新浏览器仍看到服务启动那一刻的数字，与 README.md:108「实时显示」及验收标准 1「实时聚合」/3「样本达到门槛后横幅自动…转为正向提示」不符（横幅会一直停在 insufficient）。

**建议**

在 app.py 构建 Blocks 后注册页面加载刷新：让 build_recommend_tab 返回 banner 组件，然后 `app.load(banner_markdown, outputs=banner)`（或在 tab 内用 `banner.load(...)`/`gr.Timer`）；同时补一条结构断言测试（例如断言 Blocks 的 dependencies 中存在 trigger=load 且 outputs 含 banner 的依赖），并在 README 说明「每次打开页面/刷新时重新聚合」。修复人：本分支开发者。完全解决判据：打开页面即拿到最新 ledger 数字，且有测试锁定该 load 绑定。

### P1 · 横幅数据源 tracking 表只由「推荐跟踪」页手工写入，调度任务从不刷新，样本永远为 0

- 位置：`astock/track.py:319-327,944-1073`
- 状态：未闭环
- 置信度：0.7

**证据**

横幅唯一数据源是 ledger 的 tracking 表（astock/ledger.py:315-339 tracking_rows），而该表唯一写入点是 compute_tracking() 内的 save_tracking（astock/track.py:1070），调用链为 astock/use_cases.py:467-470 run_tracking ← astock/track_ui.py:17-19 run_track ← track_ui.py:59 按钮点击；grep compute_tracking/run_tracking 在 jobs.py/scheduler 中无任何调用。因此用户只要不手工点开「推荐跟踪」页，tracking 表永远为空：即使 ledger 里已有大量 source=live 的每日推荐，横幅仍固定显示「实盘样本 **0** 个交易日 · 样本不足，不显示显著性」，验收标准 2/3 在常规使用路径上不可达，README.md:108「直接聚合 SQLite tracking 表实盘记录」也掩盖了这个前置手动步骤。

**建议**

让每日自动推荐任务结束后同步刷新 T+5 结算（在调度器成功回调里调用 compute_tracking 等价逻辑，或对 tracking 缺失/落后于最新 live run 的天数做增量补算）；若出于网络成本不在调度中补算，则横幅必须显式展示数据截止日（如「跟踪数据截至 2026-10-30，落后 N 个交易日」）并给出触发补算的入口，而不是声称「样本不足」。补测试：存在 live run 但 tracking 落后时，横幅不得断言「样本不足」/或必须显示 stale 文案；以及调度路径确实刷新了 tracking。修复人：本分支开发者。完全解决判据：不手工打开跟踪页，隔日打开推荐页即可看到随样本增长而变化的数字。

### P2 · DB 异常被吞成「样本不足」，use_case 的 error 分支实际不可达

- 位置：`astock/track.py:319-327`
- 状态：未闭环
- 置信度：0.75

**证据**

alpha_banner_snapshot 用 `except Exception: rows = []` 吞掉所有 ledger 异常（docstring 写「ledger failure means no sample」），于是 DB 被锁/文件损坏/schema 缺失时横幅显示「实盘样本 0 个交易日 · 样本不足」，把基础设施故障伪装成统计结论；同时 astock/use_cases.py:578-587 的 failed 分支（banner_state="error"、⚠️ 提示）在真实 DB 故障下不可达（tests/test_use_cases.py 只能靠 patch snapshot 抛异常来覆盖）。

**建议**

让 snapshot 把异常转换为独立状态（如 state="error"，携带简短原因），或直接向上抛由 run_alpha_banner 走 failed 分支渲染 ⚠️ 横幅；横幅文案需区分「样本不足（数据正常但没有足够样本）」与「统计读取失败」。补测试：patch tracking_rows 抛 sqlite3.OperationalError 时断言 banner_state=="error"/文案含失败提示而非「样本不足」。修复人：本分支开发者。完全解决判据：DB 故障与样本不足在 UI 上可区分，且测试覆盖该分支。

## 变更文件

- `README.md`
- `astock/ledger.py`
- `astock/recommend_ui.py`
- `astock/track.py`
- `astock/use_cases.py`
- `tests/test_alpha_banner.py`
- `tests/test_core_paths.py`
- `tests/test_use_cases.py`
- `warning: in the working copy of '.devloop/reviews/rev-20261108-001/issues/iss-mw81x690-cbec5c.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261108-001/thread.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261109-001/issues/iss-mw8joahc-4fc16e.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261109-001/issues/iss-mw8m51bp-dd9930.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261109-001/thread.json', LF will be replaced by CRLF the next time Git touches it`

## 交给开发会话

1. 在分支 `当前分支` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
