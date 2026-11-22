# PR 评审：评审：用例层 RunResult：统一推荐/跟踪入口

> 自动生成于 2026-11-10T06:45:22.322Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261110-001`
- 计划项：`plan-20261103-002`
- 分支：`feat/plan-20261103-002-runresult` → `main`
- 轮次：第 2 轮（full）
- 结论：通过（approve）
- 开发会话：`session-5e97dfd9-c0e2-4889-8c05-15bdc17fa798`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 0 / P2 4

## 本轮结论

第 2 轮（scope=full，复核 ef32515）结论：approve。本轮新增 1 条 P2，openP0=0、openP1=0；第 1 轮 1 条 P1 与 3 条 P2 经 diff + 用例逐条核验为已修复。

一、上轮 4 条问题的核验（均已修复）
1) iss-mwaawcwh（P1，历史读取 15:00 门限）：已删除。use_cases.py:341-347 现为 `stale = as_of_ts < pd.Timestamp(expected)`，与 recommend.py:760 同口径；新增 tests/test_use_cases.py:241-257 `test_history_older_than_expected_before_close_is_stale`（now=2026-11-06 10:00、data_as_of=2026-11-05、expected=2026-11-06 → 断言 stale 且含「早于最近交易日」），正好锁住晨间回归。
2) iss-mwaawfsb（P2，新鲜度 fail-open）：已修复。data_as_of 解析失败 → use_cases.py:332-339 stale=True + 「无法解析…无法确认数据新鲜度」（tests/test_use_cases.py:259-274）；日历异常 → use_cases.py:315-318 记 calendar_error，use_cases.py:328-330 保持 success 并 freshness_checked=False，渲染器 use_cases.py:207-208 追加「⚠️ 交易日历不可用，未校验数据新鲜度」（tests:276-291 断言 success + calendar_error + 提示文案）。
3) iss-mwaawn5a（P2，跟踪误诊）：已修复。use_cases.py:480-482 统计 T+1 非空行数，516-524 在 usable==0 且 t1_rows>0 时改词为「缺沪深300 基准数据」并写 reason=awaiting_benchmark、bench_missing=True；两份用例分开断言（tests:440-448 缺基准、450-467 缺 T+1 且断言无 bench_missing）。
4) iss-mwaawyq9（P2，scheduler 传无效 now）：已修复。scheduler.py:216-220 不再传 now；use_cases.py:235 `_generate_view(rec)` 删除未用形参，use_cases.py:372 docstring 明确 now 仅历史读取使用，生成侧时钟不可注入成为事实。

二、验收标准复核（全量重读 11 个文件 + 本轮 diff）
1) 统一 RunResult：recommend 三入口均经 run_recommendation 返回 RunResult（use_cases.py:354-422），track 经 run_tracking（459-557）；__post_init__ 仍强制 stale/failed 有 message。达成。
2) 生成与历史共用同一用例函数：共用 _render_recommendation（169-224），台账与生成两条路径的 status_text 现在都由同一渲染器加图标（use_cases.py:202-208 与 301-304），第 1 轮的「stale 时 ✅/⚠️ 同现」在台账路径已消除。达成。
3) UI 只装配：recommend_ui.py / track_ui.py 本轮无改动（diff 为空），仍只做 _outputs 映射，符合 ARCH-01；ARCH-02 依赖方向未被破坏（无新增反向 import）。
4) stale/failed 可读诊断：晨间落后、缺/坏 data_as_of、日历不可用、缺基准、台账异常均有明确文案，达成。

三、测试充分性
diff 显示本轮只改了 tests/test_use_cases.py（+4 个用例/断言：晨间 stale、data_as_of 不可解析、日历不可用提示、缺基准/缺 T+1 拆分，并补 status_line 不含 ✅、status_text 由渲染器加 ✅）；其余 4 个测试文件与 source 的 recommend/track/两个 UI 均无改动，与「218 passed / mypy 0 error」的构成一致，未发现因改名或 mock 而弱化的断言。scheduler 的 now= 删除属纯删参，test_hardening.py 本就 mock run_recommendation，无需新增用例；如需回归保护，可在 SchedulerIdempotencyTests 的 assert_called 里加 `assertNotIn("now", fn.call_args.kwargs)`（非必须，未计级）。

四、剩余问题与完成判定
- 新增 P2 iss-mwabsgfv-2b4276（astock/recommend.py:814-821）：生成侧 status 固定以「✅ 股票池 …」开头且 stale 时自带「 · ⚠️ 数据未更新」，经 use_cases.py:198-201 追加到 ⚠️ 消息后，stale 页面出现 1 个 ✅ + 2 个 ⚠️，与「图标统一由渲染器按 status 添加」不符。建议开发删掉 recommend.py:814 的 stale_note 与 815 行的 ✅ 前缀（或让渲染器在 stale 分支剥离 base 图标），并在 tests/test_use_cases.py:129-142 补 `assertNotIn("✅", status_text)`、`count("⚠️")==1`。P2 不阻断验收。
- 修复责任：本计划项开发在 feat/plan-20261103-002-runresult 上追加一次提交处理该 P2；同时请在第 1 轮 3 条 P2 上回填 feedback=option:fixed（工具里仍是 open），以让线程计数与代码事实一致。
- 完全解决判定：验收标准 1-4 已全部满足，openP0=0、openP1=0，即可标记完全解决；该 P2 与上轮 3 条 P2 的 feedback 回填属于收尾动作，不影响本轮 approve。后续 push、建 PR、跑 CI 由开发执行，CI 通过后再更新交接文档；app.py 剩余迁移另立任务。

## 本轮问题

### P2 · 生成侧 stale 时 status_text 同时出现 ✅ 与两个 ⚠️，与「图标由渲染器按 status 统一添加」不一致

- 位置：`astock/recommend.py:814-821`
- 状态：未闭环
- 置信度：0.75

**证据**

recommend.py:814-821 里 `stale_note = \" · ⚠️ 数据未更新\" if stale_data else \"\"`，status 固定以 `f\"✅ 股票池 {len(codes)} 只 · …{stale_note} · run_id {run_id}\"` 开头，所以 stale 批次自带 \"✅ … ⚠️ 数据未更新\"。use_cases.py:198-201 的 stale 分支是 `status_text = f\"⚠️ {message}\"` 再把整个 `base`（= rec.status，见 use_cases.py:256）追加在后面，最终页面显示「⚠️ 数据未更新：…\n\n✅ 股票池 42 只 · … · ⚠️ 数据未更新 · run_id …」——一次 stale 里出现一个 ✅ 和两个 ⚠️。台账路径已在 ef32515 把 status_line 改成纯事实描述（use_cases.py:301-304），生成路径没有同步，与交接文档「✅/⚠️/❌ 统一由渲染器按 status 添加」的说法不符；tests/test_use_cases.py:142 只断言 `startswith(\"⚠️\")`，未覆盖混排。

**建议**

二选一并补测试：(1) 在 recommend.py:815 把 status 改成不以图标开头（例如 `f\"股票池 {len(codes)} 只 · …\"`），并删掉 814 行的 `\" · ⚠️ 数据未更新\"`（stale 与否由 _generate_view 的 status 表达，渲染器已加 ⚠️ 前缀）；(2) 若要保持 recommend.py 的输出不变，则在 use_cases.py:198-201 追加 base 前先剥离开头的 ✅/⚠️（如 `base = base.lstrip(\"✅⚠️❌ \")`）。同时在 GenerateRecommendationTests.test_stale_data_is_reported_as_stale（tests/test_use_cases.py:129-142）补 `self.assertNotIn(\"✅\", result.payload[\"status_text\"])` 与 `assertEqual(result.payload[\"status_text\"].count(\"⚠️\"), 1)`。

## 历史未闭环问题

- P2 · 新鲜度校验在 data_as_of 不可解析 / 日历不可用时 fail-open，页面无任何提示 · `astock/use_cases.py:310-338`（第 1 轮）
- P2 · 跟踪 stale 的诊断把"缺沪深300 基准"说成"还没有 T+1 收盘数据" · `astock/use_cases.py:443-446`（第 1 轮）
- P2 · scheduler 向生成分支传 now 但生成侧不使用，形成"可注入时钟"的假象 · `astock/scheduler.py:211-221`（第 1 轮）

## 变更文件

- `astock/recommend.py`
- `astock/recommend_ui.py`
- `astock/scheduler.py`
- `astock/track.py`
- `astock/track_ui.py`
- `astock/use_cases.py`
- `tests/test_contracts.py`
- `tests/test_core_paths.py`
- `tests/test_hardening.py`
- `tests/test_track_nav.py`
- `tests/test_use_cases.py`

## 交给开发会话

1. 在分支 `feat/plan-20261103-002-runresult` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
