# AStockAgent 仓库单一真源说明

日期：2026-11-21  
任务：`plan-20261103-013` · 根目录卫生与仓库单一真源校验  
适用范围：本地开发、CI、发布与交接文档

## 1. 结论

AStockAgent 的**唯一 Git 仓库和发布源**是工作区根目录：

```text
C:\Users\b1995\Documents\AStockAgent
```

- `git rev-parse --show-toplevel` 必须指向该目录。
- `origin` 必须是 `github.com/b199510027373-create/AStockAgent`。
- 默认发布分支是 `main`。
- 提交、推送、打 tag、生成发布产物都直接在根目录执行。

`release\AStockAgent` 是**历史独立仓库副本**：它有自己的 `.git`，保留仅用于查阅旧提交，
不再同步、不再作为发布入口，也不参与 CI 收集。根目录的 `release/` 已被 `.gitignore` 排除。

## 2. 校验脚本

脚本：`tools/check_repo_source.py`

```powershell
# 本地 main 分支发布前
.\.venv\Scripts\python.exe tools\check_repo_source.py

# 功能分支 / PR 的 detached checkout
.\.venv\Scripts\python.exe tools\check_repo_source.py --any-branch --allow-detached

# 检查另一个目录（例如确认没有误入 release 副本）
.\.venv\Scripts\python.exe tools\check_repo_source.py --root C:\Users\b1995\Documents\AStockAgent\release\AStockAgent
```

检查项：

1. `git rev-parse --show-toplevel` 与 `--root` 解析后的目录一致；
2. `remote.origin.url` 归一化后等于 `github.com/b199510027373-create/AStockAgent`；
3. 当前分支等于 `main`（`--any-branch` 可跳过）；
4. detached HEAD 默认报错，`--allow-detached` 仅用于 CI 的 PR checkout；
5. 若检查目录本身就是 `release/AStockAgent`（旧仓库自身也能通过 toplevel/remote 检查），直接阻断；
6. 从仓库根检查时发现 `release/AStockAgent/.git` 只输出告警，提醒它不是发布源。

退出码：`0` 通过；`1` 存在阻断问题。告警不影响退出码。

CI 在 `.github/workflows/ci.yml` 的 `test` job 中、依赖安装之前运行该脚本：

- `push` 到 `main`：严格校验 `main` 分支；
- `pull_request`：校验根目录与 remote，允许 GitHub Actions 的 detached HEAD。

## 3. 根目录卫生

根目录只保留源码、配置、测试和文档。以下内容不提交：

- `mypy_*.txt`、`coverage_*.txt`、`daily_report.txt`、`temp_*.png` 等一次性报告/截图；
- `astock-chat-*`、`astock-files-*`、`astock-tools-*`、`.dsh-office-*`、`.pytest-*`、
  `pip-*`、`tmp*` 等运行时目录；
- `.pr-body-*.md`、`.pr-comment-*.md` 等 PR 草稿；
- 个人探针脚本（`probe_*`、`resume_*`、`font_test*` 等）。

忽略规则在 `.gitignore` 末尾的「根目录卫生」段，全部以 `/` 锚定仓库根，避免误伤
`astock/`、`tests/` 下的同名文件。`tests/conftest.py` 还会把 pytest 的临时目录固定到
已忽略的 `.tmp-tests/`：本机系统 TEMP 不可写时，Python 默认会退回到当前工作目录，
否则 `astock-chat-*`、`pytest-of-*` 等又会落回仓库根。

需要保留但不提交的整理产物放在：

```text
_archive/                 # 本地归档，整体忽略
.devloop/drafts/          # PR / handoff / review 草稿，整体忽略
.devloop/handoffs/archive/
.devloop/reviews/archive/
```

正式 handoff 和评审记录仍放在 `.devloop/handoffs/`、`.devloop/reviews/` 并随仓库提交；
只有草稿进入 `drafts/` 或 `archive/`。

## 4. 常见失败与处理

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `git 工作区根与检查目录不一致` | 在子目录或 `release/AStockAgent` 里执行 | `cd` 回仓库根，或把 `--root` 指向根目录 |
| `origin 指向 ...，期望 ...` | remote 被改成 fork/旧仓库 | `git remote set-url origin https://github.com/b199510027373-create/AStockAgent.git` |
| `当前分支为 ...，期望 main` | 在功能分支上做发布检查 | 合并到 `main` 后重跑；功能分支用 `--any-branch` |
| `当前为 detached HEAD` | CI PR checkout 或手工 checkout 提交 | CI 用 `--allow-detached`；本地切回 `main` |
| `检查目录是历史旧仓库 release/AStockAgent` | 在旧副本里执行了检查 | `cd` 回仓库根后重跑；不要从该目录提交/发布 |
| `release/AStockAgent 仍是历史嵌套仓库` | 旧副本还在根目录下 | 只是告警；不要从其目录提交或发布 |

## 5. 相关文档

- `docs/AStockAgent_CI_work_calendar.md`：CI 流程与工作日历（已改为根目录单仓库流程）。
- `.github/workflows/ci.yml`：CI 中的 `Verify repository source of truth` 步骤。
- `docs/AStockAgent_development_status_2026-11-08.md`、`docs/AStockAgent_contracts_*.md`：
  历史交接记录，其中涉及 `release/AStockAgent` 的同步描述均已标注为历史行为。
