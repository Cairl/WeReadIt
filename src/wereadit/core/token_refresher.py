"""App 端 Token 自动续期。

微信读书 App 端 skey/accessToken 有效期极短（约 2 小时），无法长期使用。
本模块通过重放 App 端 /login 请求（i.weread.qq.com/login）实现自动续期：
/login 以请求 body 中的 deviceId 等长效设备凭证换取新 token，
抓包一次即可长期反复重放（社区实证）。

参考：https://www.ppanda.com/posts/tech/微信读书三方插件cookie失效问题修复

设计要点：
- login curl 重放使用独立的 requests 调用，不带 web 端 cookie，
  避免 web cookie 干扰 App 端认证（两套独立认证体系）。
- 响应中 skey/accessToken 的位置未公开，因此递归遍历响应 JSON
  （任意嵌套，深度限 5 层），外加响应 header、Set-Cookie 两路兜底。
- 刷新结果以 RefreshResult 返回：token + 命中字段名 + 人话诊断，
  诊断可直接进推送，无需翻 Actions 日志。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests

from wereadit.constants import COOKIE_DATA_VARIANTS, LOGIN_TIMEOUT, RENEW_TIMEOUT, RENEW_URL
from wereadit.infra.curl_parser import parse_curl_full
from wereadit.infra.http import HttpClient

logger = logging.getLogger(__name__)

# 响应中可能包含 token 的字段名（按优先级尝试）
_TOKEN_KEYS = ("skey", "accessToken", "access_token", "token")
# 递归提取/结构摘要的最大深度（防异常响应导致栈溢出）
_MAX_EXTRACT_DEPTH = 5
# 结构摘要最多输出的条目数
_MAX_SUMMARY_ITEMS = 20


@dataclass(frozen=True)
class RefreshResult:
    """Token 刷新结果。

    token 为 None 表示失败，此时 diagnosis 为人话诊断 + 下一步指引；
    token_key 记录命中字段名（如 "skey" / "accessToken"），供平台一致性校验。
    """

    token: str | None = None
    token_key: str = ""
    diagnosis: str = ""

    @property
    def ok(self) -> bool:
        return self.token is not None


def _find_token_in_json(obj: object, depth: int = 0) -> tuple[str | None, str]:
    """递归查找 token 字段，返回 (token, 命中字段名)；未找到返回 (None, "")。"""
    if depth > _MAX_EXTRACT_DEPTH:
        return None, ""
    if isinstance(obj, dict):
        for key in _TOKEN_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value, key
        for value in obj.values():
            found, key = _find_token_in_json(value, depth + 1)
            if found:
                return found, key
    elif isinstance(obj, list):
        for item in obj:
            found, key = _find_token_in_json(item, depth + 1)
            if found:
                return found, key
    return None, ""


def _extract_token_from_response(response: requests.Response) -> tuple[str | None, str]:
    """从响应中提取 token，返回 (token, 命中字段名)。

    尝试以下位置（按优先级）：
    1. 响应体 JSON（递归任意嵌套）
    2. 响应 header（不区分大小写）
    3. Set-Cookie
    """
    # 1. 响应体 JSON（递归）
    try:
        data = response.json()
    except ValueError:
        data = None
    if data is not None:
        token, key = _find_token_in_json(data)
        if token:
            return token, key

    # 2. 响应 header（不区分大小写）
    for key in _TOKEN_KEYS:
        value = response.headers.get(key) or response.headers.get(key.capitalize())
        if value:
            return value, key

    # 3. Set-Cookie
    set_cookie = response.headers.get("Set-Cookie", "")
    for part in set_cookie.split(";"):
        part = part.strip()
        for key in _TOKEN_KEYS:
            if part.startswith(f"{key}="):
                return part[len(key) + 1 :], key

    return None, ""


def _collect_structure(obj: object, prefix: str, depth: int, items: list[str]) -> None:
    """递归收集 JSON 键路径:类型（只收键名与类型，不收值，脱敏）。"""
    if len(items) >= _MAX_SUMMARY_ITEMS or depth > _MAX_EXTRACT_DEPTH:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if len(items) >= _MAX_SUMMARY_ITEMS:
                return
            path = f"{prefix}.{key}" if prefix else str(key)
            items.append(f"{path}:{type(value).__name__}")
            _collect_structure(value, path, depth + 1, items)
    elif isinstance(obj, list):
        for index, item in enumerate(obj[:3]):  # 列表只展开前 3 个元素
            if len(items) >= _MAX_SUMMARY_ITEMS:
                return
            path = f"{prefix}[{index}]"
            items.append(f"{path}:{type(item).__name__}")
            _collect_structure(item, path, depth + 1, items)


def _summarize_structure(obj: object) -> str:
    """生成 JSON 键路径:类型 摘要（如 "errcode:int, data.user.skey:str"）。"""
    items: list[str] = []
    _collect_structure(obj, "", 0, items)
    return ", ".join(items) if items else "(空)"


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
        return RefreshResult()

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
        return RefreshResult()

    new_token, _ = _extract_token_from_response(response)
    if new_token:
        logger.info("App Token 刷新成功, 新 token=%s...", new_token[:8])
    else:
        logger.warning(
            "App Token 刷新失败: 响应中未找到 skey/accessToken, HTTP=%s, 响应体=%s",
            response.status_code,
            response.text[:500],
        )
    return RefreshResult(token=new_token) if new_token else RefreshResult()


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
