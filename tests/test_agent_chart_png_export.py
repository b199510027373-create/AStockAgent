"""真实 PNG 导出（非 monkeypatch）：Windows 开发机与 Linux CI 都要跑。

依赖锁定见 ``requirements.txt``（plotly==6.7.0 / kaleido==1.5.1）与
``docs/AStockAgent_png_export_ops.md``；CI 由独立 job ``png-export`` 执行本文件，
失败即红、超时由 ``--timeout=180`` 兜底（评审 P1）。
"""

from __future__ import annotations

import hashlib
import importlib.util
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

import pytest

from astock.agent import chart_render, store

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("kaleido") is None,
    reason="未安装 kaleido：PNG 导出按契约降级为 HTML artifact（CI 必须安装，见 requirements.txt）",
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


def _render(tmp_path: Path, **kwargs: Any):
    options: dict[str, Any] = {
        "db_path": str(tmp_path / "agent.db"),
        "artifact_root": str(tmp_path / "agent"),
        "include_plotlyjs": "cdn",
    }
    options.update(kwargs)
    return chart_render.render(_spec(), **options)


def _meta(tmp_path: Path, artifact_id: str) -> dict[str, Any]:
    conn = store.connect(str(tmp_path / "agent.db"))
    try:
        row = store.get_artifact(conn, artifact_id)
    finally:
        conn.close()
    assert row is not None
    return row.meta


def test_png_export_writes_file_and_records_meta(tmp_path: Path) -> None:
    rendered = _render(tmp_path, export_png=True)

    assert rendered.degraded is False, rendered.warnings
    assert rendered.png_path is not None
    png = Path(rendered.png_path)
    assert png.exists() and png.read_bytes().startswith(b"\x89PNG")

    row_meta = _meta(tmp_path, rendered.artifact_id)
    assert row_meta["png_path"] == rendered.png_path
    assert row_meta["degraded"] is False
    assert row_meta["png_digest"] == hashlib.sha256(png.read_bytes()).hexdigest()
    assert row_meta["png_digest"] != row_meta["div_id"]  # PNG 与 HTML 各自独立寻址


def test_png_meta_links_png_with_html_artifact(tmp_path: Path) -> None:
    """PNG 与 HTML 分属不同 digest 目录，meta 必须让两者可互查（评审 P1）。"""
    rendered = _render(tmp_path, png_mode="require")
    png = Path(rendered.png_path or "")
    meta = _meta(tmp_path, rendered.artifact_id)
    payload = png.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()

    assert meta["png_mode"] == "require"
    assert meta["degraded"] is False, rendered.warnings
    assert meta["png_digest"] == digest
    assert meta["png_rel_path"] and digest in meta["png_rel_path"]
    assert meta["png_bytes"] == png.stat().st_size == len(payload)
    assert (meta["png_width"], meta["png_scale"]) == (960, 1)
    assert meta["png_height"] > 0 and meta["png_elapsed_ms"] >= 0
    assert "Noto Sans CJK SC" in meta["font_family"]
    assert meta["kaleido_version"] == pkg_version("kaleido")
    assert meta["chrome_version"]
    # HTML artifact 行仍以 HTML digest 为主键；PNG 是独立内容寻址目录。
    assert meta["png_digest"] != rendered.digest


def test_png_mode_off_does_not_export(tmp_path: Path) -> None:
    rendered = _render(tmp_path, png_mode="off")

    assert rendered.png_path is None
    assert rendered.degraded is False
    meta = _meta(tmp_path, rendered.artifact_id)
    assert meta["png_mode"] == "off"
    assert meta["png_digest"] is None
    assert meta["png_error"] is None
