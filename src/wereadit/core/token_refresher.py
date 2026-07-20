"""App 端 Token 自动续期。

微信读书 App 端 skey/accessToken 有效期极短（约 2 小时），无法长期使用。
本模块提供两种自动续期路径，exchanger.py 按瀑布式依次尝试：

1. **login curl 重放**（refresh_app_token）：用户抓包 App 的 /login 请求，
   脚本重放获取新 skey。需要用户手动抓包配置 WEREAD_LOGIN_CURL_BASH。
2. **web wr_skey 复用**（refresh_app_token_via_web）：通过 web 端 login/renewal
   接口获取 wr_skey 完整值，尝试作为 App skey。如果 web wr_skey 和 App skey
   是同一个值（只是 cookie vs header 传输方式不同），则可实现全自动续期，
   无需任何手动操作。

参考：https://www.ppanda.com/posts/tech/微信读书三方插件cookie失效问题修复

设计要点：
- login curl 重放使用独立的 requests 调用，不带 web 端 cookie，
  避免 web cookie 干扰 App 端认证（两套独立认证体系）。
- web wr_skey 复用使用项目的 HttpClient（带 web cookie），调用 web renewal 接口。
  注意：reader.py 中 wr_skey 被截断到 8 位用于 web 接口，这里获取完整值。
- 响应中 skey/accessToken 的位置未公开，因此从响应体 JSON、
  响应 header、Set-Cookie 三个位置依次尝试提取。
"""

from __future__ import annotations

import json
import logging

import requests

from wereadit.constants import COOKIE_DATA_VARIANTS, LOGIN_TIMEOUT, RENEW_TIMEOUT, RENEW_URL
from wereadit.infra.curl_parser import parse_curl_full
from wereadit.infra.http import HttpClient

logger = logging.getLogger(__name__)

# 响应中可能包含 token 的字段名（按优先级尝试）
_TOKEN_KEYS = ("skey", "accessToken", "access_token", "token")


def refresh_app_token(login_curl: str) -> str | None:
    """重放 /login 请求刷新 App 端 skey/accessToken。

    用户抓包 App 的 /login 请求（i.weread.qq.com/login），配置为环境变量。
    脚本重放该请求，从响应中提取新的 skey/accessToken。

    Args:
        login_curl: /login 请求的 cURL 命令（抓包工具「复制为 Bash」）

    Returns:
        新的 skey/accessToken，或 None（刷新失败）
    """
    url, headers, cookies, body = parse_curl_full(login_curl)
    if not url:
        logger.error("login curl 解析失败：未找到 URL")
        return None

    logger.info("刷新 App Token: POST %s", url)
    try:
        response = requests.post(
            url,
            data=body if body else None,
            headers=headers,
            cookies=cookies,
            timeout=LOGIN_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("刷新 App Token 请求失败: %s", exc)
        return None

    new_token = _extract_token_from_response(response)
    if new_token:
        logger.info("App Token 刷新成功, 新 token=%s...", new_token[:8])
    else:
        logger.warning(
            "App Token 刷新失败: 响应中未找到 skey/accessToken, HTTP=%s, 响应体=%s",
            response.status_code,
            response.text[:500],
        )
    return new_token


def _extract_token_from_response(response: requests.Response) -> str | None:
    """从响应中提取 skey/accessToken。

    尝试以下位置（按优先级）：
    1. 响应体 JSON 中的 skey/accessToken/token 字段
    2. 响应 header 中的 skey/accessToken
    3. Set-Cookie 中的 skey
    """
    # 1. 响应体 JSON
    try:
        data = response.json()
        if isinstance(data, dict):
            for key in _TOKEN_KEYS:
                value = data.get(key)
                if value and isinstance(value, str):
                    return value
    except (ValueError, KeyError):
        pass

    # 2. 响应 header（不区分大小写）
    for key in _TOKEN_KEYS:
        value = response.headers.get(key) or response.headers.get(key.capitalize())
        if value:
            return value

    # 3. Set-Cookie
    set_cookie = response.headers.get("Set-Cookie", "")
    for part in set_cookie.split(";"):
        part = part.strip()
        for key in _TOKEN_KEYS:
            if part.startswith(f"{key}="):
                return part[len(key) + 1 :]

    return None


def refresh_app_token_via_web(client: HttpClient) -> str | None:
    """通过 web 端 renewal 接口获取 wr_skey 完整值，尝试作为 App skey。

    web 端 wr_skey 可通过 login/renewal 自动刷新（reader.py 已实现）。
    如果 wr_skey 完整值与 App 端 skey 是同一个值（只是 cookie vs header
    传输方式不同），则可实现全自动续期，无需手动抓包 App token。

    注意：reader.py 中 wr_skey 被截断到 8 位用于 web 接口（保活策略），
    这里获取 renewal 响应中的完整值用于 App 接口尝试。

    Args:
        client: HTTP 客户端（带 web cookie，用于调用 renewal 接口）

    Returns:
        wr_skey 完整值，或 None（刷新失败）
    """
    logger.info("尝试用 web wr_skey 作为 App skey")
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = client.post(
                RENEW_URL,
                data=json.dumps(cookie_data, separators=(",", ":")),
                timeout=RENEW_TIMEOUT,
            )
            # 获取 wr_skey 完整值（不截断）
            if "wr_skey" in response.cookies:
                full_skey = response.cookies["wr_skey"]
                logger.info(
                    "web renewal 返回 wr_skey, 完整长度=%d, 前8位=%s...",
                    len(full_skey),
                    full_skey[:8],
                )
                return full_skey
        except requests.RequestException as exc:
            logger.warning("web renewal 请求失败: %s", exc)
            continue

    logger.warning("web renewal 未返回 wr_skey，无法尝试 web skey 续期")
    return None
