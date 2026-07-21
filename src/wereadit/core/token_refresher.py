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

import logging
import time
from dataclasses import dataclass

import requests

from wereadit.constants import LOGIN_MAX_ATTEMPTS, LOGIN_RETRY_INTERVAL, LOGIN_TIMEOUT
from wereadit.infra.curl_parser import parse_curl_full

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


def diagnose_login_curl(login_curl: str) -> str:
    """静态体检 login curl（不发请求），返回诊断；空串表示通过。

    校验项：
    1. 非空且能解析出 URL
    2. URL 是 /login 请求（重放其他请求无法换新 token）
    3. body 含 deviceId（长效设备凭证，是 /login 换新 token 的依据）
    """
    if not login_curl.strip():
        return "WEREAD_APP_CURL 为空，请按 README「第 2 步：配置 WEREAD_APP_CURL」配置"

    url, _, _, body = parse_curl_full(login_curl)
    if not url:
        return (
            "无法从 WEREAD_APP_CURL 解析出 URL，"
            "请确认 Secret 中是完整的 cURL 命令（抓包工具「复制为 cURL (Bash)」）"
        )
    if "/login" not in url:
        return (
            f"抓到的请求不是 /login 而是 {url}。"
            "请按 README 抓包指引，在 App 触发 Token 刷新（杀掉 App 重新打开）时，"
            "抓取 i.weread.qq.com/login 请求"
        )
    if "deviceId" not in body:
        return (
            "/login 请求 body 中缺少 deviceId（长效设备凭证），重放无法换新 Token。"
            "请抓取 App 冷启动（杀掉 App 重新打开）时的 /login 请求，确保 body 含 deviceId"
        )
    return ""


def refresh_app_token(login_curl: str) -> RefreshResult:
    """重放 /login 请求刷新 App 端 skey/accessToken。

    用户抓包 App 的 /login 请求（i.weread.qq.com/login），配置为环境变量。
    脚本重放该请求，从响应中提取新的 skey/accessToken。

    错误四分类：
    - 配置错误（解析不出 URL）：不重试
    - 网络错误（异常 / HTTP 5xx）：指数退避重试（最多 LOGIN_MAX_ATTEMPTS 次）
    - 服务端拒绝（HTTP 4xx / errcode 非 0）：不重试，指引重新抓包
    - 结构未知（200 但提取不到 token）：不重试，诊断含响应结构摘要

    注意：本函数不抛异常，所有失败均以 RefreshResult.diagnosis 返回（调用方无需 try/except）。

    Args:
        login_curl: /login 请求的 cURL 命令（抓包工具「复制为 Bash」）

    Returns:
        RefreshResult：成功含新 token 与命中字段名；失败含人话诊断
    """
    url, headers, cookies, body = parse_curl_full(login_curl)
    if not url:
        return RefreshResult(
            diagnosis="login curl 解析失败：未找到 URL，请检查 WEREAD_APP_CURL 是否为完整 cURL 命令"
        )

    logger.info("刷新 App Token: POST %s", url)
    last_network_error = ""
    for attempt in range(LOGIN_MAX_ATTEMPTS):
        try:
            response = requests.post(
                url,
                data=body if body else None,
                headers=headers,
                cookies=cookies,
                timeout=LOGIN_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_network_error = str(exc)
            logger.warning(
                "刷新 App Token 网络异常（第 %d/%d 次）: %s",
                attempt + 1,
                LOGIN_MAX_ATTEMPTS,
                exc,
            )
            if attempt < LOGIN_MAX_ATTEMPTS - 1:
                time.sleep(LOGIN_RETRY_INTERVAL * (2**attempt))
            continue

        if response.status_code >= 500:
            last_network_error = f"HTTP {response.status_code}"
            logger.warning(
                "刷新 App Token 服务端错误（第 %d/%d 次）: HTTP %s",
                attempt + 1,
                LOGIN_MAX_ATTEMPTS,
                response.status_code,
            )
            if attempt < LOGIN_MAX_ATTEMPTS - 1:
                time.sleep(LOGIN_RETRY_INTERVAL * (2**attempt))
            continue

        if response.status_code >= 400:
            logger.warning(
                "刷新 App Token 被服务端拒绝: HTTP %s, 响应体=%s",
                response.status_code,
                response.text[:500],
            )
            return RefreshResult(
                diagnosis=(
                    f"login 凭证已被服务端拒绝 (HTTP {response.status_code})，"
                    "请重新抓包更新 WEREAD_APP_CURL"
                )
            )

        # 检查 errcode（weread 约定 errcode==0 或缺失为成功）
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            errcode = data.get("errcode")
            if errcode not in (None, 0):
                errmsg = data.get("errmsg", "unknown")
                logger.warning(
                    "刷新 App Token 失败: errcode=%s, errmsg=%s", errcode, errmsg
                )
                return RefreshResult(
                    diagnosis=(
                        f"login 凭证已失效 (errcode={errcode}, {errmsg})，"
                        "请重新抓包更新 WEREAD_APP_CURL"
                    )
                )

        token, token_key = _extract_token_from_response(response)
        if token:
            return RefreshResult(token=token, token_key=token_key)

        structure = _summarize_structure(data)
        logger.warning("刷新 App Token: 响应 200 但未找到 token, 结构=%s", structure)
        return RefreshResult(
            diagnosis=(
                f"/login 响应 200 但未找到 token，响应结构: {structure}。"
                "请把此信息反馈给开发者适配新的响应格式"
            )
        )

    return RefreshResult(
        diagnosis=(
            f"刷新 App Token 网络异常（重试 {LOGIN_MAX_ATTEMPTS} 次均失败）: {last_network_error}。"
            "本次为网络问题，明日自动重试"
        )
    )
