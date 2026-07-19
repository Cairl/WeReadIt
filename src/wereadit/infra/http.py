"""HTTP 客户端封装。

基于 requests.Session 复用 TCP 连接，统一超时与日志。
所有外部请求都应通过 HttpClient 发起，便于替换底层库与加统一拦截器。

【保活策略 - cookies 业务层独占】
不使用 Session 的 cookie 自动管理机制，原因：
1. 服务器响应中的 Set-Cookie 会被 Session 自动合并到 jar，
   可能覆盖业务层 refresh_cookie 设置的 wr_skey[:8] 截断值
2. 原版 wxread 用 requests.post(cookies=cookies) 显式传，响应不回写
3. 业务层需要完全控制 cookies（如 wr_skey 续期）

实现：Session 仅用于 headers 与 TCP 连接复用，
cookies 存在业务层独立字典，每次请求显式传入。
详见 wxread_keepalive_improvement_plan.md P0.1。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from wereadit.constants import DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)


class HttpClient:
    """基于 requests.Session 的 HTTP 客户端封装。

    通过 Session 复用 TCP 连接，避免每次请求都重新握手。
    cookies 由业务层独占管理，避免服务器 Set-Cookie 自动覆盖。
    """

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = requests.Session()
        # Session.cookies 保持为空，仅用于 TCP 复用
        if headers:
            self._session.headers.update(headers)
        # cookies 业务层独占，避免 Session 自动合并 Set-Cookie
        self._cookies: dict[str, str] = dict(cookies) if cookies else {}
        self._timeout = timeout

    @property
    def cookies(self) -> dict[str, str]:
        """返回当前 cookies 的副本。"""
        return dict(self._cookies)

    def update_cookie(self, key: str, value: str) -> None:
        """更新单个 cookie（如 cookie 刷新后更新 wr_skey）。

        只更新业务层字典，不会影响 Session.cookies。
        """
        self._cookies[key] = value

    def post(
        self,
        url: str,
        *,
        data: str | bytes | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        """发起 POST 请求。

        cookies 始终取自业务层字典（self._cookies），
        服务器响应中的 Set-Cookie 不会回写，由业务层显式管理。
        """
        logger.debug("POST %s", url)
        return self._session.post(
            url,
            data=data,
            json=json,
            headers=headers,
            cookies=self._cookies,
            timeout=timeout or self._timeout,
            proxies=proxies,
        )

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        """发起 GET 请求。

        cookies 始终取自业务层字典（self._cookies）。
        """
        logger.debug("GET %s", url)
        return self._session.get(
            url,
            headers=headers,
            cookies=self._cookies,
            timeout=timeout or self._timeout,
            proxies=proxies,
        )

    def close(self) -> None:
        """关闭底层 Session。"""
        self._session.close()
