"""阅读核心逻辑。

封装阅读循环、cookie 刷新、synckey 修复。
保留原 main.py 的业务行为：
- 每次阅读间隔 30 秒
- cookie 过期时自动刷新，刷新后不重发当前请求（保持原行为）
- synckey 缺失时尝试修复

改进点：
- 函数式接口，无全局可变状态
- 通过 HttpClient 发请求，复用 TCP 连接
- 返回 ReadResult 数据类，便于上层编排
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from wereadit.config import Config
from wereadit.constants import (
    COOKIE_DATA_VARIANTS,
    DEFAULT_READ_DATA,
    FIX_SYNCKEY_BOOK_IDS,
    FIX_SYNCKEY_URL,
    READ_INTERVAL_SECONDS,
    READ_URL,
    RENEW_URL,
    SECONDS_PER_READ,
    SIGN_KEY,
)
from wereadit.exceptions import CookieExpiredError
from wereadit.infra.http import HttpClient
from wereadit.models import ReadResult
from wereadit.utils.crypto import sign_request

logger = logging.getLogger(__name__)


def refresh_cookie(client: HttpClient, cfg: Config) -> str:
    """刷新 cookie 密钥 wr_skey。

    尝试 COOKIE_DATA_VARIANTS 中的多种 payload，成功则更新 client 的 cookie 并返回新 skey。
    失败抛 CookieExpiredError。
    """
    logger.info("刷新 cookie")
    new_skey = _get_wr_skey(client, cfg)
    if new_skey:
        client.update_cookie("wr_skey", new_skey)
        logger.info("密钥刷新成功，新密钥：%s***", new_skey[:2])
        logger.info("重新本次阅读。")
        return new_skey

    err_msg = "无法获取新密钥或者 WEREADIT_CURL_BASH 配置有误，终止运行。"
    logger.error(err_msg)
    raise CookieExpiredError(err_msg)


def _get_wr_skey(client: HttpClient, cfg: Config) -> str | None:
    """尝试多种 payload 刷新 wr_skey。"""
    for cookie_data in COOKIE_DATA_VARIANTS:
        try:
            response = client.post(
                RENEW_URL,
                data=json.dumps(cookie_data, separators=(",", ":")),
                headers=cfg.headers,
            )
            if "wr_skey" in response.cookies:
                return response.cookies["wr_skey"][:8]
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh_cookie 请求失败，payload=%s，原因：%s", cookie_data, exc)
            continue
    return None


def fix_no_synckey(client: HttpClient, cfg: Config) -> None:
    """修复无 synckey 的情况。"""
    try:
        client.post(
            FIX_SYNCKEY_URL,
            data=json.dumps({"bookIds": FIX_SYNCKEY_BOOK_IDS}, separators=(",", ":")),
            headers=cfg.headers,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fix_no_synckey 请求失败：%s", exc)


def read_books(client: HttpClient, cfg: Config, refresh_print=None) -> ReadResult:
    """执行阅读循环。

    Args:
        client: HTTP 客户端
        cfg: 运行时配置
        refresh_print: 可选的进度打印函数（行内刷新）

    Returns:
        ReadResult：完成次数与累计分钟数
    """
    # 初始 cookie 刷新（保留原行为：循环前先刷一次）
    refresh_cookie(client, cfg)

    data: dict[str, Any] = dict(DEFAULT_READ_DATA)
    index = 1
    last_time = int(time.time()) - SECONDS_PER_READ
    total = cfg.read_num
    logger.info("需要阅读 %d 次。", total)

    while index <= total:
        data.pop("s", None)
        data["b"] = random.choice(cfg.books) if cfg.books else data["b"]
        data["c"] = random.choice(cfg.chapters) if cfg.chapters else data["c"]
        this_time = int(time.time())
        data["ct"] = this_time
        data["rt"] = this_time - last_time
        data["ts"] = int(this_time * 1000) + random.randint(0, 1000)
        data["rn"] = random.randint(0, 1000)
        sign_request(data, SIGN_KEY)

        if refresh_print:
            refresh_print(
                f"阅读进度: 第 {index}/{total} 次，已阅读 {(index - 1) * 0.5:.1f} 分钟"
            )
        logger.debug("data: %s", data)

        response = client.post(
            READ_URL,
            data=json.dumps(data, separators=(",", ":")),
            headers=cfg.headers,
        )
        res_data = response.json()
        logger.debug("response: %s", res_data)

        if "succ" in res_data:
            if "synckey" in res_data:
                last_time = this_time
                index += 1
                time.sleep(READ_INTERVAL_SECONDS)
            else:
                logger.warning("无 synckey，尝试修复...")
                fix_no_synckey(client, cfg)
        else:
            logger.warning("cookie 已过期，尝试刷新...")
            refresh_cookie(client, cfg)

    logger.info("阅读脚本已完成。")
    return ReadResult(completed_count=index - 1, total_minutes=(index - 1) * 0.5)
