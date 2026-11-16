"""CI 质量门禁的配置守护（plan-20261103-012）。

这些断言不测业务逻辑，只钉住「门禁本身」：一旦有人把 ruff/mypy 降回子集选择、
把覆盖率门槛调低、或把 CI 改回裸装最新版工具，这里会红。

为什么用测试而不是靠肉眼审 CI：CI 配置天然容易被顺手改坏，而改坏之后
流水线照样是绿的（门槛被删掉也绿），只有配置级断言能拦住这种退化。
"""

from __future__ import annotations

import configparser
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
RUFF_PATH = ROOT / "ruff.toml"
COVERAGE_PATH = ROOT / ".coveragerc"
MYPY_PATH = ROOT / "mypy.ini"

# 实测基线（plan-20261103-012）：CI Linux 76.47%（门禁权威值）、本地 Windows 76.70%。
# 门槛等于 CI 实测显示值 76.47（评审 iss-mwjmi7bw-9b664c：常量若低于实测值，
# 门槛可被静默下调 0.47pp 而用例仍绿）。调整门槛必须同步改本断言并过评审。
MIN_COVERAGE_FLOOR = 76.47

# mypy 只排除 UI / 图表模块；这个白名单是「覆盖全部非 UI 模块」的唯一定义处。
MYPY_UI_WHITELIST = {
    f"astock/{name}.py"
    for name in (
        "ui",
        "charts",
        "backtest_chart",
        "backtest_ui",
        "optimize_ui",
        "factor_ui",
        "strategy_ui",
        "track_ui",
        "recommend_ui",
    )
}
# astock 下共 44 个 .py，排除上述 9 个 → 35 个受检 + app.py = CI 日志里的 36 files。
EXPECTED_CHECKED_MODULES = 35


def _ci() -> dict[str, Any]:
    return yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))


def _triggers(ci: dict[str, Any]) -> dict[str, Any]:
    # PyYAML 把裸 `on:` 解析成布尔 True（YAML 1.1 行为），两种写法都要认。
    triggers = ci.get("on", ci.get(True))
    assert isinstance(triggers, dict), f"ci.yml 缺少 on 触发器：{ci.keys()}"
    return triggers


def _run_commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _test_job() -> dict[str, Any]:
    job = _ci()["jobs"]["test"]
    assert isinstance(job, dict)
    return job


def test_ci_runs_on_pull_request_and_main_push() -> None:
    """验收要求 PR 与 main 都跑 pytest + mypy + ruff，缺一个就等于半盲。"""
    triggers = _triggers(_ci())

    push_branches = (triggers.get("push") or {}).get("branches") or []
    pr_branches = (triggers.get("pull_request") or {}).get("branches") or []
    assert "main" in push_branches, f"push 未覆盖 main：{triggers}"
    assert "main" in pr_branches, f"pull_request 未覆盖 main：{triggers}"


def test_ci_installs_pinned_dev_requirements_once() -> None:
    """工具链只能来自 requirements-dev.txt，禁止裸装 ruff/mypy（否则版本漂移）。"""
    commands = "\n".join(_run_commands(_test_job()))

    assert "pip install -r requirements-dev.txt" in commands
    assert "pip check" in commands
    bare_tool_install = [
        line
        for line in commands.splitlines()
        if "pip install" in line and ("ruff" in line or "mypy" in line) and "requirements" not in line
    ]
    assert not bare_tool_install, f"CI 仍在裸装工具：{bare_tool_install}"


def test_ci_runs_full_repo_ruff_as_blocking_gate() -> None:
    """验收命令就是 `ruff check astock tests`：不许再传 --select 子集。"""
    commands = "\n".join(_run_commands(_test_job()))

    assert "ruff check astock tests" in commands
    assert "--select" not in commands, "CI 不应再用 --select 收窄规则集（规则集固定在 ruff.toml）"


def test_ci_runs_mypy_over_package_entrypoint() -> None:
    commands = "\n".join(_run_commands(_test_job()))
    assert "mypy astock app.py" in commands


def test_ci_runs_pytest_with_coverage_gate() -> None:
    """pytest 与 coverage 都要跑，且门槛只在 .coveragerc 里定义一次。"""
    commands = "\n".join(_run_commands(_test_job()))

    assert 'coverage run -m pytest tests -q -m "not png"' in commands
    assert "coverage report" in commands
    assert "--fail-under" not in commands, "门槛重复写在 CI 与 .coveragerc 会互相打架"


def test_ci_and_coverage_document_phased_targets() -> None:
    """验收要求分阶段提升目标写进 CI 注释，读者不看 PR 也知道下一步做什么。"""
    ci_text = CI_PATH.read_text(encoding="utf-8")
    coverage_text = COVERAGE_PATH.read_text(encoding="utf-8")

    for text, label in ((ci_text, "ci.yml"), (coverage_text, ".coveragerc")):
        assert "80%" in text, f"{label} 缺少 P2 目标 80%"
        assert "85%" in text, f"{label} 缺少 P3 目标 85%"


def test_coverage_threshold_not_below_measured_baseline() -> None:
    """门槛按实测重设：旧值 50% 形同虚设，新值不得低于实测覆盖率。"""
    parser = configparser.ConfigParser()
    parser.read_string(COVERAGE_PATH.read_text(encoding="utf-8"))
    report = parser["report"]

    fail_under = float(report["fail_under"])
    assert fail_under >= MIN_COVERAGE_FLOOR, f"覆盖率门槛被下调到实测值以下：{fail_under}"
    assert int(report.get("precision", "0")) >= 1, "precision 至少要能看出小数点，否则 76.9% 会显示成 77%"


def _ruff_config() -> dict[str, Any]:
    return tomllib.loads(RUFF_PATH.read_text(encoding="utf-8"))


def test_ruff_config_uses_explicit_rule_selection() -> None:
    """验收要求显式 select/ignore，不依赖 Ruff 默认规则集（默认集会随版本变）。"""
    lint = _ruff_config()["lint"]

    assert lint.get("select"), "ruff.toml 必须显式 select 规则族"
    assert "BLE" in lint["select"], "盲捕异常审计要求 BLE 系列开启"
    assert "S110" in lint["select"] and "S112" in lint["select"], "静默异常审计要求 S110/S112 开启"
    assert "ISC" in lint["select"], "隐式字符串拼接要求 ISC 开启"
    assert "C4" in lint["select"], "C408（dict() 调用）要求 C4 开启"


def test_ruff_config_documents_ignored_families() -> None:
    """被忽略的规则必须写明理由，否则下一次升级会有人悄悄放开。"""
    ignore = _ruff_config()["lint"]["ignore"]

    assert {"RUF001", "RUF002", "RUF003"} <= set(ignore), "中文全角标点噪声应显式忽略"
    assert "B905" in ignore


def test_ruff_config_keeps_scheduler_observation_window_exemption() -> None:
    """PR #9 五日观察（至 2026-11-21）期间不得动调度运行时代码。"""
    per_file = _ruff_config()["lint"]["per-file-ignores"]

    for path in ("astock/scheduler.py", "astock/jobs.py"):
        assert path in per_file, f"{path} 的观察期豁免丢失，等于提前改动调度样本"
        assert {"BLE001", "S110", "S112"} <= set(per_file[path])

    # scheduler.py 保持 92f5e47 原文（历史 noqa + try/except pass 未清理），
    # 因此还豁免 RUF100 / SIM105；观察期结束必须连同这一段一起删除。
    scheduler_exempt = set(per_file["astock/scheduler.py"])
    assert {"RUF100", "SIM105"} <= scheduler_exempt, "观察期需保留 scheduler.py 原文，缺失 RUF100/SIM105 豁免会逼人改代码"

    # 14 处 BLE001 的审计结论必须可查（评审 iss-mwjmhnq7-9a1c07 的判定条件之一）。
    ruff_text = RUFF_PATH.read_text(encoding="utf-8")
    assert "BLE001 审计结论" in ruff_text, "ruff.toml 应集中记录 scheduler.py 的 BLE001 审计理由"
    markers = (":165", ":360", ":422", ":427", ":432", ":437", ":522", ":536", ":556", ":614", ":618", ":626", ":640", ":669")
    for marker in markers:
        assert marker in ruff_text, f"ruff.toml 审计结论缺少 {marker} 站点"


def test_mypy_exclude_matches_frozen_ui_whitelist() -> None:
    """mypy 的 exclude 只能覆盖 9 个 UI/图表模块，新增非 UI 模块不得被静默排除。

    评审 iss-mwjmi7cn-50b1b4：此前只断言 ``mypy astock app.py`` 命令字符串存在，
    把 astock/foo.py 加进 exclude 正则后 mypy 仍 Success，覆盖率会被悄悄破坏。
    """
    parser = configparser.ConfigParser()
    parser.read_string(MYPY_PATH.read_text(encoding="utf-8"))
    pattern = parser["mypy"]["exclude"]
    regex = re.compile(pattern)

    # mypy 递归检查 astock 包（含 astock/agent/*.py），exclude 正则只作用于顶层模块：
    # 44 个 .py - 9 个 UI 白名单 = 35 个受检模块，加 app.py 即 CI 日志的 36 files。
    package_files = {path.relative_to(ROOT).as_posix() for path in (ROOT / "astock").rglob("*.py")}
    excluded = {path for path in package_files if regex.fullmatch(path)}

    assert excluded == MYPY_UI_WHITELIST, (
        f"mypy exclude 白名单被改动：多排了 {excluded - MYPY_UI_WHITELIST}，"
        f"少排了 {MYPY_UI_WHITELIST - excluded}"
    )
    for path in MYPY_UI_WHITELIST:
        assert (ROOT / path).exists(), f"白名单里的 {path} 不存在，可能是改名后忘记同步"
    checked = package_files - excluded
    assert len(checked) == EXPECTED_CHECKED_MODULES, (
        f"受检非 UI 模块数变为 {len(checked)}（期望 {EXPECTED_CHECKED_MODULES}）：新增模块属正常演进，"
        "请确认它确实被 mypy 检查后同步修改本常量"
    )


def test_ruff_config_excludes_legacy_root_script() -> None:
    """app.py 是历史原型脚本，不在验收命令范围内，必须显式排除而不是默默漏检。"""
    assert "app.py" in _ruff_config().get("exclude", []), "app.py 应显式 exclude 并注明 P3 排期"
