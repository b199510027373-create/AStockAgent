"""artifact 审计库维护脚本：correction 与孤儿 blob GC（plan-20261114-002 / spec §12）。

裁决（2026-11-14）明确写在这里，避免以后又被搬回 Agent/UI：

* ``agent_artifacts`` / ``agent_artifact_versions`` 行**永久保留**：只随 run 删除而删 link，
  artifact 与 version 本身永不因为「过期」被清掉。
* 唯一允许的清理是**文件**层：``prune_orphan_blobs`` 只删没有被任何
  ``agent_artifacts.path`` 引用的 blob，默认 dry-run。
* meta 是「不可变 provenance」。发现问题只能追加一条 correction（版本 +1，
  旧版本与已有 link 不动），并且只能由维护脚本/管理员执行——Agent 与 UI 没有入口。
* 每条 correction 必须写 actor、reason、时间（由库函数填 UTC 时间）与 old/new 值。

用法::

    python tools/artifact_admin.py correct --artifact-id art_xxx \\
        --field data_as_of --value 2024-01-04 --actor ops --reason "手工核对原始行情"
    python tools/artifact_admin.py gc            # dry-run，只打印
    python tools/artifact_admin.py gc --apply    # 真删孤儿 blob
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # 允许 `python tools/artifact_admin.py` 直接跑
    sys.path.insert(0, str(ROOT))

from astock.agent import store  # noqa: E402  （路径注入后再 import）

#: correction 只覆盖 provenance 字段；spec/data_digest/content_digest 与身份不可改。
PATCHABLE_FIELDS = store.CORRECTION_FIELDS


def _parse_value(field: str, raw: str) -> Any:
    if field in {"warnings", "degraded"} or raw[:1] in "[{":
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--value 不是合法 JSON：{exc}") from exc
    return raw


def command_correct(args: argparse.Namespace) -> int:
    actor = (args.actor or "").strip()
    reason = (args.reason or "").strip()
    # §12：actor + 理由必填，不因为「本地单用户」就省掉。
    if not actor:
        raise SystemExit("缺少 --actor：correction 必须记录是谁改的")
    if not reason:
        raise SystemExit("缺少 --reason：correction 必须记录为什么改")
    if args.field not in PATCHABLE_FIELDS:
        raise SystemExit(
            f"字段 {args.field!r} 不可 correction；只能改 {list(PATCHABLE_FIELDS)}"
            "（spec/data_digest/content_digest/artifact_id 是身份，不允许补丁）"
        )

    conn = store.connect(args.db)
    try:
        version = store.correct_artifact(
            conn,
            args.artifact_id,
            {args.field: _parse_value(args.field, args.value)},
            reason=reason,
            actor=actor,
        )
    finally:
        conn.close()
    print(f"已写入 {args.artifact_id} 的新版本 v{version}（{args.field}）；旧版本保持可回放")
    return 0


def command_gc(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        result = store.prune_orphan_blobs(
            conn,
            older_than_days=args.older_than_days,
            root=args.root,
            dry_run=not args.apply,
        )
    finally:
        conn.close()
    verb = "候选" if args.apply else "候选（dry-run）"
    candidates = result.get("candidates") or []
    print(
        f"{verb} {len(candidates)} 个孤儿 blob，实际删除 {result.get('deleted', 0)} 个"
        f"（{args.older_than_days} 天前，root={args.root or 'data/agent'}）"
    )
    for rel in candidates[:20]:
        print(f"  - {rel}")
    if len(candidates) > 20:
        print(f"  … 其余 {len(candidates) - 20} 个")
    if not args.apply:
        print("dry-run：加 --apply 才真正删除；artifact/version 行永远保留")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="artifact 审计库维护（correction / 孤儿 blob GC）")
    parser.add_argument("--db", default=None, help="审计库路径，默认 data/astock.db")
    sub = parser.add_subparsers(dest="command", required=True)

    correct = sub.add_parser("correct", help="追加一条 correction（版本 +1，旧版本不动）")
    correct.add_argument("--artifact-id", required=True)
    correct.add_argument("--field", required=True, choices=sorted(PATCHABLE_FIELDS))
    correct.add_argument("--value", required=True, help="新值；warnings/degraded 用 JSON")
    correct.add_argument("--actor", required=True, help="谁改的（必填）")
    correct.add_argument("--reason", required=True, help="为什么改（必填）")
    correct.set_defaults(func=command_correct)

    gc = sub.add_parser("gc", help="清理没有被任何 artifact 引用的 blob 文件（默认 dry-run）")
    gc.add_argument("--root", default=None, help="artifact 根目录，默认 data/agent")
    gc.add_argument("--older-than-days", type=int, default=90)
    gc.add_argument("--apply", action="store_true", help="真正删除；不加即 dry-run")
    gc.set_defaults(func=command_gc)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
