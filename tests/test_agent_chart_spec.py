"""ChartSpec v1 校验用例（冻结清单 §3；ADR-016 C1）。

覆盖：字段白名单、枚举、长度上限、词表消歧（``1min`` vs ``1mo``）、别名折叠、
结构化错误形状、交给模型的 JSON Schema。
"""

from __future__ import annotations

import json

import pytest

from astock.agent.chart_spec import (
    MAX_ANNOTATIONS,
    MAX_INLINE_ROWS,
    MAX_LAYERS,
    MAX_META_BYTES,
    MAX_TITLE_CHARS,
    MAX_TRANSFORMS,
    ChartSpecError,
    chart_spec_json_schema,
    check_chart_spec,
    validate_chart_spec,
)


def market_spec(**overrides: object) -> dict:
    spec: dict = {
        "version": 1,
        "kind": "candlestick",
        "data": {"source": "market", "ref": "000001.SZ"},
        "encode": {"x": "trade_date"},
    }
    spec.update(overrides)
    return spec


def inline_spec(kind: str = "line", **overrides: object) -> dict:
    spec: dict = {
        "version": 1,
        "kind": kind,
        "data": {
            "source": "inline",
            "fields": ["day", "close"],
            "rows": [{"day": "2024-01-02", "close": 10.0}, {"day": "2024-01-03", "close": 10.5}],
        },
        "encode": {"x": "day", "y": "close"},
    }
    spec.update(overrides)
    return spec


def error_fields(exc: ChartSpecError) -> set[str]:
    return {issue.field for issue in exc.issues}


# ---------------------------------------------------------------- 合法与归一化


def test_minimal_candlestick_is_normalized_with_frozen_defaults() -> None:
    spec = validate_chart_spec(market_spec())
    assert spec["version"] == 1
    assert spec["kind"] == "candlestick"
    assert spec["data"]["period"] == "day"
    assert spec["data"]["range"] == "1y"
    assert spec["data"]["adj"] == "qfq"
    assert spec["theme"] == "light"
    assert spec["transforms"] == [] and spec["layers"] == [] and spec["annotations"] == []
    assert spec["title"] == ""
    assert spec["meta"] == {}
    assert set(spec) == {
        "version",
        "kind",
        "title",
        "data",
        "encode",
        "transforms",
        "layers",
        "theme",
        "annotations",
        "meta",
    }


def test_inline_source_defaults_to_unadjusted_prices() -> None:
    spec = validate_chart_spec(inline_spec())
    assert spec["data"]["source"] == "inline"
    assert spec["data"]["adj"] == "none"
    assert spec["data"]["fields"] == ["day", "close"]


def test_adj_raw_is_rejected_after_q4_b() -> None:
    spec = market_spec()
    spec["data"]["adj"] = "raw"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.adj" in error_fields(info.value)


def test_layers_and_annotations_are_filled_with_documented_defaults() -> None:
    spec = market_spec(
        layers=[{"type": "ma"}, {"type": "boll", "window": 26}, {"type": "macd"}],
        annotations=[{"type": "hline", "y": 10.5, "label": "支撑"}],
    )
    normalized = validate_chart_spec(spec)
    assert normalized["layers"][0] == {"type": "ma", "window": 20}
    assert normalized["layers"][1] == {"type": "boll", "window": 26, "k": 2.0}
    assert normalized["layers"][2] == {"type": "macd", "fast": 12, "slow": 26, "signal": 9}
    assert normalized["annotations"][0]["label"] == "支撑"


def test_all_six_kinds_are_accepted() -> None:
    encodes = {
        "candlestick": {"x": "trade_date"},
        "line": {"x": "trade_date", "y": "close"},
        "bar": {"x": "trade_date", "y": ["open", "close"]},
        "scatter": {"x": "trade_date", "y": "close", "color": "close"},
        "heatmap": {"x": "trade_date", "y": "sector", "color": "close"},
        "histogram": {"x": "trade_date", "y": "close"},
    }
    for kind, encode in encodes.items():
        spec = {
            "version": 1,
            "kind": kind,
            "data": {"source": "market", "ref": "000001.SZ"},
            "encode": encode,
        }
        assert validate_chart_spec(spec)["kind"] == kind


# ---------------------------------------------------------------- 字段白名单


def test_unknown_top_level_field_is_rejected_not_ignored() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(panes_extra=[], unknown="x"))
    assert "$.unknown" in error_fields(info.value)


def test_unknown_nested_fields_are_rejected() -> None:
    spec = market_spec()
    spec["data"]["frequency"] = "day"
    spec["encode"]["z"] = "close"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    fields = error_fields(info.value)
    assert "$.data.frequency" in fields
    assert "$.encode.z" in fields


def test_unknown_layer_or_annotation_parameter_is_rejected() -> None:
    spec = market_spec(
        layers=[{"type": "ma", "window": 20, "color": "red"}],
        annotations=[{"type": "hline", "y": 1, "width": 2}],
    )
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    fields = error_fields(info.value)
    assert "$.layers[0].color" in fields
    assert "$.annotations[0].width" in fields


# ---------------------------------------------------------------- 枚举


def test_unknown_kind_and_theme_are_rejected() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(kind="pie", theme="sepia"))
    fields = error_fields(info.value)
    assert "$.kind" in fields
    assert "$.theme" in fields


def test_version_must_be_exactly_one() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(version=2))
    assert "$.version" in error_fields(info.value)


def test_unsupported_transform_op_is_rejected() -> None:
    for op in ("join", "pivot", "window", "python"):
        spec = inline_spec(transforms=[{"op": op}])
        with pytest.raises(ChartSpecError) as info:
            validate_chart_spec(spec)
        assert "$.transforms[0].op" in error_fields(info.value), op


def test_transform_whitelist_accepts_five_ops() -> None:
    spec = inline_spec(
        transforms=[
            {"op": "filter", "field": "close", "cmp": "gt", "value": 1},
            {"op": "sort", "by": "close", "desc": True},
            {"op": "limit", "n": 10, "from": "tail"},
        ]
    )
    normalized = validate_chart_spec(spec)
    assert [t["op"] for t in normalized["transforms"]] == ["filter", "sort", "limit"]

    grouped = inline_spec(
        transforms=[{"op": "groupby", "by": ["day"], "metrics": {"close": "mean"}}]
    )
    assert validate_chart_spec(grouped)["transforms"][0]["by"] == ["day"]


def test_filter_cmp_in_requires_array_value() -> None:
    spec = inline_spec(transforms=[{"op": "filter", "field": "close", "cmp": "in", "value": 1}])
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.transforms[0].value" in error_fields(info.value)


def test_unknown_metric_function_is_rejected() -> None:
    spec = inline_spec(
        transforms=[{"op": "agg", "by": "day", "metrics": {"close": "median"}}]
    )
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.transforms[0].metrics.close" in error_fields(info.value)


# ---------------------------------------------------------------- 词表消歧（1min vs 1mo）


def test_period_minute_vocabulary_uses_min_suffix() -> None:
    spec = market_spec()
    spec["data"]["period"] = "1min"
    assert validate_chart_spec(spec)["data"]["period"] == "1min"
    for period in ("5min", "15min", "30min", "60min"):
        spec["data"]["period"] = period
        assert validate_chart_spec(spec)["data"]["period"] == period

    spec["data"]["period"] = "1m"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.period" in error_fields(info.value)


def test_range_month_vocabulary_uses_mo_suffix() -> None:
    spec = market_spec()
    for window in ("1mo", "3mo", "6mo", "1y", "3y", "5y", "ytd", "max"):
        spec["data"]["range"] = window
        assert validate_chart_spec(spec)["data"]["range"] == window

    spec["data"]["range"] = "1m"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.range" in error_fields(info.value)


def test_explicit_date_range_is_accepted_and_bounded() -> None:
    spec = market_spec()
    spec["data"]["range"] = "2023-01-03:2024-01-03"
    assert validate_chart_spec(spec)["data"]["range"] == "2023-01-03:2024-01-03"

    spec["data"]["range"] = "2024-01-03:2014-01-03"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.range" in error_fields(info.value)


def test_minute_period_requires_market_source() -> None:
    spec = inline_spec()
    spec["data"]["period"] = "5min"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.period" in error_fields(info.value)


# ---------------------------------------------------------------- kind 与 encode


def test_candlestick_rejects_encode_y_and_color() -> None:
    spec = market_spec()
    spec["encode"] = {"x": "trade_date", "y": "close", "color": "close"}
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    fields = error_fields(info.value)
    assert "$.encode.y" in fields and "$.encode.color" in fields


def test_non_candlestick_requires_y() -> None:
    spec = market_spec(kind="line")
    spec["encode"] = {"x": "trade_date"}
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.encode.y" in error_fields(info.value)


def test_heatmap_requires_y_and_color() -> None:
    spec = market_spec(kind="heatmap")
    spec["encode"] = {"x": "trade_date"}
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    fields = error_fields(info.value)
    assert "$.encode.y" in fields and "$.encode.color" in fields


def test_encode_y_is_capped_at_four_series() -> None:
    spec = inline_spec(kind="bar")
    spec["data"]["fields"] = ["day", "a", "b", "c", "d", "e"]
    spec["data"]["rows"] = [{"day": "2024-01-02", "a": 1, "b": 2, "c": 3, "d": 4, "e": 5}]
    spec["encode"]["y"] = ["a", "b", "c", "d", "e"]
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.encode.y" in error_fields(info.value)


def test_layers_only_apply_to_candlestick() -> None:
    spec = inline_spec(kind="line", layers=[{"type": "ma", "window": 20}])
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.layers[0]" in error_fields(info.value)


def test_field_names_are_snake_case_whitelisted() -> None:
    spec = inline_spec()
    spec["encode"]["x"] = "Trade Date"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.encode.x" in error_fields(info.value)


# ---------------------------------------------------------------- 数量上限


def test_transform_count_cap() -> None:
    spec = inline_spec(
        transforms=[{"op": "limit", "n": 5} for _ in range(MAX_TRANSFORMS + 1)]
    )
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.transforms" in error_fields(info.value)


def test_layer_count_cap() -> None:
    spec = market_spec(layers=[{"type": "ma", "window": 20} for _ in range(MAX_LAYERS + 1)])
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.layers" in error_fields(info.value)


def test_annotation_count_cap() -> None:
    spec = market_spec(
        annotations=[{"type": "hline", "y": float(i)} for i in range(MAX_ANNOTATIONS + 1)]
    )
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.annotations" in error_fields(info.value)


def test_title_length_and_plain_text_rules() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(title="x" * (MAX_TITLE_CHARS + 1)))
    assert "$.title" in error_fields(info.value)

    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(title="<script>alert(1)</script>"))
    assert "$.title" in error_fields(info.value)


def test_meta_json_size_cap() -> None:
    payload = {"blob": "x" * MAX_META_BYTES}
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(meta=payload))
    assert "$.meta" in error_fields(info.value)


def test_inline_row_cap() -> None:
    rows = [{"day": "2024-01-02", "close": 1.0} for _ in range(MAX_INLINE_ROWS + 1)]
    spec = inline_spec()
    spec["data"]["rows"] = rows
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.rows" in error_fields(info.value)


def test_layer_parameter_bounds() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(layers=[{"type": "ma", "window": 1}]))
    assert "$.layers[0].window" in error_fields(info.value)

    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(layers=[{"type": "boll", "k": 5}]))
    assert "$.layers[0].k" in error_fields(info.value)


def test_annotation_required_parameters() -> None:
    spec = market_spec(annotations=[{"type": "text", "x": 1}])
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    fields = error_fields(info.value)
    assert "$.annotations[0].y" in fields and "$.annotations[0].label" in fields


# ---------------------------------------------------------------- data 子结构


def test_market_source_requires_valid_ref() -> None:
    for ref in ("000001", "000001.ss", "abc"):
        spec = market_spec()
        spec["data"]["ref"] = ref
        with pytest.raises(ChartSpecError) as info:
            validate_chart_spec(spec)
        assert "$.data.ref" in error_fields(info.value), ref


def test_index_refs_are_allowed_for_market_source() -> None:
    for ref in ("000300.SH", "399006.SZ", "830799.BJ"):
        spec = market_spec()
        spec["data"]["ref"] = ref
        assert validate_chart_spec(spec)["data"]["ref"] == ref


def test_artifact_source_requires_artifact_id() -> None:
    spec = market_spec(kind="line")
    spec["data"] = {"source": "artifact", "ref": "art_0123456789abcdef"}
    spec["encode"] = {"x": "trade_date", "y": "close"}
    assert validate_chart_spec(spec)["data"]["ref"] == "art_0123456789abcdef"

    spec["data"]["ref"] = "art_1234"
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.ref" in error_fields(info.value)


def test_inline_rows_must_match_declared_fields() -> None:
    spec = inline_spec()
    spec["data"]["rows"] = [{"day": "2024-01-02", "close": 10.0, "extra": 1}]
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.rows[0].extra" in error_fields(info.value)

    spec = inline_spec()
    spec["data"]["rows"] = [{"day": "2024-01-02"}]
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.rows[0].close" in error_fields(info.value)


def test_inline_requires_rows_and_fields() -> None:
    spec = inline_spec()
    spec["data"] = {"source": "inline"}
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    fields = error_fields(info.value)
    assert "$.data.rows" in fields and "$.data.fields" in fields


# ---------------------------------------------------------------- Q4=B：别名已删除


def test_panes_and_style_are_rejected_as_unknown_fields() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(panes=[{"type": "volume"}]))
    assert "$.panes" in error_fields(info.value)

    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(style="dark"))
    assert "$.style" in error_fields(info.value)


def test_style_object_form_is_rejected_too() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(market_spec(style={"theme": "dark"}))
    assert "$.style" in error_fields(info.value)


# ---------------------------------------------------------------- 回传模型的错误与 schema


def test_structural_error_payload_can_be_fed_back_to_model() -> None:
    payload = check_chart_spec({"version": 1, "kind": "pie"})
    assert payload["ok"] is False
    assert isinstance(payload["errors"], list) and payload["errors"]
    for item in payload["errors"]:
        assert set(item) == {"field", "reason"}
        assert item["field"].startswith("$")
        assert item["reason"]
    assert "hint" in payload
    assert json.dumps(payload, ensure_ascii=False)  # 可直接序列化回模型


def test_check_chart_spec_returns_normalized_spec_on_success() -> None:
    payload = check_chart_spec(market_spec())
    assert payload["ok"] is True
    assert payload["spec"]["data"]["period"] == "day"


def test_json_schema_is_a_whitelist_of_fields_and_enums() -> None:
    schema = chart_spec_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["kind"]["enum"] == [
        "candlestick",
        "line",
        "bar",
        "scatter",
        "heatmap",
        "histogram",
    ]
    period_enum = schema["properties"]["data"]["properties"]["period"]["enum"]
    assert "1min" in period_enum and "1m" not in period_enum
    assert schema["properties"]["transforms"]["items"]["properties"]["op"]["enum"] == [
        "filter",
        "groupby",
        "agg",
        "sort",
        "limit",
    ]
    assert schema["properties"]["layers"]["maxItems"] == MAX_LAYERS
    assert schema["properties"]["annotations"]["maxItems"] == MAX_ANNOTATIONS
    assert schema["properties"]["title"]["maxLength"] == MAX_TITLE_CHARS


def test_returned_json_schema_is_a_copy() -> None:
    first = chart_spec_json_schema()
    first["properties"]["kind"]["enum"].append("pie")
    assert "pie" not in chart_spec_json_schema()["properties"]["kind"]["enum"]


def test_non_object_spec_is_rejected_with_root_field() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(["not", "a", "spec"])
    assert error_fields(info.value) == {"$"}


def test_multiple_problems_are_collected_in_one_pass() -> None:
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec({"version": 3, "kind": "pie", "encode": {}})
    fields = error_fields(info.value)
    assert {"$.version", "$.kind", "$.data", "$.encode.x"} <= fields


# ---------------------------------------------------------------- 非法日期（评审 P1）


@pytest.mark.parametrize("window", ["2026-13-01:2026-12-01", "2026-02-30:2026-12-01"])
def test_impossible_calendar_dates_are_field_errors_not_crashes(window: str) -> None:
    spec = market_spec()
    spec["data"] = {
        "source": "market",
        "ref": "000001.SZ",
        "range": window,
    }
    # 不抛裸 ValueError，而是结构化错误回传模型
    payload = check_chart_spec(spec)
    assert payload["ok"] is False
    assert any(item["field"] == "$.data.range" for item in payload["errors"])
    with pytest.raises(ChartSpecError) as info:
        validate_chart_spec(spec)
    assert "$.data.range" in error_fields(info.value)


# ---------------------------------------------------------------- schema 封闭性（评审 P2）


def test_json_schema_closes_nested_items_and_whitelists_metrics() -> None:
    schema = chart_spec_json_schema()
    transforms = schema["properties"]["transforms"]["items"]
    layers = schema["properties"]["layers"]["items"]
    annotations = schema["properties"]["annotations"]["items"]
    assert transforms["additionalProperties"] is False
    assert layers["additionalProperties"] is False
    assert annotations["additionalProperties"] is False
    metric_schema = transforms["properties"]["metrics"]["additionalProperties"]
    assert metric_schema["enum"] == ["sum", "mean", "min", "max", "count", "last"]


def test_json_schema_pins_ref_and_range_shapes() -> None:
    schema = chart_spec_json_schema()
    data = schema["properties"]["data"]["properties"]
    assert len(data["ref"]["anyOf"]) == 3
    patterns = [item["pattern"] for item in data["ref"]["anyOf"]]
    assert any(r"art_" in pattern for pattern in patterns)
    range_one_of = data["range"]["oneOf"]
    assert any("1mo" in (item.get("enum") or []) for item in range_one_of)
    assert any(
        r"\d{4}-\d{2}-\d{2}" in (item.get("pattern") or "") for item in range_one_of
    )
    assert "1m" not in (range_one_of[0]["enum"])


def test_json_schema_uses_anyof_for_overlapping_ref_patterns() -> None:
    schema = chart_spec_json_schema()
    ref = schema["properties"]["data"]["properties"]["ref"]
    assert "oneOf" not in ref
    assert len(ref["anyOf"]) == 3
    patterns = [item["pattern"] for item in ref["anyOf"]]
    assert any(r"art_" in pattern for pattern in patterns)


def test_jsonschema_accepts_index_refs_and_rejects_old_aliases() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = chart_spec_json_schema()

    for ref in ("000001.SH", "399001.SZ", "000001.SZ", "830799.BJ"):
        jsonschema.validate(
            {
                "version": 1,
                "kind": "candlestick",
                "data": {"source": "market", "ref": ref},
                "encode": {"x": "trade_date"},
            },
            schema,
        )

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "version": 1,
                "kind": "candlestick",
                "data": {"source": "market", "ref": "000001.SZ"},
                "encode": {"x": "trade_date"},
                "panes": [],
            },
            schema,
        )

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "version": 1,
                "kind": "candlestick",
                "data": {"source": "market", "ref": "000001.SZ", "adj": "raw"},
                "encode": {"x": "trade_date"},
            },
            schema,
        )


def test_json_schema_no_longer_declares_compat_aliases() -> None:
    schema = chart_spec_json_schema()
    assert "panes" not in schema["properties"]
    assert "style" not in schema["properties"]
    assert schema["properties"]["data"]["properties"]["adj"]["enum"] == [
        "qfq",
        "hfq",
        "none",
    ]


def test_freeze_doc_records_alias_removal() -> None:
    from pathlib import Path

    doc = Path(__file__).resolve().parents[1] / "docs" / "AStockAgent_interface_freeze_v1.md"
    text = doc.read_text(encoding="utf-8")
    assert "别名已删除" in text
    assert "Q4=B" in text
    assert "panes" in text and "style" in text and "raw" in text


def test_freeze_v1_stays_v1_and_v2_lives_in_its_own_file() -> None:
    """冻结清单分版本：v1 正文保持 v1 口径，v2 口径只写在自己的文件里。

    2026-11-14 曾把 ``agent_artifact_versions``/``view_as_of``/``data_digest`` 等 v2 口径
    就地写进 v1 冻结清单，而当时的实现仍是 v1，导致「文档说 v2、代码是 v1」。
    2026-11-19（plan-20261114-002）v2 正式生效，但生效方式是**新增 v2 文件**，
    仍然不允许把 v2 条目塞回 v1 正文。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    v1 = (root / "docs" / "AStockAgent_interface_freeze_v1.md").read_text(encoding="utf-8")
    for marker in (
        "v2 覆盖",
        "agent_artifact_versions",
        "view_as_of",
        "data_digest",
        "已由",
    ):
        assert marker not in v1, marker
    # v1 仍然记录自己那套 meta（不许随着「去 v2」一起丢）
    assert "time_point_scope" in v1
    assert "png_path" in v1

    draft = (root / "docs" / "AStockAgent_interface_freeze_v2.md").read_text(encoding="utf-8")
    assert "plan-20261114-002" in draft
    assert "agent_artifact_versions" in draft
    assert "view_as_of" in draft and "view_time" in draft
    assert "chart_meta_v2" in draft
    assert "embed_html" in draft
    # 已生效：不再是「草案、未实现」
    assert "草案、未实现" not in draft


def test_freeze_v1_doc_documents_both_bbox_classes() -> None:
    """文件 bbox 的两类语义是 v1 已实现口径，回滚 v2 时不得退化成单一类型。

    评审 P2 ``iss-mwik3v22-213a0f``：上一轮按 556c8dc^ 恢复 v1 正文时把 §4 的
    「PDF 页面坐标 / 表格行列坐标」写成了单一 ``[x0,y0,x1,y1]``，并把已实现语义误标进
    v2 草案。这里同时锁 v1 的完整说明与 v2 草案不重复声明。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    v1 = (root / "docs" / "AStockAgent_interface_freeze_v1.md").read_text(encoding="utf-8")
    assert "[x0,y0,x1,y1] | [row_start,col_start,row_end,col_end] | null" in v1
    assert "row_start,col_start,row_end,col_end" in v1
    assert "x0,y0,x1,y1" in v1
    assert "pdfplumber" in v1

    draft = (root / "docs" / "AStockAgent_interface_freeze_v2.md").read_text(encoding="utf-8")
    assert "row_start" not in draft
    assert "x0,y0,x1,y1" not in draft


def test_freeze_v2_and_adr17_document_table_chart_contract() -> None:
    """table_chart 的权限/审计/引用/PNG 语义必须写进冻结文档与 ADR-017。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "AStockAgent_interface_freeze_v2.md").read_text(encoding="utf-8")
    assert "## 3.5 `table_chart` 表格图表契约" in doc
    for marker in (
        "file:read",
        "chart:render",
        "chart_spec",
        "png_path",
        "export_png=True",
        "agent_artifact_versions",
        "A1:B<n>",
        "xlsx-005",
        "allowed_paths",
        "png_mode",
        "(空)",
        "ASTOCK_AGENT_EXPORT_DIR",
        "/gradio_api/file=",
    ):
        assert marker in doc, marker

    adr = (
        root / ".devloop" / "architecture" / "adr" / "ADR-017-agent-audit-security.md"
    ).read_text(encoding="utf-8")
    assert "plan-20261124-001" in adr
    assert "table_chart" in adr
    assert "source='render'" in adr
    assert "png_mode=\"auto\"" in adr
    assert "agent_export_allowed_paths()" in adr
    assert "png_requested" in adr
    assert "(空)" in adr
    assert "ASTOCK_AGENT_EXPORT_DIR" in adr
    assert "test_gradio_serves_exported_html_and_png" in adr

