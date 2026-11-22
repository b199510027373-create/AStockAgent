# PR 评审：评审：动作词与分位口径全站一致性核查

> 自动生成于 2026-11-11T05:06:23.984Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261111-002`
- 计划项：`plan-20261103-006`
- 分支：`feat/adr-gate-freeze` → `main`
- 轮次：第 4 轮（diff）
- 结论：通过（approve）
- 开发会话：`session-220c8f42-b3a7-4832-9fd9-c539d019a00c`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 1 / P2 1

## 本轮结论

第 4 轮（diff 范围 976319d..a748d8a）复核完成：仅两个测试文件变更，均为 65874f3「隔离 _save 对真实 ledger 的写入」。tests/test_research_semantics.py 的 PersistedPoolSnapshotTests 在 with 内新增 `patch.object(ledger, "DB_PATH", Path(tmp) / "ledger.db")`（第 173-180 行附近），tests/test_contracts.py:428-433 在 test_save_writes_schema_version_2 的 with 内新增 `patch.object(ledger, "DB_PATH", self.root / "ledger.db")`；两文件均已导入 ledger 与 patch，patch 目标与 ledger._connect 读取的模块级 DB_PATH 一致，隔离生效。该改动只影响测试，不改变生产行为，无新增 P0/P1/P2。第 3 轮记录的 P2 iss-mwbnbkbm-e288d3 在代码层面已修复，但线程中仍为 open、无 feedback。另：第 2 轮 P1 iss-mwbmtukp-633a00 的修复（3cad679：diagnostics.candidate_pool 落盘、§6.4 改为快照复算、读回复算测试）第 3 轮已核实通过，但该 issue 至今仍 status=open、feedback=null，导致工具侧 openP1=1，即使本轮 approve 且无开放 P2，线程也无法标记 resolved。

完全解决条件（由开发执行，无需再改代码）：①对 iss-mwbmtukp-633a00 提交 option=fixed、effective=true 的反馈，引用 3cad679 与 tests/test_research_semantics.py::PersistedPoolSnapshotTests 的复算断言；②对 iss-mwbnbkbm-e288d3 提交 option=fixed 反馈，引用 65874f3 及上述两处 ledger.DB_PATH patch。两项完成后使 openP0=0、openP1=0（openP2 有处置），评审线程 rev-20261111-002 即可判定完全解决，plan-20261103-006 可进入 done。本轮无需新增 issue。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P1 · §6.4 声称「任意批次可用 JSON table 的前 candidate_n 行复算分位」，但落盘只保存 final_rows，复算必然对不上 · `docs/AStockAgent_interface_freeze_v1.md:377`（第 2 轮）
- P2 · 新增池快照测试调用 _save 时未隔离 ledger，会把假批次 rec_pool 写进开发机真实 data/astock.db · `tests/test_research_semantics.py:173-186`（第 3 轮）

## 变更文件

- `app.py`
- `astock/agent/fake.py`
- `astock/backfill.py`
- `astock/contracts.py`
- `astock/ledger.py`
- `astock/recommend.py`
- `astock/recommend_ui.py`
- `astock/track.py`
- `astock/use_cases.py`
- `docs/AStockAgent_contract_rule_map_v1.md`
- `docs/AStockAgent_interface_freeze_v1.md`
- `tests/test_alpha_banner.py`
- `tests/test_architecture_gate.py`
- `tests/test_contracts.py`
- `tests/test_hardening.py`
- `tests/test_research_semantics.py`
- `tests/test_use_cases.py`

## 交给开发会话

1. 在分支 `feat/adr-gate-freeze` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
