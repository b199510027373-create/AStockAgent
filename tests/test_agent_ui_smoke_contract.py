"""`tests/smoke/agent_ui` 的静态契约与纯函数单测（plan-20261119-001 验收 1–5）。

这组测试保证冒烟脚本本身「可复现、可维护、不进普通 CI」：

- 不残留本机绝对路径 / 账号名 / token / 大截图；
- 六条路径的 id 与 README 一一对应；
- 普通 CI（ci.yml）不会误跑需要浏览器的冒烟，只有独立 nightly 入口；
- 启动脚本的路径与端口逻辑是纯函数，可在这里直接验证。
"""

from __future__ import annotations

import ast
import importlib.util
import re
import socket
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_DIR = REPO_ROOT / "tests" / "smoke" / "agent_ui"
DRIVER = SMOKE_DIR / "smoke_drive.py"
RUNNER = SMOKE_DIR / "run_smoke.py"
CDP = SMOKE_DIR / "cdp.py"
FIXTURE_APP = SMOKE_DIR / "fixture_app.py"
README = SMOKE_DIR / "README.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
NIGHTLY = WORKFLOWS / "agent-ui-smoke.yml"
CI = WORKFLOWS / "ci.yml"
REQUIREMENTS_DEV = REPO_ROOT / "requirements-dev.txt"

EXPECTED_CHECKS = ("upload", "stream", "steps", "figure", "citations", "interrupt")
MAX_FIXTURE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 512 * 1024

_TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".txt", ".json", ".ps1", ".sh"}
_SCREENSHOT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
#: 运行期产物（浏览器 profile、日志、临时库），已被 .gitignore 排除，不参与入库检查。
_RUNTIME_DIRS = {".workdir", "__pycache__"}

#: 只匹配「某台机器上的家目录」与常见凭据前缀，不误伤 macOS 的 /Applications 等系统路径。
_PERSONAL_OR_SECRET_PATTERNS = (
    (re.compile(r"[A-Za-z]:\\Users\\", re.IGNORECASE), "Windows 个人目录"),
    (re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"), "Unix 个人目录"),
    (re.compile(r"b1995", re.IGNORECASE), "个人账号名"),
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "类 OpenAI 密钥"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"), "Bearer token"),
    (re.compile(r"(?i)api[_-]?key\s*[:=]\s*[\"'][^\"']{8,}"), "硬编码 api_key"),
)


def _shipped_files() -> list[Path]:
    """目录里真正会入库的文件（跳过 .workdir / __pycache__ 等运行期产物）。"""
    assert SMOKE_DIR.is_dir(), f"冒烟目录不存在：{SMOKE_DIR}"
    return sorted(
        path
        for path in SMOKE_DIR.rglob("*")
        if path.is_file()
        and not any(part in _RUNTIME_DIRS for part in path.relative_to(SMOKE_DIR).parts)
    )


def _text_files() -> list[Path]:
    return [path for path in _shipped_files() if path.suffix.lower() in _TEXT_SUFFIXES]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _driver_checks() -> tuple[str, ...]:
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CHECKS" for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("smoke_drive.py 里找不到 CHECKS 常量")


def _workflow(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path.name} 不是合法的 workflow YAML"
    return raw


def _triggers(raw: dict) -> dict:
    # YAML 1.1 会把未加引号的 `on:` 解析成布尔 True。
    value = raw.get("on", raw.get(True))
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# 验收 1–2：干净检出可运行、不含本机路径/密钥/大体积截图
# ---------------------------------------------------------------------------
def test_smoke_package_has_no_personal_paths_or_secrets() -> None:
    hits: list[str] = []
    for path in _text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, label in _PERSONAL_OR_SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{path.relative_to(REPO_ROOT)}:{line} {label} -> {match.group(0)!r}")
    assert not hits, "冒烟脚本残留本机路径/凭据：\n" + "\n".join(hits)


def test_smoke_package_ships_only_small_fixtures() -> None:
    images = [
        path.relative_to(REPO_ROOT)
        for path in _shipped_files()
        if path.suffix.lower() in _SCREENSHOT_SUFFIXES
    ]
    assert not images, f"冒烟目录不应包含截图：{images}"

    fixtures = SMOKE_DIR / "fixtures"
    pdf = fixtures / "年报.pdf"
    xlsx = fixtures / "利润表.xlsx"
    for path in (pdf, xlsx):
        assert path.is_file(), f"缺少夹具：{path}"
        assert path.stat().st_size <= MAX_FIXTURE_BYTES, f"夹具过大：{path}"

    total = sum(path.stat().st_size for path in _shipped_files())
    assert total <= MAX_PACKAGE_BYTES, f"冒烟目录总大小 {total}B 超过 {MAX_PACKAGE_BYTES}B"


def test_smoke_scripts_are_not_collected_by_pytest() -> None:
    collected = [
        path.name
        for path in _shipped_files()
        if path.suffix == ".py"
        and (path.name.startswith("test_") or path.name.endswith("_test.py") or path.name == "conftest.py")
    ]
    assert not collected, f"这些文件会被主测试流水线收集（冒烟应手动/nightly 触发）：{collected}"


# ---------------------------------------------------------------------------
# 验收 3–4：README 与六条路径一一对应
# ---------------------------------------------------------------------------
def test_six_checks_match_readme_and_fixture() -> None:
    assert _driver_checks() == EXPECTED_CHECKS
    readme = README.read_text(encoding="utf-8")
    for check in EXPECTED_CHECKS:
        assert f"`{check}`" in readme, f"README 没有说明 {check} 路径"
    for label in ("上传", "流式", "步骤", "内嵌图", "引用", "中断"):
        assert label in readme, f"README 缺少「{label}」路径的描述"


def test_readme_documents_dependencies_start_failure_and_limits() -> None:
    readme = README.read_text(encoding="utf-8")
    needles = (
        "## 依赖",
        "## 快速开始",
        "## 失败判据",
        "## 已知限制",
        "退出码",
        "run_smoke.py",
        "agent-ui-smoke.yml",
        "--shots-dir",
        "AGENT_SMOKE_BROWSER",
    )
    missing = [needle for needle in needles if needle not in readme]
    assert not missing, f"README 缺少章节/说明：{missing}"


def test_fixture_app_supports_both_scenarios() -> None:
    source = FIXTURE_APP.read_text(encoding="utf-8")
    assert '"degraded"' in source and '"interactive"' in source
    assert "AGENT_FILES_DIR" in source, "夹具必须把上传目录指到工作目录"
    assert "chunk_delay" in source, "夹具需要可调流式延时，中断路径才稳定"


# ---------------------------------------------------------------------------
# 验收 5：默认不进普通 CI，提供手动/nightly 入口
# ---------------------------------------------------------------------------
def test_plain_ci_does_not_run_the_browser_smoke() -> None:
    text = CI.read_text(encoding="utf-8").lower()
    assert "smoke" not in text, "ci.yml 不应包含浏览器冒烟（验收 5：默认不进普通 CI）"
    assert "tests/smoke" not in text, "ci.yml 不应收集 tests/smoke"


def test_nightly_workflow_is_scheduled_and_manual_only() -> None:
    raw = _workflow(NIGHTLY)
    triggers = _triggers(raw)
    assert "schedule" in triggers, "nightly 需要 schedule 触发器"
    assert "workflow_dispatch" in triggers, "nightly 需要手动触发入口"
    assert "push" not in triggers and "pull_request" not in triggers, "nightly 不应随 push/PR 运行"

    steps = raw["jobs"]["smoke"]["steps"]
    runs = [str(step.get("run", "")) for step in steps]
    assert any("tests/smoke/agent_ui/run_smoke.py" in run for run in runs), "nightly 必须调用固定启动脚本"
    uploads = [step for step in steps if "upload-artifact" in str(step.get("uses", ""))]
    assert uploads, "nightly 需要上传失败证据（report.json / 日志）"
    assert "report.json" in str(uploads[0].get("with", {}).get("path", ""))


def test_nightly_installs_the_websocket_driver_dependency() -> None:
    assert CDP.is_file()
    assert "websocket" in CDP.read_text(encoding="utf-8"), "cdp.py 应使用 websocket-client"
    requirements = REQUIREMENTS_DEV.read_text(encoding="utf-8")
    assert re.search(r"^websocket-client==\d", requirements, re.MULTILINE), (
        "requirements-dev.txt 需要锁定 websocket-client（nightly 只装这个文件）"
    )


# ---------------------------------------------------------------------------
# 启动脚本的纯函数：路径不写死、端口自动挑、失败给清楚原因
# ---------------------------------------------------------------------------
def test_runner_derives_repo_root_from_file_location() -> None:
    runner = _load(RUNNER, "agent_smoke_run")
    assert runner.REPO_ROOT == REPO_ROOT, "run_smoke.py 必须从 __file__ 推导仓库根，不能写死路径"
    assert runner.DEFAULT_TIMEOUT == 300.0
    assert runner.browser_candidates(), "按平台至少要给出一组浏览器候选"


def test_runner_finds_a_free_port_and_reports_missing_browser() -> None:
    runner = _load(RUNNER, "agent_smoke_run")
    port = runner.pick_free_port()
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
    with pytest.raises(FileNotFoundError, match="指定的浏览器不存在"):
        runner.find_browser("definitely-not-a-browser-binary")
    assert runner.find_browser(sys.executable) == sys.executable, "显式路径应原样返回"


def test_runner_waits_for_log_and_closed_port() -> None:
    runner = _load(RUNNER, "agent_smoke_run")
    # 用本文件自身当「日志」：内容里含 smoke，且不写任何临时文件。
    # 拼出来的 needle 保证全文都不出现（直接写字面量会命中测试自身源码）。
    absent = "QZX" + "-ABSENT"
    assert runner.wait_for_log(Path(__file__), "agent_ui_smoke_contract", timeout=1)
    assert not runner.wait_for_log(Path(__file__), absent, timeout=0.3)
    assert not runner.wait_for_log(SMOKE_DIR / "no-such.log", "READY", timeout=0.3)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        closed = int(sock.getsockname()[1])
    assert not runner.wait_port("127.0.0.1", closed, timeout=0.3)


def test_cdp_debug_address_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    cdp = _load(CDP, "agent_smoke_cdp")
    assert cdp.DEFAULT_DEBUG_HOST == "127.0.0.1"
    assert cdp.DEFAULT_DEBUG_PORT == 9223
    monkeypatch.delenv(cdp.DEBUG_HOST_ENV, raising=False)
    monkeypatch.delenv(cdp.DEBUG_PORT_ENV, raising=False)
    assert cdp.debug_url() == "http://127.0.0.1:9223"
    monkeypatch.setenv(cdp.DEBUG_PORT_ENV, "1234")
    monkeypatch.setenv(cdp.DEBUG_HOST_ENV, "192.0.2.10")
    assert cdp.debug_url() == "http://192.0.2.10:1234"


def test_cdp_tries_all_loopback_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    # 回归：Windows 上 Edge 的 DevTools 只监听 [::1]，只试 127.0.0.1 会假失败。
    cdp = _load(CDP, "agent_smoke_cdp")
    monkeypatch.delenv(cdp.DEBUG_HOST_ENV, raising=False)
    monkeypatch.delenv(cdp.DEBUG_PORT_ENV, raising=False)
    bases = cdp._candidate_bases()
    assert bases[0] == "http://127.0.0.1:9223"
    assert "http://localhost:9223" in bases
    assert "http://[::1]:9223" in bases
    monkeypatch.setenv(cdp.DEBUG_HOST_ENV, "192.0.2.10")
    assert cdp._candidate_bases() == ["http://192.0.2.10:9223"], "显式远端主机不应回退到本机别名"
    assert cdp._candidate_bases(url="http://127.0.0.1:1/") == ["http://127.0.0.1:1"]


def test_gitignore_excludes_local_scratch_artifacts() -> None:
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for needle in (".smoke-agent-ui/", "tests/smoke/agent_ui/.workdir/"):
        assert needle in text, f".gitignore 缺少 {needle}"
