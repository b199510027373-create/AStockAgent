"""chart_render 渲染管线用例（ADR-016 §3、冻结清单 §3.4/§6）。

重点：六种 kind 能出图、图题角标同屏两个时点、K 线复用 indicators 的 MA/BOLL/成交量、
每次渲染落 ``agent_artifacts`` 且可按 ``artifact_id`` 回放、无 kaleido 时降级不抛异常、
渲染器不执行任意 Python。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from astock.agent import chart_render, store
from astock.agent.chart_render import (
    PRICE_DOWN,
    PRICE_UP,
    apply_transforms,
    figure_tool_result,
    render,
    spec_from_artifact,
)
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
    assert head.startswith("<!doctype html>") or head.startswith("<html>")
    assert "plotly.js" in rendered.html
    assert len(rendered.html) > 1_000_000  # plotly.js 内联 → 自包含
    assert rendered.data_as_of == "2024-01-04"
    assert rendered.data_time == "2024-01-04 15:00"
    assert rendered.path.startswith("artifacts/")
    assert rendered.artifact_id.startswith("art_")

    row = _artifact_row(rendered, paths)
    assert row is not None
    assert row.kind == "figure"
    assert row.path == rendered.path
    assert row.digest == rendered.digest

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
        "spec",
        "params",
        "data_as_of",
        "data_time",
        "source",
        "code_version",
        "time_point_scope",
        "div_id",
    ):
        assert key in meta, key
    assert meta["data_as_of"] == "2024-01-04"
    assert meta["data_time"] == "2024-01-04 15:00"
    assert meta["source"] == "inline"
    assert meta["code_version"] == chart_render.CODE_VERSION
    assert meta["spec"]["kind"] == "line"
    assert meta["params"]["kind"] == "line"
    assert meta["data"]["rows"][0]["day"].startswith("2024-01-02")
    # 复现前提：记录运行时版本，避免「同 spec 同数据却字节不同」无从追查。
    assert meta["params"]["plotly_version"]
    assert meta["params"]["python_version"]
    assert meta["params"]["code_version"] == rendered.code_version


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
    assert "数据截止 2024-" in html
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
    assert "最新 bar 2024-01-02 09:35" in rendered.html


def test_missing_time_points_render_unknown_not_realtime(paths: dict[str, str]) -> None:
    rendered = _render(inline_line_spec(), paths, data_as_of="", data_time="")
    caption = rendered.caption
    assert "时点未知" in caption
    assert "实时" not in caption


def test_raw_price_caliber_is_noted_in_caption(paths: dict[str, str]) -> None:
    spec = candlestick_spec()
    spec["data"]["adj"] = "raw"
    rendered = _render(spec, paths, frame=ohlcv_frame())
    assert "不复权" in rendered.caption
    assert "raw" in rendered.caption
    assert "前复权" not in rendered.caption


def test_degrades_to_html_when_kaleido_missing(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_png(figure, *, width: int, height: int):  # noqa: ANN001 - 测试替身
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
    rows = row.meta["data"]["rows"]
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
    ("adj", "expected"), [("qfq", "qfq"), ("hfq", "hfq"), ("none", None), ("raw", None)]
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
    # raw 必须映射成 None，否则 data 层会当成前复权（评审 P0 根因）
    assert calls["adj"] is expected
    assert rendered.params["adj"] == ("none" if adj == "raw" else adj)


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


def test_market_branch_raw_caption_and_snapshot_agree(
    paths: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = ohlcv_frame()
    monkeypatch.setattr("astock.data.load_daily", lambda ts_code, **kwargs: raw)

    spec = candlestick_spec()
    spec["data"]["adj"] = "raw"
    rendered = _render(spec, paths)
    row = _artifact_row(rendered, paths)

    assert "不复权" in rendered.caption and "raw" in rendered.caption
    # 渲染图注、artifact meta 与快照价格同源（都是 data 层返回的原始价）
    assert rendered.source == "market"
    assert row.meta["data"]["rows"][-1]["close"] == pytest.approx(raw["close"].iloc[-1])


# ---------------------------------------------------------------- 时点与快照一致性（评审 P1）


def test_time_point_follows_transformed_rows(paths: dict[str, str]) -> None:
    spec = inline_line_spec(
        transforms=[
            {"op": "filter", "field": "close", "cmp": "gt", "value": 10.0},
            {"op": "sort", "by": "close", "desc": True},
            {"op": "limit", "n": 1},
        ]
    )
    rendered = _render(spec, paths)
    row = _artifact_row(rendered, paths)
    assert rendered.data_as_of == "2024-01-03"
    assert rendered.data_time == "2024-01-03 15:00"
    assert row.meta["data_as_of"] == "2024-01-03"
    assert row.meta["data_as_of"] == row.meta["data"]["rows"][-1]["day"]
    assert "2024-01-03" in rendered.caption


def test_artifact_replay_recomputes_time_from_snapshot(paths: dict[str, str]) -> None:
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

    # 篡改 meta 里的时点：回放必须按快照时间列重算，不得信任旧 meta
    conn = store.connect(paths["db_path"])
    try:
        meta = dict(row.meta)
        meta["data_as_of"] = "2020-01-01"
        meta["data_time"] = "2020-01-01 15:00"
        conn.execute(
            "UPDATE agent_artifacts SET meta_json=? WHERE artifact_id=?",
            (json.dumps(meta, ensure_ascii=False), row.artifact_id),
        )
        conn.commit()
        spec = {
            "version": 1,
            "kind": "line",
            "data": {"source": "artifact", "ref": row.artifact_id, "period": "day"},
            "encode": {"x": "day", "y": "close"},
        }
        replay = _render(spec, paths)
    finally:
        conn.close()

    assert replay.data_as_of == "2024-01-03"
    assert "2024-01-03" in replay.caption


def test_snapshot_truncation_always_keeps_latest_bar() -> None:
    frame = ohlcv_frame(10).sort_values("trade_date", ascending=False)
    snapshot = chart_render.data_snapshot(frame, limit=3)
    assert snapshot["truncated"] is True
    latest = pd.Timestamp(frame["trade_date"].max())
    assert pd.Timestamp(snapshot["rows"][-1]["trade_date"]) == latest



# ---------------------------------------------------------------- div id / 时点语义（D2/D4/D10）


def test_div_id_is_derived_from_spec_and_snapshot(paths: dict[str, str]) -> None:
    first = _render(inline_line_spec(), paths)
    second = _render(inline_line_spec(), paths)
    first_row = _artifact_row(first, paths)
    assert first_row is not None
    div_id = first_row.meta["div_id"]
    assert div_id.startswith(chart_render.FIGURE_DIV_PREFIX + "-")
    assert f'id="{div_id}"' in first.html
    assert second.artifact_id == first.artifact_id
    assert second.html == first.html

    other = dict(inline_line_spec())
    other["title"] = "换一张图"
    other_row = _artifact_row(_render(other, paths), paths)
    assert other_row is not None
    assert other_row.meta["div_id"] != div_id  # 同页多图不共享 id


def test_time_point_scope_marks_market_view_and_unknown(paths: dict[str, str]) -> None:
    market = _render(candlestick_spec(), paths, frame=ohlcv_frame())
    assert market.time_point_scope == "market"
    assert market.metadata()["time_point_scope"] == "market"

    viewed = _render(
        inline_line_spec(
            transforms=[{"op": "limit", "n": 1}],
        ),
        paths,
    )
    assert viewed.time_point_scope == "view"

    unknown = _render(inline_line_spec(), paths, data_as_of="", data_time="")
    assert unknown.time_point_scope == "unknown"
    assert unknown.data_as_of == ""


def test_time_probe_only_trusts_date_like_columns(paths: dict[str, str]) -> None:
    # code 列的取值虽然形如日期，但列名不在白名单里 → 不得据此猜时点（D10）。
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
    rendered = _render(spec, paths)
    assert rendered.data_as_of == ""
    assert rendered.time_point_scope == "unknown"
    assert "时点未知" in rendered.caption


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
