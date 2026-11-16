# PNG 导出运维：Chrome / 中文字体 / 版本升级（plan-20261114-001）

ADR-016 补充裁决（2026-11-14，Q6=A）把 PNG 导出从「可选降级」升级为 v1 支持功能：
默认 `png_mode="auto"` 会真实导出 PNG；Windows 开发机与 Linux CI 必须都能出图，
且 PNG 与 HTML artifact 可互相追溯。本文件记录 spike 结论、环境安装、失败语义与升级检查清单。

## 1. 锁定版本（不要用无上限 `>=`）

| 组件 | 锁定值 | 原因 |
| --- | --- | --- |
| `plotly` | `6.7.0` | HTML/PNG 字节随 plotly 版本漂移；`to_image` 参数与布局序列化行为按此版本验证 |
| `kaleido` | `1.5.1` | 1.x 走系统 Chrome（无内置浏览器），与 CI 预装 Chrome 组合已验证；0.2.x API 不同 |
| Python（CI） | 3.13 | 与 `ubuntu-latest` setup-python 一致 |
| Chrome（CI） | `ubuntu-latest` 预装 `google-chrome` | job 会打印 `google-chrome --version` 并写入 `ASTOCK_CHROME_VERSION` |
| 中文字体 | `fonts-noto-cjk`（Linux）/ 微软雅黑、黑体（Windows） | 标题/坐标轴/图例缺 CJK 字形会变方块 |

`requirements.txt` 已写死 `plotly==6.7.0`、`kaleido==1.5.1`；`tests/test_requirements_pins.py`
断言两行必须是 `==x.y.z`，`pip check` 在 CI 两个 job 都执行。

字体栈由 `astock.agent.chart_render.font_family()` 提供，默认：

```text
Noto Sans CJK SC, Microsoft YaHei, SimHei, sans-serif
```

可用环境变量 `ASTOCK_CHART_FONT` 覆盖（例如只装了思源黑体、或想指定「Noto Sans CJK TC」）。
字体栈会写入 HTML layout 与 PNG meta 的 `font_family`，换机器复现时先比对这一项。

## 2. 目标环境 spike（2026-11-16，Windows 开发机）

实测命令：`.venv\Scripts\python.exe .dsh-spike-png.py`（等价于本文件 §9 的手工验证）。

| 项 | 实测值 |
| --- | --- |
| OS | Windows 11 `10.0.26200` |
| Python | 3.13.5 |
| plotly / kaleido | 6.7.0 / 1.5.1（依赖 choreographer 1.4.0） |
| Chrome | `C:\Users\<user>\AppData\Local\Google\Chrome\Application\chrome.exe`（kaleido 自动发现，`BROWSER_PATH` 未设置） |
| 中文字体 | `C:\Windows\Fonts\msyh.ttc`（微软雅黑）、`simhei.ttf`（黑体） |
| 测试图 | K 线 + 中文标题「贵州茅台（600519.SH）日 K 线 · 前复权」+ 中文坐标轴/图例 |
| 首次导出耗时 | 3.227 s（含启动无头 Chrome） |
| 产物 | 960×520 PNG，32,731 bytes，`\x89PNG` 头正常，前景像素占比 0.4263 |
| 目视结论 | 标题/坐标轴/图例中文均正常，无方块 |

Linux CI 侧（`png-export` job）使用同款 plotly/kaleido 版本 + ubuntu-latest 预装 Chrome +
`fonts-noto-cjk`，job 每次打印 `plotly/kaleido` 版本、`fc-list :lang=zh` 前 3 条与 Chrome 版本；
中文样张通过 `ASTOCK_PNG_SAMPLE_DIR` 落到 `png-artifacts/` 并由 `actions/upload-artifact` 上传。

## 3. 安装与自检

### Windows 开发机

1. 安装 Google Chrome（Stable 即可）；kaleido 1.x 会自动发现，找不到时设 `BROWSER_PATH` 指向 `chrome.exe`。
2. 中文字体：系统自带微软雅黑/黑体即可；若被裁剪，安装「Noto Sans CJK SC」。
3. 安装依赖并自检：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest tests/test_agent_chart_png_export.py tests/test_agent_chart_png_zh.py -q
```

### Linux（CI / 服务器）

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends fonts-noto-cjk
fc-list :lang=zh | head          # 必须有输出
google-chrome --version          # ubuntu-latest 预装；没有则用 kaleido 自带下载器兜底：
python -c "import kaleido; kaleido.get_chrome_sync()"
python -m pytest tests/test_agent_chart_png_export.py tests/test_agent_chart_png_zh.py -q --timeout=180
```

CI 的 `png-export` job 会设 `ASTOCK_REQUIRE_CJK=1`：缺中文字体时测试直接失败（不是 skip），
避免「CI 绿但中文全是方块」。本地没有 CJK 字体时该用例 skip 并提示安装。

## 4. 导出语义（`png_mode` 三态）

`render(..., png_mode=...)`：

| 模式 | 行为 | 失败时 |
| --- | --- | --- |
| `off` | 不尝试导出，只出 HTML | — |
| `auto`（默认） | 尝试导出 | `degraded=True` + 告警；缺 kaleido/Chrome 等环境失败会缓存探测结果，后续图不再重复起浏览器，只标注缓存原因 |
| `require` | 显式请求 PNG | 抛 `PNGExportError`，`agent_runs.status=failed`（含 Agent 循环预建的 running run，经 `store.fail_run` 显式落状态），不写 artifact 行（不留半成品） |

`export_png` 是兼容别名：`False` → `off`，`True` → `auto`；显式 `png_mode` 优先。
环境探测可用 `chart_render.reset_png_probe_cache()` 重置（例如运行期补装了 Chrome）。

「默认路径不得每图静默 degraded」的落地方式：默认 `auto` 在依赖齐全时正常出 PNG；
环境缺失时只告警一次并缓存原因，且 `meta.png_error` 每图可查；需要强保证的调用方用 `require`。

已知引擎噪音：kaleido 1.5.1 + Chrome 首次渲染偶发
`ReferenceError: kaleido_scopes is not defined`（页面脚本竞态，Windows 与 CI 均有观测，
非环境缺失）。`_try_png` 对该类错误自动重试一次，重试成功即不降级；其他失败不重试。

## 5. PNG artifact 追溯字段

HTML artifact 行（`kind='figure'`）的 `meta_json` 同时记录 PNG 信息，PNG 与 HTML 分属不同
内容寻址目录，用以下字段互查：

| 字段 | 含义 |
| --- | --- |
| `png_path` | PNG 绝对路径（沿用既有字段，`prune_artifacts` 据此保护文件） |
| `png_rel_path` | PNG 相对 artifact 根的路径（`<digest>/chart_<artifact_id>.png`） |
| `png_digest` | PNG 内容 sha256（`prune_artifacts` 也据此保护） |
| `png_bytes` / `png_width` / `png_height` / `png_scale` | 字节数与像素尺寸（scale 固定 1） |
| `png_elapsed_ms` | 本次导出耗时 |
| `font_family` | 实际使用的字体栈 |
| `kaleido_version` / `chrome_version` / `chrome_path` | 导出引擎版本；kaleido 0.2.x 记 `bundled`，Linux 无版本号时由 CI 的 `ASTOCK_CHROME_VERSION` 注入 |
| `png_mode` / `png_error` / `degraded` | 本次导出模式、本次失败原因、是否降级 |
| `png_reused` | 本次重写同一 `artifact_id` 时是否沿用了此前导出的 PNG |
| `png_source_mode` | 产出当前 `png_path` 的那次导出模式（复用旧图时不会写成本次的 `off`/失败模式） |
| `png_last_attempt_error` | 复用旧 PNG 时保留的本次尝试失败原因；此时 `png_error` 为 `None`，避免「有 PNG 却报导出失败」的矛盾 |

`params`（HTML 复现前提）里同时带上 `plotly_version`、`kaleido_version`、`chrome_version`、
`chrome_path`、`font_family`、`python_version`，换环境排查时先比对这些值。

## 6. CI 结构

- 主 job `test`：`pip install -r requirements.txt` + `pip check` → lint/type-check →
  全量 `pytest tests -m "not png"`。主 job 不装中文字体、不依赖 Chrome/kaleido 运行时，
  真实导出用例由 marker 排除，宿主环境抖动不会拖红主流水线（评审 P1）。
- 独立 job `png-export`（`timeout-minutes: 10`）：装 `fonts-noto-cjk` 并定位 Chrome，只跑
  `tests/test_agent_chart_png_export.py` 与 `tests/test_agent_chart_png_zh.py`（`-m png`），
  每用例 `--timeout=180`；失败/超时在 PR 上单独可见，不拖垮主流水线；
  `if: always()` 上传 `png-artifacts/**/*.png`，PR 里可直接下载中文样张。
- 首次在新环境跑通前可先把该 job 设为非阻塞，确认稳定后必须保持必过（当前为必过）。

## 7. 版本升级检查清单

升级 `plotly` / `kaleido` / Chrome 时按顺序执行：

1. 只改 `requirements.txt` 的精确版本（例如 `plotly==6.8.0`），同步本文件 §1 表格。
2. 本地跑 `pip install -r requirements.txt && pip check`，确认 `tests/test_requirements_pins.py` 通过。
3. 跑 `pytest tests/test_agent_chart_png_export.py tests/test_agent_chart_png_zh.py -q --timeout=180`，
   确认 PNG 头、尺寸、`png_digest` 与 sha256 一致、中文样张无方块。
4. 检查 `meta.png_digest` 变化是否可接受：PNG 字节随版本漂移属预期，但**同一版本内**必须稳定；
   若同一版本重复导出 digest 变化，先查 `include_plotlyjs`/字体/Chrome 是否被环境改动。
5. 推送后确认 `png-export` job 通过，并下载 artifact 目视检查中文标题/坐标轴/图例。
6. kaleido 0.2.x → 1.x 属破坏性升级：0.2.x 自带浏览器（`chrome_version="bundled"`），
   1.x 需要系统 Chrome 且 `to_image` 走 choreographer；升级时同时更新 CI 的 Chrome 安装步骤。

## 8. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 中文变方块 `□□□` | 环境缺 CJK 字体，或字体栈被 `ASTOCK_CHART_FONT` 覆盖成无中文字体 | Linux 装 `fonts-noto-cjk`；确认 `fc-list :lang=zh` 有输出；检查 meta `font_family` |
| `kaleido_scopes is not defined` | kaleido 1.5.1 首帧页面竞态 | `_try_png` 已自动重试一次；连续出现时重启进程或升级 kaleido |
| `Chrome executable not found` / `BrowserFailedError` | kaleido 1.x 找不到 Chrome | 装 Chrome 或设 `BROWSER_PATH`；容器内用 `kaleido.get_chrome_sync()` |
| `requires the Kaleido package` | 未装 kaleido / 用了 `export_png=False` 之外的旧调用 | `pip install -r requirements.txt`；默认 `auto` 会降级但 `require` 会报错 |
| 导出超时 | 首次启动 Chrome 较慢或资源不足 | 单测 `--timeout=180`；CI job 限 10 分钟；长期可加大 runner |
| `png_digest` 与文件 sha256 不一致 | artifact 被外部改写或 meta 串行化丢失 | 重新渲染；`prune_artifacts` 依赖 `png_path`/`png_digest` 保护文件 |
| PNG 与 HTML 描述不一致 | HTML 被复用但 PNG 来自旧环境 | 比对 `params.plotly_version`、`kaleido_version`、`chrome_version`、`font_family` |

## 9. 手工验证（任一环境）

```python
from astock.agent import chart_render

spec = {
    "version": 1,
    "kind": "candlestick",
    "title": "贵州茅台（600519.SH）日 K 线",
    "data": {"source": "inline", "fields": ["day", "open", "high", "low", "close"],
             "rows": [{"day": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5}]},
    "encode": {"x": "day"},
}
rendered = chart_render.render(spec, png_mode="require")
print(rendered.png_path, rendered.params["kaleido_version"], rendered.params["chrome_version"])
```

产物打开后确认：标题/坐标轴/图例中文正常、无方块、宽高与 `meta.png_width/png_height` 一致。
