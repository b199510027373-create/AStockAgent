# ADR-011 缓存契约：base_factor + 原子写 + LRU

- 状态：accepted
- 日期：2026-11-03
- 来源：评审 §2.1 D6 / §2.2 D12；修复记录 §1

## 背景

行情缓存曾有三个问题同时存在：

- qfq 以"区间末因子"为基准，但 meta 不记 `base_factor`，跨日混用会产生伪收益；
- 写入不是原子的：进程中断会留下半个 Parquet，下次读取直接崩；
- 没有容量上限，缓存目录只增不减。

## 决策

1. 每个缓存文件配 meta，必须记录：`code/adj/base_factor/first_date/last_date/fetched_at/rows/source`。
2. 缓存有效性以 `base_factor` 为准：基准变化即失效并重拉，禁止复用。
3. 写入使用唯一临时文件 + `os.replace` 原子替换；读侧对损坏文件容错（删除后重拉）。
4. 内存缓存与磁盘缓存都设 LRU 上限。
5. qfq 与 raw 文件路径分离（版本化路径 `_v3`），避免新旧口径混用。
6. 同一进程内用 `threading.local` 复用 Tushare client，避免每只股票新建连接。

## 后果

- 正面：复权基准可追溯；缓存损坏不会拖垮整个流程；磁盘占用可控。
- 负面：meta 文件增加一次 IO；`base_factor` 变化时首次加载变慢。
- 后续：日线改为按 `trade_date` 分区的 Parquet dataset 后，横截面/因子研究可直接 `scan`，把 PIT 因子从 86 秒降到 10 秒级。

## 证据

- `astock/data.py:314-325`（`base_factor` 写入 meta）
- 修复记录 D6/D12
- 测试：`test_data_pit_events.py` 覆盖缓存命中与复权基准
