# PR 评审：评审：Agent 审计表与安全限额（v2：内容/版本/引用）

> 自动生成于 2026-11-18T04:32:11.950Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261117-003`
- 计划项：`plan-20261107-008`
- 分支：`feat/plan-20261107-008-v2-audit` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-98870ec5-3d66-430a-9952-f2cda3728500`
- 本轮问题：0（P0 0 / P1 0 / P2 0）
- 线程未闭环：P0 0 / P1 7 / P2 5

## 本轮结论

第 3 轮复核结论：approve。复核范围 50470c2..bfe4e65（仅 4 文件：chart_render.py、store.py、tests/test_agent_chart_render.py、tests/test_agent_store.py），并逐条对照 r1/r2 的 12 条 open issue。

已确认修复：
- r1 P1 artifact 身份：store.py:311-327 改为 canonical_spec+data_digest+env_fingerprint，文件名不参与；record_artifact 同内容不同文件名复用同一行；env 变化生成新 artifact。测试 test_artifact_identity_ignores_filename_but_includes_env 覆盖。
- r1 P1 provenance：runtime.py:183-213/510-525 传入 temperature/seed/code_version；store.ensure_run 冲突时 COALESCE 补空列；测试覆盖三列落库。
- r1 P1 _finish_audit：runtime.py:553-606 将 record_step 与 finish_run 分离，record_step 失败仍执行终态写入，finish_run 失败用 UPDATE 兜底 failed；测试覆盖两种失败注入。
- r1 P1 fail_run 被覆盖：store.finish_run 保护既有 failed 不被 success 降级；runtime 收尾不再覆盖 failed；测试覆盖。
- r1 P1 data snapshot/recomputed：chart_render 将快照写 data.json blob，meta 只存 data_digest/data_blob/data_rows/data_truncated；store.load_data_snapshot/load_artifact(recomputed) 从 blob 回放；测试 test_load_artifact_verifies_bytes_and_replays 与 test_gc_keeps_data_blob_referenced_by_versions 覆盖。
- r1 P2 备份：store.py:1365-1395 检查 wal_checkpoint 返回值、使用 conn.backup、integrity_check，失败中止；测试覆盖 checkpoint busy。
- r1 P2 ensure_schema db_path：_connection_db_path 从 PRAGMA database_list 推导并透传；测试覆盖自定义文件库。
- r1 P2 RenderedFigure metadata：新增 artifact_version/meta_digest 并写入 metadata。
- r1 P2 token：finish_run 增加 prompt_tokens 参数并写列，runtime 明确 total/completion 语义（若仍只拿到总数，已在字段语义中说明，不阻断）。
- r2 P1 include_plotlyjs 身份：chart_render.py:1453-1465 将 include_plotlyjs 纳入 identity_spec；测试 test_plotlyjs_mode_is_part_of_artifact_identity 断言 inline/cdn 两个 artifact 且均可读。
- r2 P1 GC data_blob：store._protected_paths 加入 data_blob；prune 后 data.json 保留且 recomputed 可回放；测试覆盖。
- r2 P2 内存库备份：migrate_v1_to_v2 在 db_path=None 时使用 _connection_db_path，内存库不生成 DEFAULT_DB_PATH.bak；测试覆盖。

测试与 CI：开发者 ho-20261118-004 记录 73 passed（store+chart）、153 passed（store/runtime/chart/architecture_gate）、全量 676 passed/5 deselected、mypy 39 files、ruff 通过，CI run 44236085726（sha 81ecb81）test/png-export success。本轮新增 3 条回归测试均直击原问题，未发现新的 P0/P1。openP0=0、openP1=0，可标记完全解决并合并 PR #30；合并前只需确认最终合并提交的 CI 仍为 green。

## 本轮问题

本轮未记录问题。

## 历史未闭环问题

- P1 · artifact_id 仍由 digest+文件名派生，环境变化会撞身份、同内容不同文件名会重复建行 · `astock/agent/store.py:312-316, 711-719`（第 1 轮）
- P1 · runtime 审计未记录 temperature/seed/code_version，且 ensure_run 的 DO NOTHING 使后续无法补写 · `astock/agent/runtime.py:475-483`（第 1 轮）
- P1 · _finish_audit 把 record_step 与 finish_run 放在同一个吞异常的 try 里，任一步失败都会留下永久 running · `astock/agent/runtime.py:511-545`（第 1 轮）
- P1 · chart_render 的 fail_run 会被 runtime 的 finish_run 以 success/partial 覆盖，审计终态与工具失败不一致 · `astock/agent/chart_render.py:1462-1469`（第 1 轮）
- P1 · chart_render 仍把 5000 行 data 快照写进 meta_json 且不写 data_digest，真实 artifact 的 recomputed 回放不可用 · `astock/agent/chart_render.py:1535, 1207-1234`（第 1 轮）
- P2 · 迁移备份先 checkpoint 但吞掉失败，再用 shutil.copy2 复制主文件，WAL 未落盘时备份可能缺已提交事务 · `astock/agent/store.py:1250-1259`（第 1 轮）
- P2 · ensure_schema 调 migrate_v1_to_v2 时未传 db_path，自定义连接迁移会把 DEFAULT_DB_PATH 当备份源 · `astock/agent/store.py:391-398`（第 1 轮）
- P2 · RenderedFigure.metadata 缺 artifact_version/meta_digest，record_artifact 返回的版本号被丢弃 · `astock/agent/chart_render.py:298-311, 1537`（第 1 轮）
- P2 · runtime 把总 token 作为 completion_tokens 传入 finish_run，prompt_tokens 永远为 0，成本拆分失真 · `astock/agent/runtime.py:534-540`（第 1 轮）
- P1 · artifact 身份未包含 include_plotlyjs，同一 spec/data/env 切换内联/CDN 会撞身份并硬失败 · `astock/agent/chart_render.py:1368, 1443-1461`（第 2 轮）
- P2 · 内存库 v1 迁移仍把 DEFAULT_DB_PATH 当备份目标（P2-G 仅部分修复） · `astock/agent/store.py:402-412, 1421-1422`（第 2 轮）
- P1 · GC 的 protected 集合漏掉 data_blob，90 天后会删掉仍被 artifact 引用的数据快照 · `astock/agent/store.py:1204-1220, 1270-1287`（第 2 轮）

## 变更文件

- `astock/agent/chart_render.py`
- `astock/agent/runtime.py`
- `astock/agent/store.py`
- `docs/AStockAgent_chart_audit_v2_change_spec.md`
- `tests/test_agent_chart_render.py`
- `tests/test_agent_runtime.py`
- `tests/test_agent_store.py`

## 交给开发会话

1. 在分支 `feat/plan-20261107-008-v2-audit` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
