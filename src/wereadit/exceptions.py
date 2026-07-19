"""自定义异常层级。

按业务场景区分异常类型，便于上层精准 catch 与推送通知。
"""

from __future__ import annotations


class WeReadItError(Exception):
    """WeReadIt 项目所有自定义异常的基类。"""


class CookieExpiredError(WeReadItError):
    """Cookie 过期且自动刷新失败。"""


class ReadFailedError(WeReadItError):
    """阅读请求失败（非 cookie 过期原因）。"""


class ExchangeError(WeReadItError):
    """兑换奖励异常，携带 errcode 便于上层判断。"""

    def __init__(self, message: str, errcode: int | None = None) -> None:
        super().__init__(message)
        self.errcode = errcode
