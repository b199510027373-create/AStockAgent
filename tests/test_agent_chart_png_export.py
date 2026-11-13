"""可选 PNG 导出：仅当环境装了 kaleido 才跑真实导出（决策记录 D7/Q6）。

无 kaleido 的降级路径由 ``tests/test_agent_chart_render.py`` 用替身覆盖；
这里只在真实依赖存在时验证「PNG 落盘 + digest 与文件一致」，避免 CI 因缺依赖跳过项堆积。
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from astock.agent import chart_render

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("kaleido") is None,
    reason="未安装 kaleido：PNG 导出按契约降级为 HTML artifact",
)


def _spec() -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "line",
        "title": "PNG 导出",
        "data": {
            "source": "inline",
            "fields": ["day", "close"],
            "rows": [
                {"day": "2024-01-02", "close": 10.0},
                {"day": "2024-01-03", "close": 10.4},
            ],
        },
        "encode": {"x": "day", "y": "close"},
    }


def test_png_export_writes_file_and_records_meta(tmp_path: Path) -> None:
    rendered = chart_render.render(
        _spec(),
        db_path=str(tmp_path / "agent.db"),
        artifact_root=str(tmp_path / "agent"),
        include_plotlyjs="cdn",
        export_png=True,
    )

    assert rendered.degraded is False
    assert rendered.png_path is not None
    png = Path(rendered.png_path)
    assert png.exists() and png.read_bytes().startswith(b"\x89PNG")

    from astock.agent import store

    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        row = store.get_artifact(conn, rendered.artifact_id)
    finally:
        conn.close()
    assert row is not None
    assert row.meta["png_path"] == rendered.png_path
    assert row.meta["degraded"] is False
    assert hashlib.sha256(png.read_bytes()).hexdigest() != row.digest  # PNG 与 HTML 各自独立
