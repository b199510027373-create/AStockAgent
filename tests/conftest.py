"""全局夹具：PNG 探测缓存是进程级状态，测试之间必须隔离。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from astock.agent import chart_render

# 本机系统 TEMP 可能不可写，Python 会退回到当前工作目录，导致 astock-chat-* /
# astock-files-* / pytest-of-* 落在仓库根（plan-20261103-013）。统一改到已忽略的
# .tmp-tests/，既保留隔离，又不污染 git status。
_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp-tests"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_TMP_ROOT)


@pytest.fixture(autouse=True)
def _reset_png_probe_cache():
    """``png_mode="auto"`` 会缓存「kaleido/Chrome 不可用」，避免上一个用例污染下一个。"""
    chart_render.reset_png_probe_cache()
    yield
    chart_render.reset_png_probe_cache()
