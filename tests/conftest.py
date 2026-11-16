"""全局夹具：PNG 探测缓存是进程级状态，测试之间必须隔离。"""

from __future__ import annotations

import pytest

from astock.agent import chart_render


@pytest.fixture(autouse=True)
def _reset_png_probe_cache():
    """``png_mode="auto"`` 会缓存「kaleido/Chrome 不可用」，避免上一个用例污染下一个。"""
    chart_render.reset_png_probe_cache()
    yield
    chart_render.reset_png_probe_cache()
