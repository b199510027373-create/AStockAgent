# AStockAgent 接口冻结清单 v2 草案（已并入正式清单）

> **状态：已被取代。** 2026-11-19 起 v2 口径正式生效，正文见
> `docs/AStockAgent_interface_freeze_v2.md`（plan-20261114-002）。
> 本文件只作为历史留档：它记录了 2026-11-14 评审时「v2 尚未实现、不得据此读 `main`」的边界。
> 不要再把新条目写回这里——改 v2 正文并按流程提 ADR。

## 4. 文件 `extracted.jsonl` 的 `bbox` 语义

**不属于 v2 草案**：`bbox` 的两类坐标（PDF 页面坐标 / 表格行列坐标）是 v1 已实现口径，
见 `docs/AStockAgent_interface_freeze_v1.md` §4；v2 不重复声明。

## 原草案条目去向

- 图表 meta / `data_as_of`+`view_as_of` / `div_id` / versions：→ v2 §3.4
- `agent_*` 表结构与合成 run 退场：→ v2 §5
- 留存清理与 correction：→ v2 §7
- 时间列契约与 transforms 口径：→ v2 §8、§9
- 集合竞价 / 盘后 bar：→ v2 §10
