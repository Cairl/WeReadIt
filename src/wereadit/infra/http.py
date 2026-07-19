"""HTTP 客户端封装。

基于 requests.Session 复用 TCP 连接，统一超时与日志。
所有外部请求都应通过 HttpClient 发起，便于替换底层库与加统一拦截器。
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
    持有 cookies 字典，可在运行时被业务层更新（如 cookie 刷新后）。
    """

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = requests.Session()
        if headers:
            self._session.headers.update(headers)
        if cookies:
            self._session.cookies.update(cookies)
        self._timeout = timeout

    @property
    def cookies(self) -> dict[str, str]:
        """返回当前 cookies 的副本。"""
        return dict(self._session.cookies)

    def update_cookie(self, key: str, value: str) -> None:
        """更新单个 cookie（如 cookie 刷新后更新 wr_skey）。"""
        self._session.cookies.set(key, value)

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
        """发起 POST 请求。"""
        logger.debug("POST %s", url)
        return self._session.post(
            url,
            data=data,
            json=json,
            headers=headers,
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
        """发起 GET 请求。"""
        logger.debug("GET %s", url)
        return self._session.get(
            url,
            headers=headers,
            timeout=timeout or self._timeout,
            proxies=proxies,
        )

    def close(self) -> None:
        """关闭底层 Session。"""
        self._session.close()
