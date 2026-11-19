"""维护脚本 ``tools/artifact_admin.py``：correction 与孤儿 blob GC（spec §12）。

裁决要点（2026-11-14）：

* artifact / version 行永久保留，只随 run 删 link；
* 唯一可清理的是没人引用的 blob 文件，且默认 dry-run；
* correction 只能由维护脚本/管理员写，actor + 理由 + 时间必填，只追加版本。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from astock.agent import chart_service, store

ROOT = Path(__file__).resolve().parents[1]


def _admin_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "artifact_admin", ROOT / "tools" / "artifact_admin.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_INLINE_SPEC: dict[str, Any] = {
    "version": 1,
    "kind": "line",
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


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, str]:
    return {
        "db_path": str(tmp_path / "agent.db"),
        "artifact_root": str(tmp_path / "agent"),
    }


def _render(paths: dict[str, str]) -> Any:
    return chart_service.render_chart(
        _INLINE_SPEC,
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
        include_plotlyjs="cdn",
        export_png=False,
    )


def test_correct_requires_actor_and_reason(
    paths: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = _render(paths)
    admin = _admin_module()
    with pytest.raises(SystemExit, match="--actor"):
        admin.main(
            [
                "--db",
                paths["db_path"],
                "correct",
                "--artifact-id",
                rendered.artifact_id,
                "--field",
                "data_as_of",
                "--value",
                "2024-01-04",
                "--actor",
                "  ",
                "--reason",
                "核对",
            ]
        )
    with pytest.raises(SystemExit, match="--reason"):
        admin.main(
            [
                "--db",
                paths["db_path"],
                "correct",
                "--artifact-id",
                rendered.artifact_id,
                "--field",
                "data_as_of",
                "--value",
                "2024-01-04",
                "--actor",
                "ops",
                "--reason",
                "",
            ]
        )
    # 两次都没有写进任何东西。
    conn = store.connect(paths["db_path"])
    try:
        assert store.get_artifact(conn, rendered.artifact_id).version == 1  # type: ignore[union-attr]
    finally:
        conn.close()


def test_correct_appends_version_and_keeps_history(paths: dict[str, str]) -> None:
    rendered = _render(paths)
    admin = _admin_module()
    assert (
        admin.main(
            [
                "--db",
                paths["db_path"],
                "correct",
                "--artifact-id",
                rendered.artifact_id,
                "--field",
                "data_as_of",
                "--value",
                "2024-01-04",
                "--actor",
                "ops",
                "--reason",
                "手工核对原始行情",
            ]
        )
        == 0
    )

    conn = store.connect(paths["db_path"])
    try:
        latest = store.get_artifact(conn, rendered.artifact_id)
        assert latest is not None and latest.version == 2
        assert latest.meta["data_as_of"] == "2024-01-04"
        # 旧版本仍是原值（as_recorded 不受纠正影响）
        first = store.get_artifact(conn, rendered.artifact_id, version=1)
        assert first is not None and first.meta["data_as_of"] == "2024-01-03"
        rows = conn.execute(
            "SELECT field, old_value, new_value, reason, actor, created_at"
            " FROM corrections WHERE artifact_id = ? ORDER BY correction_id",
            (rendered.artifact_id,),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    field, old_value, new_value, reason, actor, created_at = rows[0]
    assert field == "data_as_of"
    assert json.loads(old_value) == "2024-01-03"
    assert json.loads(new_value) == "2024-01-04"
    assert reason == "手工核对原始行情"
    assert actor == "ops"
    assert created_at  # 时间必填，库函数写 UTC 时间


def test_correct_rejects_identity_fields(paths: dict[str, str]) -> None:
    import argparse

    rendered = _render(paths)
    admin = _admin_module()
    for field in ("content_digest", "data_digest", "spec"):
        # argparse 层：非法字段直接被 choices 拦下（exit code 2）
        with pytest.raises(SystemExit) as blocked:
            admin.main(
                [
                    "--db",
                    paths["db_path"],
                    "correct",
                    "--artifact-id",
                    rendered.artifact_id,
                    "--field",
                    field,
                    "--value",
                    "x",
                    "--actor",
                    "ops",
                    "--reason",
                    "想改身份",
                ]
            )
        assert blocked.value.code == 2
        # 脚本层兜底：即便绕过 argparse，也不允许写身份字段
        with pytest.raises(SystemExit, match="不可 correction"):
            admin.command_correct(
                argparse.Namespace(
                    db=paths["db_path"],
                    artifact_id=rendered.artifact_id,
                    field=field,
                    value="x",
                    actor="ops",
                    reason="想改身份",
                )
            )


def test_gc_defaults_to_dry_run_and_never_drops_rows(paths: dict[str, str]) -> None:
    rendered = _render(paths)
    admin = _admin_module()
    blob = Path(paths["artifact_root"]) / rendered.path
    orphan = Path(paths["artifact_root"]) / "artifacts" / "ff" / "deadbeef" / "orphan.html"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("<html>old</html>", encoding="utf-8")
    # 把两个文件都做成「90 天前」
    import os
    import time

    old = time.time() - 120 * 86_400
    os.utime(blob, (old, old))
    os.utime(orphan, (old, old))

    assert admin.main(["--db", paths["db_path"], "gc", "--root", paths["artifact_root"]]) == 0
    assert blob.exists() and orphan.exists()  # dry-run 不删

    assert (
        admin.main(
            [
                "--db",
                paths["db_path"],
                "gc",
                "--root",
                paths["artifact_root"],
                "--apply",
            ]
        )
        == 0
    )
    assert blob.exists(), "被 artifact 引用的 blob 不能删"
    assert not orphan.exists(), "孤儿 blob 应被清理"

    conn = store.connect(paths["db_path"])
    try:
        # §12：artifact / version 行永久保留
        assert store.get_artifact(conn, rendered.artifact_id) is not None
        assert conn.execute("SELECT COUNT(*) FROM agent_artifact_versions").fetchone()[0] == 1
    finally:
        conn.close()
