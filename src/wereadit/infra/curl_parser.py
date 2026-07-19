"""curl 命令解析。

从浏览器复制的 `curl_bash` 字符串中提取 headers 与 cookies。
支持 `-H 'Cookie: xxx'` 和 `-b 'xxx'` 两种 cookie 提取方式。
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
