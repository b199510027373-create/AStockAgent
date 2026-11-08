# PR 评审：评审：契约层 contracts.py：DataFrame schema 校验

> 自动生成于 2026-11-08T13:56:10.330Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261108-001`
- 计划项：`plan-20261103-001`
- 分支：`feat/contracts` → `main`
- 轮次：第 2 轮（full）
- 结论：需要修改（request_changes）
- 开发会话：未关联
- 本轮问题：3（P0 0 / P1 1 / P2 2）
- 线程未闭环：P0 0 / P1 1 / P2 2

## 本轮结论

第 2 轮（scope=full，diff d756231..2fce08f：contracts.py/data.py/events.py/ledger.py/recommend.py/test_contracts.py）。r1 六条已逐条复核，全部按建议修复：contracts.py:90-120 非空/NaN 检查（non_empty 用 isna()|空串，非字符串也报错）、:240-242/:318-320 allow_empty 收紧为无列哨兵、:565-580 DATA-02 改为先全量校验再整体替换（不再就地改写 payload）；recommend.py:116-118/:140-141/:361-362 ContractError 先于 Exception 重新抛出、:749-754 只采纳 reason/catalysts/risks 且 prompt 明确禁止输出 action、_report_markdown 建议完全由 _action(综合分) 决定；events.py:111-133 新增 _require_source_columns/_require_parseable_dates，各 fetch_* 均让 ContractError 冒泡；data.py 在 _fetch_daily 原始列、复权合并后、缓存命中、指数、分钟线各路径都过了 validate_bars。验收标准 1/3/4 满足，第 2 条（关键入口全部过校验）在 data/events/recommend 侧成立，但交互层消费方未同步。

本轮新开 3 条：P1 app.py:114 引用了本次已移除的 evidence "date" 键（citations 现只有 pub_time），且调用点在 try 之外 → 单股体检与问答页只要有引用资料即 KeyError，属本次改造引入的回归，必修；P2 contracts.py:184-194/278-285 BAR_SCHEMA/EVENT_SCHEMA 导出但无人使用、与 required 重复且已漂移（adj_factor、dtype 标注）；P2 contracts.py:565-577 DATA-02 比较未处理时区，带 +08:00 的 pub_time 会抛 TypeError 而非 ContractError。

说明：本环境无命令执行能力，未运行 pytest；结论基于静态审阅与测试源码核对（tests/test_contracts.py 43 个用例，ReviewRegressionTests/LlmActionContractTests 覆盖了 r1 各项）。conclusion: 契约层本身质量达标，剩一条 P1 回归在 app.py 与两处 P2，修完即可 approve。

## 本轮问题

### P1 · 证据字典键从 date 改为 pub_time 后，AI 引用区块仍读 item["date"]，单股体检与问答页只要有引用就 KeyError

- 位置：`app.py:109-119, 139, 357`
- 状态：未闭环
- 置信度：0.9

**证据**

events.py 本次把 Evidence 的键统一为 pub_time（contracts.py:348-406 required_keys 含 pub_time；events.py:833 validate_evidence_list(items) 校验，:837-843 读 item["pub_time"] 构造引用），evidence_for_stock 也不再写 "date" 键。但 app.py:114 `date = pd.Timestamp(item["date"]).strftime("%Y-%m-%d")` 未同步：app.py:139 `ai += _citation_block(citations)` 位于 :130-133 的 try 之外（该 try 只包住 build_ai_context），app.py:357 问答页同样在 try 外调用。因此只要有任意一条引用资料，就抛 `KeyError: 'date'`：单股体检整页报错、问答回复为空。这是本次契约改造引入的回归——改造前 build_ai_context 返回的是 "date" 键。

**建议**

改为兼容读取：`ts = item.get("pub_time") or item.get("date")`，`date = pd.Timestamp(ts).strftime("%Y-%m-%d")`（或直接只用 pub_time）；同时给 `_citation_block` 补一个单测，用 events.build_ai_context 的真实返回结构（evidence_for_stock 的 items）断言不抛异常且渲染出日期。

### P2 · BAR_SCHEMA/EVENT_SCHEMA 导出但无人使用，且与 required 元组重复且已漂移

- 位置：`astock/contracts.py:184-194, 209-218, 278-285, 298-305`
- 状态：未闭环
- 置信度：0.85

**证据**

全仓 grep BAR_SCHEMA|EVENT_SCHEMA 只有 6 处命中，全部是「定义 + __all__ 导出」（contracts.py:26-27 导出、:184-194 定义、:278-285 定义），没有任何校验逻辑或测试读取它们；真正的必需列由 BarFrame.required(:209-218) 与 EventFrame.required(:298-305) 另行硬编码。两份 schema 已经漂移：BAR_SCHEMA:184-194 含 adj_factor 且标注 dtype（"datetime64[ns]"、"float64"），而 required 不含 adj_factor（它由 require_adj 条件要求，:255-257），validate 也不校验这些标注的 dtype。后续按 BAR_SCHEMA 改 schema 的人会以为改了契约，实际不生效。

**建议**

消除双份真相：二选一——(a) 让 required 从 schema 派生，如 `required = tuple(k for k in BAR_SCHEMA if k != "adj_factor")`（EventFrame 同），并让 dtype 标注真正参与校验（或删掉 dtype 值只留列名集合）；(b) 删除 BAR_SCHEMA/EVENT_SCHEMA 并从 __all__(:26-27) 移出。两种都补一个测试断言 `set(BarFrame.required) <= set(BAR_SCHEMA)`，防止再漂移。

### P2 · DATA-02 检查用带时区的 pub_time 与 naive generated_at 直接比较，抛 TypeError 而非 ContractError

- 位置：`astock/contracts.py:393-406, 565-577`
- 状态：未闭环
- 置信度：0.7

**证据**

contracts.py:565-577 是本次新加的 DATA-02 实现：`pub = pd.Timestamp(item["pub_time"])` 直接与 `gen_ts = pd.Timestamp(data["generated_at"])`（naive，通常是 "2026-11-08 17:14:02" 这种无时区字符串）比较，`if pub > gen_ts:` 外层没有 try/except。而上游明确允许带时区的时间：Evidence.validate 对 pub_time 只做 `pd.Timestamp(record["pub_time"])` 解析并原样保留调用方表示（:393-406），`_to_datetime` 对 `pd.DatetimeTZDtype` 直接放行（:132-140）。因此一份 `evidence[a].pub_time = "2026-11-06T09:00:00+08:00"` 的 payload 能通过 Evidence 校验，却在 :570 抛 `TypeError: Cannot compare tz-naive and tz-aware timestamps`——不是 ContractError，绕过 contracts.py 统一的异常语义，调用方 `_save` 的契约错误处理（recommend.py:558 附近）也抓不到，且异常信息不指出是哪个契约/字段。

**建议**

比较前统一时区：`gen_ts = pd.Timestamp(data["generated_at"]); gen_ts = gen_ts.tz_localize(None) if gen_ts.tzinfo else gen_ts`，并对 `pub` 同样处理（或改用 `pub.astimezone(gen_ts.tzinfo) if gen_ts.tzinfo else pub.tz_convert(None)`）；把整个比较包在 `try/except TypeError` 里转成 ContractError 作为兜底，并补一个带 +08:00 偏移的 evidence 用例，断言要么正常比较、要么抛 ContractError。

## 变更文件

未获取到变更文件清单；请用 `devloop_git files` 重新获取。

## 交给开发会话

1. 在分支 `feat/contracts` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
