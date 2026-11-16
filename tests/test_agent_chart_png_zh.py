"""中文 PNG 导出：标题/坐标轴/图例不得出现方块（ADR-016 Q6=A，评审 P1）。

CI 侧另有 ``fc-list :lang=zh`` 自检；这里的像素检查只做轻量复核——统计前景像素与
标题带笔画，防止「字体栈没生效、整张图只剩空白/方块」。像素级 golden 会随 Chrome
版本抗锯齿漂移，故不采用。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from astock.agent import chart_render

from PIL import Image  # noqa: E402  Pillow 随 pdfplumber 安装；缺失时 import 失败即暴露环境问题

#: CI 的 png-export job 设 ``ASTOCK_REQUIRE_CJK=1``：缺字体直接失败而不是 skip。
CJK_ENFORCED = os.getenv("ASTOCK_REQUIRE_CJK") == "1"


def _cjk_font_installed() -> bool:
    if platform.system() == "Windows":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        return any(
            (fonts / name).exists()
            for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc")
        )
    fc_list = shutil.which("fc-list")
    if not fc_list:
        return False
    try:
        result = subprocess.run(
            [fc_list, ":lang=zh"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


@pytest.fixture(autouse=True)
def _requires_cjk_font() -> None:
    if _cjk_font_installed():
        return
    message = "未安装中文字体：Linux 装 fonts-noto-cjk，Windows 需微软雅黑/黑体"
    if CJK_ENFORCED:
        pytest.fail(message)
    pytest.skip(message)


def _spec() -> dict[str, Any]:
    rows = [
        {
            "day": f"2024-01-{day:02d}",
            "open": 1680.0 + day,
            "high": 1700.0 + day,
            "low": 1660.0 + day,
            "close": 1690.0 + day,
            "vol": 10_000.0 + day * 100,
        }
        for day in range(2, 22)
    ]
    return {
        "version": 1,
        "kind": "candlestick",
        "title": "贵州茅台（600519.SH）日 K 线 · 前复权",
        "data": {
            "source": "inline",
            "fields": ["day", "open", "high", "low", "close", "vol"],
            "rows": rows,
        },
        "encode": {"x": "day"},
        "layers": [{"type": "volume"}],
    }


def test_chinese_candlestick_png_renders_glyphs(tmp_path: Path) -> None:
    rendered = chart_render.render(
        _spec(),
        db_path=str(tmp_path / "agent.db"),
        artifact_root=str(tmp_path / "agent"),
        include_plotlyjs="cdn",
        png_mode="require",
    )

    assert rendered.degraded is False
    png = Path(rendered.png_path or "")
    payload = png.read_bytes()
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")

    sample_dir = os.getenv("ASTOCK_PNG_SAMPLE_DIR")
    if sample_dir:  # CI 上传 artifact：把中文样张拷到稳定目录
        target = Path(sample_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "chinese_candlestick.png").write_bytes(payload)

    assert rendered.params["font_family"].startswith("Noto Sans CJK SC")
    assert rendered.params["kaleido_version"].startswith("1.")

    image = Image.open(png).convert("RGB")
    assert image.size == (960, 540)  # K 线 + 成交量两栏；宽度默认 960
    width, height = image.size

    colours = image.getcolors(maxcolors=1 << 24) or []
    assert len(colours) > 8, "只有纯色：标题/坐标轴/图例都没画出来"
    ink = sum(count for count, colour in colours if sum(colour) < 700)
    assert ink / (width * height) > 0.02, "前景像素过少：疑似字形全掉或整图空白"

    # 标题带（上边距 78px 内）必须有笔画；中文丢字形时标题区只剩空白/极少量噪声。
    title_band = image.crop((0, 0, width, 78))
    band_colours = title_band.getcolors(maxcolors=1 << 24) or []
    band_ink = sum(count for count, colour in band_colours if sum(colour) < 700)
    assert band_ink > 150, f"标题带笔画过少（{band_ink} px）：中文标题可能没渲染出来"


def test_chinese_font_family_is_recorded_in_meta(tmp_path: Path) -> None:
    """字体必须进 meta：换机器复现 PNG 时要知道用的是哪套 CJK 字体（评审 P1）。"""
    rendered = chart_render.render(
        _spec(),
        db_path=str(tmp_path / "agent.db"),
        artifact_root=str(tmp_path / "agent"),
        include_plotlyjs="cdn",
        png_mode="require",
    )
    assert chart_render.font_family() == rendered.params["font_family"]
    assert "SimHei" in rendered.params["font_family"]
    assert rendered.params["chrome_version"]
