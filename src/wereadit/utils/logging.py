"""日志配置。

简化版：移除原 log_utils.py 的 RefreshSafeHandler 闭包状态机，
改为标准 logging 配置 + 一个 refresh_print 函数（保留兼容）。

如需更复杂的进度条效果，建议未来引入 tqdm 或 rich。
"""

from __future__ import annotations

import logging
import sys

_WIDTH = 120


def setup_logging(width: int = _WIDTH) -> logging.Logger:
    """配置 root logger，返回名为 wereadit 的 logger。

    refresh_print 的「行内刷新」功能改为可选：
    若需要保留原视觉行为，可调用 get_refresh_print()。
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)-8s - %(name)s - %(message)s")
    )
    root.addHandler(handler)

    return logging.getLogger("wereadit")


def make_refresh_print(width: int = _WIDTH):
    """返回一个 refresh_print 函数，用于行内刷新输出。

    保留原 log_utils.py 的视觉行为：用 \\r 覆盖当前行，
    logging 输出时先 clear 当前行避免错位。
    """

    state = {"active": False}

    def clear() -> None:
        if not state["active"]:
            return
        print("\r" + " " * width + "\r", end="", flush=True)
        state["active"] = False

    def refresh_print(message: str) -> None:
        state["active"] = True
        print(f"\r{message:<{width}}", end="", flush=True)

    # 让 logging 输出前先 clear，避免行内刷新与日志互相覆盖
    class _RefreshSafeHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            clear()
            msg = self.format(record)
            stream = sys.stderr if record.levelno >= logging.WARNING else sys.stdout
            stream.write(msg + "\n")
            stream.flush()

    root = logging.getLogger()
    # 替换默认 handler
    for h in list(root.handlers):
        root.removeHandler(h)
    safe_handler = _RefreshSafeHandler()
    safe_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)-8s - %(name)s - %(message)s")
    )
    root.addHandler(safe_handler)
    root.setLevel(logging.INFO)

    return refresh_print
