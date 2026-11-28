"""CLI 包装：校验产品×论文事实源（panel_monthly_v2 / 因子方向表）。

用法::

    python tools/check_thesis_sync.py --check
    python tools/check_thesis_sync.py --check --json
    python tools/check_thesis_sync.py --data-root "D:\\毕业论文\\论文需要\\data"

退出码：0=一致，1=manifest 或数据不一致，2=论文数据未挂载（环境事实，不算失败）。
真正的校验逻辑在 ``astock/thesis_sync.py``，本脚本只负责把仓库根放进 ``sys.path``。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astock.thesis_sync import main  # noqa: E402 - 必须先补 sys.path 再导入

if __name__ == "__main__":
    raise SystemExit(main())
