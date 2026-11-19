"""chart_render 渲染管线用例（ADR-016 §3、冻结清单 §3.4/§6）。

重点：六种 kind 能出图、图题角标同屏两个时点、K 线复用 indicators 的 MA/BOLL/成交量、
每次渲染落 ``agent_artifacts`` 且可按 ``artifact_id`` 回放、无 kaleido 时降级不抛异常、
渲染器不执行任意 Python。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from astock import data as market
from astock import indicators
from astock.agent import chart_render, chart_service, store
from astock.agent.chart_render import (
    FIGURE_DIV_ID,
    PRICE_DOWN,
    PRICE_UP,
    apply_transforms,
    embed_html,
    figure_tool_result,
    spec_from_artifact,
)
from astock.agent.chart_service import render_chart as render
from astock.agent.chart_spec import ChartSpecError

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 夹具


def ohlcv_frame(n: int = 80, *, code: str = "000001.SZ") -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=n)
    close = [10.0 + i * 0.05 + (i % 4) * 0.02 for i in range(n)]
    return pd.DataFrame(
        {
            "ts_code": code,
            "trade_date": dates,
            "open": [value - 0.05 for value in close],
            "high": [value + 0.12 for value in close],
            "low": [value - 0.12 for value in close],
            "close": close,
            "vol": [1_000.0 + i * 10 for i in range(n)],
            "amount": [10_000.0 + i * 50 for i in range(n)],
        }
    )


def minute_frame(n: int = 20) -> pd.DataFrame:
    stamps = pd.date_range("2024-01-02 09:35", periods=n, freq="5min")
    close = [10.0 + i * 0.01 for i in range(n)]
    return pd.DataFrame(
        {
            "ts_code": "000001.SZ",
            "trade_time": stamps,
            "open": close,
            "high": [value + 0.02 for value in close],
            "low": [value - 0.02 for value in close],
            "close": close,
            "vol": [100.0 + i for i in range(n)],
            "amount": [1_000.0 + i for i in range(n)],
        }
    )


def inline_line_spec(**overrides: object) -> dict:
    spec: dict = {
        "version": 1,
        "kind": "line",
        "title": "收盘走势",
        "data": {
            "source": "inline",
            "fields": ["day", "close"],
            "rows": [
                {"day": "2024-01-02", "close": 10.0},
                {"day": "2024-01-03", "close": 10.6},
                {"day": "2024-01-04", "close": 10.4},
            ],
        },
        "encode": {"x": "day", "y": "close"},
    }
    spec.update(overrides)
    return spec


def candlestick_spec(**overrides: object) -> dict:
    spec: dict = {
        "version": 1,
        "kind": "candlestick",
        "title": "平安银行 K 线",
        "data": {"source": "market", "ref": "000001.SZ"},
        "encode": {"x": "trade_date"},
    }
    spec.update(overrides)
    return spec


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, str]:
    return {
        "db_path": str(tmp_path / "agent.db"),
        "artifact_root": str(tmp_path / "agent"),
    }


def _render(spec: dict, paths: dict[str, str], **kwargs: object):
    options = {
        "include_plotlyjs": "cdn",
        "export_png": False,
        "db_path": paths["db_path"],
        "artifact_root": paths["artifact_root"],
    }
    options.update(kwargs)
    return render(spec, **options)


def _artifact_row(rendered, paths: dict[str, str]):
    conn = store.connect(paths["db_path"])
    try:
        return store.get_artifact(conn, rendered.artifact_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- 基本渲染与 artifact


def test_line_chart_renders_self_contained_html_and_records_artifact(paths: dict[str, str]) -> None:
    rendered = _render(inline_line_spec(), paths, include_plotlyjs="inline")

    # plotly 6.x 的 full_html 以 <html> 开头，plotly 7.x 前置 <!doctype html>，两者都是完整文档
    head = rendered.html.lstrip().lower()
    assert head.startswith(("<!doctype html>", "<html>"))
    assert "plotly.js" in rendered.html
    assert len(rendered.html) > 1_000_000  # plotly.js 内联 → 自包含
    assert rendered.data_as_of == "2024-01-04"
    assert rendered.data_time == "2024-01-04 15:00"
    assert rendered.path.startswith("artifacts/")
    assert rendered.artifact_id.startswith("art_")

    row = _artifact_row(rendered, paths)
    assert row is not None
    assert row.kind == "chart"
    assert row.path == rendered.path
    # 库里 digest 是裸 hex（content_digest 规范化后才带 sha256: 前缀）
    assert rendered.digest == f"sha256:{row.digest}"

    absolute = Path(paths["artifact_root"]) / rendered.path
    assert absolute.exists()
    assert hashlib.sha256(absolute.read_bytes()).hexdigest() == row.digest


def test_artifact_meta_records_spec_params_time_points_source_version(
    paths: dict[str, str],
) -> None:
    rendered = _render(inline_line_spec(), paths)
    row = _artifact_row(rendered, paths)
    meta = row.meta
    for key in (
        "schema_version",
        "spec",
        "params",
        "data_as_of",
        "data_time",
        "view_as_of",
        "view_time",
        "session",
        "source",
        "code_version",
        "data_digest",
        "data_blob",
        "data_rows",
        "data_truncated",
    ):
        assert key in meta, key
    assert meta["schema_version"] == chart_render.CHART_META_SCHEMA_VERSION
    assert "div_id" not in meta  # Q3：规范化 HTML 用固定 div id，meta 不再记 per-artifact id
    assert "time_point_scope" not in meta  # Q1：时点语义由 data_*/view_* 两对字段表达
    assert meta["data_as_of"] == "2024-01-04"
    assert meta["data_time"] == "2024-01-04 15:00"
    assert meta["source"] == "inline"
    assert meta["code_version"] == chart_render.CODE_VERSION
    assert meta["spec"]["kind"] == "line"
    assert meta["params"]["kind"] == "line"
    assert store.load_data_snapshot(meta, root=paths["artifact_root"])["rows"][
        0
    ]["day"].startswith("2024-01-02")
    # 复现前提：记录运行时版本，避免「同 spec 同数据却字节不同」无从追查。
    assert meta["params"]["plotly_version"]
    assert meta["params"]["python_version"]
    assert meta["params"]["code_version"] == rendered.code_version
    assert meta["params"]["include_plotlyjs"] == "cdn"
    # Q2：meta 只留 provenance——行数据落 data.json，大快照不进 meta_json。
    assert "data" not in meta
    assert "rows" not in meta
    assert len(store.canonical_json(meta)) < 8000


def test_rerender_upgrades_stale_degraded_meta(paths: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """先降级、后补齐 PNG：同一 artifact_id 的 meta 必须升级，不能停在旧降级态（评审 P1）。"""
    monkeypatch.setattr(
        chart_render,
        "_try_png",
        lambda figure, *, width, height: (None, "kaleido missing"),
    )
    first = _render(inline_line_spec(), paths, export_png=True)
    assert first.degraded is True
    assert _artifact_row(first, paths).meta["degraded"] is True

    monkeypatch.setattr(
        chart_render, "_try_png", lambda figure, *, width, height: (b"\x89PNG\r\n", None)
    )
    chart_render.reset_png_probe_cache()  # 上一轮环境失败已被 auto 缓存，清掉才能升级
    second = _render(inline_line_spec(), paths, export_png=True)
    assert second.artifact_id == first.artifact_id
    assert second.degraded is False

    row = _artifact_row(second, paths)
    assert row.meta["degraded"] is False
    assert row.meta["png_path"] == second.png_path
    # 版本追加：首版降级、二版补齐，旧版本不变（评审 P2-3）。
    assert first.artifact_version == 1
    assert second.artifact_version == 2
    assert second.metadata()["artifact_version"] == 2
    assert second.meta_digest == store.meta_digest(row.meta)
    assert second.metadata()["meta_digest"] == second.meta_digest
    conn = store.connect(paths["db_path"])
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
        versions = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_artifact_versions"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 1
    assert versions == 2


def test_render_meta_stores_snapshot_blob_instead_of_rows(paths: dict[str, str]) -> None:
    """真实 render 不把完整 rows 塞进 meta，回放走独立 blob（评审 P1-5）。"""
    rendered = _render(inline_line_spec(), paths)
    row = _artifact_row(rendered, paths)
    assert row is not None
    assert "data" not in row.meta
    assert row.meta["data_digest"].startswith("sha256:")
    assert row.meta["data_blob"].endswith("/data.json")
    assert row.meta["data_rows"] == 3
    assert row.meta["data_truncated"] is False

    conn = store.connect(paths["db_path"])
    try:
        loaded = store.load_artifact(
            conn,
            rendered.artifact_id,
            mode="recomputed",
            root=paths["artifact_root"],
        )
    finally:
        conn.close()
    assert loaded["data"]["row_count"] == 3
    assert [item["close"] for item in loaded["data"]["rows"]] == [10.0, 10.6, 10.4]


def test_env_change_renders_second_artifact_without_conflict(paths: dict[str, str]) -> None:
    """环境指纹变化 → 新 artifact，同内容同环境不重复建行（评审 P1-1）。"""
    first = _render(inline_line_spec(), paths, code_version="astock-env-a")
    second = _render(inline_line_spec(), paths, code_version="astock-env-b")
    assert first.artifact_id != second.artifact_id

    conn = store.connect(paths["db_path"])
    try:
        rows = conn.execute(
            "SELECT artifact_id, env_fingerprint FROM agent_artifacts ORDER BY artifact_id"
        ).fetchall()
        assert len(rows) == 2
        assert len({row["env_fingerprint"] for row in rows}) == 2
        for rendered in (first, second):
            loaded = store.load_artifact(conn, rendered.artifact_id, root=paths["artifact_root"])
            assert loaded["meta"]["code_version"] == rendered.code_version
    finally:
        conn.close()


def test_plotlyjs_mode_is_part_of_artifact_identity(paths: dict[str, str]) -> None:
    """同一 spec/data/env 下 inline 与 cdn 的 HTML 不同，必须生成两个 artifact（评审 r2 P1）。"""
    inline = _render(inline_line_spec(), paths, include_plotlyjs="inline")
    cdn = _render(inline_line_spec(), paths, include_plotlyjs="cdn")
    assert inline.artifact_id != cdn.artifact_id

    conn = store.connect(paths["db_path"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 2
        for rendered in (inline, cdn):
            loaded = store.load_artifact(
                conn, rendered.artifact_id, root=paths["artifact_root"]
            )
            assert b"plotly" in loaded["content"].lower()
    finally:
        conn.close()
    assert len(inline.html) > len(cdn.html)  # inline 内嵌 plotly.js，CDN 只留引用


def test_gc_keeps_data_blob_referenced_by_versions(paths: dict[str, str]) -> None:
    """GC 必须保护 meta.data_blob；无引用 blob 仍会被删（评审 r2 P1）。"""
    rendered = _render(inline_line_spec(), paths)
    row = _artifact_row(rendered, paths)
    assert row is not None
    data_blob = row.meta["data_blob"]
    base = Path(paths["artifact_root"])
    blob_path = base / data_blob
    assert blob_path.is_file()
    old = (datetime.now(UTC) - timedelta(days=100)).timestamp()
    os.utime(blob_path, (old, old))
    orphan = base / "artifacts" / "zz" / "orphan.html"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (old, old))

    conn = store.connect(paths["db_path"])
    try:
        result = store.prune_orphan_blobs(
            conn,
            older_than_days=90,
            root=paths["artifact_root"],
            dry_run=False,
        )
        assert blob_path.is_file()
        assert not orphan.exists()
        assert data_blob in store._protected_paths(conn, base)
        loaded = store.load_artifact(
            conn,
            rendered.artifact_id,
            mode="recomputed",
            root=paths["artifact_root"],
        )
        assert loaded["data"]["row_count"] == 3
    finally:
        conn.close()
    assert result["deleted"] >= 1


def test_transforms_keep_market_time_and_drop_view_time_when_rows_vanish(
    paths: dict[str, str],
) -> None:
    """Q1：行情时点在 transforms **之前**算，视图时点是 transforms 之后（规格 §3）。"""
    spec = inline_line_spec()
    spec["transforms"] = [{"op": "filter", "field": "close", "cmp": "gt", "value": 999}]
    rendered = _render(spec, paths)

    # 行情数据仍然是 01-02..01-04 这份数据；只是过滤后没有可见行。
    assert rendered.data_as_of == "2024-01-04"
    assert rendered.data_time == "2024-01-04 15:00"
    assert rendered.view_as_of == ""
    assert rendered.view_time == ""
    assert "行情 2024-01-04 15:00" in rendered.caption
    assert "视图时点未知" in rendered.caption
    meta = _artifact_row(rendered, paths).meta
    assert meta["data_as_of"] == "2024-01-04"
    assert meta["view_as_of"] == ""


def test_same_spec_renders_to_same_artifact_id_and_single_row(paths: dict[str, str]) -> None:
    first = _render(inline_line_spec(), paths)
    second = _render(inline_line_spec(), paths)
    assert first.artifact_id == second.artifact_id
    assert first.digest == second.digest

    conn = store.connect(paths["db_path"])
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
    finally:
        conn.close()
    assert count == 1


def test_artifact_can_replay_original_spec(paths: dict[str, str]) -> None:
    rendered = _render(inline_line_spec(), paths)
    row = _artifact_row(rendered, paths)
    snapshot_spec = spec_from_artifact(row.meta)
    assert snapshot_spec["kind"] == "line"

    replayed = _render(snapshot_spec, paths)
    assert replayed.data_as_of == rendered.data_as_of
    assert replayed.artifact_id == rendered.artifact_id

    artifact_source_spec = json.loads(json.dumps(snapshot_spec))
    artifact_source_spec["data"] = {"source": "artifact", "ref": rendered.artifact_id}
    from_artifact = _render(artifact_source_spec, paths)
    assert from_artifact.data_as_of == rendered.data_as_of
    assert from_artifact.data_time == rendered.data_time
    assert from_artifact.source == "artifact"


def test_artifact_source_rejects_unknown_id(paths: dict[str, str]) -> None:
    spec = inline_line_spec()
    spec["data"] = {"source": "artifact", "ref": "art_0123456789abcdef"}
    with pytest.raises(ChartSpecError) as info:
        _render(spec, paths)
    assert {issue.field for issue in info.value.issues} == {"$.data.ref"}


# ---------------------------------------------------------------- 六种 kind


def test_candlestick_supports_ma_boll_and_volume(paths: dict[str, str]) -> None:
    spec = candlestick_spec(
        layers=[{"type": "ma", "window": 20}, {"type": "boll"}, {"type": "volume"}],
    )
    rendered = _render(spec, paths, frame=ohlcv_frame())
    html = rendered.html
    assert "MA20" in html
    assert "BOLL 上轨" in html and "BOLL 下轨" in html
    assert "成交量" in html
    assert "行情 2024-" in html
    assert "前复权（qfq）" in html


def test_candlestick_indicator_panes(paths: dict[str, str]) -> None:
    spec = candlestick_spec(layers=[{"type": "macd"}, {"type": "rsi"}, {"type": "kdj"}])
    rendered = _render(spec, paths, frame=ohlcv_frame())
    for token in ("DIF", "DEA", "MACD 柱", "RSI", "K", "D", "J"):
        assert token in rendered.html, token


def test_bar_scatter_heatmap_histogram_render(paths: dict[str, str]) -> None:
    bar = inline_line_spec()
    bar["kind"] = "bar"
    bar["encode"] = {"x": "day", "y": ["close"]}
    assert "bar" in _render(bar, paths).html

    scatter = inline_line_spec()
    scatter["kind"] = "scatter"
    scatter["encode"] = {"x": "day", "y": "close", "color": "close"}
    assert "scatter" in _render(scatter, paths).html

    heatmap = inline_line_spec()
    heatmap["kind"] = "heatmap"
    heatmap["data"] = {
        "source": "inline",
        "fields": ["day", "sector", "excess"],
        "rows": [
            {"day": "2024-01-02", "sector": "银行", "excess": 1.2},
            {"day": "2024-01-02", "sector": "券商", "excess": -0.4},
            {"day": "2024-01-03", "sector": "银行", "excess": 0.6},
            {"day": "2024-01-03", "sector": "券商", "excess": 0.9},
        ],
    }
    heatmap["encode"] = {"x": "day", "y": "sector", "color": "excess"}
    assert "heatmap" in _render(heatmap, paths).html

    histogram = inline_line_spec()
    histogram["kind"] = "histogram"
    histogram["encode"] = {"x": "day", "y": "close"}
    assert "histogram" in _render(histogram, paths).html


def test_candlestick_without_ohlc_reports_structured_error(paths: dict[str, str]) -> None:
    spec = candlestick_spec()
    with pytest.raises(ChartSpecError) as info:
        _render(spec, paths, frame=pd.DataFrame({"trade_date": ["2024-01-02"], "close": [10.0]}))
    assert "$.kind" in {issue.field for issue in info.value.issues}

    conn = store.connect(paths["db_path"])
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
    finally:
        conn.close()
    assert count == 0


# ---------------------------------------------------------------- 时点 / 图注 / 降级


def test_minute_frame_uses_bar_end_time_as_data_time(paths: dict[str, str]) -> None:
    spec = candlestick_spec()
    spec["data"]["period"] = "5min"
    spec["encode"] = {"x": "trade_time"}
    rendered = _render(spec, paths, frame=minute_frame(1))
    assert rendered.data_as_of == "2024-01-02"
    assert rendered.data_time == "2024-01-02 09:35"
    assert "行情 2024-01-02 09:35" in rendered.html
    assert rendered.session == "continuous"


def test_auction_only_minute_frame_renders_with_pre_session_and_no_indicators(
    paths: dict[str, str],
) -> None:
    """Q7：09:25 集合竞价 bar 照常画 K 线、标 session=pre，连续指标为空且明确提示。"""
    auction = pd.DataFrame(
        {
            "ts_code": "000001.SZ",
            "trade_time": pd.to_datetime(["2024-01-02 09:25:00"]),
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.05],
            "vol": [500.0],
            "amount": [5_000.0],
        }
    )
    spec = candlestick_spec(layers=[{"type": "ma", "window": 5}, {"type": "volume"}])
    spec["data"]["period"] = "5min"
    spec["encode"] = {"x": "trade_time"}
    rendered = _render(spec, paths, frame=auction)

    assert rendered.session == "pre"
    assert rendered.data_time == "2024-01-02 09:25"
    assert "集合竞价" in rendered.caption
    assert "盘前，暂无连续竞价数据" in rendered.caption
    # Q7：连续指标只在连续竞价 bar 上算；集合竞价帧没有连续 bar。

    assert market.continuous_bars(auction, freq="5min").empty
    indicators = chart_render._indicator_frame(
        auction, spec["layers"], source_frame=market.continuous_bars(auction, freq="5min")
    )
    for series in indicators["panes"]:
        assert pd.Series(series).dropna().empty  # 没有连续 bar → 指标全空，不拿竞价 bar 充数
    for series in indicators["overlays"].values():
        assert pd.Series(series).dropna().empty
    assert indicators["indicator_frame"].empty
    assert "成交量" in rendered.html
    assert _artifact_row(rendered, paths).meta["session"] == "pre"


def test_missing_time_points_render_unknown_not_realtime(paths: dict[str, str]) -> None:
    rendered = _render(inline_line_spec(), paths, data_as_of="", data_time="")
    caption = rendered.caption
    assert "时点未知" in caption
    assert "实时" not in caption


def test_unadjusted_price_caliber_is_noted_in_caption(paths: dict[str, str]) -> None:
    spec = candlestick_spec()
    spec["data"]["adj"] = "none"
    rendered = _render(spec, paths, frame=ohlcv_frame())
    assert "不复权" in rendered.caption
    assert "前复权" not in rendered.caption


def test_degrades_to_html_when_kaleido_missing(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_png(figure, *, width: int, height: int):
        return None, 'Image export using the "kaleido" engine requires the Kaleido package'

    monkeypatch.setattr(chart_render, "_try_png", failing_png)
    rendered = _render(inline_line_spec(), paths, export_png=True)

    assert rendered.png_path is None
    assert rendered.degraded is True
    assert any("PNG 导出降级" in warning for warning in rendered.warnings)
    assert "PNG 导出降级" in rendered.summary_text()

    result = figure_tool_result(rendered)
    assert result.figure == rendered.html
    assert result.metadata["degraded"] is True
    assert result.metadata["artifact_id"] == rendered.artifact_id


def test_png_is_recorded_when_exporter_available(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chart_render, "_try_png", lambda figure, *, width, height: (b"png-bytes", None))
    rendered = _render(inline_line_spec(), paths, export_png=True)
    assert rendered.degraded is False
    assert rendered.png_path is not None
    assert Path(rendered.png_path).read_bytes() == b"png-bytes"


# ---------------------------------------------------------------- png_mode 三态（评审 P2）


def test_png_mode_require_raises_and_marks_run_failed(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式请求 PNG 失败必须报错：不静默降级、不落半成品 artifact 行。"""
    conn = store.connect(paths["db_path"])
    try:
        store.ensure_run(conn, "run_require", status="running")
    finally:
        conn.close()

    def failing(figure, *, width: int, height: int):
        return None, "kaleido not installed"

    monkeypatch.setattr(chart_render, "_try_png", failing)
    with pytest.raises(chart_render.PNGExportError, match="kaleido not installed"):
        _render(inline_line_spec(), paths, png_mode="require", run_id="run_require")

    conn = store.connect(paths["db_path"])
    try:
        run = conn.execute(
            "SELECT status FROM agent_runs WHERE run_id = ?", ("run_require",)
        ).fetchone()
        artifacts = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
    finally:
        conn.close()
    assert run is not None and run["status"] == "failed"
    assert artifacts == 0


def test_unknown_run_id_fails_fast_without_creating_run(paths: dict[str, str]) -> None:
    """chart_service 不替调用方造 run：run_id 不存在直接报错（评审 P1）。

    旧行为是 ``ensure_run(status="running")`` 自建 run，成功路径又没人 finish，
    会留下永久 running 的假审计记录。
    """
    with pytest.raises(ValueError, match="必须由 AgentRuntime 先 ensure_run"):
        _render(inline_line_spec(), paths, run_id="run_missing", step=3)

    conn = store.connect(paths["db_path"])
    try:
        runs = conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()["n"]
        artifacts = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
    finally:
        conn.close()
    assert runs == 0
    assert artifacts == 0


def test_successful_render_never_finishes_existing_run(paths: dict[str, str]) -> None:
    """成功渲染只挂 link，不改 run 状态：finish 归 runtime（评审 P1）。"""
    conn = store.connect(paths["db_path"])
    try:
        store.ensure_run(conn, "run_runtime", status="running", session_id="s1")
    finally:
        conn.close()

    rendered = _render(inline_line_spec(), paths, run_id="run_runtime", step=2)

    conn = store.connect(paths["db_path"])
    try:
        run = conn.execute(
            "SELECT status FROM agent_runs WHERE run_id = ?", ("run_runtime",)
        ).fetchone()
        link = conn.execute(
            "SELECT run_id, step, source FROM agent_run_artifacts WHERE artifact_id = ?",
            (rendered.artifact_id,),
        ).fetchone()
    finally:
        conn.close()
    assert run is not None and run["status"] == "running"
    assert link is not None
    assert (link["run_id"], link["step"], link["source"]) == ("run_runtime", 2, "agent")


def test_png_mode_require_fails_precreated_run(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent 循环预建的 running run 遇到 require 失败也要落 failed（评审 P2）。

    ``ensure_run`` 是 ``ON CONFLICT DO NOTHING``，只有显式 ``fail_run`` 才能把既有行改成
    failed；否则 ops 文档宣称的 ``agent_runs.status=failed`` 只对新建 run 成立。
    """
    conn = store.connect(paths["db_path"])
    try:
        store.ensure_run(conn, "run_precreated", status="running")
    finally:
        conn.close()

    monkeypatch.setattr(
        chart_render,
        "_try_png",
        lambda figure, *, width, height: (None, "Chrome executable not found"),
    )
    with pytest.raises(chart_render.PNGExportError, match="Chrome executable not found"):
        _render(inline_line_spec(), paths, png_mode="require", run_id="run_precreated")

    conn = store.connect(paths["db_path"])
    try:
        run = conn.execute(
            "SELECT status, error, finished_at FROM agent_runs WHERE run_id = ?",
            ("run_precreated",),
        ).fetchone()
        artifacts = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
    finally:
        conn.close()
    assert run is not None
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert "Chrome executable not found" in (run["error"] or "")
    assert artifacts == 0


def test_png_mode_auto_caches_env_failure_and_warns_once(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto 首次环境失败后不再逐图起 Chrome：只探测一次，后续标注缓存原因。"""
    calls: list[str] = []

    def failing(figure, *, width: int, height: int):
        calls.append("export")
        return None, "Chrome executable not found"

    monkeypatch.setattr(chart_render, "_try_png", failing)
    first = _render(inline_line_spec(), paths, png_mode="auto")
    second = _render(inline_line_spec(), paths, png_mode="auto")

    assert calls == ["export"]
    assert first.degraded is True and second.degraded is True
    assert any("跳过重试" in warning for warning in second.warnings)
    assert _artifact_row(second, paths).meta["png_error"] == "Chrome executable not found"


def test_png_mode_off_never_calls_exporter(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object):
        raise AssertionError("png_mode=off 不应尝试导出")

    monkeypatch.setattr(chart_render, "_try_png", unexpected)
    rendered = _render(inline_line_spec(), paths, png_mode="off")

    assert rendered.png_path is None
    assert rendered.degraded is False
    meta = _artifact_row(rendered, paths).meta
    assert meta["png_mode"] == "off" and meta["png_digest"] is None


def test_png_mode_rejects_unknown_value(paths: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="png_mode"):
        _render(inline_line_spec(), paths, png_mode="sometimes")


# ---------------------------------------------------------------- ToolResult 同屏


def test_tool_result_carries_both_time_points_and_table(paths: dict[str, str]) -> None:
    rendered = _render(inline_line_spec(), paths)
    table = [{"day": "2024-01-04", "close": 10.4}]
    result = figure_tool_result(rendered, table=table)
    assert result.data_as_of == "2024-01-04"
    assert result.data_time == "2024-01-04 15:00"
    assert result.text is not None and "2024-01-04 15:00" in result.text
    assert result.table == table
    assert result.figure == rendered.html
    assert result.metadata["artifact_path"] == rendered.path
    assert result.metadata["data_time"] == "2024-01-04 15:00"


# ---------------------------------------------------------------- transforms


def test_transforms_filter_sort_limit_on_inline_rows(paths: dict[str, str]) -> None:
    spec = inline_line_spec(
        transforms=[
            {"op": "filter", "field": "close", "cmp": "gt", "value": 10.0},
            {"op": "sort", "by": "close", "desc": True},
            {"op": "limit", "n": 1},
        ]
    )
    rendered = _render(spec, paths)
    row = _artifact_row(rendered, paths)
    rows = store.load_data_snapshot(row.meta, root=paths["artifact_root"])["rows"]
    assert len(rows) == 1
    assert rows[0]["close"] == pytest.approx(10.6)


def test_groupby_metric_column_naming() -> None:
    frame = pd.DataFrame(
        {
            "day": ["2024-01-02", "2024-01-02", "2024-01-03"],
            "amount": [1.0, 2.0, 3.0],
            "sector": ["银行", "银行", "券商"],
        }
    )
    grouped = apply_transforms(
        frame, [{"op": "groupby", "by": ["day"], "metrics": {"amount": "sum"}}]
    )
    assert list(grouped.columns) == ["day", "amount_sum"]
    assert grouped["amount_sum"].tolist() == [3.0, 3.0]


def test_filter_on_missing_column_is_structured_error() -> None:
    frame = pd.DataFrame({"day": ["2024-01-02"], "close": [1.0]})
    with pytest.raises(ChartSpecError) as info:
        apply_transforms(
            frame, [{"op": "filter", "field": "amount", "cmp": "gt", "value": 1}]
        )
    assert {issue.field for issue in info.value.issues} == {"$.transforms[0].field"}


# ---------------------------------------------------------------- 复权口径（评审 P0）


@pytest.mark.parametrize(
    ("adj", "expected"), [("qfq", "qfq"), ("hfq", "hfq"), ("none", None)]
)
def test_market_adj_is_mapped_to_data_layer(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch, adj: str, expected: str | None
) -> None:
    calls: dict[str, object] = {}

    def fake_load_daily(ts_code: str, **kwargs: object) -> pd.DataFrame:
        calls["ts_code"] = ts_code
        calls["adj"] = kwargs.get("adj")
        return ohlcv_frame()

    monkeypatch.setattr("astock.data.load_daily", fake_load_daily)
    spec = candlestick_spec()
    spec["data"]["adj"] = adj
    rendered = _render(spec, paths)

    assert calls["ts_code"] == "000001.SZ"
    # none 必须映射成 None，否则 data 层会当成前复权（评审 P0 根因）
    assert calls["adj"] is expected
    assert rendered.params["adj"] == adj


def test_adj_raw_is_rejected_before_touching_market_data(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> pd.DataFrame:
        raise AssertionError("非法 spec 不应进入取数阶段")

    monkeypatch.setattr("astock.data.load_daily", forbidden)
    spec = candlestick_spec()
    spec["data"]["adj"] = "raw"
    with pytest.raises(ChartSpecError) as info:
        _render(spec, paths)
    assert "$.data.adj" in {issue.field for issue in info.value.issues}


def test_minute_market_branch_maps_adj_too(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, object] = {}

    def fake_load_minute(ts_code: str, **kwargs: object) -> pd.DataFrame:
        calls["freq"] = kwargs.get("freq")
        calls["adj"] = kwargs.get("adj")
        return minute_frame(2)

    monkeypatch.setattr("astock.data.load_minute", fake_load_minute)
    spec = candlestick_spec()
    spec["data"].update({"period": "5min", "adj": "none", "range": "1mo"})
    spec["encode"] = {"x": "trade_time"}
    rendered = _render(spec, paths)

    assert calls["freq"] == "5min"
    assert calls["adj"] is None
    assert "不复权" in rendered.caption


def test_market_branch_unadjusted_caption_and_snapshot_agree(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = ohlcv_frame()
    monkeypatch.setattr("astock.data.load_daily", lambda ts_code, **kwargs: raw)

    spec = candlestick_spec()
    spec["data"]["adj"] = "none"
    rendered = _render(spec, paths)
    row = _artifact_row(rendered, paths)

    assert "不复权" in rendered.caption
    # 渲染图注、artifact meta 与快照价格同源（都是 data 层返回的原始价）
    assert rendered.source == "market"
    assert store.load_data_snapshot(
        row.meta, root=paths["artifact_root"]
    )["rows"][-1]["close"] == pytest.approx(raw["close"].iloc[-1])


# ---------------------------------------------------------------- 时点与快照一致性（评审 P1）


def test_time_point_follows_transformed_rows(paths: dict[str, str]) -> None:
    """Q1 验收：过滤掉最新一天后，行情时点仍是 01-04，视图时点是 01-03。"""
    spec = inline_line_spec(
        transforms=[
            {"op": "filter", "field": "close", "cmp": "gt", "value": 10.0},
            {"op": "sort", "by": "close", "desc": True},
            {"op": "limit", "n": 1},
        ]
    )
    rendered = _render(spec, paths)
    row = _artifact_row(rendered, paths)
    assert rendered.data_as_of == "2024-01-04"
    assert rendered.data_time == "2024-01-04 15:00"
    assert rendered.view_as_of == "2024-01-03"
    assert rendered.view_time == "2024-01-03 15:00"
    assert row.meta["data_as_of"] == "2024-01-04"
    assert row.meta["view_as_of"] == "2024-01-03"
    # 图注同时展示两个时点（ADR-016：同屏）
    assert "行情 2024-01-04 15:00" in rendered.caption
    assert "视图截止 2024-01-03" in rendered.caption
    # 快照是 transforms 之后的数据（视图），行情时点来自 meta
    assert store.load_data_snapshot(row.meta, root=paths["artifact_root"])["rows"][-1][
        "day"
    ] == "2024-01-03"


def test_groupby_drops_time_column_and_reports_unknown_view(paths: dict[str, str]) -> None:
    """Q1 验收：groupby 聚合掉时间列后视图时点未知，但行情时点不丢。"""
    spec = {
        "version": 1,
        "kind": "bar",
        "data": {
            "source": "inline",
            "fields": ["day", "sector", "excess"],
            "time_field": "day",
            "rows": [
                {"day": "2024-01-02", "sector": "银行", "excess": 1.2},
                {"day": "2024-01-02", "sector": "银行", "excess": 0.4},
                {"day": "2024-01-03", "sector": "券商", "excess": -0.2},
            ],
        },
        "encode": {"x": "sector", "y": "excess_mean"},
        "transforms": [
            {"op": "groupby", "by": ["sector"], "metrics": {"excess": "mean"}}
        ],
    }
    rendered = _render(spec, paths)
    assert rendered.data_as_of == "2024-01-03"
    assert rendered.view_as_of == ""
    assert rendered.view_unknown_reason == "aggregated"
    assert "行情 2024-01-03 15:00" in rendered.caption
    assert "视图时点未知（聚合后无时间列）" in rendered.caption


def test_explicit_time_params_only_override_their_own_group(paths: dict[str, str]) -> None:
    """Q1：显式 data_* 只覆盖行情时点，不影响从视图算出的 view_*。"""
    rendered = _render(
        inline_line_spec(transforms=[{"op": "limit", "n": 2}]),
        paths,
        data_as_of="2024-01-05",
        data_time="2024-01-05 15:00",
    )
    assert rendered.data_as_of == "2024-01-05"
    assert rendered.view_as_of == "2024-01-03"
    assert "行情 2024-01-05 15:00" in rendered.caption
    assert "视图截止 2024-01-03" in rendered.caption


def test_artifact_replay_trusts_recorded_market_time_and_recomputes_view(
    paths: dict[str, str],
) -> None:
    """§3.5：重算只重算 view_*，data_* 采信 meta（不得从裁剪快照反推）。"""
    rendered = _render(
        inline_line_spec(
            transforms=[
                {"op": "filter", "field": "close", "cmp": "gt", "value": 10.0},
                {"op": "sort", "by": "close", "desc": True},
                {"op": "limit", "n": 1},
            ]
        ),
        paths,
    )
    row = _artifact_row(rendered, paths)
    assert row is not None

    # v2 的 meta 不可变：修正只能追加版本 + corrections，历史版本不变。
    conn = store.connect(paths["db_path"])
    try:
        store.correct_artifact(
            conn,
            row.artifact_id,
            {"data_as_of": "2020-01-01", "data_time": "2020-01-01 15:00"},
            reason="test tamper",
            actor="pytest",
        )
        spec = {
            "version": 1,
            "kind": "line",
            "data": {"source": "artifact", "ref": row.artifact_id, "period": "day"},
            "encode": {"x": "day", "y": "close"},
        }
        replay = _render(spec, paths, artifact_root=paths["artifact_root"])
    finally:
        conn.close()

    # 行情时点：采信纠正后的 meta，不按快照「反推」成 01-03。
    assert replay.data_as_of == "2020-01-01"
    # 视图时点：按快照 + 当前代码重算。
    assert replay.view_as_of == "2024-01-03"


def test_snapshot_truncation_always_keeps_latest_bar() -> None:
    frame = ohlcv_frame(10).sort_values("trade_date", ascending=False)
    snapshot = chart_render.data_snapshot(frame, limit=3)
    assert snapshot["truncated"] is True
    latest = pd.Timestamp(frame["trade_date"].max())
    assert pd.Timestamp(snapshot["rows"][-1]["trade_date"]) == latest



# ---------------------------------------------------------------- Q3 div id / D10 时间语义


def test_canonical_html_uses_fixed_div_id_and_embed_html_is_per_instance(
    paths: dict[str, str],
) -> None:
    """Q3：规范 HTML 固定 div id（字节稳定）；每次嵌入换唯一 id，双图/重复嵌入都不撞。"""
    first = _render(inline_line_spec(), paths)
    second = _render(inline_line_spec(), paths)
    assert first.html == second.html
    assert first.artifact_id == second.artifact_id
    assert f'id="{FIGURE_DIV_ID}"' in first.html

    a = embed_html(first.html, "astock-chart-aaaa1111")
    b = embed_html(first.html, "astock-chart-bbbb2222")
    assert 'id="astock-chart-aaaa1111"' in a
    assert 'id="astock-chart-bbbb2222"' in b
    assert a != b
    # 同一份 artifact 重复嵌入两次也要拿到两个不同 id
    c = embed_html(first.html, "astock-chart-bbbb2222")
    assert embed_html(first.html, "astock-chart-cccc3333") != c
    assert FIGURE_DIV_ID + '"' not in a  # 全部替换，不留旧 id

    with pytest.raises(ValueError, match="div_id"):
        embed_html(first.html, "1bad-id")
    with pytest.raises(ValueError, match="找不到"):
        embed_html("<html>no chart here</html>", "astock-chart-x1")


def test_ui_embed_html_swaps_div_id_per_figure(paths: dict[str, str]) -> None:
    """chat 层每次嵌入都换 id，两张图各自渲染（双图验收）。"""
    from astock.agent import chat

    rendered = _render(inline_line_spec(), paths)
    figure = chat.Figure(
        step=1,
        tool="chart_render",
        html=rendered.html,
        title="收盘走势",
        spec=rendered.spec,
        source=rendered.source,
        data_as_of=rendered.data_as_of,
        data_time=rendered.data_time,
        artifact_id=rendered.artifact_id,
        artifact_path=rendered.path,
    )
    one = chat.figure_embed_html(figure)
    two = chat.figure_embed_html(figure)
    assert one != two
    ids = set(re.findall(r"astock-chart-[0-9a-f]{8}", one + two))
    assert len(ids) == 2


def test_unprovable_time_column_raises_instead_of_guessing(paths: dict[str, str]) -> None:
    """D10：字符串代码/纯数字日期列不能被猜成时间列，必须报契约错误。"""
    spec = {
        "version": 1,
        "kind": "line",
        "data": {
            "source": "inline",
            "fields": ["code", "close"],
            "rows": [
                {"code": "20240102", "close": 10.0},
                {"code": "20240103", "close": 10.2},
            ],
        },
        "encode": {"x": "code", "y": "close"},
    }
    with pytest.raises(ChartSpecError) as info:
        _render(spec, paths)
    assert "$.data.time_field" in {issue.field for issue in info.value.issues} or any(
        "无法证明时间语义" in issue.message for issue in info.value.issues
    )


def test_text_label_column_is_never_treated_as_time(paths: dict[str, str]) -> None:
    spec = {
        "version": 1,
        "kind": "line",
        "data": {
            "source": "inline",
            "fields": ["label", "close"],
            "rows": [
                {"label": "2024-01-04 收盘", "close": 10.0},
                {"label": "2024-01-05 收盘", "close": 10.2},
            ],
        },
        "encode": {"x": "label", "y": "close"},
    }
    with pytest.raises(ChartSpecError):
        _render(spec, paths)


def test_explicit_time_field_makes_otherwise_unprovable_data_renderable(
    paths: dict[str, str],
) -> None:
    """D10：显式声明 time_field（真实 datetime）就能证明语义；只画「视图时点未知」时也可声明 none。"""
    spec = {
        "version": 1,
        "kind": "line",
        "data": {
            "source": "inline",
            "fields": ["code", "day", "close"],
            "time_field": "day",
            "rows": [
                {"code": "20240102", "day": "2024-01-02", "close": 10.0},
                {"code": "20240103", "day": "2024-01-03", "close": 10.2},
            ],
        },
        "encode": {"x": "code", "y": "close"},
    }
    rendered = _render(spec, paths, frame=None)
    assert rendered.data_as_of == "2024-01-03"

    no_time = {
        "version": 1,
        "kind": "line",
        "data": {
            "source": "inline",
            "fields": ["code", "close"],
            "time_semantics": "none",
            "rows": [
                {"code": "20240102", "close": 10.0},
                {"code": "20240103", "close": 10.2},
            ],
        },
        "encode": {"x": "code", "y": "close"},
    }
    rendered_none = _render(no_time, paths)
    assert rendered_none.data_as_of == ""
    assert "时点未知" in rendered_none.caption


def test_time_field_must_be_declared_and_provable(paths: dict[str, str]) -> None:
    spec = inline_line_spec()
    spec["data"]["time_field"] = "nope"
    with pytest.raises(ChartSpecError) as info:
        _render(spec, paths)
    assert {issue.field for issue in info.value.issues} == {"$.data.time_field"}

    bad = inline_line_spec()
    bad["data"]["time_semantics"] = "sometimes"
    with pytest.raises(ChartSpecError) as info2:
        _render(bad, paths)
    assert {issue.field for issue in info2.value.issues} == {"$.data.time_semantics"}


# ---------------------------------------------------------------- 安全边界


def test_renderer_never_executes_arbitrary_python() -> None:
    forbidden = ("exec(", "eval(", "__import__(", "subprocess", "os.system")
    for name in ("chart_spec.py", "chart_render.py"):
        text = (ROOT / "astock" / "agent" / name).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{name} 出现危险符号 {token}"
        # 允许 re.compile，但必须没有内建 compile()/eval 式代码执行
        assert not re.search(r"(?<![\w.])compile\(", text), f"{name} 出现内建 compile("


def test_price_colors_stay_in_sync_with_charts_module() -> None:
    from astock import charts

    assert PRICE_UP == charts.UP
    assert PRICE_DOWN == charts.DOWN


def test_invalid_spec_fails_before_touching_store(paths: dict[str, str]) -> None:
    bad = inline_line_spec(unknown="x")
    with pytest.raises(ChartSpecError):
        _render(bad, paths)
    conn = store.connect(paths["db_path"])
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()["n"] == 0
    finally:
        conn.close()


class _FlakyImageFigure:
    """``to_image`` 按预设顺序抛错，用来验证重试边界（不依赖真实 kaleido/Chrome）。"""

    def __init__(self, errors: list[str]) -> None:
        self._errors = list(errors)
        self.calls = 0

    def to_image(self, **_kwargs: object) -> bytes:
        self.calls += 1
        if self._errors:
            raise RuntimeError(self._errors.pop(0))
        return b"\x89PNG\r\n\x1a\npayload"


def test_png_export_retries_transient_kaleido_race() -> None:
    """kaleido 1.5.1 首帧 ``kaleido_scopes`` 竞态应重试一次，不得直接降级。"""
    figure = _FlakyImageFigure(["ReferenceError: kaleido_scopes is not defined"])
    payload, reason = chart_render._try_png(figure, width=800, height=400)  # type: ignore[arg-type]

    assert reason is None
    assert payload is not None and payload.startswith(b"\x89PNG")
    assert figure.calls == 2


def test_png_export_does_not_retry_missing_dependency() -> None:
    """真正缺依赖时不重试，保持快速失败与明确原因。"""
    figure = _FlakyImageFigure(
        ['Image export using the "kaleido" engine requires the Kaleido package']
    )
    payload, reason = chart_render._try_png(figure, width=800, height=400)  # type: ignore[arg-type]

    assert payload is None
    assert reason is not None and "Kaleido package" in reason
    assert figure.calls == 1


# ---------------------------------------------------------------- Q5 / D9 审计边界


def _run_row(paths: dict[str, str], run_id: str):
    conn = store.connect(paths["db_path"])
    try:
        return conn.execute(
            "SELECT run_id, status, run_kind FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    finally:
        conn.close()


def _link_rows(paths: dict[str, str], artifact_id: str) -> list[dict]:
    conn = store.connect(paths["db_path"])
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT run_id, artifact_version, source, step FROM agent_run_artifacts"
                " WHERE artifact_id = ? ORDER BY link_id",
                (artifact_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_direct_render_records_artifact_without_synthetic_run(paths: dict[str, str]) -> None:
    """Q5/D9：直接渲染只落 artifact/version/link，绝不合成 run_chart_* 假 run。"""
    rendered = _render(inline_line_spec(), paths)

    conn = store.connect(paths["db_path"])
    try:
        runs = conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()["n"]
        artifacts = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
        versions = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_artifact_versions"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert runs == 0
    assert artifacts == 1
    assert versions == 1
    links = _link_rows(paths, rendered.artifact_id)
    assert len(links) == 1
    assert links[0]["run_id"] is None
    assert links[0]["source"] == "render"


def test_agent_render_links_real_run_and_delete_keeps_artifact(paths: dict[str, str]) -> None:
    conn = store.connect(paths["db_path"])
    try:
        store.ensure_run(conn, "run_agent_1", session_id="sess-1")
    finally:
        conn.close()

    rendered = _render(inline_line_spec(), paths, run_id="run_agent_1", step=3, session_id="sess-1")
    links = _link_rows(paths, rendered.artifact_id)
    assert [(link["run_id"], link["source"], link["step"]) for link in links] == [
        ("run_agent_1", "agent", 3)
    ]
    assert _run_row(paths, "run_agent_1")["status"] == "running"  # 真实 run 由 Agent 自己收尾

    conn = store.connect(paths["db_path"])
    try:
        conn.execute("DELETE FROM agent_runs WHERE run_id = ?", ("run_agent_1",))
        conn.commit()
        artifacts = conn.execute("SELECT COUNT(*) AS n FROM agent_artifacts").fetchone()["n"]
    finally:
        conn.close()
    assert artifacts == 1  # artifact 永久保留，只级联删 link
    assert _link_rows(paths, rendered.artifact_id) == []


def test_two_runs_link_to_one_artifact(paths: dict[str, str]) -> None:
    conn = store.connect(paths["db_path"])
    try:
        store.ensure_run(conn, "run_a", session_id="s")
        store.ensure_run(conn, "run_b", session_id="s")
    finally:
        conn.close()

    first = _render(inline_line_spec(), paths, run_id="run_a", step=1)
    second = _render(inline_line_spec(), paths, run_id="run_b", step=2)
    assert first.artifact_id == second.artifact_id
    run_ids = {link["run_id"] for link in _link_rows(paths, first.artifact_id)}
    assert run_ids == {"run_a", "run_b"}


def test_chart_render_is_pure_and_chart_service_is_the_only_writer() -> None:
    """Q5：纯渲染器不得 import 审计写入路径；唯一写入点在 chart_service。"""
    import ast

    renderer = (ROOT / "astock" / "agent" / "chart_render.py").read_text(encoding="utf-8")
    store_imports: set[str] = set()
    for node in ast.walk(ast.parse(renderer)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "astock.agent.store"
        ):
            store_imports.update(alias.name for alias in node.names)
    allowed = {"PNG_DEGRADED_WARNING_PREFIX", "compute_env_fingerprint"}
    assert store_imports <= allowed, f"chart_render 仍 import 审计写路径 {store_imports - allowed}"
    assert "import astock.agent.store" not in renderer or store_imports

    service = (ROOT / "astock" / "agent" / "chart_service.py").read_text(encoding="utf-8")
    for token in ("record_artifact", "put_content"):
        assert token in service, f"chart_service 缺少审计写入 {token}"


# ---------------------------------------------------------------- D11 transforms = stats 口径


def test_transform_groupby_matches_stats_caliber() -> None:
    from astock import stats

    frame = pd.DataFrame(
        {
            "sector": ["银行", "券商", "银行", "券商", "银行"],
            "amount": [1.0, 5.0, 3.0, 7.0, 2.0],
        }
    )
    expected = stats.aggregate_groups(
        frame, by=["sector"], metrics={"amount": "mean"}
    ).frame
    actual = apply_transforms(
        frame, [{"op": "groupby", "by": ["sector"], "metrics": {"amount": "mean"}}]
    )
    pd.testing.assert_frame_equal(
        actual.reset_index(drop=True), expected.reset_index(drop=True)
    )
    assert actual.set_index("sector")["amount_mean"].to_dict() == pytest.approx(
        {"银行": 2.0, "券商": 6.0}
    )


def test_transform_groupby_keeps_empty_and_nan_groups_with_warning() -> None:
    frame = pd.DataFrame(
        {
            "sector": ["银行", "券商", None, "银行"],
            "amount": [1.0, None, 4.0, 3.0],
        }
    )
    notes: list[str] = []
    grouped = apply_transforms(
        frame,
        [{"op": "groupby", "by": ["sector"], "metrics": {"amount": "mean"}}],
        notes=notes,
    )
    keys = grouped["sector"].tolist()
    assert len(keys) == 3  # 空组、全 NaN 组都保留（D11）
    assert sum(1 for key in keys if pd.isna(key)) == 1
    assert {key for key in keys if not pd.isna(key)} == {"银行", "券商"}
    assert any("空组" in note for note in notes)


def test_transform_sort_is_stable_and_limit_follows() -> None:
    frame = pd.DataFrame(
        {
            "sector": ["a", "a", "b", "b"],
            "tag": ["first", "second", "third", "fourth"],
            "amount": [1.0, 1.0, 1.0, 1.0],
        }
    )
    notes: list[str] = []
    ordered = apply_transforms(
        frame, [{"op": "sort", "by": "amount", "desc": True}], notes=notes
    )
    assert ordered["tag"].tolist() == ["first", "second", "third", "fourth"]

    limited = apply_transforms(
        frame,
        [
            {"op": "sort", "by": "amount", "desc": True},
            {"op": "limit", "n": 2},
        ],
    )
    assert limited["tag"].tolist() == ["first", "second"]


def test_same_name_groupby_output_is_disambiguated_with_warning() -> None:
    frame = pd.DataFrame({"sector": ["银行"], "amount": [1.0], "amount_sum": [9.0]})
    notes: list[str] = []
    grouped = apply_transforms(
        frame,
        [{"op": "groupby", "by": ["sector"], "metrics": {"amount": "sum"}}],
        notes=notes,
    )
    assert "amount_sum_2" in grouped.columns
    assert any("同名" in note for note in notes)


# ---------------------------------------------------------------- 评审第 1 轮修复
# rev-20261119-002：P0 日/周/月指标恒空；P1 runtime→tool link；P1 run 生命周期；
# P2 meta 摘要校验；P2 图注「视图时点未知」归因。


@pytest.mark.parametrize("period", ["day", "week", "month"])
def test_period_indicators_are_numeric_and_match_indicators(
    period: str, paths: dict[str, str]
) -> None:
    """P0：日/周/月线的 MA/BOLL/MACD 必须真的算出来（不能静默全 NaN）。

    ``classify_bar_time`` 对非分钟频率返回 ``close``，旧实现把所有周期都喂给
    ``continuous_bars``（只留 ``continuous``）＝指标样本恒空；这里用数值对齐
    :func:`astock.indicators.sma` 卡住回归。
    """
    frame = ohlcv_frame(120)
    spec = candlestick_spec(layers=[{"type": "ma", "window": 20}, {"type": "volume"}])
    spec["data"]["period"] = period
    rendered = _render(spec, paths, frame=frame)

    assert rendered.session == "close"
    assert not any("连续指标无可用 bar" in note for note in rendered.warnings)

    figure = rendered.figure
    assert figure is not None
    trace = next(trace for trace in figure.data if trace.name == "MA20")
    values = pd.Series(trace.y, dtype="float64")
    expected = indicators.sma(frame["close"], 20)

    assert values.notna().sum() == expected.notna().sum() > 0
    pd.testing.assert_series_equal(
        values.iloc[-10:].reset_index(drop=True),
        expected.iloc[-10:].reset_index(drop=True),
        check_names=False,
        rtol=1e-9,
    )


def test_auction_only_minute_frame_warns_indicators_are_empty(paths: dict[str, str]) -> None:
    """P0：指标样本为空时必须显式写 warning，不能静默全 NaN。"""
    auction = pd.DataFrame(
        {
            "ts_code": "000001.SZ",
            "trade_time": pd.to_datetime(["2024-01-02 09:25:00"]),
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.05],
            "vol": [500.0],
            "amount": [5_000.0],
        }
    )
    spec = candlestick_spec(layers=[{"type": "ma", "window": 5}])
    spec["data"]["period"] = "5min"
    spec["encode"] = {"x": "trade_time"}
    rendered = _render(spec, paths, frame=auction)

    assert any("连续指标无可用 bar" in note for note in rendered.warnings)
    assert any("连续指标无可用 bar" in note for note in rendered.meta["warnings"])


def test_view_unknown_reason_names_time_semantics_none(paths: dict[str, str]) -> None:
    """P2：只有聚合丢列才写「聚合后无时间列」，声明 none 时要写明原因。"""
    spec = inline_line_spec()
    spec["data"]["time_semantics"] = "none"
    rendered = _render(
        spec, paths, data_as_of="2024-01-04", data_time="2024-01-04 15:00"
    )

    assert rendered.view_unknown_reason == "none"
    assert "视图时点未知（声明 time_semantics=none）" in rendered.caption
    assert "聚合后无时间列" not in rendered.caption


def test_replay_verifies_stored_meta_digest_and_rejects_tampering(
    paths: dict[str, str],
) -> None:
    """P2：回放要拿库内 meta_digest 校验，绕过 correction 改 meta_json 必须报错。"""
    rendered = _render(inline_line_spec(), paths)
    conn = store.connect(paths["db_path"])
    try:
        record = store.get_artifact(conn, rendered.artifact_id)
        assert record is not None
        payload = chart_service.load_chart_artifact(
            conn, rendered.artifact_id, root=paths["artifact_root"]
        )
        assert payload["meta_digest"] == record.meta_digest
        assert record.meta_digest == store.meta_digest(record.meta)

        tampered = dict(record.meta)
        tampered["data_as_of"] = "1999-01-01"
        # 模拟「维护脚本直接改库」：正常路径被 immutability trigger 挡住，
        # 这里先去掉 trigger 再改，验证回放端的摘要校验确实能发现。
        conn.execute("DROP TRIGGER IF EXISTS trg_agent_artifact_versions_no_update")
        conn.execute(
            "UPDATE agent_artifact_versions SET meta_json = ? "
            "WHERE artifact_id = ? AND version = ?",
            (store.canonical_json(tampered), record.artifact_id, record.version),
        )
        conn.commit()

        with pytest.raises(store.ArtifactReplayUnavailable, match="meta 摘要不一致"):
            chart_service.load_chart_artifact(
                conn, rendered.artifact_id, root=paths["artifact_root"]
            )
    finally:
        conn.close()


def test_replay_reuses_recorded_include_plotlyjs_without_stringifying_bool(
    paths: dict[str, str],
) -> None:
    """边角：记录里 include_plotlyjs=False（bool）时，重算不得被 str() 变成 "False"。"""
    rendered = _render(inline_line_spec(), paths, include_plotlyjs=False)
    assert rendered.meta["params"]["include_plotlyjs"] is False

    conn = store.connect(paths["db_path"])
    try:
        payload = chart_service.load_chart_artifact(
            conn,
            rendered.artifact_id,
            mode="recomputed",
            root=paths["artifact_root"],
        )
    finally:
        conn.close()

    assert payload["meta"]["params"]["include_plotlyjs"] is False
    # False 表示不内联也不引 CDN；若被写成 "False"，plotly 会把非空字符串当 CDN 路径
    assert "cdn.plot.ly" not in payload["html"]
