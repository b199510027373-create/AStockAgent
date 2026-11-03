# AStockAgent CI 工作日历与交接说明

日期：2026-11-02  
项目：AStockAgent  
交接用途：给后续对话继续维护、排查或扩展 CI 使用

## 1. 当前结论

- CI 已配置、已提交、已推送。
- GitHub Actions 对应提交已运行完成，结果为 `success`。
- 本地 29 个单元测试全部通过（提交 e8cd9af 时；当前代码已增至 39 个）。
- 当前 `main` 与 `origin/main` 一致。
- 发布仓库工作区干净。
- 没有提交 `.env`、密钥、虚拟环境、缓存、推荐产物或 Python 字节码。

## 2. 仓库位置

主项目目录：

```text
C:\Users\b1995\Documents\AStockAgent
```

真正关联 GitHub 的 Git 仓库是：

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

注意：主项目根目录本身没有 `.git`。后续如需提交或推送代码，应进入 `release\AStockAgent`，并先核对两个目录中的代码是否一致。

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

执行内容：

1. 检出仓库。
2. 安装 Python 3.13。
3. 安装 `requirements.txt` 中的依赖。
4. 检查 Git 是否跟踪了密钥或运行产物。
5. `compileall` 语法编译。
6. `ruff` 基础规则检查。
7. `mypy astock app.py`（核心模块 + 旧入口；见 `mypy.ini`）。
8. 覆盖率运行与报告（不设门槛）。

```text
ruff check astock app.py tests --select E9,F63,F7,F82
mypy astock app.py
coverage run -m unittest discover -s tests -v
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

### 4.1 本地测试

执行目录：

```text
C:\Users\b1995\Documents\AStockAgent\release\AStockAgent
```

执行命令：

```powershell
& 'C:\Users\b1995\Documents\AStockAgent\.venv\Scripts\python.exe' -m unittest discover -s tests -v
```

结果：

```text
Ran 29 tests
OK
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
- 本地 39 个 unittest 通过；根项目与 `release/AStockAgent` 的 `app.py`、`astock/*.py`、`tests/*.py`、`requirements.txt` 哈希一致。
- `git ls-files` 未发现 `.env`、`env.env`、`.venv/`、`cache/`、`data/recommendations/`、`__pycache__/`、`*.pyc`；`.env.example` 已跟踪。
- CI 当前覆盖「依赖安装 + 敏感文件检查 + 语法编译 + ruff 基础规则 + 核心模块 mypy + 覆盖率报告 + 单元测试」；覆盖率只出报告、不设门槛；UI/图表模块在 `.coveragerc` 中排除。
- 2026-11-03 基线：`mypy astock app.py` 通过 22 个源文件；覆盖率（排除 UI/图表）**47%**（3491 statements / 1841 miss）。

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

可选的后续改进：

- 增加静态检查，例如 `ruff`。
- 为关键模块补充覆盖率报告。
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
