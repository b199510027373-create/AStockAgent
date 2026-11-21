# PR 评审：评审：根目录卫生与仓库单一真源校验

> 自动生成于 2026-11-21T09:07:27.838Z；可直接转交开发会话。

## 概览

- 线程：`rev-20261121-001`
- 计划项：`plan-20261103-013`
- 分支：`—` → `main`
- 轮次：第 1 轮（full）
- 结论：通过（approve）
- 开发会话：`session-4106a9e9-99f3-4ef0-b0b5-fdc3b93c25db`
- 本轮问题：4（P0 0 / P1 0 / P2 4）
- 线程未闭环：P0 0 / P1 0 / P2 4

## 本轮结论

第 1 轮全量评审（base=main，分支 chore/repo-single-source，HEAD=309591a，13 文件 +742/−51）。已读 ADR/契约与 plan-20261103-013 五条验收标准，逐文件核对 diff、脚本行为与测试充分性，并核对 handoff ho-20261121-010/011 的证据。

结论：approve。未发现 P0/P1，openP0=0、openP1=0。核心实现正确：check_repo_source.py 对 toplevel/remote 归一化（https/ssh/scp/省略协议、去 .git、大小写）、release/AStockAgent 历史目录识别、detached 需显式放行、legacy clone 只警告不阻断，CLI 退出码 0/1 与文档表格一致；ci.yml 在装依赖前运行门禁（PR 用 --any-branch --allow-detached，main 严格）；双目录同步要求已从 CI 日历、development_status、contracts 交付与 PR handoff、chart pipeline 决策中移除并标注为历史；README 补链。架构层面未触及 astock/app.py，ARCH/STAT/AGENT 规则无违反，.gitignore 仍保留 data/*.db、data/agent/ 等数据规则（LEDGER-01 未被破坏）。测试充分性：22 个新用例覆盖 remote 归一化 7 变体与 4 类不可解析输入、canonical/错误 remote、子目录作根、错误分支、detached、legacy clone、CLI 退出码，对本次"脚本+文档+忽略规则"改动属可接受水平；行为级 ignore 断言列为 P2 改进项而非阻断。开发报告的 pytest 854 passed/3 skipped、mypy 44 files、ruff、CI run 44887441629（test+png-export SUCCESS）、PR #44 MERGEABLE/CLEAN 均按 handoff 采信（本会话无 shell/gh，未复跑）。

剩余 4 条 P2（均 open，不阻断 approve，但需在合并后 follow-up 修完才算彻底收口）：(1) docs/AStockAgent_repo_source_of_truth.md:78-79 称正式 handoff/review 记录「随仓库提交」，实测 22 组 handoff（44 文件）与 calendar/reviews 新目录全部未跟踪且 .gitignore 未覆盖，文档与 git status 不一致；(2) tools/check_repo_source.py:169-185 只校验 remote.origin.url，未校验 pushurl，推送目标被改到 release/fork 仍 exit 0；(3) tests/test_repo_source_check.py:212-234 的 ignore 测试仅字符串包含、tempdir 测试因 conftest 强制赋值而恒真，缺少 check-ignore 行为级断言；(4) .gitignore:78-97 把一次性个人文件名写死（/甲.txt、/name_crop.png、/font_test*、/probe_*、/resume_* 等）且 L93/L94 与 L44/L46 重复，属永久屏蔽而非归档/删除。

责任与判定：以上 4 条由开发在 PR #44 合并后以独立 follow-up 提交修复——文档口径与 git status 对齐（入库或改写文案+补 ignore）、pushurl 纳入校验或文档声明不校验、ignore 断言改为 `git check-ignore -q` 正反例、清理单文件 ignore 并附 `git status --short` 证据。4 条全部关闭（或开发明确书面降级理由被接受）时判定完全解决。

## 本轮问题

### P2 · 根目录卫生段把一次性个人文件写死进 .gitignore，且与既有规则重复

- 位置：`.gitignore:78-97`
- 状态：未闭环
- 置信度：0.7

**证据**

`.gitignore:78-97` 新增段把一次性个人文件逐个写死：`/甲.txt`(L92)、`/name_crop.png`(L85)、`/pil_name.png`(L86)、`/build_resume.py`(L87)、`/build_boss_guide.py`(L88)、`/rebuild_template.py`(L89)、`/inspect_resume*`(L90)、`/list_images.py`(L91)、`/font_test*`(L82)、`/probe_*`(L83)、`/resume_*`(L84)、`/py_path_test.py`(L96)、`/test_py_workspace.txt`(L97)。这些是「永久屏蔽」而非验收项要求的「归档或删除」，会把同名未来真实文件静默变成未跟踪；同时 `/01_test.ipynb`(L93) 与既有 `01_test.ipynb`(L44)、`/my_stocks.csv`(L94) 与既有 `my_stocks.csv`(L46) 重复。开发 handoff 已说明这些产物被移入本地 `_archive/`，因此逐个文件名的规则并非必要。

**建议**

由开发确认这些一次性文件已删除或移入 `_archive/`（该目录已整体忽略），随后删除 L82-97 中针对单个文件名的规则，只保留描述运行期产物的模式规则（`/astock-chat-*/`、`/.pytest-*/`、`/pip-*/`、`/tmp*/`、`/coverage_*.txt`、`/_archive/`）；删除与 L44/L46 重复的 L93/L94。改完后补一条 `git status --short` 只含预期条目的证据，避免靠 ignore 掩盖根目录残留。若确需保留某个前缀规则（如 `/probe_*`），在规则上方注明它对应哪类可复现产物，而不是个人草稿。

### P2 · 真源说明文档声称正式 handoff/review 记录“随仓库提交”，实际全部未跟踪

- 位置：`docs/AStockAgent_repo_source_of_truth.md:78-79`
- 状态：未闭环
- 置信度：0.9

**证据**

新增的权威文档写「正式 handoff 和评审记录仍放在 `.devloop/handoffs/`、`.devloop/reviews/` 并随仓库提交；只有草稿进入 `drafts/` 或 `archive/`」。但 `chore/repo-single-source` 上 `git status --short` 显示 22 组 handoff（ho-20261119-026/029、ho-20261120-003…021、ho-20261121-002/009/010/011，共 44 个 .json/.md）全部是 `??`，另有 `?? .devloop/calendar/2027-01.json`、`?? .devloop/reviews/rev-20261119-002/`、`rev-20261120-001/`、`rev-20261120-002/`；而 `.gitignore:103-105` 只忽略 `.devloop/drafts/`、`handoffs/archive/`、`reviews/archive/`，不覆盖实际写入路径。即文档描述的状态与仓库实测不一致，验收项「handoff/review 草稿…不污染 git status」缺少可核验证据。

**建议**

二选一并落定：(a) 若正式记录应入库，就由开发在 PR #44 合并后追加一个 `chore(devloop)` 提交，把这 22 组 handoff 与新增 review 目录纳入版本库，并以提交后 `git status --short` 无 .devloop 条目作为验收证据；(b) 若确定只本地留存，则把 L78-79 改写为「正式记录由 DevLoop 会话单独提交/本地留存，未跟踪期间会出现在 git status」，并在 `.gitignore` 补 `/.devloop/handoffs/*.json`、`*.md` 等规则。完全解决标准：文档 L78-79 的表述与 `git status --short` 实测结果一致。

### P2 · .gitignore 相关测试只做字符串包含判断，且临时目录断言恒真

- 位置：`tests/test_repo_source_check.py:212-234`
- 状态：未闭环
- 置信度：0.85

**证据**

`tests/test_repo_source_check.py:212-227` 的 `test_gitignore_keeps_root_hygiene_and_ignores_release_clone` 只做 `assert needle in text` 字符串包含判断：把 `/astock-chat-*/` 改成未锚定的 `astock-chat-*/`、或把 `/tmp*/` 写成 `/tmp*`（对目录失效），测试仍会通过，验收项「临时产物/草稿不污染 git status」实际上没有任何行为级验证。另外 `tests/test_repo_source_check.py:230-234` 的 `test_pytest_tempdir_stays_inside_repo_ignored_dir` 断言 `Path(tempfile.gettempdir()).is_relative_to(ROOT/'.tmp-tests')`，而 `tests/conftest.py:15-17` 在 import 时就把 `tempfile.tempdir` 强制设成该目录，因此它是恒真断言，即使 `.tmp-tests/` 未被忽略也照样通过。

**建议**

把字符串包含判断换成行为断言：用 `subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT)` 对正例（`astock-chat-x/`、`.pytest-x/`、`pip-x/`、`_archive/x`、`.devloop/drafts/x`、`.tmp-tests/x`）断言 returncode==0，对反例（`astock/data.py`、`docs/x.md`）断言 returncode==1；git 不可用时沿用现有 `pytestmark` skip。恒真断言改为显式检查：`tempfile.tempdir == str((ROOT/'.tmp-tests'))` 且 `git check-ignore -q .tmp-tests/x` 为真，这样才验证了「临时目录落在已忽略目录」这一真实诉求。

### P2 · 校验脚本只看 remote.origin.url，pushurl 指向别处仍会通过

- 位置：`tools/check_repo_source.py:169-185`
- 状态：未闭环
- 置信度：0.75

**证据**

L169 `remote = _run_git(resolved_root, "config", "--get", "remote.origin.url")` 只校验 fetch URL；git 允许 `remote.origin.pushurl`（或 `url.<base>.pushInsteadOf`）把推送目标改到另一仓库。若把 pushurl 指向 `release/AStockAgent` 的 remote 或任意 fork，脚本仍输出 OK 并 exit 0，与本文件 L6-7「发布、提交、推送都必须在仓库根完成」及文档 §1 的承诺不符；现有 22 个测试也未覆盖该路径（tests/test_repo_source_check.py 仅设置 `remote set-url origin`）。

**建议**

在 check_repo 中补一条：`push_url = _run_git(root, "config", "--get", "remote.origin.pushurl")`，非空时同样用 normalize_remote 归一化并与 expected_remote（忽略大小写）比较，不一致就 append 到 problems（文案如「origin.pushurl 指向 X，期望 Y」）；在 tests/test_repo_source_check.py 增加用例 `_git(repo, "config", "remote.origin.pushurl", "https://github.com/other/repo.git")` 断言 `result.ok is False`。若评定为超出本期范围，则必须在文档 §2 明确「本脚本不校验 pushurl」。

## 变更文件

- `.github/workflows/ci.yml`
- `.gitignore`
- `README.md`
- `docs/AStockAgent_CI_work_calendar.md`
- `docs/AStockAgent_chart_pipeline_decisions_v1.md`
- `docs/AStockAgent_contracts_PR_handoff_2026-11-09.md`
- `docs/AStockAgent_contracts_delivery_2026-11-09.md`
- `docs/AStockAgent_development_status_2026-11-08.md`
- `docs/AStockAgent_repo_source_of_truth.md`
- `tests/conftest.py`
- `tests/test_repo_source_check.py`
- `tests/test_sw_crosswalk.py`
- `tools/check_repo_source.py`
- `warning: in the working copy of '.devloop/calendar/2026-11.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/calendar/2026-12.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261103-009.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261103-010.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261103-011.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261103-013.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261103-014.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261107-009.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261109-001.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261110-001.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261110-002.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261114-004.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261117-001.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/plan/items/plan-20261119-001.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261113-001/PR.md', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261113-001/thread.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261116-002/issues/iss-mwoit6on-7d19f2.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261116-002/issues/iss-mwoit6pr-6c8c9f.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261116-002/issues/iss-mwoit6qf-174fb1.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261116-002/issues/iss-mwojg80u-28e11e.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261116-002/thread.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261119-001/issues/iss-mwn7nzfl-0c196c.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/reviews/rev-20261119-001/thread.json', LF will be replaced by CRLF the next time Git touches it`
- `warning: in the working copy of '.devloop/worklog/2026-11.jsonl', LF will be replaced by CRLF the next time Git touches it`

## 交给开发会话

1. 在分支 `当前分支` 上处理上面的未闭环问题；只改相关文件。
2. 修复后回到评审线程，逐条填写真实反馈（已修复 / 换方案 / 延后 / 不采纳 / 重复）。
3. 只有 P0 全部闭环后才可请求 approve；有 P1/P2 未闭环时在结论中说明取舍。
