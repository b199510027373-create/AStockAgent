"""产品 × 论文协同事实源：读取 manifest、校验面板/方向表、裁决引用（plan-20261128-006）。

为什么需要这个模块
------------------
论文（plan-20261123-005/006）与产品（plan-20261128-002/003/014）必须引用同一份
``panel_monthly_v2`` 与同一张因子方向表。此前产品仓库没有任何代码读取这两样东西，
口径只靠文档口头约定，冲突时无法判断谁对。本模块把约定变成可执行检查：

* 唯一的机器可读事实源是 ``docs/thesis_product_fact_source_v1.json``；
* ``check_source()`` 校验面板 sha256 / 行数 / 列数 / 月份起止、12 个基础方向与
  ``factor_def_v2.csv`` 是否一致、40 行方向表与导出 CSV 是否一致；
* 论文数据目录未挂载时返回 ``unavailable``（环境事实），不伪造通过；
* ``citation()`` 让“可引用 / 不可引用 / 仅产品口径”变成可调用接口，避免把
  ``not_citable`` 里的说法写进产品文案。

本模块不联网、不构建面板、不重跑实证；没有论文数据时只校验 manifest 自身。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astock.config import BASE_DIR

MANIFEST_RELATIVE = Path("docs") / "thesis_product_fact_source_v1.json"
HANDOFF_RELATIVE = Path("docs") / "AStockAgent_thesis_product_handoff_v1.md"
PLAN_ITEMS_RELATIVE = Path(".devloop") / "plan" / "items"

#: 与论文 compose/types.py 的 BASE_FACTORS 保持一致；manifest 的 base 键必须与此相同。
BASE_FACTORS: tuple[str, ...] = (
    "momentum_20",
    "momentum_60",
    "reversal_5",
    "volatility_20",
    "volume_ratio",
    "amount_ratio",
    "amihud_20",
    "ma_gap_20",
    "rsi_14",
    "macd_hist",
    "atr_pct",
    "turnover_20",
)
SUPPLEMENT_FIELDS: tuple[str, ...] = ("amount_m", "ret_std_d", "free_float_mv")
REQUIRED_ROLES: tuple[str, ...] = (
    "thesis-005",
    "thesis-006",
    "product-002",
    "product-003",
    "product-014",
)
DIRECTION_ROWS = 40
VALID_DIRECTIONS = frozenset({-1.0, 1.0})
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ThesisSyncError(RuntimeError):
    """事实源缺失、结构非法或数据与 manifest 不一致。"""


class CitationBlocked(ThesisSyncError):
    """引用被 manifest 明确禁止（``not_citable`` 或受众不匹配的 ``product_only``）。"""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"不允许引用 {key!r}：{reason}")
        self.key = key
        self.reason = reason


@dataclass(frozen=True)
class CheckResult:
    """一次事实源检查的结果。``status`` 取 ``ok`` / ``unavailable`` / ``invalid``。"""

    status: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def manifest_path(root: Path | None = None) -> Path:
    """返回事实源 manifest 路径；``root`` 默认为仓库根。"""

    return (Path(root) if root is not None else BASE_DIR) / MANIFEST_RELATIVE


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    """读取并解析 manifest（不校验内容，校验见 :func:`validate_manifest`）。"""

    resolved = Path(path) if path is not None else manifest_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ThesisSyncError(f"无法读取事实源 {resolved}：{exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ThesisSyncError(f"事实源 {resolved} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ThesisSyncError(f"事实源 {resolved} 顶层必须是 JSON 对象")
    return payload


def _role_ids(manifest: dict[str, Any]) -> set[str]:
    roles = manifest.get("roles")
    return set(roles) if isinstance(roles, dict) else set()


def validate_manifest(manifest: dict[str, Any]) -> tuple[str, ...]:
    """校验 manifest 结构与内部一致性，返回错误列表（空表示通过）。"""

    errors: list[str] = []

    if manifest.get("schema_version") != 1:
        errors.append("schema_version 必须为 1")
    version = manifest.get("version")
    if not isinstance(version, str) or not _SEMVER.match(version):
        errors.append(f"version 必须是 x.y.z：{version!r}")
    if manifest.get("status") not in {"draft", "v1-frozen"}:
        errors.append(f"status 必须是 draft / v1-frozen：{manifest.get('status')!r}")

    conflict = manifest.get("conflict_rule")
    if not isinstance(conflict, dict):
        errors.append("conflict_rule 缺失")
    else:
        if conflict.get("authority") != "thesis-caliber":
            errors.append("conflict_rule.authority 必须是 thesis-caliber（冲突以论文口径为准）")
        statement = conflict.get("statement")
        if not isinstance(statement, str) or len(statement) < 10:
            errors.append("conflict_rule.statement 缺失或过短")

    errors.extend(_validate_panel(manifest.get("panel")))
    errors.extend(_validate_directions(manifest.get("direction_table")))
    errors.extend(_validate_roles(manifest.get("roles")))
    errors.extend(_validate_gates(manifest.get("pending_gates")))

    citable = manifest.get("citable")
    product_only = manifest.get("product_only")
    not_citable = manifest.get("not_citable")
    cited_ids = _validate_citations("citable", citable, errors, require_evidence=True)
    product_ids = _validate_citations("product_only", product_only, errors, require_evidence=True)
    blocked_ids = _validate_citations("not_citable", not_citable, errors, require_evidence=False)

    overlap = (cited_ids & blocked_ids) | (product_ids & blocked_ids) | (cited_ids & product_ids)
    if overlap:
        errors.append(f"citable / product_only / not_citable 的 id 不得重复：{sorted(overlap)}")

    patch = manifest.get("thesis_link_patch")
    if not isinstance(patch, dict) or not isinstance(patch.get("note"), str):
        errors.append("thesis_link_patch 缺失或缺少 note")
    return tuple(errors)


def _validate_panel(panel: Any) -> list[str]:
    if not isinstance(panel, dict):
        return ["panel 缺失"]
    errors: list[str] = []
    for key in ("id", "contract", "root", "file", "sha256", "start", "end", "gate"):
        if not isinstance(panel.get(key), str) or not panel[key]:
            errors.append(f"panel.{key} 必须是非空字符串")
    sha = panel.get("sha256")
    if isinstance(sha, str) and not _SHA256.match(sha):
        errors.append("panel.sha256 必须是 64 位小写十六进制")
    for key in ("rows", "columns", "codes", "months"):
        value = panel.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"panel.{key} 必须是正整数")
    for key in ("start", "end"):
        value = panel.get(key)
        if isinstance(value, str) and not _DATE.match(value):
            errors.append(f"panel.{key} 必须是 YYYY-MM-DD")
    required = panel.get("required_columns")
    if not isinstance(required, list) or not all(isinstance(x, str) for x in required):
        errors.append("panel.required_columns 必须是字符串列表")
    else:
        missing = [name for name in BASE_FACTORS if name not in required]
        if missing:
            errors.append(f"panel.required_columns 缺少基础因子：{missing}")
    if panel.get("gate") != "passed":
        errors.append(f"panel.gate 必须是 passed：{panel.get('gate')!r}")
    return errors


def _direction_map(table: Any) -> dict[str, float]:
    """合并 base + derived（40 行模型特征）；supplement 是面板补充字段，不属于模型表。"""

    merged: dict[str, float] = {}
    for section in ("base", "derived"):
        block = table.get(section) if isinstance(table, dict) else None
        if not isinstance(block, dict):
            continue
        for name, value in block.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            merged[str(name)] = float(value)
    return merged


def _validate_directions(table: Any) -> list[str]:
    if not isinstance(table, dict):
        return ["direction_table 缺失"]
    errors: list[str] = []
    base = table.get("base")
    derived = table.get("derived")
    if not isinstance(base, dict) or set(base) != set(BASE_FACTORS):
        errors.append(f"direction_table.base 必须恰好是 12 个基础因子：{sorted(BASE_FACTORS)}")
    if not isinstance(derived, dict) or len(derived) != DIRECTION_ROWS - len(BASE_FACTORS):
        errors.append("direction_table.derived 必须恰好 28 行")
    supplement = table.get("supplement")
    if not isinstance(supplement, dict) or set(supplement) != set(SUPPLEMENT_FIELDS):
        errors.append(f"direction_table.supplement 必须恰好是 {list(SUPPLEMENT_FIELDS)}")
    if table.get("rows") != DIRECTION_ROWS:
        errors.append(f"direction_table.rows 必须是 {DIRECTION_ROWS}")
    export_paths = table.get("export_paths")
    if not isinstance(export_paths, list) or not export_paths or not all(isinstance(p, str) and p for p in export_paths):
        errors.append("direction_table.export_paths 必须是非空路径列表（可用副本按优先级排列）")

    merged: dict[str, float] = {}
    for section in ("base", "derived", "supplement"):
        block = table.get(section)
        if not isinstance(block, dict):
            continue
        for name, value in block.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"direction_table.{section}.{name} 必须是数值")
                continue
            merged[str(name)] = float(value)

    model_rows = _direction_map(table)
    if len(model_rows) != DIRECTION_ROWS:
        errors.append(f"方向表合并后必须 {DIRECTION_ROWS} 行，实际 {len(model_rows)}")
    bad = {name: value for name, value in merged.items() if value not in VALID_DIRECTIONS}
    if bad:
        errors.append(f"方向只能是 +1/-1：{bad}")
    return errors


def _validate_roles(roles: Any) -> list[str]:
    if not isinstance(roles, dict):
        return ["roles 缺失"]
    errors: list[str] = []
    if set(roles) != set(REQUIRED_ROLES):
        errors.append(f"roles 必须恰好包含 {list(REQUIRED_ROLES)}，实际 {sorted(roles)}")
    for role_id, role in roles.items():
        if not isinstance(role, dict):
            errors.append(f"roles.{role_id} 必须是对象")
            continue
        if role.get("owner") not in {"thesis", "product"}:
            errors.append(f"roles.{role_id}.owner 必须是 thesis / product")
        plan = role.get("plan")
        if not isinstance(plan, str) or not plan.startswith("plan-"):
            errors.append(f"roles.{role_id}.plan 必须是 plan-* id")
        for key in ("inputs", "outputs", "forbidden"):
            value = role.get(key)
            if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
                errors.append(f"roles.{role_id}.{key} 必须是非空字符串列表")
    return errors


def _validate_gates(gates: Any) -> list[str]:
    if not isinstance(gates, list) or not gates:
        return ["pending_gates 必须是非空列表"]
    errors: list[str] = []
    for idx, gate in enumerate(gates):
        if not isinstance(gate, dict):
            errors.append(f"pending_gates[{idx}] 必须是对象")
            continue
        for key in ("id", "blocks", "until", "fallback"):
            if not isinstance(gate.get(key), str) or not gate[key]:
                errors.append(f"pending_gates[{idx}].{key} 必须是非空字符串")
        if gate.get("status") not in {"open", "closed", "watch"}:
            errors.append(f"pending_gates[{idx}].status 必须是 open / closed / watch")
    return errors


def _validate_citations(
    section: str,
    items: Any,
    errors: list[str],
    *,
    require_evidence: bool,
) -> set[str]:
    if not isinstance(items, list) or not items:
        errors.append(f"{section} 必须是非空列表")
        return set()
    ids: set[str] = set()
    roles = set(REQUIRED_ROLES)
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{section}[{idx}] 必须是对象")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            errors.append(f"{section}[{idx}].id 必须是非空字符串")
            continue
        if item_id in ids:
            errors.append(f"{section} 出现重复 id：{item_id}")
        ids.add(item_id)
        if require_evidence:
            for key in ("statement", "label", "evidence"):
                if not isinstance(item.get(key), str) or not item[key]:
                    errors.append(f"{section}.{item_id}.{key} 必须是非空字符串")
            if item.get("owner") not in roles:
                errors.append(f"{section}.{item_id}.owner 必须是 roles 中的角色")
        else:
            if not isinstance(item.get("statement"), str) or not item["statement"]:
                errors.append(f"{section}.{item_id}.statement 必须是非空字符串")
            for key in ("reason", "instead"):
                if not isinstance(item.get(key), str) or not item[key]:
                    errors.append(f"{section}.{item_id}.{key} 必须是非空字符串")
    return ids


def direction_map(manifest: dict[str, Any]) -> dict[str, float]:
    """返回合并后的 40 行方向表（base + derived + supplement）。"""

    table = manifest.get("direction_table")
    if not isinstance(table, dict):
        raise ThesisSyncError("manifest 缺少 direction_table")
    return _direction_map(table)


def citation(manifest: dict[str, Any], key: str, *, audience: str = "product") -> dict[str, Any]:
    """按 id 取一条可引用结论；命中 ``not_citable`` 或受众不符时抛 :class:`CitationBlocked`。

    ``audience="thesis"`` 时 ``product_only`` 条目也会被拒——产品样本不得进入论文实证。
    """

    for item in manifest.get("citable") or []:
        if isinstance(item, dict) and item.get("id") == key:
            return item
    for item in manifest.get("product_only") or []:
        if isinstance(item, dict) and item.get("id") == key:
            if audience == "thesis":
                raise CitationBlocked(key, str(item.get("label") or "仅产品口径，不得写入论文"))
            return item
    for item in manifest.get("not_citable") or []:
        if isinstance(item, dict) and item.get("id") == key:
            reason = str(item.get("reason") or "manifest 禁止引用")
            instead = item.get("instead")
            if isinstance(instead, str) and instead:
                reason = f"{reason}；改用：{instead}"
            raise CitationBlocked(key, reason)
    raise ThesisSyncError(f"事实源没有条目 {key!r}")


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_direction_csv(path: Path) -> dict[str, float]:
    """读取 ``factor`` / ``direction`` 两列的方向表（兼容 UTF-8 BOM）。"""

    import pandas as pd

    frame = pd.read_csv(path, encoding="utf-8-sig")
    columns = {str(col).strip().lower(): col for col in frame.columns}
    if "factor" not in columns or "direction" not in columns:
        raise ThesisSyncError(f"{path} 缺少 factor / direction 列：{list(frame.columns)}")
    frame = frame.rename(columns={columns["factor"]: "factor", columns["direction"]: "direction"})
    frame["factor"] = frame["factor"].astype(str).str.strip()
    frame["direction"] = pd.to_numeric(frame["direction"], errors="coerce")
    if frame["direction"].isna().any():
        raise ThesisSyncError(f"{path} 的 direction 存在非数值")
    duplicated = frame["factor"][frame["factor"].duplicated()].tolist()
    if duplicated:
        raise ThesisSyncError(f"{path} 的方向表出现重复因子：{duplicated}")
    return {str(row.factor): float(row.direction) for row in frame.itertuples(index=False)}


def _compare_directions(
    actual: dict[str, float],
    expected: dict[str, float],
    *,
    source: str,
    errors: list[str],
    only_expected: bool = False,
) -> None:
    names = set(expected) if only_expected else set(expected) | set(actual)
    for name in sorted(names):
        want = expected.get(name)
        got = actual.get(name)
        if want is None:
            errors.append(f"{source} 多出未登记因子 {name}={got}")
        elif got is None:
            errors.append(f"{source} 缺少因子 {name}")
        elif abs(float(got) - float(want)) > 1e-9:
            errors.append(f"{source} 的 {name} 方向为 {got}，manifest 为 {want}")


def _inspect_panel(path: Path, panel: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    """校验面板文件本身；返回实测 facts（仅在无错误时可信）。"""

    import pyarrow.parquet as pq

    facts: dict[str, Any] = {"path": str(path)}
    try:
        parquet = pq.ParquetFile(path)
        names = list(parquet.schema_arrow.names)
        rows = int(parquet.metadata.num_rows)
    except (OSError, ValueError) as exc:
        errors.append(f"面板无法读取：{exc}")
        return facts

    facts.update({"rows": rows, "columns": len(names)})
    if rows != panel.get("rows"):
        errors.append(f"面板行数 {rows} != manifest {panel.get('rows')}")
    if len(names) != panel.get("columns"):
        errors.append(f"面板列数 {len(names)} != manifest {panel.get('columns')}")

    required = [name for name in panel.get("required_columns") or [] if isinstance(name, str)]
    missing = [name for name in required if name not in names]
    if missing:
        errors.append(f"面板缺少必需列：{missing}")

    if "date" not in names or "code" not in names:
        errors.append("面板缺少 date / code，无法核对月份与股票数")
        return facts

    frame = pq.read_table(path, columns=["date", "code"]).to_pandas()
    frame["date"] = frame["date"].astype(str).str.slice(0, 10)
    codes = int(frame["code"].nunique())
    months = int(frame["date"].nunique())
    start = str(frame["date"].min())
    end = str(frame["date"].max())
    facts.update({"codes": codes, "months": months, "start": start, "end": end})
    if codes != panel.get("codes"):
        errors.append(f"面板股票数 {codes} != manifest {panel.get('codes')}")
    if months != panel.get("months"):
        errors.append(f"面板月份数 {months} != manifest {panel.get('months')}")
    if start != panel.get("start") or end != panel.get("end"):
        errors.append(f"面板区间 {start}..{end} != manifest {panel.get('start')}..{panel.get('end')}")
    return facts


def check_source(
    manifest: dict[str, Any] | None = None,
    *,
    data_root: Path | None = None,
) -> CheckResult:
    """校验 manifest 与论文侧数据产物。

    * manifest 结构非法 → ``invalid``；
    * 面板文件不存在（论文目录未挂载）→ ``unavailable``，附路径与原因；
    * 面板存在但 hash/行数/列/区间/方向表不一致 → ``invalid``；
    * 全部一致 → ``ok``。
    """

    payload = load_manifest() if manifest is None else manifest
    errors = list(validate_manifest(payload))
    warnings: list[str] = []
    facts: dict[str, Any] = {}
    if errors:
        return CheckResult("invalid", tuple(errors), tuple(warnings), facts)

    panel = payload["panel"]
    root = Path(data_root) if data_root is not None else Path(str(panel["root"]))
    panel_path = root / str(panel["file"])
    facts.update({"manifest_version": payload.get("version"), "panel_root": str(root)})
    if not panel_path.exists():
        return CheckResult(
            "unavailable",
            (),
            (f"面板不存在：{panel_path}（论文数据目录未挂载，跳过数据核对）",),
            facts,
        )

    digest = sha256_file(panel_path)
    facts["sha256"] = digest
    if digest != panel.get("sha256"):
        errors.append(f"面板 sha256 {digest} != manifest {panel.get('sha256')}")
    facts.update(_inspect_panel(panel_path, panel, errors))

    expected_directions = direction_map(payload)
    factor_def = root / "factor_def_v2.csv"
    if factor_def.exists():
        try:
            base_actual = _read_direction_csv(factor_def)
        except ThesisSyncError as exc:
            errors.append(str(exc))
        else:
            table = payload["direction_table"]
            base_expected = {name: float(value) for name, value in table["base"].items()}
            supplement_expected = {name: float(v) for name, v in table["supplement"].items()}
            _compare_directions(base_actual, {**base_expected, **supplement_expected}, source="factor_def_v2.csv", errors=errors)
    else:
        warnings.append(f"未找到 {factor_def}，跳过基础方向核对")

    direction_table = payload["direction_table"]
    export_paths = [Path(str(item)) for item in direction_table.get("export_paths") or []]
    checked_exports: list[str] = []
    for export_path in export_paths:
        if not export_path.is_file():
            continue
        try:
            exported = _read_direction_csv(export_path)
        except ThesisSyncError as exc:
            errors.append(str(exc))
            continue
        checked_exports.append(str(export_path))
        _compare_directions(exported, expected_directions, source=export_path.name, errors=errors)
    facts["direction_exports"] = checked_exports
    if not checked_exports:
        warnings.append("未找到任何导出方向表副本，跳过 40 行核对（方向只由 manifest 与 factor_def_v2.csv 约束）")

    status = "invalid" if errors else "ok"
    return CheckResult(status, tuple(errors), tuple(warnings), facts)


def _render(result: CheckResult, *, as_json: bool) -> str:
    if as_json:
        payload = {
            "status": result.status,
            "ok": result.ok,
            "errors": list(result.errors),
            "warnings": list(result.warnings),
            "facts": result.facts,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    lines = [f"状态: {result.status}"]
    for key, value in sorted(result.facts.items()):
        lines.append(f"  {key}: {value}")
    for warning in result.warnings:
        lines.append(f"WARN: {warning}")
    for error in result.errors:
        lines.append(f"ERROR: {error}")
    if result.status == "ok":
        lines.append("OK: 产品与论文引用同一 panel v2 / 方向表 / 事实源")
    elif result.status == "unavailable":
        lines.append("SKIP: 论文数据未挂载；manifest 自身校验通过")
    return "\n".join(lines)


@dataclass(frozen=True)
class PlanPatch:
    """一次论文侧计划项 links 合并的结果。``conflicts`` 非空即失败。"""

    plan_id: str
    path: Path
    added: tuple[tuple[str, Any], ...] = ()
    conflicts: tuple[str, ...] = ()
    changed: bool = False
    backup: Path | None = None


def merge_plan_links(
    existing: dict[str, Any],
    wanted: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """只增不改地合并计划项 links；返回 (合并结果, 新增项, 冲突说明)。"""

    merged = dict(existing)
    added: dict[str, Any] = {}
    conflicts: list[str] = []
    for key, value in wanted.items():
        if key in merged:
            if merged[key] != value:
                conflicts.append(f"links.{key} 已存在且不同：{merged[key]!r} != {value!r}")
            continue
        merged[key] = value
        added[key] = value
    return merged, added, conflicts


def apply_plan_links(
    root: Path | str,
    *,
    manifest: dict[str, Any] | None = None,
    dry_run: bool = False,
    backup_suffix: str = ".bak",
) -> tuple[PlanPatch, ...]:
    """把 manifest 的 ``thesis_link_patch`` 合并进论文侧计划项（只增不改，先备份）。

    论文侧 ``.devloop`` 不在本仓库，且可能对产品会话只读；函数返回每个计划项的
    结果（新增键、冲突、是否写入、备份路径），由调用方决定打印与退出码。
    """

    payload = load_manifest() if manifest is None else manifest
    patch = payload.get("thesis_link_patch")
    if not isinstance(patch, dict):
        raise ThesisSyncError("manifest 缺少 thesis_link_patch 段落")
    items_dir = Path(root) / PLAN_ITEMS_RELATIVE
    results: list[PlanPatch] = []
    for plan_id in sorted(patch):
        spec = patch[plan_id]
        path = items_dir / f"{plan_id}.json"
        if not plan_id.startswith("plan-") or not isinstance(spec, dict):
            continue
        wanted = spec.get("links")
        if not isinstance(wanted, dict) or not wanted:
            results.append(PlanPatch(plan_id, path, (), ("thesis_link_patch 的 links 必须是非空对象",), False, None))
            continue
        if not path.is_file():
            results.append(PlanPatch(plan_id, path, (), (f"计划项文件不存在：{path}",), False, None))
            continue
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            results.append(PlanPatch(plan_id, path, (), (f"计划项无法解析：{exc}",), False, None))
            continue
        if not isinstance(current, dict):
            results.append(PlanPatch(plan_id, path, (), ("计划项顶层必须是 JSON 对象",), False, None))
            continue
        links = current.get("links")
        if links is None:
            links = {}
        if not isinstance(links, dict):
            results.append(PlanPatch(plan_id, path, (), ("links 必须是 JSON 对象",), False, None))
            continue
        merged, added, conflicts = merge_plan_links(links, wanted)
        if conflicts or not added:
            results.append(PlanPatch(plan_id, path, (), tuple(conflicts), False, None))
            continue
        backup: Path | None = None
        if not dry_run:
            try:
                backup = path.with_name(path.name + backup_suffix)
                if not backup.exists():
                    shutil.copy2(path, backup)
                current["links"] = merged
                path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                results.append(PlanPatch(plan_id, path, (), (f"写入失败：{exc}",), False, None))
                continue
        results.append(PlanPatch(plan_id, path, tuple(added.items()), (), True, backup))
    return tuple(results)


def _render_plan_patches(patches: tuple[PlanPatch, ...], *, as_json: bool, dry_run: bool) -> str:
    if as_json:
        payload = [
            {
                "plan_id": patch.plan_id,
                "path": str(patch.path),
                "added": dict(patch.added),
                "conflicts": list(patch.conflicts),
                "changed": patch.changed,
                "backup": str(patch.backup) if patch.backup else None,
                "dry_run": dry_run,
            }
            for patch in patches
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)
    lines: list[str] = []
    for patch in patches:
        if patch.conflicts:
            lines.append(f"ERROR {patch.plan_id}: " + "；".join(patch.conflicts))
        elif patch.changed:
            keys = ", ".join(sorted(key for key, _ in patch.added))
            verb = "待写入" if dry_run else "已写入"
            lines.append(f"{verb} {patch.plan_id}: {keys}" + (f"（备份 {patch.backup.name}）" if patch.backup else ""))
        else:
            lines.append(f"SKIP {patch.plan_id}: 已包含全部事实源 links")
    return "\n".join(lines)


def main_plan_links(argv: list[str] | None = None) -> int:
    """CLI 入口：``tools/apply_thesis_plan_links.py``。

    退出码：0=全部写入或已是最新，1=冲突/缺失/写入失败。
    """

    parser = argparse.ArgumentParser(description="把事实源 links 合并进论文侧计划项（只增不改，先备份）")
    parser.add_argument("--root", type=Path, required=True, help="论文仓库根目录（含 .devloop/plan/items）")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest 路径，默认仓库内 docs/")
    parser.add_argument("--dry-run", action="store_true", help="只计算差异，不写文件、不备份")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    try:
        payload = load_manifest(args.manifest)
        patches = apply_plan_links(args.root, manifest=payload, dry_run=args.dry_run)
    except ThesisSyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(_render_plan_patches(patches, as_json=args.json, dry_run=args.dry_run))
    return 1 if any(patch.conflicts for patch in patches) else 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``python -m astock.thesis_sync`` / ``tools/check_thesis_sync.py``。

    退出码：0=ok，1=invalid，2=unavailable（环境未挂载，不作为失败）。
    """

    parser = argparse.ArgumentParser(description="校验产品×论文事实源（panel v2 / 因子方向表）")
    parser.add_argument("--check", action="store_true", help="执行校验（默认行为，保留以便文档命令）")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest 路径，默认仓库内 docs/")
    parser.add_argument("--data-root", type=Path, default=None, help="论文数据根目录（含 panel 与 factor_def_v2.csv）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    try:
        payload = load_manifest(args.manifest)
    except ThesisSyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    result = check_source(payload, data_root=args.data_root)
    print(_render(result, as_json=args.json))
    if result.status == "invalid":
        return 1
    if result.status == "unavailable":
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
