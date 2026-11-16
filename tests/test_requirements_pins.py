"""依赖锁版本：运行时与开发依赖都必须锁精确版本（plan-20261103-012）。

两条独立诉求被同一份测试守住：

1. PNG 字节随 plotly/kaleido 版本漂移（评审 P1，plan-20261114-001）——
   导出器不许「装到哪个版本算哪个」，锁定值见 ``docs/AStockAgent_png_export_ops.md``。
2. CI 结论必须可归因（plan-20261103-012）——2026-11-16 的 main 因为裸装
   ``ruff mypy`` 跑出了 ruff 0.17.5 / coverage 7.17.3，与本地实测版本不一致。
   现在 CI 只从 ``requirements-dev.txt`` 安装，因此这一整份清单都要精确锁。

失败时的修法：把该行的版本改成实测通过全量测试的版本，别放宽成 ``>=``。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# name==1.2.3 / name==1.2 / name==1.2.3.4 —— 只接受精确锁，不接受 marker、extras、区间。
_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\d+(?:\.\d+)+$")


def _requirement_lines(filename: str) -> list[str]:
    lines = (ROOT / filename).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _runtime_requirements() -> list[str]:
    return _requirement_lines("requirements.txt")


def _dev_requirements() -> list[str]:
    # `-r requirements.txt` 是复用行，不是依赖声明。
    return [line for line in _requirement_lines("requirements-dev.txt") if not line.startswith("-r ")]


@pytest.mark.parametrize("name", ["plotly", "kaleido"])
def test_png_exporter_versions_are_pinned(name: str) -> None:
    matches = [line for line in _runtime_requirements() if line.startswith(name)]
    assert len(matches) == 1, f"{name} 必须且只能出现一次：{matches}"
    assert re.fullmatch(rf"{name}==\d+\.\d+\.\d+", matches[0]), f"{name} 必须锁精确版本：{matches[0]}"


@pytest.mark.parametrize("name", ["plotly", "kaleido"])
def test_png_exporter_versions_do_not_drift(name: str) -> None:
    """``>=`` / 无版本号都会让 PNG 在不同机器/时间产生不同像素，禁止。"""
    for line in _runtime_requirements():
        if line.startswith(name):
            assert ">=" not in line and "~=" not in line, line
            assert "==" in line, line


@pytest.mark.parametrize("filename", ["requirements.txt", "requirements-dev.txt"])
def test_every_requirement_line_is_exactly_pinned(filename: str) -> None:
    """整份清单逐行精确锁：任何一行放宽都会让 CI 装到漂移版本。"""
    lines = _dev_requirements() if filename == "requirements-dev.txt" else _runtime_requirements()
    assert lines, f"{filename} 不应为空"
    unpinned = [line for line in lines if not _PINNED.fullmatch(line)]
    assert not unpinned, f"{filename} 存在未精确锁定的依赖：{unpinned}"


@pytest.mark.parametrize("filename", ["requirements.txt", "requirements-dev.txt"])
@pytest.mark.parametrize("operator", [">=", "<=", "~=", "!=", ">", "<"])
def test_no_range_operators(filename: str, operator: str) -> None:
    lines = _dev_requirements() if filename == "requirements-dev.txt" else _runtime_requirements()
    offenders = [line for line in lines if operator in line]
    assert not offenders, f"{filename} 不应出现区间运算符 {operator}：{offenders}"


def test_dev_requirements_reuse_runtime_requirements() -> None:
    """开发环境必须包含运行时依赖，否则本地测试跑的不是 CI 那套运行时。"""
    lines = _requirement_lines("requirements-dev.txt")
    assert "-r requirements.txt" in lines


@pytest.mark.parametrize(
    "name",
    ["ruff", "mypy", "coverage", "pytest", "pytest-timeout"],
)
def test_dev_toolchain_is_pinned(name: str) -> None:
    """CI 的工具版本必须来自 requirements-dev.txt，不允许裸 pip install 最新版。"""
    matches = [line for line in _dev_requirements() if line.startswith(f"{name}==")]
    assert len(matches) == 1, f"{name} 必须且只能在 requirements-dev.txt 出现一次：{matches}"
    assert _PINNED.fullmatch(matches[0]), f"{name} 必须锁精确版本：{matches[0]}"


@pytest.mark.parametrize(
    "name",
    [
        "gradio",
        "pandas",
        "numpy",
        "plotly",
        "kaleido",
        "openai",
        "httpx",
        "requests",
        "python-dotenv",
        "pyarrow",
        "pdfplumber",
        "python-docx",
        "openpyxl",
        "xlrd",
        "duckdb",
        "tushare",
    ],
)
def test_runtime_dependency_present_and_pinned(name: str) -> None:
    """运行时直接依赖一个都不能漏：漏锁等于回到「装到哪个版本算哪个」。"""
    matches = [line for line in _runtime_requirements() if line.startswith(f"{name}==")]
    assert len(matches) == 1, f"{name} 必须以精确版本出现在 requirements.txt：{matches}"
