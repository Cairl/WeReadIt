"""余额来源探测（只读诊断工具）。

背景：2026-07-29 前 query_coin_balance 把 /web/pay/balance 的
giftBalance/peerBalance 当作"赠币余额"推送，实测该值长期不随兑换变化
（是静态字段，并非 App 内看到的实时赠币余额）。公开逆向文档没有收录
实时余额接口，本模块改为实证路线：对候选接口发只读请求，把响应中全部
数值字段按点分路径拍平输出，在 Actions 日志中一次性定位真实字段，
不猜、不编、不拿 mock 值冒充真实命中。

安全约束：
- 只输出键名与数值；字符串值（token/昵称/头像/签名等）一律不输出。
- 全部请求只读（GET 或查询型 POST），不产生兑换/阅读等副作用。
- 每个接口独立 try/except，单点失败不影响其他接口与整体退出码。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from wereadit.constants import (
    BALANCE_URL,
    EXCHANGE_TIMEOUT,
    EXCHANGE_URL,
    PLATFORM_IOS,
)

if TYPE_CHECKING:
    from wereadit.config import Config
    from wereadit.infra.http import HttpClient

logger = logging.getLogger(__name__)

# 拍平递归深度上限（防异常响应栈溢出）
_MAX_DEPTH = 5
# 单响应最多输出的数值字段条数（防日志刷屏）
_MAX_FIELDS = 60

# App 端探测用的平台请求头（与 exchanger._build_headers 同构，探测独立实现，
# 避免诊断工具反向依赖业务实现，业务重构时探测不受影响）
_IOS_APP_HEADERS = {
    "channelid": "AppStore",
    "v": "9.0.0",
    "User-Agent": "WeRead/9.0.0 (iPhone; iOS 17.0; Scale/3.00)",
}
_ANDROID_APP_HEADERS = {
    "baseapi": "32",
    "appver": "9.0.0",
    "User-Agent": "WeRead/9.0.0 WRBrand/huawei Dalvik/2.1.0",
}


def _flatten_numbers(
    obj: Any, prefix: str, depth: int, out: dict[str, float]
) -> None:
    """递归拍平 JSON 中的数值叶子为 {点分路径: 数值}。

    列表不逐元素展开：记录 "路径[]" = 长度，并展开首个 dict 元素
    （路径带 [0] 前缀），兼顾结构可见性与日志体积。bool 不算数值。
    """
    if len(out) >= _MAX_FIELDS or depth > _MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if len(out) >= _MAX_FIELDS:
                return
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):
                out[path] = value
            elif isinstance(value, dict | list):
                _flatten_numbers(value, path, depth + 1, out)
    elif isinstance(obj, list):
        out[f"{prefix}[]"] = len(obj)
        if obj and isinstance(obj[0], dict):
            _flatten_numbers(obj[0], f"{prefix}[0]", depth + 1, out)


def _top_keys(data: Any) -> str:
    """返回顶层键名列表（结构线索，键名不含敏感值）。"""
    if isinstance(data, dict):
        return ", ".join(str(k) for k in data.keys()) or "(空对象)"
    if isinstance(data, list):
        return f"(数组, 长度 {len(data)})"
    return type(data).__name__


def _format_probe_result(name: str, status: int, data: Any) -> list[str]:
    """格式化单个接口的探测结果。"""
    lines = [f"  {name}: HTTP {status}"]
    if isinstance(data, dict) and data.get("errcode") not in (None, 0):
        lines.append(f"    errcode={data.get('errcode')}, errmsg={data.get('errmsg')}")
    numbers: dict[str, float] = {}
    _flatten_numbers(data, "", 0, numbers)
    if numbers:
        for path, value in numbers.items():
            lines.append(f"    {path} = {value}")
    else:
        lines.append(f"    (无数值字段) 顶层键: {_top_keys(data)}")
    return lines


def _probe_one(
    client: HttpClient,
    name: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> list[str]:
    """探测单个接口，独立容错，返回报告行。"""
    try:
        if method == "POST":
            resp = client.post(
                url, json=json_body, headers=headers, timeout=EXCHANGE_TIMEOUT
            )
        else:
            resp = client.get(url, headers=headers, timeout=EXCHANGE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 探测工具单点失败不影响其他接口
        return [f"  {name}: 请求异常 {exc!r}"]
    try:
        data = resp.json()
    except ValueError:
        # 非 JSON 响应只报状态码与长度，不输出原文（可能含 HTML/请求 ID）
        return [f"  {name}: HTTP {resp.status_code}, 非 JSON 响应({len(resp.text)} 字符)"]
    return _format_probe_result(name, resp.status_code, data)


def _build_app_probe_headers(token: str, vid: str, platform: str) -> dict[str, str]:
    """构造 App 端探测请求头（token 按平台放 skey 或 accessToken）。"""
    if platform == PLATFORM_IOS:
        return {**_IOS_APP_HEADERS, "skey": token, "vid": str(vid)}
    return {**_ANDROID_APP_HEADERS, "accessToken": token, "vid": str(vid)}


def probe_balance_sources(
    client: HttpClient,
    cfg: Config,
    *,
    app_token: str = "",
    platform: str = "",
) -> str:
    """对候选余额接口逐一探测，返回人话报告（同时写 INFO 日志）。

    Args:
        client: 带 web cookie 的 HTTP 客户端（web 端接口认证用）
        cfg: 运行时配置（取 wr_vid）
        app_token: 可选，/login 重放得到的新鲜 App token；为空则跳过 App 端探测
        platform: ios / android，决定 App 端 token 放哪个 header

    Returns:
        多行报告文本；任何单点失败都体现在报告里，不抛异常。
    """
    vid = cfg.cookies.get("wr_vid", "")
    lines: list[str] = ["余额来源探测（只读，数值字段全量拍平）:"]

    # ---- web 端（cookie 认证）----
    lines.append("[web 端]")
    lines.extend(
        _probe_one(
            client,
            "web/pay/balance",
            "POST",
            BALANCE_URL,
            json_body={"zoneid": "1", "release": "1", "pf": "weread_wx-2001-iap-2001-iphone"},
        )
    )
    lines.extend(
        _probe_one(
            client,
            "web/user",
            "GET",
            f"https://weread.qq.com/web/user?userVid={vid}",
        )
    )
    lines.extend(
        _probe_one(
            client,
            "web/pay/memberCardSummary",
            "GET",
            "https://weread.qq.com/web/pay/memberCardSummary",
        )
    )

    # ---- App 端（header token 认证，需要新鲜 token）----
    if app_token:
        app_headers = _build_app_probe_headers(app_token, vid, platform)
        lines.append("[App 端]")
        lines.extend(
            _probe_one(
                client,
                "i/user/profile",
                "GET",
                "https://i.weread.qq.com/user/profile",
                headers=app_headers,
            )
        )
        lines.extend(
            _probe_one(
                client,
                "i/pay/balance",
                "GET",
                "https://i.weread.qq.com/pay/balance",
                headers=app_headers,
            )
        )
        lines.extend(
            _probe_one(
                client,
                "i/pay/memberCardSummary",
                "GET",
                "https://i.weread.qq.com/pay/memberCardSummary",
                headers=app_headers,
            )
        )
        lines.extend(
            _probe_one(
                client,
                "i/reader/welfareCoin",
                "GET",
                "https://i.weread.qq.com/reader/welfareCoin",
                headers=app_headers,
            )
        )
        # 兑换查询接口（isExchangeAward=0 纯查询，与每日任务一致无副作用），
        # 全量拍平以确认其是否真的不含余额字段
        lines.extend(
            _probe_one(
                client,
                "i/weekly/exchange(查询)",
                "POST",
                EXCHANGE_URL,
                headers=app_headers,
                json_body={
                    "awardLevelId": 0,
                    "isExchangeAward": 0,
                    "isVisitReadGoal": 1,
                    "unread": 0,
                    "pf": "iOS" if platform == PLATFORM_IOS else "android",
                    "awardChoiceType": 0,
                },
            )
        )
    else:
        lines.append("[App 端] 跳过（无可用 app_token）")

    report = "\n".join(lines)
    logger.info("\n%s", report)
    return report
