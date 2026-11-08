# AStockAgent 开发状态与交接文档

日期：2026-11-08  
用途：交接给其他 Codex / ChatGPT 会话继续开发  
项目根目录：`C:\Users\b1995\Documents\AStockAgent`

## 1. 当前状态摘要

- 项目根目录现在已经是正式 Git 仓库。
- 当前分支：`main`
- 当前提交：`3bf61a2`（PR #2 合并提交）
- 本地 `main` 与 `origin/main` 完全一致。
- PR #1、PR #2 均已合并。
- 最新 `main` CI 已通过。
- 根目录和 `release/AStockAgent` 均已同步到 `3bf61a2`。
- Jupyter Notebook 当前正在运行。
- Tushare token 与 HTTP 地址已配置在未跟踪的 `.env`。
- `.env` 没有被 Git 跟踪。
- 根目录的 notebook 改动已保存到本地保险分支 `wip/notebooks-20261108`；2026-11-08 清理记录见第 12 节。

## 2. 仓库信息

GitHub 仓库：

```text
https://github.com/b199510027373-create/AStockAgent.git
```

根目录 Git 仓库：

```text
C:\Users\b1995\Documents\AStockAgent
```

发布目录 Git 仓库：

```text
C:\Users\b1995\Documents\AStockAgent\release\AStockAgent
```

远程名：

```text
origin
```

当前上游：

```text
origin/main
```

发布仓库当前状态：

```text
main
4abe388
工作区干净
```

注意：根目录 `.git` 是从发布仓库复制而来，Windows 所有权与当前执行用户存在差异。普通 `git` 命令可能报：

```text
detected dubious ownership
```

当前可用的临时方式是每条命令显式传入：

```powershell
$root = 'C:\Users\b1995\Documents\AStockAgent'
git -c "safe.directory=$root" -C $root status --short
```

不要在没有确认的情况下修改全局 Git 安全配置。

## 3. Git 提交与 PR

本次同步分支：

```text
sync/dev-root
```

PR：

```text
https://github.com/b199510027373-create/AStockAgent/pull/1
```

状态：

```text
已合并
```

合并提交：

```text
4abe388033682c4a34d97efced8d61a61c265c3f
```

相关提交：

```text
2fce08f sync: root development changes
b4d44b3 test: isolate contract entry mocks
23cc52a chore: ignore Jupyter project metadata
e13c4d7 test: scope metadata mock to evidence test
4abe388 Merge pull request #1 from sync/dev-root
```

## 4. CI 状态

工作流文件：

```text
.github/workflows/ci.yml
```

触发条件：

- 推送到 `main`
- 针对 `main` 的 Pull Request
- 手动触发

CI 内容：

1. Python 3.13
2. 安装 `requirements.txt`
3. 检查禁止入库的密钥和运行产物
4. 编译 Python 源码
5. Ruff 基础错误检查
6. Mypy 类型检查
7. 运行全部测试
8. 覆盖率门槛 50%

最新成功运行：

```text
https://github.com/b199510027373-create/AStockAgent/actions/runs/42478721907
```

本地验证结果：

```text
Ran 90 tests
OK
Mypy: Success, no issues found in 23 source files
```

本地 Ruff 在当前 `.venv` 中存在启动异常，但 GitHub Actions 中的 Ruff 已通过。

## 5. 已完成的主要开发内容

### 5.1 契约层

新增：

```text
astock/contracts.py
tests/test_contracts.py
```

覆盖：

- `BarFrame`
- `EventFrame`
- `Evidence`
- `RecommendationRun`
- 缺失列、类型错误、空 code、NaN、日期漂移
- 供应商返回结构变化时显式失败

### 5.2 数据与事件入口

已修改：

```text
astock/data.py
astock/events.py
astock/ledger.py
astock/recommend.py
```

主要行为：

- 行情与事件出口接入契约校验
- 公告缺少 `ts_code` 时用请求代码补全
- 供应商列漂移不再静默变成空结果
- Ledger 与推荐输出保持兼容

### 5.3 DevLoop 元数据

以下目录已经提交：

```text
.devloop/
```

包含：

- 架构 ADR
- 契约和模块说明
- 计划条目
- 工作日历
- 评审记录
- 工作日志

### 5.4 Git 忽略规则

当前重点排除：

```text
.env
.env.*
!.env.example
env.env
cache/
data/recommendations/
.venv/
venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ipynb_checkpoints/
release/
anaconda_projects/
.tmp-tests/
coverage_report.txt
coverage_run.txt
mypy_*.txt
temp_*.png
filtered_stocks.csv
```

## 6. 当前未提交内容

以下 notebook 有未提交修改，必须先与用户确认是否保留或提交：

```text
notebooks/01_data_pipeline.ipynb
notebooks/02_indicators.ipynb
notebooks/03_portfolio_check.ipynb
notebooks/04_jupyter_dashboard.ipynb
notebooks/07_data_engine_test.ipynb
notebooks/08_indicator_factory_test.ipynb
notebooks/09_portfolio_manager.ipynb
```

当前未跟踪文件/目录：

```text
01_test.ipynb
daily_report.txt
deliverables/
my_stocks.csv
notebooks/Untitled.ipynb
notebooks/app_write.ipynb
notebooks/test.ipynb
notebooks/ts_init.py
tools/
```

这些内容可能包含：

- 用户运行结果
- 临时 notebook
- 旧的演示数据
- 交付文档
- 调试文件

默认处理方式：

1. 不要执行 `git add -A`。
2. 不要回滚用户未提交文件。
3. 先查看 `git diff` 和文件用途。
4. 只显式暂存确定要提交的路径。

## 7. Jupyter 状态

Jupyter 当前可访问：

```text
http://127.0.0.1:1234/tree?token=codex-local
```

工作目录：

```text
C:\Users\b1995\Documents\AStockAgent
```

启动方式说明：

- 使用 `C:\Anaconda\python.exe`
- 通过 `PYTHONPATH` 加载项目 `.venv\Lib\site-packages`
- Jupyter runtime 和 config 重定向到 `.tmp-tests`

这样做的原因：

- Anaconda base 缺少 `tushare`、`openai`、`gradio`
- 直接使用 `.venv\Scripts\python.exe` 会出现 `_ctypes` DLL 加载问题
- 系统 Python 加载 `.venv` 的纯 Python 包后可以正常导入项目依赖

如需重新启动，可参考：

```powershell
$root = 'C:\Users\b1995\Documents\AStockAgent'
$env:JUPYTER_RUNTIME_DIR = Join-Path $root '.tmp-tests\jupyter-runtime'
$env:JUPYTER_CONFIG_DIR = Join-Path $root '.tmp-tests\jupyter-config'
$env:PYTHONPATH = Join-Path $root '.venv\Lib\site-packages'

C:\Anaconda\python.exe -u -m notebook `
  --no-browser `
  --ip=127.0.0.1 `
  --port=1234 `
  --notebook-dir=$root `
  --ServerApp.token='codex-local'
```

注意：以上是本地开发令牌，不要将 Jupyter 服务暴露到公网。

## 8. Tushare 状态

`.env` 已配置以下键：

```text
TUSHARE_TOKEN
TUSHARE_API_URL
DEEPSEEK_API_KEY
DEEPSEEK_BASE
```

状态：

```text
TUSHARE_TOKEN: 已配置
TUSHARE_API_URL: 已配置
DEEPSEEK_API_KEY: 已配置
```

已完成的只读连通性测试：

```text
trade_cal: 成功，返回 8 行
daily: 成功，返回 5 行
```

安全要求：

- 不要打印 `.env` 内容
- 不要提交 `.env`
- 不要把 token 粘贴到文档、Issue、PR 或聊天记录
- 如需在 CI 中使用，只能放在 GitHub Actions Secrets

当前项目使用 Tushare HTTP 接口，不依赖 MCP 客户端。若后续要接 MCP，需要单独设计和测试，不要直接替换现有数据访问层。

## 9. 开发环境注意事项

### 9.1 Python

系统 Python：

```text
C:\Anaconda\python.exe
Python 3.13.5
```

项目虚拟环境：

```text
C:\Users\b1995\Documents\AStockAgent\.venv
```

`.venv` 直接启动可能出现：

```text
ImportError: DLL load failed while importing _ctypes
```

当前测试替代方式：

```powershell
$env:PYTHONPATH = 'C:\Users\b1995\Documents\AStockAgent\.venv\Lib\site-packages'
C:\Anaconda\python.exe -m unittest discover -s tests -v
```

### 9.2 依赖

运行依赖：

```text
requirements.txt
```

开发依赖：

```text
requirements-dev.txt
```

### 9.3 安全

必须继续保持：

- `.env` 不提交
- `cache/` 不提交
- `data/recommendations/` 不提交
- `.venv/` 不提交
- `__pycache__/` 和 `*.pyc` 不提交
- `release/` 不作为根仓库内容提交
- `anaconda_projects/` 不提交

## 10. 推荐继续操作

下一个会话建议按以下顺序开始：

1. 读取本文件。
2. 执行：

```powershell
$root = 'C:\Users\b1995\Documents\AStockAgent'
git -c "safe.directory=$root" -C $root status --short
git -c "safe.directory=$root" -C $root log -5 --oneline
```

3. 确认未提交 notebook 和未跟踪文件的处理意图。
4. 修改代码后先运行：

```powershell
$env:PYTHONPATH = 'C:\Users\b1995\Documents\AStockAgent\.venv\Lib\site-packages'
C:\Anaconda\python.exe -m unittest discover -s tests -v
C:\Anaconda\python.exe -m mypy astock app.py
```

5. 显式暂存目标文件，不使用 `git add -A`。
6. 推送后检查 GitHub Actions。
7. 不修改 `.env` 的真实密钥内容。

## 11. 给下一个会话的可粘贴开场

```text
请继续开发 AStockAgent。

项目根目录：C:\Users\b1995\Documents\AStockAgent
GitHub：https://github.com/b199510027373-create/AStockAgent.git
当前分支：main
当前提交：3bf61a2
PR #1、PR #2 已合并，最新 CI 已通过。

根目录现在已经是 Git 仓库，但存在 Windows 所有权提示；读取 Git 状态时使用：
git -c "safe.directory=C:\Users\b1995\Documents\AStockAgent" -C "C:\Users\b1995\Documents\AStockAgent" status --short

7 个 notebook 的旧改动保存在本地分支 wip/notebooks-20261108（27df40d），main 工作区是干净的。不要执行 git add -A，不要自动回滚用户文件。

Jupyter 正在：
http://127.0.0.1:1234/tree?token=codex-local

Tushare 已配置在未跟踪的 .env，且 trade_cal 和 daily 已验证可用。
绝对不要提交 .env、cache/、data/recommendations/、.venv/、__pycache__/、*.pyc。

开始工作前先查看 docs/AStockAgent_development_status_2026-11-08.md。
```

## 12. 2026-11-08 清理完成（最新）

- 7 个 notebook 修改已提交到本地保险分支 `wip/notebooks-20261108`（提交 `27df40d`），未推送。
- `main` 工作区已恢复干净。
- 提交 `.gitignore`、本状态文档、`notebooks/ts_init.py`、`tools/build_product_doc.py` 到分支 `chore/status-and-cleanup`（提交 `1337a4e`），该分支已推送到 origin。
- 以下本地生成物/临时文件已加入 `.gitignore`：`deliverables/`、`01_test.ipynb`、`daily_report.txt`、`my_stocks.csv`、`notebooks/Untitled.ipynb`、`notebooks/test.ipynb`、`notebooks/app_write.ipynb`、`tools/*.pkl`。
- PR #2 已合并到 `main`，合并提交 `3bf61a2`。
- `release/AStockAgent` 已执行 `git pull --ff-only`，同步到 `3bf61a2`。

