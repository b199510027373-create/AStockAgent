"""产品×论文协同契约的守护测试（plan-20261128-006）。

测两类东西：

1. **事实源自身**：manifest 结构合法、40 行方向表与 ``factor_def_v2.csv`` 一致、
   五个计划角色齐全、可引用/不可引用不交叠、交接文档覆盖 manifest 的全部条目 id。
2. **校验行为**：面板 sha256/行数/列/月份不一致要报错，方向表被改动要报错，
   论文数据未挂载时返回 ``unavailable`` 而不是伪造通过，CLI 退出码与 JSON 输出可用。

全部使用 tmp_path 造小面板，CI（Linux、无 D 盘）也能跑。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from astock import thesis_sync
from astock.thesis_sync import (
    BASE_FACTORS,
    REQUIRED_ROLES,
    CitationBlocked,
    ThesisSyncError,
    apply_plan_links,
    check_source,
    citation,
    direction_map,
    load_manifest,
    main,
    main_plan_links,
    merge_plan_links,
    sha256_file,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "docs" / "AStockAgent_thesis_product_handoff_v1.md"
PANEL_NAME = "panel_monthly_v2.parquet"

# 论文 ``factor_def_v2.csv`` 的 12 个基础方向（与 compose/types.py 的 DIRECTIONS 前 12 行一致）。
BASE_DIRECTIONS = {
    "momentum_20": 1.0,
    "momentum_60": 1.0,
    "reversal_5": 1.0,
    "volatility_20": -1.0,
    "volume_ratio": -1.0,
    "amount_ratio": -1.0,
    "amihud_20": -1.0,
    "ma_gap_20": 1.0,
    "rsi_14": -1.0,
    "macd_hist": 1.0,
    "atr_pct": -1.0,
    "turnover_20": -1.0,
}


@pytest.fixture()
def manifest() -> dict[str, Any]:
    return load_manifest()


def _write_direction_csv(path: Path, rows: dict[str, float]) -> None:
    pd.DataFrame({"factor": list(rows), "direction": list(rows.values())}).to_csv(
        path, index=False, encoding="utf-8-sig"
    )


def _fixture(tmp_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """在 ``tmp_path`` 造一份与 manifest 自洽的小面板 + 两份方向表。"""

    payload = copy.deepcopy(manifest)
    panel = payload["panel"]
    columns = list(panel["required_columns"])
    frame = pd.DataFrame(
        {
            "date": ["2025-01-31", "2025-01-31", "2025-02-28", "2025-02-28"],
            "code": ["000001.SZ", "600000.SH", "000001.SZ", "600000.SH"],
        }
    )
    for name in columns:
        if name not in frame.columns:
            frame[name] = 0.0
    panel_path = tmp_path / PANEL_NAME
    frame.to_parquet(panel_path, index=False)

    panel.update(
        {
            "root": str(tmp_path),
            "rows": len(frame),
            "columns": len(frame.columns),
            "codes": int(frame["code"].nunique()),
            "months": int(frame["date"].nunique()),
            "start": "2025-01-31",
            "end": "2025-02-28",
            "sha256": sha256_file(panel_path),
        }
    )

    table = payload["direction_table"]
    factor_def = {**table["base"], **table["supplement"]}
    _write_direction_csv(tmp_path / "factor_def_v2.csv", factor_def)
    export_path = tmp_path / "factor_directions.csv"
    _write_direction_csv(export_path, direction_map(payload))
    table["export_paths"] = [str(export_path)]
    return payload


def test_manifest_is_valid(manifest: dict[str, Any]) -> None:
    assert validate_manifest(manifest) == ()


def test_manifest_covers_the_five_roles(manifest: dict[str, Any]) -> None:
    assert set(manifest["roles"]) == set(REQUIRED_ROLES)
    assert manifest["conflict_rule"]["authority"] == "thesis-caliber"
    assert manifest["panel"]["id"] == "panel_monthly_v2"
    assert manifest["panel"]["sha256"].startswith("28d06335")


def test_direction_table_matches_factor_def(manifest: dict[str, Any]) -> None:
    table = manifest["direction_table"]
    assert len(direction_map(manifest)) == 40
    assert table["base"] == BASE_DIRECTIONS
    assert {name: table["supplement"][name] for name in table["supplement"]} == {
        "amount_m": -1.0,
        "ret_std_d": -1.0,
        "free_float_mv": -1.0,
    }
    # 模块里钉住的基础因子清单必须与 manifest 一致，避免两边各改各的。
    assert tuple(table["base"]) == BASE_FACTORS


def test_validate_rejects_invalid_direction_value(manifest: dict[str, Any]) -> None:
    payload = copy.deepcopy(manifest)
    payload["direction_table"]["base"]["volatility_20"] = 0.5
    errors = validate_manifest(payload)
    assert any("方向只能是 +1/-1" in error for error in errors)


def test_validate_rejects_overlapping_citations(manifest: dict[str, Any]) -> None:
    payload = copy.deepcopy(manifest)
    payload["citable"].append(copy.deepcopy(payload["not_citable"][0]))
    errors = validate_manifest(payload)
    assert any("不得重复" in error for error in errors)


def test_validate_rejects_citable_without_evidence(manifest: dict[str, Any]) -> None:
    payload = copy.deepcopy(manifest)
    payload["citable"][0]["evidence"] = ""
    assert any("evidence" in error for error in validate_manifest(payload))


def test_validate_rejects_missing_role(manifest: dict[str, Any]) -> None:
    payload = copy.deepcopy(manifest)
    del payload["roles"]["product-014"]
    assert any("roles 必须恰好包含" in error for error in validate_manifest(payload))


def test_validate_rejects_direction_row_count(manifest: dict[str, Any]) -> None:
    payload = copy.deepcopy(manifest)
    payload["direction_table"]["derived"].pop("circ_ratio")
    assert any("28 行" in error for error in validate_manifest(payload))


def test_citation_rules(manifest: dict[str, Any]) -> None:
    item = citation(manifest, "panel-v2-caliber")
    assert item["owner"] == "thesis-005"

    with pytest.raises(CitationBlocked) as blocked:
        citation(manifest, "lgbm-22pct")
    assert "缺失收益口径" in str(blocked.value)

    # product_only 只在产品受众可见；写论文时必须被拒。
    assert "product-observation" in citation(manifest, "live-sample-observation")["label"]
    with pytest.raises(CitationBlocked):
        citation(manifest, "live-sample-observation", audience="thesis")

    with pytest.raises(ThesisSyncError):
        citation(manifest, "no-such-id")


def test_load_manifest_rejects_broken_files(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ThesisSyncError):
        load_manifest(broken)
    with pytest.raises(ThesisSyncError):
        load_manifest(tmp_path / "missing.json")


def test_check_source_unavailable_without_panel(manifest: dict[str, Any], tmp_path: Path) -> None:
    result = check_source(manifest, data_root=tmp_path)
    assert result.status == "unavailable"
    assert not result.ok
    assert any(PANEL_NAME in warning for warning in result.warnings)


def test_check_source_ok_with_fixture(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "ok", result.errors
    assert result.facts["rows"] == 4
    assert result.facts["columns"] == 24
    assert result.facts["months"] == 2
    assert result.facts["sha256"] == payload["panel"]["sha256"]


def test_check_source_detects_hash_mismatch(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    payload["panel"]["sha256"] = "0" * 64
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "invalid"
    assert any("sha256" in error for error in result.errors)


def test_check_source_detects_row_mismatch(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    payload["panel"]["rows"] = 5
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "invalid"
    assert any("行数" in error for error in result.errors)


def test_check_source_detects_month_range_mismatch(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    payload["panel"]["start"] = "2024-01-31"
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "invalid"
    assert any("区间" in error for error in result.errors)


def test_check_source_detects_exported_direction_change(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    rows = direction_map(payload)
    rows["volatility_20"] = 1.0
    _write_direction_csv(tmp_path / "factor_directions.csv", rows)
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "invalid"
    assert any("volatility_20" in error for error in result.errors)


def test_check_source_detects_copy_drift(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    stale = tmp_path / "stale_copy.csv"
    rows = direction_map(payload)
    rows["turnover_20"] = 1.0
    _write_direction_csv(stale, rows)
    payload["direction_table"]["export_paths"].append(str(stale))
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "invalid"
    assert any("turnover_20" in error for error in result.errors)
    assert result.facts["direction_exports"] == [str(tmp_path / "factor_directions.csv"), str(stale)]


def test_check_source_detects_factor_def_change(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    factor_def = {**payload["direction_table"]["base"], **payload["direction_table"]["supplement"]}
    factor_def["reversal_5"] = -1.0
    _write_direction_csv(tmp_path / "factor_def_v2.csv", factor_def)
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "invalid"
    assert any("factor_def_v2.csv" in error for error in result.errors)


def test_check_source_warns_when_optional_tables_absent(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = _fixture(tmp_path, manifest)
    (tmp_path / "factor_def_v2.csv").unlink()
    payload["direction_table"]["export_paths"] = [str(tmp_path / "not-exported.csv")]
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "ok"
    assert len(result.warnings) == 2


def test_check_source_rejects_invalid_manifest(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = copy.deepcopy(manifest)
    payload["schema_version"] = 2
    result = check_source(payload, data_root=tmp_path)
    assert result.status == "invalid"
    assert result.facts == {}


def test_cli_exit_codes(manifest: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = _fixture(tmp_path, manifest)
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert main(["--manifest", str(manifest_file), "--data-root", str(tmp_path)]) == 0
    assert "OK: 产品与论文引用同一" in capsys.readouterr().out

    assert main(["--manifest", str(manifest_file), "--data-root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--manifest", str(manifest_file), "--data-root", str(empty)]) == 2

    payload["panel"]["rows"] = 9
    manifest_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert main(["--manifest", str(manifest_file), "--data-root", str(tmp_path)]) == 1


def test_cli_reports_broken_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    assert main(["--manifest", str(broken)]) == 1
    assert "ERROR" in capsys.readouterr().err


def test_handoff_doc_covers_every_manifest_item(manifest: dict[str, Any]) -> None:
    text = HANDOFF.read_text(encoding="utf-8")
    for item in manifest["citable"] + manifest["product_only"] + manifest["not_citable"]:
        assert f"`{item['id']}`" in text, item["id"]
    for role in manifest["roles"].values():
        assert role["plan"] in text
    for gate in manifest["pending_gates"]:
        assert gate["id"] in text
    assert "thesis_product_fact_source_v1.json" in text
    assert "panel_monthly_v2" in text
    assert "product-observation" in text
    assert "tools/check_thesis_sync.py" in text


def test_manifest_and_module_agree_on_paths() -> None:
    assert (ROOT / thesis_sync.MANIFEST_RELATIVE).is_file()
    assert (ROOT / thesis_sync.HANDOFF_RELATIVE).is_file()
    assert thesis_sync.manifest_path() == ROOT / thesis_sync.MANIFEST_RELATIVE


def _plan_root(tmp_path: Path, ids: tuple[str, ...] = ("plan-20261123-005", "plan-20261123-006")) -> Path:
    items = tmp_path / ".devloop" / "plan" / "items"
    items.mkdir(parents=True)
    for plan_id in ids:
        (items / f"{plan_id}.json").write_text(
            json.dumps({"id": plan_id, "status": "todo", "links": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
    return tmp_path


def test_merge_plan_links_only_adds() -> None:
    merged, added, conflicts = merge_plan_links({"docs": ["a.md"]}, {"fact_source": "f.json", "docs": ["a.md"]})
    assert conflicts == []
    assert merged == {"docs": ["a.md"], "fact_source": "f.json"}
    assert added == {"fact_source": "f.json"}


def test_merge_plan_links_reports_conflict() -> None:
    merged, added, conflicts = merge_plan_links({"fact_source": "old.json"}, {"fact_source": "new.json"})
    assert merged == {"fact_source": "old.json"}
    assert added == {}
    assert conflicts and "已存在且不同" in conflicts[0]


def test_apply_plan_links_writes_links_and_backup(manifest: dict[str, Any], tmp_path: Path) -> None:
    root = _plan_root(tmp_path)
    patches = apply_plan_links(root, manifest=manifest)
    assert [patch.plan_id for patch in patches] == ["plan-20261123-005", "plan-20261123-006"]
    for patch in patches:
        assert patch.conflicts == ()
        assert patch.changed is True
        assert {key for key, _ in patch.added} == {"fact_source", "handoff", "sync_check", "data", "counterpart_plan"}
        payload = json.loads(patch.path.read_text(encoding="utf-8"))
        assert payload["links"]["counterpart_plan"] == "plan-20261128-006"
        assert payload["status"] == "todo"
        assert patch.backup is not None and patch.backup.is_file()
        assert json.loads(patch.backup.read_text(encoding="utf-8"))["links"] == {}


def test_apply_plan_links_is_idempotent(manifest: dict[str, Any], tmp_path: Path) -> None:
    root = _plan_root(tmp_path)
    apply_plan_links(root, manifest=manifest)
    patches = apply_plan_links(root, manifest=manifest)
    assert all(not patch.changed and patch.conflicts == () for patch in patches)
    assert len((root / ".devloop" / "plan" / "items" / "plan-20261123-005.json.bak").read_text("utf-8")) > 0


def test_apply_plan_links_dry_run_does_not_write(manifest: dict[str, Any], tmp_path: Path) -> None:
    root = _plan_root(tmp_path)
    patches = apply_plan_links(root, manifest=manifest, dry_run=True)
    assert all(patch.changed and patch.backup is None for patch in patches)
    payload = json.loads(patches[0].path.read_text(encoding="utf-8"))
    assert payload["links"] == {}
    assert not list(patches[0].path.parent.glob("*.bak"))


def test_apply_plan_links_reports_missing_plan_file(manifest: dict[str, Any], tmp_path: Path) -> None:
    root = _plan_root(tmp_path, ids=("plan-20261123-005",))
    patches = apply_plan_links(root, manifest=manifest)
    missing = [patch for patch in patches if patch.plan_id == "plan-20261123-006"]
    assert missing and "不存在" in missing[0].conflicts[0]


def test_apply_plan_links_requires_patch_section(manifest: dict[str, Any], tmp_path: Path) -> None:
    payload = copy.deepcopy(manifest)
    payload.pop("thesis_link_patch")
    with pytest.raises(ThesisSyncError, match="thesis_link_patch"):
        apply_plan_links(tmp_path, manifest=payload)


def test_plan_links_cli(manifest: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _plan_root(tmp_path)
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    assert main_plan_links(["--root", str(root), "--manifest", str(manifest_file), "--dry-run"]) == 0
    assert "待写入 plan-20261123-005" in capsys.readouterr().out

    assert main_plan_links(["--root", str(root), "--manifest", str(manifest_file), "--json"]) == 0
    patched = json.loads(capsys.readouterr().out)
    assert [item["plan_id"] for item in patched] == ["plan-20261123-005", "plan-20261123-006"]
    assert all(item["changed"] for item in patched)

    assert main_plan_links(["--root", str(tmp_path / "nowhere"), "--manifest", str(manifest_file)]) == 1


def test_plan_links_cli_reports_broken_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("[]", encoding="utf-8")
    assert main_plan_links(["--root", str(tmp_path), "--manifest", str(broken)]) == 1
    assert "ERROR" in capsys.readouterr().err
