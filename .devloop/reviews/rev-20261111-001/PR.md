# PR 评审：评审：架构契约评审门禁：ADR-002/010/013–018 与接口冻结

> 自动生成于 2026-11-11T03:45:21.846Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261111-001`
- 计划项：`plan-20261110-003`
- 分支：`feat/adr-gate-freeze` → `main`
- 轮次：第 3 轮（full）
- 结论：通过（approve）
- 开发会话：`session-195d3959-8114-42c5-a38b-fd9665fdc7c7`
- 本轮问题：1（P0 0 / P1 0 / P2 1）
- 线程未闭环：P0 0 / P1 0 / P2 3

## 本轮结论

第 3 轮（full）评审 bfcb738（已合并为 1ffaee0，CI run 42954426539 pass，PR #7 MERGED）。上轮 2 条 P2 均已按要求落地，我逐行核验通过；另发现 1 条新的 import 级绕过 P2（不阻塞）。结论 approve：openP0=0、openP1=0，本线程可标记完全解决。

核验 iss-mwbkgxnj-9b117b（tests/test_architecture_gate.py:505-583）：新增 _import_bindings 建立 `import os as o` → o=os、`from os import system` → os.system 的绑定表，_qualify 沿绑定表解析点号链，FORBIDDEN_SYMBOLS 补 subprocess.run/Popen/check_output、pathlib.Path.unlink/rmdir，DELETION_METHODS 兜底接收者为表达式的 `.unlink()/.rmdir()`；我按 AST 推演了 `from os import system`、`from shutil import rmtree as rm`、`import os as o; o.remove()`、`Path('x').unlink()`、`pathlib.Path('x').rmdir()` 五种写法，均被标记且与 :605-619 的断言一致；:586-595 的无害守卫新增 `import urllib.parse` + `urllib.parse.quote` 仍要求零命中，说明前一轮「os/urllib 按模块名一刀切」的误报没有被重新引入。

核验 iss-mwbkgxoi-6b397a（docs/AStockAgent_interface_freeze_v1.md:58 等）：v1 布局定为四件（original.<ext> / extracted.jsonl / tables/<sheet_slug>.parquet / meta.json），:58 改「四件产物」，plan-20261107-003 的 C1 验收删掉 preview.png 并列出同一份四件清单；新增 test_frozen_layout_artifact_count_is_consistent（:450-462）把三处口径锁在一起（§4 目录树计数==4、「四件产物」存在、「三件产物」与 preview.png 不得出现、C1 条目无 preview.png）。grep 确认 .devloop/architecture 与 docs 中已无 preview.png 残留，ADR-015 的「四件产物」表述与冻结清单一致。

新增 P2 iss-mwbksxno-55b7ee（tests/test_architecture_gate.py:507-583）：FORBIDDEN_IMPORTS 只按完整模块串匹配，`from urllib import request; request.urlopen(...)`、`import http.client`、`importlib.import_module('subprocess')`、`__import__('socket')` 仍零命中（现有反例只覆盖 `from urllib.request import urlopen`）。这属同一防御层的残余缺口，建议在 005/006 建 astock/agent/tools 之前按建议补「ImportFrom 全路径归一 + http.client/urllib3 + __import__/import_module 常量参数」并加反例；不影响本任务（冻结清单与门禁主体已达成）。

台账说明：iss-mwbkgxnj-9b117b 与 iss-mwbkgxoi-6b397a 在 bfcb738 已修复，但 issue 状态仍为 open（开发未提交逐条 feedback），请开发在下次会话把这些条目标记 fixed，保持记录与代码一致；本轮新增的 iss-mwbksxno-55b7ee 由开发在 plan-20261107-005/006 开工前关闭。除此之外，plan-20261110-003 的 7 条验收均已有可执行证据（gate 31 passed/1 skipped、全量 270 passed/1 skipped、mypy 32 文件、CI pass），评审流程到此闭环。

## 本轮问题

### P2 · import 级仍可绕过：`from urllib import request`、`import http.client`、`importlib.import_module('subprocess')` 均零命中

- 位置：`tests/test_architecture_gate.py:507-583`
- 状态：未闭环
- 置信度：0.85

**证据**

tests/test_architecture_gate.py:507 FORBIDDEN_IMPORTS 只按「完整模块串」匹配，:575 对 ImportFrom 只比较 `node.module`；`from urllib import request` 的 module 是 "urllib"，既不等于也不以 "urllib.request." 开头，因此不报；随后 :545 把 request 绑定为 "urllib.request"，:578-580 把 `request.urlopen('http://x')` 解析成 "urllib.request.urlopen"，而 :508-527 的 FORBIDDEN_SYMBOLS 里没有这个符号，`urlopen` 也不在 :529 的删除方法集里，于是整个函数返回 []。同理 `import http.client` / `from http import client; client.HTTPSConnection(...)`（FORBIDDEN_IMPORTS 无 "http"）、`importlib.import_module('subprocess')`、`__import__('socket')` 都是零命中——这些正是 AGENT-01「禁止任意 HTTP、禁止 shell」要挡的能力。现有反例 :602 只覆盖 `from urllib.request import urlopen`（模块串恰好命中），恰好漏掉同一能力的另一种写法；:571 的 `alias.name.startswith(bad + ".")` 也只处理「导入更深模块」，不处理「从父模块导入子模块」。

**建议**

把 import 检查改成「全路径归一后再判前缀」：1) ImportFrom 时用 `full = f"{module}.{alias.name}"`，对 FORBIDDEN_IMPORTS 的每个根做双向前缀判断（full==root、full.startswith(root+".")、root.startswith(full+".") 任一成立即命中，可覆盖 `from urllib import request` 与 `from http import client`）；2) FORBIDDEN_IMPORTS 补 "http.client"、"http"、"urllib3"（若允许 white-list，就显式写允许的 http 用法）；3) 对 `__import__("x")` 与 `importlib.import_module("x")` 的常量参数做同样的根前缀判定；4) 在本文件 :598 起补四条断言：`from urllib import request\nrequest.urlopen('http://x')` → 非空、`import http.client` → 非空、`importlib.import_module('subprocess')` → 非空、`__import__('socket')` → 非空，并保留 :586-595 的无害用法零命中。修好后这条门禁才是 AGENT-01 可依赖的自动化，否则 005/006 建 tools 目录时它仍会给出虚假安全感。

## 历史未闭环问题

- P2 · 危险符号扫描可被 `from os import system` / 别名 import / `Path.unlink()` 绕过，AGENT-01 自动化名不副实 · `tests/test_architecture_gate.py:492-534`（第 2 轮）
- P2 · 文件布局「三件/四件产物」与 §4 清单、plan-20261107-003 验收里的 preview.png 三处不一致 · `docs/AStockAgent_interface_freeze_v1.md:58`（第 2 轮）

## 变更文件

- `docs/AStockAgent_contract_rule_map_v1.md`
- `docs/AStockAgent_interface_freeze_v1.md`
- `tests/test_architecture_gate.py`

## 交给开发会话

1. 在分支 `feat/adr-gate-freeze` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
