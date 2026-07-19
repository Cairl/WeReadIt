"""兼容入口：保留 `python main.py` 的旧用法。

新代码请使用 `python -m wereadit`，或安装后直接 `wereadit`。
"""

from __future__ import annotations

import sys

# 把 src/ 加入 sys.path，兼容未安装的场景
from pathlib import Path

_SRC = Path(__file__).parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from wereadit.app import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
