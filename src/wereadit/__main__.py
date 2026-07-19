"""入口：python -m wereadit

也可以直接 `python main.py`（根目录的兼容入口）。
"""

from __future__ import annotations

import sys

from wereadit.app import main

if __name__ == "__main__":
    sys.exit(main())
