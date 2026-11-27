# PR 评审：评审：Agent 评测集与离线回归

> 自动生成于 2026-11-23T16:51:52.127Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261123-002`
- 计划项：`plan-20261107-009`
- 分支：`feat/plan-20261107-009-agent-evals` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-16e94b00-18ee-4cbf-9919-f9aae04a91d8`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 4 / P2 5

## 本轮结论

第 3 轮全量评审完成（scope=full）。按 handoff ho-20261124-004、commit 3f1525f/e8a8122、CI run 45219521406 与仓库代码逐项核验，第 2 轮 3 条问题均已修复：
1) iss-mwthi1tw-365766（table_chart 审计库 wiring）：runtime.py:98-122 新增 build_default_registry(*, audit_db_path, artifact_root) 并透传 build_file_registry；runtime.py:264-282 先确定 self.db_path/artifact_root，再构建默认 registry；db_path=None 时用 ":memory:" 审计库 + tempfile.TemporaryDirectory 承接 artifact，不写 DEFAULT_DB_PATH。tests/test_agent_tools.py:817-887 覆盖自定义 db_path 的 agent_runs/agent_run_artifacts link 与 artifact 文件；:890-950 覆盖 db_path=None 不写默认审计库。证据成立。
2) iss-mwthi1uv-38c1a2（CDN）：file_tools.py:671 已改为 include_plotlyjs="inline"；tests/test_agent_tools.py 新增无 "<script src" 外链断言，HTML 自包含。证据成立。
3) iss-mwthi1vw-f4c474（CLI baseline 失败路径）：tests/test_agent_evals.py 新增 test_cli_matching_baseline_returns_zero 与 test_cli_baseline_regression_returns_one，覆盖 main --baseline 的 0/1 退出码。证据成立。
历史 6 条（iss-mwtakt0o-5897b8、iss-mwtakutg-1e8d0d、iss-mwtakwqk-2ff17c、iss-mwtakz5k-a710bc、iss-mwtal1xv-d61a75、iss-mwtal7l1-03d2e8）在第 2 轮已逐条核验为修复，本轮未回退：CI 有 baseline gate；xlsx-005 用 table_chart，真实工具测试在 tests/test_agent_tools.py；evals.py docstring 明确 tool_selection_accuracy 为脚本一致率且有错工具负向测试；refusal-001/002 文案与运行时收口一致并由 step metadata 证明原因；market 缺 data_as_of/data_time 有负向测试；evals._catalog 复用 build_default_registry。验收 #1-#5 均有实现或测试证据；测试执行归开发，本轮未执行测试，证据来自 handoff 的 60 passed / 935 passed 与 CI run 45219521406。结论：approve，本轮未新增问题。线程 openP1=4 是前两轮历史 issue 未被工具逐条关闭导致的展示计数，不是本轮代码缺口；以本 summary 与 reviewer 交付为准。完全解决条件：openP0=0、openP1=0（代码核验已满足；工具计数若仍显示历史 open，需由维护者按历史记录关闭，不需要开发再改代码）。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P1 · 验收标准 #5 未落地：CI/测试从不使用已提交 baseline 做回归比较 · `.github/workflows/ci.yml:68-84`（第 1 轮）
- P1 · 验收标准 #1 的「表格图」未被真实覆盖：xlsx-005 只有表格数据，没有图表/figure · `.devloop/evals/agent_eval_tasks.json:362-403`（第 1 轮）
- P2 · tool_selection_accuracy 恒为 1.0，不能反映工具选择能力 · `astock/agent/evals.py:285-287,534-562`（第 1 轮）
- P2 · refusal-001/002 的 final_answer 与实际断言不一致，扫描件/加密原因从未被验证 · `.devloop/evals/agent_eval_tasks.json:595-598,629-632`（第 1 轮）
- P2 · ADR-018 的 market 字段断言缺少负向测试 · `astock/agent/evals.py:514-519`（第 1 轮）
- P2 · _default_registry 私有导致评测复制注册表，生产新增工具时会漏检 · `astock/agent/runtime.py:89-100`（第 1 轮）
- P1 · table_chart 的审计库 wiring 断裂：默认注册表不传 audit_db_path，自定义 db_path 时图表渲染必失败 · `astock/agent/runtime.py:97-110,242-252`（第 2 轮）
- P1 · table_chart 用 CDN 渲染 HTML，偏离 ADR-016「自包含 HTML」决策，离线 artifact 会空白 · `astock/agent/tools/file_tools.py:663-673`（第 2 轮）
- P2 · CLI 只测了 --no-baseline 成功路径，缺 baseline 回归返回 1 的用例，CI gate 的失败路径无保护 · `tests/test_agent_evals.py:181-197`（第 2 轮）

## 变更文件

- `.github/workflows/ci.yml`
- `astock/agent/evals.py`
- `astock/agent/runtime.py`
- `astock/agent/tools/__init__.py`
- `astock/agent/tools/file_tools.py`
- `docs/AStockAgent_interface_freeze_v1.md`
- `tests/test_agent_evals.py`
- `tests/test_agent_tools.py`
- `tests/test_ci_quality_gates.py`

## 交给开发会话

1. 在分支 `feat/plan-20261107-009-agent-evals` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
