"""curl 命令解析。

从浏览器复制的 `curl_bash` 字符串中提取 headers 与 cookies。
支持 `-H 'Cookie: xxx'` 和 `-b 'xxx'` 两种 cookie 提取方式。

`parse_curl` 仅提取 headers + cookies（用于 web 端 read 请求）。
`parse_curl_full` 额外提取 URL + body（用于 App 端 /login 重放）。
"""

from __future__ import annotations

import re
from typing import Final

_COOKIE_HEADER_KEY: Final[str] = "cookie"


def parse_curl(curl_command: str) -> tuple[dict[str, str], dict[str, str]]:
    """解析 curl 命令，返回 (headers, cookies)。

    Args:
        curl_command: 浏览器「复制为 Bash」得到的 curl 字符串。

    Returns:
        元组 (headers, cookies)：
        - headers: 不包含 Cookie 字段的其他 header
        - cookies: 从 Cookie header 或 -b 参数解析的键值对
    """
    headers_temp: dict[str, str] = {}
    for match in re.findall(r"-H '([^:]+): ([^']+)'", curl_command):
        headers_temp[match[0]] = match[1]

    cookie_header = next(
        (v for k, v in headers_temp.items() if k.lower() == _COOKIE_HEADER_KEY),
        "",
    )

    cookie_b = re.search(r"-b '([^']+)'", curl_command)
    cookie_string = cookie_b.group(1) if cookie_b else cookie_header

    cookies: dict[str, str] = {}
    if cookie_string:
        for cookie in cookie_string.split("; "):
            if "=" in cookie:
                key, value = cookie.split("=", 1)
                cookies[key.strip()] = value.strip()

    headers = {k: v for k, v in headers_temp.items() if k.lower() != _COOKIE_HEADER_KEY}
    return headers, cookies


def parse_curl_full(
    curl_command: str,
) -> tuple[str, dict[str, str], dict[str, str], str]:
    """解析 curl 命令，返回 (url, headers, cookies, body)。

    在 parse_curl 基础上额外提取 URL 和 POST body，用于 App 端 /login 请求重放。

    Args:
        curl_command: 浏览器或抓包工具「复制为 Bash」得到的 curl 字符串。

    Returns:
        元组 (url, headers, cookies, body)：
        - url: 请求 URL（curl 命令中第一个引号包围的 http(s) 地址）
        - headers: 不包含 Cookie 字段的 header
        - cookies: 从 Cookie header 或 -b 参数解析的键值对
        - body: POST body（--data/-d/--data-raw/--data-binary 参数值），无则为空串
    """
    headers, cookies = parse_curl(curl_command)

    # URL：curl 命令中第一个引号包围的 http(s) 地址
    url_match = re.search(r"""['"]?(https?://[^'"]+)['"]?""", curl_command)
    url = url_match.group(1) if url_match else ""

    # body：--data / -d / --data-raw / --data-binary
    body = ""
    for pattern in (
        r"--data-raw\s+'([^']*)'",
        r"--data-raw\s+\"([^\"]*)\"",
        r"--data-binary\s+'([^']*)'",
        r"--data\s+'([^']*)'",
        r"--data\s+\"([^\"]*)\"",
        r"-d\s+'([^']*)'",
        r"-d\s+\"([^\"]*)\"",
    ):
        body_match = re.search(pattern, curl_command)
        if body_match:
            body = body_match.group(1)
            break

    return url, headers, cookies, body
