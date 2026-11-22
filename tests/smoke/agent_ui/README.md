# Agent 会话 UI 冒烟（六条主路径 · 可复现）

用**真实 Gradio 页面 + 真实浏览器（Chrome DevTools Protocol）**验证 Agent 会话 tab 的六条主路径：
上传、流式、步骤、内嵌图、引用、中断。目的不是替代 pytest 单测，而是挡住「后端全绿、界面却坏了」
这类回归（例如流式变成一次性、中断丢半截回答、引用锚点失效）。

默认**不进入普通 CI**；只在 nightly workflow（`.github/workflows/agent-ui-smoke.yml`）或手动运行。

## 目录结构

| 文件 | 作用 |
| --- | --- |
| `cdp.py` | 极简 CDP 客户端（`websocket-client` + 标准库），地址可用环境变量覆盖 |
| `fixture_app.py` | 夹具应用：确定性的 FakeModel + read_file 工具 + 图表/引用元数据 |
| `smoke_drive.py` | 断言驱动：逐条执行六条路径，输出 `PASS/FAIL` 与 `report.json` |
| `run_smoke.py` | 固定启动方式：挑端口 → 起夹具 → 起 headless 浏览器 → 跑驱动 → 清理 |
| `fixtures/年报.pdf` | 合成 PDF（3 页纯文本），用于上传路径 |
| `fixtures/利润表.xlsx` | 合成 xlsx（工作表「利润表」），用于上传路径 |

## 依赖

- Python 3.11+（仓库使用 3.13），并已安装项目依赖：`gradio`、`pdfplumber`、`openpyxl`、`websocket-client`
- Chrome 或 Edge（任选；`run_smoke.py` 自动发现常见安装路径）
- 不需要 `reportlab`：两个夹具文件已入库，不在运行时生成

安装（仓库根目录）：

```text
python -m pip install -r requirements.txt
python -m pip install websocket-client
```

Windows 本机若 `python` 不可用，用项目解释器：

```text
.venv\Scripts\python.exe -m pip install -r requirements.txt websocket-client
```

## 快速开始（一条命令）

```text
python tests/smoke/agent_ui/run_smoke.py
```

Windows：

```text
.venv\Scripts\python.exe tests\smoke\agent_ui\run_smoke.py
```

它会自动完成：挑空闲端口 → 起夹具 app（含降级场景）→ 起 headless 浏览器 → 跑驱动 →
打印证据目录 → 成功时删除临时目录。**退出码 0 表示六条路径全过**，可直接被 nightly 或脚本消费。
默认要求降级场景也起来；只有显式传 `--no-degraded` 才跳过该断言（此时 `run.json` 记
`degraded_expected=false`）。

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--workdir PATH` | 固定工作目录（日志、`report.json`、`run.json`、临时 sqlite） |
| `--shots-dir PATH` | 可选：把 3 张关键截图写到该目录（默认不截图、不入库） |
| `--keep-workdir` | 成功后也保留工作目录，便于看日志 |
| `--browser PATH` | 指定浏览器可执行文件 |
| `--no-degraded` | 跳过「未配置模型」降级横幅断言 |
| `--port` | 夹具端口（默认自动挑空闲端口） |
| `--timeout` | **驱动总预算**（默认 300s）。驱动内部每次等待都收敛到剩余预算；`run_smoke.py` 给子进程的硬兜底是 `--timeout + 120s`，超了就按 `driver timeout` 失败 |

手动两步时 `smoke_drive.py` 也接受 `--timeout`，语义相同：整轮断言（含导航、上传、流式、
中断后重问）共用这一个预算，预算用尽即报 `driver timeout（--timeout 预算已用尽）`。

## 手动两步（调试用）

```text
# 终端 1：起夹具（workdir 里会生成 files/ 与 smoke.db）
python -m tests.smoke.agent_ui.fixture_app --port 7866 --workdir .workdir

# 终端 2：先手动起一个带调试端口的浏览器，再跑驱动
chrome --headless=new --remote-debugging-port=9223 --user-data-dir=%TEMP%\agent-smoke about:blank
python -m tests.smoke.agent_ui.smoke_drive --url http://127.0.0.1:7866 \
       --db .workdir/smoke.db --debug-port 9223
```

两个终端都需要仓库根目录在 `PYTHONPATH` 中（或在仓库根目录执行）：

```text
set PYTHONPATH=%CD%        # Windows
export PYTHONPATH=$PWD     # Linux / macOS
```

## 六条路径与通过判据

| # | 路径 | 操作 | 通过判据（失败即 `FAIL <id>`） |
| --- | --- | --- | --- |
| 1 | `upload` | 用 CDP `DOM.setFileInputFiles` 选 PDF + xlsx | 上传状态出现 `附件解析：2/2 成功`，并含 `✅ 年报.pdf（PDF 3 页）`、`✅ 利润表.xlsx（1 张表：利润表）` |
| 2 | `stream` | 提问后用 MutationObserver 记录状态与 `.message-content` 长度 | 出现 `正在输出回答`，且回答文本长度至少有 3 个不同值（证明是增量流而非一次性） |
| 3 | `steps` | 生成结束 | `ol.astock-steps li` ≥ 3 且 `data-status` 全为 `ok`，每步有参数与耗时；正文含 `read_file`、`已读取工具结果`、`✅ 已完成` |
| 4 | `figure` | 检查图表区 | `iframe.astock-figure-frame` 的 `srcdoc` 含 `smoke-chart`；图注为 `营收趋势｜数据截至 2025-12-31｜数据时间 2026-03-20`；正文含 `查看图表 spec / 来源` |
| 5 | `citations` | 点击首个引用链接 | `a[href^="#cite-"]` ≥ 2；点击后 `location.hash === "#cite-1"`，卡片含 `文件：年报.pdf` 与 `定位：` |
| 6 | `interrupt` | 流式开始后点 `⏹ 中断`，再问一轮 | 状态含 `已停止输出` 或 `已中断`，半截回答非空且含 `保留`；随后一轮必须**新增**一次 `✅ 已完成`（数量比发送前多），且末条不再含 `正在输出回答` / `正在分析问题`（只看全页存在会被上一轮完成标记或本轮流式中间态提前满足） |
| + | `audit` | 查夹具 sqlite | `agent_runs` ≥ 2 且 `agent_steps` ≥ 4（AGENT-02 每次工具调用落库） |
| + | `degraded` | 打开未配置模型的实例 | 横幅含 `模型未就绪` |

## 失败判据

- 任一检查抛 `AssertionError` → 打印 `FAIL <id> :: <detail>` 与上下文，写 `report.json`，退出码 **1**。
- 启动失败：夹具 180s 内没有 `READY url=`、降级夹具 180s 内没有就绪（未传 `--no-degraded` 时）、
  浏览器 60s 内没有 `/json/version`、或驱动超时 → `FAIL ...` 并打印对应日志尾部，退出码 **1**。
- **`run.json` 在每条失败路径上都会写**，`stage` 字段指出失败停在 `fixture` / `degraded` /
  `browser` / `driver` 哪一段，成功时为 `done`；`ok` 同时要求驱动退出码为 0 且没有 `error`。
  驱动非零退出时 `error` 取自 `report.json`（驱动自己撞上 `--timeout` 预算时记为 `driver timeout`）。
- 失败时**保留**工作目录：`app.log` / `browser.log` / `driver.log` / `run.json` 都会写入；
  驱动被外层 `--timeout + 120s` 强杀时 `report.json` 可能缺失，但 `driver.log` 会被补写。
  成功时默认删除临时目录，加 `--keep-workdir` 可保留。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_SMOKE_BROWSER` | 自动发现 | 浏览器可执行文件路径 |
| `AGENT_SMOKE_DEBUG_HOST` / `AGENT_SMOKE_DEBUG_PORT` | `127.0.0.1` / `9223` | 手动模式下 CDP 地址 |
| `AGENT_FILES_DIR` | 被 `run_smoke.py` 清掉 | 夹具一律把上传目录指向工作目录，不写真实 `data/agent/files` |
| `GRADIO_TEMP_DIR` | `<workdir>/gradio` | 由启动脚本与夹具设置，上传缓存不落到系统临时目录或仓库 |

## 截图

默认**不产生任何截图**，仓库不保存截图。需要人工核对视觉时用 `--shots-dir`，只写 3 张关键帧
（图表区、引用卡片、中断后保留的半截回答）。截图目录不要提交。

## 为什么不进普通 CI

`.github/workflows/ci.yml` 仍只有 `test` 与 `png-export` 两个 job；本冒烟需要真实浏览器与较长等待，
放在独立的 `.github/workflows/agent-ui-smoke.yml`：

- `schedule`：每天一次（UTC 18:17）；
- `workflow_dispatch`：手动触发；
- 产物：`report.json`、`run.json` 与 `*.log`（保留 7 天）。

## 已知限制

- 断言绑定 Gradio 前端 DOM 与中文文案（`.message-content`、`iframe.astock-figure-frame`、
  `button` 文案、`ol.astock-steps`）；升级 Gradio 或改文案时要同步 `smoke_drive.py` 与 `fixture_app.py`。
- 图表只校验 `srcdoc` 与图注，不做像素比对，避免 headless 字体/缩放差异造成假失败。
- 上传走 `DOM.setFileInputFiles`，没有经过系统文件选择框；真实浏览器点击流程不覆盖。
- 只覆盖夹具中的确定性假模型，不评估真实模型质量、网络与限流。
- 夹具里的数字（营收 100 亿元等）与两个附件都是**合成数据**，仅用于断言。
- Windows headless 首次启动浏览器可能多等 3–5 秒；`run_smoke.py` 已包含就绪轮询。
- 部分 Edge 版本的 DevTools 只监听 `[::1]`。驱动会依次尝试 `127.0.0.1` / `localhost` / `[::1]`；
  手动模式连不上时，先 `curl http://localhost:<port>/json/version` 确认端点。
- 浏览器必须能在 `--user-data-dir` 里创建锁文件与缓存。若运行环境禁止写这些目录（例如受限的
  agent 沙箱），Edge 会打印 `Failed to create a ProcessSingleton for your profile directory` 并直接
  abort，表现为「浏览器调试端口没起来」；这属于环境限制，换到普通终端或放开写权限即可。
- `degraded` 场景注入的是**必然抛错**的模型工厂（不是「没传模型」）。这样即便本机配置了可用的
  `DEEPSEEK_API_KEY`，降级横幅也稳定出现，断言不会随环境漂移。
- Windows 控制台若是 GBK，汇总输出里的 emoji（如 `⏹`）可能显示成 `?`；脚本已用
  `errors="replace"` 兜底，不会因为编码问题把整次运行判失败。

## 本机一次真实运行（2026-11-20，Windows + Edge headless）

```text
$ .venv\Scripts\python.exe tests/smoke/agent_ui/run_smoke.py --workdir tests/smoke/agent_ui/.workdir --keep-workdir
INFO 工作目录 <repo>/tests/smoke/agent_ui/.workdir
INFO 夹具就绪 http://127.0.0.1:53223
INFO 降级夹具就绪 http://127.0.0.1:53225
INFO 浏览器 ...\msedge.exe（headless，调试端口 53224）
PASS upload :: 2/2 成功；年报.pdf（PDF 3 页）、利润表.xlsx（1 张表：利润表）
PASS stream :: 状态序列 ['正在分析问题', '已调用 read_file', '正在输出回答']；回答长度出现 168 个不同值
PASS steps :: 3 个步骤全 ok，参数 3 条、耗时 3 条
PASS figure :: 3 个内嵌 iframe（srcdoc 含 smoke-chart），caption 匹配
PASS citations :: 3 条引用，点击 #cite-1 后 hash 生效并显示文件/定位
PASS interrupt :: 中断保留 32 字，随后再问一轮已完成
PASS audit :: agent_runs=6 agent_steps=24（AGENT-02）
PASS degraded :: 无模型时页面可打开，横幅显示「模型未就绪」
SMOKE OK :: 8 项断言全部通过，用时 13.42s
```

`--workdir` 里的 `report.json` / `run.json` / `app.log` / `browser.log` / `driver.log` 是失败排查的
第一现场，目录已被 `.gitignore` 忽略。

