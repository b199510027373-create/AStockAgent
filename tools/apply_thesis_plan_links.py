"""把事实源 links 合并进论文侧计划项（plan-20261128-006）。

用法（论文仓库根目录含 ``.devloop/plan/items``）::

    python tools/apply_thesis_plan_links.py --root D:\\毕业论文 --dry-run
    python tools/apply_thesis_plan_links.py --root D:\\毕业论文

只增不改：已有 key 值不同会报冲突并退出 1；写入前把原文件备份为 ``*.json.bak``。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astock.thesis_sync import main_plan_links  # noqa: E402 - 需要先补 sys.path

if __name__ == "__main__":
    raise SystemExit(main_plan_links())
