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

# 实测基线（plan-20261117-001 复核，2026-11-27）：CI Linux 12474/2425 = 80.56%
# （run 46047574586，门禁权威值）、本地 Windows 12474/2395 = 80.80%（953 passed）。
# 旧基线 76.45%（run 43935079016，plan-20261103-012）已被取代：观察期结束后
# scheduler.py 恢复正常实现，覆盖率随存量清理与新增测试升到 80.56%。
# 门槛等于 CI 实测显示值 80.56（评审 iss-mwjmi7bw-9b664c：常量若低于实测值，
# 门槛可被静默下调而用例仍绿）。调整门槛必须同步改本断言并过评审。
MIN_COVERAGE_FLOOR = 80.56

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
# astock 下共 53 个 .py，排除上述 9 个 → 44 个受检 + app.py = CI 日志里的 45 files。
# plan-20261107-006 新增 astock/agent/tools/market_tools.py（非 UI，必须受 mypy 检查）。
# plan-20261107-007 新增 astock/agent/chat.py 与 astock/agent_chat_ui.py：都不在 UI
# 白名单里，因此同样受 mypy 检查（UI 白名单只豁免历史 Gradio 模块）。
# plan-20261114-002 新增 astock/agent/chart_service.py（QL5 唯一审计入口，非 UI，受检）。
# plan-20261107-009 新增 astock/agent/evals.py（离线评测 harness，非 UI，受检）。
EXPECTED_CHECKED_MODULES = 44


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


DOCS_WITH_BASELINE = (
    ROOT / "docs" / "AStockAgent_CI_work_calendar.md",
    ROOT / "docs" / "AStockAgent_development_status_2026-11-08.md",
    ROOT / "docs" / "AStockAgent_architecture_v1.3.md",
    ROOT / "docs" / "AStockAgent_product_snapshot_v1.3.md",
    ROOT / "README.md",
)


def _coverage_comment_baselines() -> tuple[tuple[int, int, float], tuple[int, int, float], int, int]:
    """从 .coveragerc 注释里取出本地/CI 实测与两边用例数（唯一数据源）。"""
    text = COVERAGE_PATH.read_text(encoding="utf-8")
    local = re.search(r"本地 Windows[^：]*：(\d+) / (\d+) = ([\d.]+)%", text)
    ci = re.search(r"本 PR CI Linux[^：]*：(\d+) stmts / (\d+) miss = ([\d.]+)%", text)
    passed = re.search(r"本地 Windows[^：]*?(\d+) passed", text)
    ci_passed = re.search(r"本 PR CI Linux[^：]*：[^\n]*?(\d+) passed", text)
    assert local and ci and passed and ci_passed, "无法从 .coveragerc 注释解析实测基线"
    local_baseline = (int(local.group(1)), int(local.group(2)), float(local.group(3)))
    ci_baseline = (int(ci.group(1)), int(ci.group(2)), float(ci.group(3)))
    return local_baseline, ci_baseline, int(passed.group(1)), int(ci_passed.group(1))


def _mypy_files() -> int:
    """CI 日志里 mypy 的受检文件数：受检模块 + app.py。"""
    return EXPECTED_CHECKED_MODULES + 1


def test_coveragerc_baseline_arithmetic_is_self_consistent() -> None:
    """语句数、未覆盖数与百分比必须自洽：9247/2155 配 76.68% 这类拼接错误要能变红。"""
    local, ci, _, _ = _coverage_comment_baselines()

    for stmts, miss, pct in (local, ci):
        computed = round((stmts - miss) / stmts * 100, 2)
        assert computed == pct, f"{stmts}/{miss} 算出来是 {computed}%，.coveragerc 注释写的是 {pct}%"


# 旧基线残留标记必须是「带上下文」的完整短语：裸 "50%" 会把 README 里正常的
# 「单票仓位上限 50%」误判成覆盖率门槛残留（评审 iss-mwjo3fur-2f386a）。
STALE_MARKERS = (
    "9247 statements",
    "2155 miss",
    "9247/2155",
    "606 passed",
    "606 个",
    "覆盖率门槛 50%",
)


def _stale_markers_found(text: str) -> list[str]:
    return [marker for marker in STALE_MARKERS if marker in text]


def _doc_baseline_problems(texts: dict[str, str]) -> list[str]:
    """5 个文档各自必须正向写出的实测基线；返回缺失项（空列表表示全部命中）。

    以 .coveragerc 注释为唯一数据源：文档里的语句数/未覆盖数/百分比/用例数/门槛
    一旦被改成别的值或漏改，对应片段就匹配不上（评审 iss-mwjo3ftq-d5ea0b）。
    """
    local, ci, local_passed, ci_passed = _coverage_comment_baselines()
    local_stmts, local_miss, local_pct = local
    ci_stmts, ci_miss, ci_pct = ci
    floor = MIN_COVERAGE_FLOOR
    mypy_files = _mypy_files()

    required = {
        "AStockAgent_development_status_2026-11-08.md": (
            f"{local_stmts} statements / {local_miss} miss = {local_pct:.2f}%",
            f"{ci_stmts}/{ci_miss} = {ci_pct:.2f}%",
            f"覆盖率门槛 {floor:.2f}%",
            f"{local_passed} passed",
            f"{mypy_files} source files",
        ),
        "AStockAgent_CI_work_calendar.md": (
            f"{ci_stmts}/{ci_miss}",
            f"{local_stmts}/{local_miss}",
            f"{ci_pct:.2f}%",
            f"{local_pct:.2f}%",
            f"{local_passed} passed",
            f"{ci_passed} passed",
            f"{mypy_files} source files",
        ),
        "AStockAgent_architecture_v1.3.md": (
            f"覆盖率门槛 {floor:.2f}%",
            f"本地 {local_pct:.2f}%",
            f"{mypy_files} 个源文件",
        ),
        "AStockAgent_product_snapshot_v1.3.md": (
            f"本地覆盖率 {local_pct:.2f}%",
            f"CI Linux {ci_pct:.2f}%",
            f"门槛 {floor:.2f}%",
            f"{local_passed} passed",
        ),
        "README.md": (f"{local_passed} passed",),
    }

    problems: list[str] = []
    for name, snippets in required.items():
        text = texts.get(name)
        if text is None:
            problems.append(f"缺少文档 {name}")
            continue
        problems.extend(f"{name} 缺少基线表述 {snippet!r}" for snippet in snippets if snippet not in text)
    return problems


def test_docs_baseline_matches_measured_numbers() -> None:
    """5 个文档的基线数字必须等于 .coveragerc 实测，且不得残留旧的错误表述。

    此前只对 3 个文件做正向断言，architecture_v1.3:107 与 product_snapshot:51
    改错数字仍绿（评审 iss-mwjo3ftq-d5ea0b）；裸 "50%" 标记会把 README 里正常的
    业务百分比误判成旧基线（评审 iss-mwjo3fur-2f386a）。
    """
    texts = {path.name: path.read_text(encoding="utf-8") for path in DOCS_WITH_BASELINE}

    for name, text in texts.items():
        found = _stale_markers_found(text)
        assert not found, f"{name} 仍残留旧基线表述 {found}"

    problems = _doc_baseline_problems(texts)
    assert not problems, "文档基线数字与 .coveragerc 实测不一致：" + "；".join(problems)


def test_stale_markers_require_context_around_percentage() -> None:
    """「50%」只有带上下文时才算旧基线：仓位上限、涨跌幅这类业务百分比不能误报。"""
    assert _stale_markers_found("单票仓位上限 50%") == []
    assert _stale_markers_found("涨跌幅限制 150%，单票上限 20%") == []
    assert _stale_markers_found("旧门槛：覆盖率门槛 50%") == ["覆盖率门槛 50%"]


def test_docs_baseline_guard_goes_red_on_wrong_numbers() -> None:
    """守护测试自证：改错 5 个文档里任一基线数字都必须让校验报错。

    「真实文档当前是绿的」不足以证明守护有效，评审要求的正是「改错必红」，
    这里用变异副本逐一验证（含 architecture_v1.3:107 与 product_snapshot:51）。
    """
    texts = {path.name: path.read_text(encoding="utf-8") for path in DOCS_WITH_BASELINE}
    assert _doc_baseline_problems(texts) == [], "真实文档当前已不一致，应先修文档"

    local, ci, local_passed, ci_passed = _coverage_comment_baselines()
    local_pct = local[2]
    ci_stmts, ci_miss, _ = ci
    mypy_files = _mypy_files()
    wrong_pct = f"{MIN_COVERAGE_FLOOR - 1:.2f}%"
    mutations = [
        ("AStockAgent_architecture_v1.3.md", f"{MIN_COVERAGE_FLOOR:.2f}%", wrong_pct),
        ("AStockAgent_architecture_v1.3.md", f"{mypy_files} 个源文件", f"{mypy_files + 1} 个源文件"),
        (
            "AStockAgent_product_snapshot_v1.3.md",
            f"本地覆盖率 {local_pct:.2f}%",
            f"本地覆盖率 {wrong_pct}",
        ),
        ("AStockAgent_development_status_2026-11-08.md", f"{local_passed} passed", f"{local_passed + 1} passed"),
        ("AStockAgent_development_status_2026-11-08.md", f"{mypy_files} source files", f"{mypy_files + 1} source files"),
        ("AStockAgent_CI_work_calendar.md", f"{ci_stmts}/{ci_miss}", f"{ci_stmts + 1}/{ci_miss}"),
        ("AStockAgent_CI_work_calendar.md", f"{ci_passed} passed", f"{ci_passed - 1} passed"),
        ("README.md", f"{local_passed} passed", f"{local_passed + 1} passed"),
    ]
    for name, before, after in mutations:
        mutated = texts[name].replace(before, after)
        assert mutated != texts[name], f"{name} 里找不到 {before!r}，守护断言已失效"
        assert _doc_baseline_problems({**texts, name: mutated}), f"把 {name} 的 {before!r} 改错后守护仍绿"

    dirty = dict(texts)
    dirty["README.md"] = texts["README.md"] + "\n旧门槛：覆盖率门槛 50%\n"
    assert _stale_markers_found(dirty["README.md"]) == ["覆盖率门槛 50%"]


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

    # isort 分区必须显式固定：测试运行会在仓库根生成 gradio/ 目录，不钉死第三方归属时
    # 本地会把 `import gradio` 判成 first-party，与干净检出的 CI 结论分叉（首轮 CI 红灯即此）。
    isort = lint["isort"]
    assert isort["known-first-party"] == ["astock"]
    assert "gradio" in isort["known-third-party"], "gradio 必须显式钉为第三方模块"


def test_ruff_config_documents_ignored_families() -> None:
    """被忽略的规则必须写明理由，否则下一次升级会有人悄悄放开。"""
    ignore = _ruff_config()["lint"]["ignore"]

    assert {"RUF001", "RUF002", "RUF003"} <= set(ignore), "中文全角标点噪声应显式忽略"
    assert "B905" in ignore


def test_ruff_config_has_no_per_file_exemptions() -> None:
    """观察期结束（2026-11-21）后不得再用 per-file-ignores 豁免整份文件。

    plan-20261117-001：scheduler.py / jobs.py 的存量已按规则清理完，新增问题只能改代码；
    文件级豁免一旦回潮，等于把「全仓 ruff 0 条」悄悄退回基线豁免。
    """
    lint = _ruff_config()["lint"]

    per_file = lint.get("per-file-ignores") or {}
    assert per_file == {}, f"ruff.toml 又出现文件级豁免：{per_file}"


def test_blind_except_exemptions_carry_reasons() -> None:
    """astock 全量：BLE001 只能留在真正的边界兜底处，且每处 noqa 必须写明理由。

    第 3 轮评审要求「逐条审计」：能用具体异常收窄的一律收窄（本项已把 8 处
    Exception 收窄为 ValueError/OSError/TypeError 等），保留的 noqa 必须自带理由，
    否则下一次没人知道它为什么不能收窄。评审 iss-mwzbx2no-cbaa41：ruff.toml 的
    措辞是全仓规则，守护范围也必须覆盖 astock/**/*.py，不能只盯 scheduler/jobs。
    """
    pattern = re.compile(r"#\s*noqa:\s*(?P<codes>[A-Z0-9]+(?:\s*,\s*[A-Z0-9]+)*)(?P<rest>.*)$")
    missing: list[str] = []
    for path in sorted((ROOT / "astock").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "noqa" not in line:
                continue
            match = pattern.search(line)
            assert match, f"{relative}:{lineno} 的 noqa 写法无法解析：{line.strip()}"
            codes = {code.strip() for code in match.group("codes").split(",")}
            reason = match.group("rest").strip()
            if "BLE001" in codes and not (reason.startswith("-") and len(reason) > 1):
                missing.append(f"{relative}:{lineno}")
    assert not missing, f"以下 BLE001 豁免缺少理由：{missing}"


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
    # 53 个 .py - 9 个 UI 白名单 = 44 个受检模块，加 app.py 即 CI 日志的 45 files。
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
