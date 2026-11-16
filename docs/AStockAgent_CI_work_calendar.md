# AStockAgent CI 工作日历与交接说明

日期：2026-11-02  
项目：AStockAgent  
交接用途：给后续对话继续维护、排查或扩展 CI 使用

## 1. 当前结论

- CI 已配置、已提交、已推送。
- GitHub Actions 对应提交已运行完成，结果为 `success`。
- 质量门禁已深化（plan-20261103-012，2026-11-17）：`ruff check astock tests` 全仓 0 条并 blocking、`mypy astock app.py` 覆盖全部非 UI 模块（36 个源文件 Success）、覆盖率门槛按实测重设为 76.45%（=CI Linux 实测显示值，本地 76.68%），依赖全部精确锁定。
- 当前 `main` 与 `origin/main` 一致。
- 没有提交 `.env`、密钥、虚拟环境、缓存、推荐产物或 Python 字节码。

## 2. 仓库位置

主项目目录（**也是 Git 仓库根**，2026-11-17 起 `.git` 已在本目录）：

```text
C:\Users\b1995\Documents\AStockAgent
```

历史上曾使用过的发布仓库副本：

```text
C:\Users\b1995\Documents\AStockAgent\release\AStockAgent
```

远程仓库：

```text
https://github.com/b199510027373-create/AStockAgent.git
```

分支：

```text
main
```

本次提交：

```text
e8cd9afe94e7d7ff97b191be6876a3fac30e623f
```

提交信息：

```text
Add CI and sync v1.1 application code
```

注意：`release/AStockAgent` 是历史发布副本，不再作为提交入口。提交/推送直接在仓库根目录进行；该副本已加入 `.gitignore`，不要再往里同步。

## 3. 完成事项

### 3.1 同步发布源码

已将当前 AStockAgent v1.1 代码同步到发布仓库，包括：

- `app.py`
- `astock/`
- `tests/`
- `README.md`
- `requirements.txt`
- `start_app.bat`
- `start_scheduler.bat`
- `ts_init.py`
- 其他运行所需文件

### 3.2 新增 GitHub Actions

工作流文件：

```text
.github/workflows/ci.yml
```

触发条件：

- 推送到 `main`
- 针对 `main` 的 Pull Request
- 手动触发 `workflow_dispatch`

执行内容（plan-20261103-012 之后的当前形态）：

1. 检出仓库。
2. 安装 Python 3.13。
3. 从 `requirements-dev.txt` 一次装齐运行时与工具链（全部精确锁定，CI 不再裸装 ruff/mypy）。
4. 检查 Git 是否跟踪了密钥或运行产物。
5. `compileall` 语法编译。
6. `ruff check astock tests`（全仓 blocking；规则集固定在 `ruff.toml` 的显式 select/ignore）。
7. `mypy astock app.py`（全部非 UI 模块；UI 文件在 `mypy.ini` 中排除）。
8. `coverage run -m pytest tests -q -m "not png"` + `coverage report -m`（门槛在 `.coveragerc`，当前 76.45%，分阶段目标 80% / 85%）。

```text
ruff check astock tests
mypy astock app.py
coverage run -m pytest tests -q -m "not png"
coverage report -m
```

### 3.3 强化忽略规则

`.gitignore` 已排除：

```text
.env
.env.*
!.env.example
env.env
cache/
data/recommendations/
.pytest_cache/
.ipynb_checkpoints/
.venv/
venv/
__pycache__/
*.py[cod]
.DS_Store
.idea/
.vscode/
```

`.env.example` 明确保留，供其他人参考配置项，不包含真实密钥。

## 4. 验证记录

### 4.1 本地测试（2026-11-17 更新）

执行目录（仓库根，不再进 `release/AStockAgent`）：

```text
C:\Users\b1995\Documents\AStockAgent
```

执行命令：

```powershell
.venv\Scripts\python.exe -m pytest tests -q -m "not png"
.venv\Scripts\ruff.exe check astock tests
.venv\Scripts\mypy.exe astock app.py
```

结果：

```text
608 passed, 1 skipped, 5 deselected, 12 subtests passed
All checks passed!
Success: no issues found in 36 source files
```

### 4.2 暂存区审计

- 暂存文件数量：41
- 禁止入库路径检查：通过
- 本地密钥值比对：通过
- 常见凭据格式检查：通过
- `.env.example` 未被忽略：通过
- 工作流 YAML 解析：通过

### 4.3 远端状态

本地 `HEAD`、`origin/main` 和 GitHub 远端 `main` 均指向：

```text
e8cd9af
```

GitHub Actions 运行结果：

```text
completed / success
```

运行链接：

```text
https://github.com/b199510027373-create/AStockAgent/actions/runs/41402554097
```

### 4.4 二次复核（2026-11-02 15:00 左右）

- 本地 `HEAD`、`origin/main`、远端 `refs/heads/main` 均为 `e8cd9af`；该提交时 `git status --short` 为空。
- **当前工作区已有 v1.2 未提交改动**（SQLite ledger、PIT 快照、原始价/ST、研究语义等）；推送后需要重新跑 GitHub Actions，不能沿用 `e8cd9af` 的 CI 结果。
- GitHub Actions API 核验：run `41402554097` 为 `completed / success`，`head_sha=e8cd9af`。
- 当时 unittest 全通过；根项目与 `release/AStockAgent` 的 `app.py`、`astock/*.py`、`tests/*.py`、`requirements.txt` 哈希一致。
- `git ls-files` 未发现 `.env`、`env.env`、`.venv/`、`cache/`、`data/recommendations/`、`__pycache__/`、`*.pyc`；`.env.example` 已跟踪。
- CI 覆盖「依赖安装（精确锁定）+ 敏感文件检查 + 语法编译 + 全仓 `ruff check astock tests` + `mypy astock app.py` + pytest + 覆盖率门槛（`.coveragerc`，当前 76.45%）」；UI/图表模块在 `.coveragerc` 中排除。
- 历史基线（2026-11-03，已被取代）：`mypy` 覆盖 22 个源文件；覆盖率 57%（3492 statements / 1493 miss）。
- 当前基线（2026-11-17 实测，plan-20261103-012，评审第 1 轮后 PR #24 / CI run 43935079016）：`mypy astock app.py` = 36 个源文件 Success；`pytest tests -q -m "not png"` 本地 608 passed / 1 skipped / 5 deselected（CI Linux 607 passed / 2 skipped）；覆盖率 CI Linux 76.45%（9248/2178，门禁权威值）、本地 76.68%（9248/2157）。比 PR #21 的 76.47% 低 0.02pp：`astock/scheduler.py` 为守住 PR #9 观察期回退到 92f5e47 原文，恢复的 try/except 分支在 Linux 未被覆盖。

## 5. 安全红线

后续对话继续处理本项目时，必须遵守：

1. 绝对不要执行 `git add .env`。
2. 绝对不要提交 `.env`、`env.env` 或其中的真实密钥。
3. 不要提交 `cache/`、`data/recommendations/`、`.venv/`、`__pycache__/` 或 `*.pyc`。
4. 不要提交 `deliverables/` 中的产品文档、评审文件或演示产物，除非用户明确要求并单独确认目标仓库。
5. 不要执行 `git add -A` 后直接推送；应先审计暂存文件。
6. 不要打印、复制或展示 `.env` 的值。
7. 如果密钥曾经进入公开 Git 历史，仅从当前文件删除不够，必须立即在服务商后台重置密钥。

## 6. 推荐操作流程

后续修改后，在发布仓库执行：

```powershell
Set-Location 'C:\Users\b1995\Documents\AStockAgent\release\AStockAgent'

git status --short
git diff --name-only
git ls-files
git diff --cached --name-only
```

确认没有敏感文件后，再显式暂存需要的路径：

```powershell
git add -- app.py astock tests README.md requirements.txt
```

推送前检查：

```powershell
git status --short
git diff --cached --check
git diff --cached --name-only
```

再次确认没有以下内容：

```text
.env
env.env
.venv/
cache/
data/recommendations/
__pycache__/
*.pyc
```

推送：

```powershell
git push origin main
```

## 7. 后续待办

当前没有阻塞项，CI 已能作为基础质量门禁。

已完成（plan-20261103-012，2026-11-17）：

- ~~增加静态检查，例如 `ruff`。~~ 已全仓归零并纳入 CI blocking（`ruff check astock tests`）。
- ~~为关键模块补充覆盖率报告。~~ 已覆盖全部非 UI 模块，门槛按实测重设为 76.45%，分阶段目标 80% / 85%。

可选的后续改进：

- PR #9 五日观察（2026-11-21）结束后，删除 `ruff.toml` 中 `astock/scheduler.py`、`astock/jobs.py` 的 per-file-ignores，清理存量 22 条问题。
- 清理仓库根历史原型脚本 `app.py`（113 条 Ruff 问题），目前已在 `ruff.toml` 里显式 exclude。
- 逐族开启 `RUF001/RUF002/RUF003`（中文全角标点）与 `B905`（zip strict），先评估再决定。
- 增加依赖漏洞扫描。
- 增加 GitHub Actions 运行结果徽章到 README。
- 如果后续需要发布文档，单独建立 `docs/` 内容策略，不要把运行数据或产品构建产物混入源码仓库。

## 8. 给下一个对话的开场说明

可以直接使用下面这段：

```text
请继续维护 AStockAgent。
主项目目录：C:\Users\b1995\Documents\AStockAgent
Git 仓库：C:\Users\b1995\Documents\AStockAgent\release\AStockAgent
GitHub：https://github.com/b199510027373-create/AStockAgent.git
分支：main
已有 CI：.github/workflows/ci.yml
最近成功提交：e8cd9af
CI 状态：GitHub Actions 已通过

继续修改前先检查两个目录是否一致。
绝对不要提交 .env、env.env 或任何密钥。
绝不提交 cache/、data/recommendations/、.venv/、__pycache__/、*.pyc。
提交前必须审计暂存区，推送后检查 GitHub Actions 结果。
```
