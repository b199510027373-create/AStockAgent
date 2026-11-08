# ADR-003 SQLite ledger 作为运行台账

- 状态：accepted
- 日期：2026-11-03
- 来源：评审 §2.1 / §4.2；修复记录 §1/§5

## 背景

修复前推荐结果是"一个批次一个 JSON 文件 + `scheduler_state.json` 记最后一次运行"。这带来三个无法回避的问题：

- 无法回答"这个推荐用了哪个版本/参数/哪批数据生成"；
- 无法比较同一天多次运行的差异，也无法可靠重放；
- 跟踪、调度、证据各自散落，失败后只能靠日志猜。

评审把"JSON 文件 → SQLite"列为 P0.5 的第一优先级，并给出了 `runs/items/evidence/jobs/tracking` 五表结构。

## 决策

1. 使用 Python 标准库 `sqlite3`，单文件 `data/astock.db`，不引入新服务。
2. 表：`runs`、`items`、`evidence`、`jobs`、`tracking`；主键见 `docs/AStockAgent_architecture_v1.3.md` §4。
3. 推荐保存时 **JSON + SQLite 双写**：JSON 仍是交换/归档格式，SQLite 是查询与审计来源。
4. 跟踪优先读 ledger，缺失才回退 JSON；调度任务状态写 `jobs`。
5. `data/astock.db` 是运行数据，必须留在 `.gitignore`，不入库。
6. 提供 `python -m astock.ledger sync|stats|list` 做历史 JSON 导入与核对。

## 后果

- 正面：可审计、可比较、可重放；"同一 as_of 多版本"变成一条 SQL；失败任务有状态机可查。
- 负面：需要迁移脚本与备份策略；JSON 与 DB 短暂不一致时以 DB 为准，需文档说明。
- 现状：双写已完成，跟踪已读库，推荐页已有"加载最近一次（SQLite）"入口；但生成与历史读取尚未收敛到同一 service（见计划项）。

## 证据

- `astock/ledger.py`（五表、`sync`/`stats` 子命令）
- 产品快照：55 runs / 263 items / 538 evidence / 585 tracking
- 修复记录 §1「SQLite 台账」与 §5「SQLite 读切换」
