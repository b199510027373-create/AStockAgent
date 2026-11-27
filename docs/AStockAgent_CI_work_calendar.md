# AStockAgent CI 工作日历与交接说明

日期：2026-11-02  
项目：AStockAgent  
交接用途：给后续对话继续维护、排查或扩展 CI 使用

## 1. 当前结论

- CI 已配置、已提交、已推送。
- GitHub Actions 对应提交已运行完成，结果为 `success`。
- 质量门禁已深化（plan-20261103-012，2026-11-17；plan-20261117-001 于 2026-11-27 复核）：`ruff check astock tests` 全仓 0 条并 blocking、`mypy astock app.py` 覆盖全部非 UI 模块（46 个源文件 Success）、覆盖率门槛按实测重设为 80.56%（=CI Linux 实测显示值，本地 80.80%），依赖全部精确锁定。
- 当前 `main` 与 `origin/main` 一致。
- 没有提交 `.env`、密钥、虚拟环境、缓存、推荐产物或 Python 字节码。

## 2. 仓库位置

**唯一 Git 仓库与发布源**（2026-11-17 起 `.git` 已在本目录，2026-11-21 明确为单一真源）：

```text
C:\Users\b1995\Documents\AStockAgent
```

远程仓库：

```text
https://github.com/b199510027373-create/AStockAgent.git
```

分支：

```text
main
```

历史副本（仅保留查阅旧提交，**不再同步、不作为发布源**）：

```text
C:\Users\b1995\Documents\AStockAgent\release\AStockAgent
```

根目录的 `release/` 已在 `.gitignore` 中排除。提交、推送、发布和 CI 都只在仓库根执行；
详细约定与校验命令见 `docs/AStockAgent_repo_source_of_truth.md`。

> 本节旧版本中的「同步到发布仓库」「检查两个目录是否一致」均已废弃。2026-11-17 之前的
> 提交哈希只用于历史追溯，不再作为当前操作入口。

## 3. 完成事项

### 3.1 根目录直接开发（不再同步副本）

AStockAgent v1.1 及之后的代码全部在仓库根目录开发和提交，包括：

- `app.py`
- `astock/`
- `tests/`
- `README.md`
- `requirements.txt`
- `start_app.bat`
- `start_scheduler.bat`
- `ts_init.py`
- 其他运行所需文件

`release/AStockAgent` 是历史独立仓库，不再接收同步。不要从该目录提交、推送或发布。

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
2. 校验仓库单一真源：`python tools/check_repo_source.py`（push 到 `main` 严格校验 `main`；PR 用 `--any-branch --allow-detached`）。
3. 安装 Python 3.13。
4. 从 `requirements-dev.txt` 一次装齐运行时与工具链（全部精确锁定，CI 不再裸装 ruff/mypy）。
5. 检查 Git 是否跟踪了密钥或运行产物。
6. `compileall` 语法编译。
7. `ruff check astock tests`（全仓 blocking；规则集固定在 `ruff.toml` 的显式 select/ignore）。
8. `mypy astock app.py`（全部非 UI 模块；UI 文件在 `mypy.ini` 中排除）。
9. `coverage run -m pytest tests -q -m "not png"` + `coverage report -m`（门槛在 `.coveragerc`，当前 80.56%，分阶段目标 80%（已达成）/ 85%）。

```text
python tools/check_repo_source.py
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
953 passed, 3 skipped, 6 deselected, 46 subtests passed
All checks passed!
Success: no issues found in 45 source files
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
- 历史基线（2026-11-02）：当时曾比对根项目与 `release/AStockAgent` 的 `app.py`、`astock/*.py`、`tests/*.py`、`requirements.txt` 哈希；该「双目录同步」做法已于 2026-11-21 废弃。现在只以仓库根为唯一真源，`release/AStockAgent` 仅保留旧提交供查阅，不再比较或同步。
- `git ls-files` 未发现 `.env`、`env.env`、`.venv/`、`cache/`、`data/recommendations/`、`__pycache__/`、`*.pyc`；`.env.example` 已跟踪。
- CI 覆盖「依赖安装（精确锁定）+ 敏感文件检查 + 语法编译 + 全仓 `ruff check astock tests` + `mypy astock app.py` + pytest + 覆盖率门槛（`.coveragerc`，当前 80.56%）」；UI/图表模块在 `.coveragerc` 中排除。
- 历史基线（2026-11-03，已被取代）：`mypy` 覆盖 22 个源文件；覆盖率 57%（3492 statements / 1493 miss）。
- 当前基线（2026-11-28 更新，plan-20261103-010 新增 astock/scoring.py）：`mypy astock app.py` = 46 source files Success；`pytest tests -q -m "not png"` 本地 953 passed / 3 skipped / 6 deselected（CI Linux 952 passed / 4 skipped）；覆盖率 CI Linux 12474/2425 = 80.56%（门禁权威值）、本地 12474/2395 = 80.80%。

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

所有操作都在仓库根执行，不再进入 `release/AStockAgent`：

```powershell
Set-Location 'C:\Users\b1995\Documents\AStockAgent'

python tools\check_repo_source.py
git status --short
git diff --name-only
git ls-files
git diff --cached --name-only
```

确认当前是根仓库、remote 正确且没有敏感文件后，再显式暂存需要的路径：

```powershell
git add -- app.py astock tests README.md requirements.txt
```

提交前检查：

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
release/
```

提交并推送：

```powershell
git commit -m "描述本次改动"
git push origin main
```

功能分支开发时，把最后一行换成 `git push -u origin <branch>` 并开 PR；CI 会用
`python tools/check_repo_source.py --any-branch --allow-detached` 校验 PR checkout。

## 7. 后续待办

当前没有阻塞项，CI 已能作为基础质量门禁。

已完成（plan-20261103-012，2026-11-17）：

- ~~增加静态检查，例如 `ruff`。~~ 已全仓归零并纳入 CI blocking（`ruff check astock tests`）。
- ~~为关键模块补充覆盖率报告。~~ 已覆盖全部非 UI 模块，门槛按实测重设为 80.56%（2026-11-27 复核），分阶段目标 80%（已达成）/ 85%。

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
仓库根（唯一真源）：C:\Users\b1995\Documents\AStockAgent
GitHub：https://github.com/b199510027373-create/AStockAgent.git
分支：main
已有 CI：.github/workflows/ci.yml
历史副本：release/AStockAgent 仅保留旧提交，不再同步、不作为发布源。

开工前先运行：python tools\check_repo_source.py
绝对不要提交 .env、env.env 或任何密钥。
绝不提交 cache/、data/recommendations/、.venv/、__pycache__/、*.pyc。
提交前必须审计暂存区，推送后检查 GitHub Actions 结果。
```
