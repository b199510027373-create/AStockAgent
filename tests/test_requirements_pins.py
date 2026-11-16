"""依赖锁版本：PNG 字节随 plotly/kaleido 版本漂移，requirements 不得用无上限 ``>=``。

评审 P1（plan-20261114-001）：PNG 导出是 v1 支持功能，不能靠「装到哪个版本算哪个」；
锁定值见 ``requirements.txt`` 与 ``docs/AStockAgent_png_export_ops.md`` 的升级检查清单。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _requirements() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


@pytest.mark.parametrize("name", ["plotly", "kaleido"])
def test_png_exporter_versions_are_pinned(name: str) -> None:
    matches = [line for line in _requirements() if line.startswith(name)]
    assert len(matches) == 1, f"{name} 必须且只能出现一次：{matches}"
    assert re.fullmatch(rf"{name}==\d+\.\d+\.\d+", matches[0]), f"{name} 必须锁精确版本：{matches[0]}"


@pytest.mark.parametrize("name", ["plotly", "kaleido"])
def test_png_exporter_versions_do_not_drift(name: str) -> None:
    """``>=`` / 无版本号都会让 PNG 在不同机器/时间产生不同像素，禁止。"""
    for line in _requirements():
        if line.startswith(name):
            assert ">=" not in line and "~=" not in line, line
            assert "==" in line, line
