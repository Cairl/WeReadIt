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

import requests

from wereadit.config import Config
from wereadit.constants import (
    CIRCUIT_BREAKER_BACKOFF,
    COOKIE_DATA_VARIANTS,
    DEFAULT_READ_DATA,
    FIX_SYNCKEY_BOOK_IDS,
    FIX_SYNCKEY_TIMEOUT,
    FIX_SYNCKEY_URL,
    MAX_COOKIE_FAIL,
    MAX_NO_SYNCKEY,
    READ_INTERVAL_SECONDS,
    READ_TIMEOUT,
    READ_URL,
    REFRESH_COOKIE_BASE_WAIT,
    REFRESH_COOKIE_MAX_ROUNDS,
    RENEW_TIMEOUT,
    RENEW_URL,
    SECONDS_PER_READ,
    SIGN_KEY,
)
from wereadit.exceptions import CookieExpiredError, ReadFailedError
from wereadit.infra.http import HttpClient
from wereadit.models import ReadResult
from wereadit.utils.crypto import sign_request

logger = logging.getLogger(__name__)


def refresh_cookie(client: HttpClient, cfg: Config) -> str:
    """刷新 cookie 密钥 wr_skey。

    尝试 COOKIE_DATA_VARIANTS 中的多种 payload，成功则更新 client 的 cookie 并返回新 skey。
    失败抛 CookieExpiredError。

    【保活策略 - 启动时与失败后都必须调】
    - 启动时强制刷新:让服务器记录"客户端上线",拿到新鲜 wr_skey
    - read 失败后刷新:被动续期,继续本次循环(不 sleep 不递增 index)
    详见 wxread_keepalive_analysis.md 第 4.1 节。
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
    """尝试多种 payload 刷新 wr_skey，支持多轮重试。

    【保活策略 - 不能简化为 1 种 payload】
    3 种变体对应服务器接口的不同状态，提高续期成功率。
    详见 wxread_keepalive_analysis.md 第 4.2 节。

    Args:
        client: HTTP 客户端
        cfg: 运行时配置
        max_rounds: 总轮数,每轮尝试 3 种 payload,轮间指数退避

    Returns:
        新 wr_skey 前 8 位,或 None(所有轮次全失败)
    """
    for round_idx in range(REFRESH_COOKIE_MAX_ROUNDS):
        for cookie_data in COOKIE_DATA_VARIANTS:
            try:
                response = client.post(
                    RENEW_URL,
                    data=json.dumps(cookie_data, separators=(",", ":")),
                    timeout=RENEW_TIMEOUT,
                )
                if "wr_skey" in response.cookies:
                    return response.cookies["wr_skey"][:8]
            except requests.RequestException as exc:
                logger.warning(
                    "refresh_cookie 请求失败，payload=%s，原因：%s",
                    cookie_data, exc,
                )
                continue
        if round_idx < REFRESH_COOKIE_MAX_ROUNDS - 1:
            wait = REFRESH_COOKIE_BASE_WAIT * (2 ** round_idx)
            logger.info(
                "所有 payload 失败，%ds 后重试第 %d/%d 轮",
                wait, round_idx + 2, REFRESH_COOKIE_MAX_ROUNDS,
            )
            time.sleep(wait)
    return None


def fix_no_synckey(client: HttpClient, cfg: Config) -> None:
    """修复无 synckey 的情况。

    【保活策略 - 不能删除】
    调用 chapterInfos 触发服务器重建阅读上下文。
    长期删除会导致"刷了但时长不增加"。
    详见 wxread_keepalive_analysis.md 第 5.3 节。
    """
    try:
        client.post(
            FIX_SYNCKEY_URL,
            data=json.dumps({"bookIds": FIX_SYNCKEY_BOOK_IDS}, separators=(",", ":")),
            timeout=FIX_SYNCKEY_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("fix_no_synckey 请求失败：%s", exc)


def read_books(client: HttpClient, cfg: Config, refresh_print=None) -> ReadResult:
    """执行阅读循环。

    Args:
        client: HTTP 客户端
        cfg: 运行时配置
        refresh_print: 可选的进度打印函数（行内刷新）

    Returns:
        ReadResult：完成次数与累计分钟数

    Raises:
        ReadFailedError: 连续 MAX_NO_SYNCKEY 次无 synckey（熔断）
        CookieExpiredError: 连续 MAX_COOKIE_FAIL 次 cookie 过期（熔断）

    【保活策略 - 多项关键设计不能改】
    - 启动强制 refresh_cookie: 上线握手,不能删
    - last_time = now - SECONDS_PER_READ: 伪造"已读 30 秒",不能删
    - data.pop("s"): 每次循环开头删除旧签名,不能删
    - b/c 随机选择: 模拟翻不同书,不能改成固定值
    - ts/rn jitter: 风控规避,不能去掉随机
    - sleep(READ_INTERVAL_SECONDS): 30 秒固定节奏,不能调快
    - 失败后不 sleep 不递增 index: 本次重试不计入进度
    详见 wxread_keepalive_analysis.md 第七章调用链。
    """
    # 初始 cookie 刷新（保留原行为：循环前先刷一次）
    # 【保活策略】启动强制 refresh_cookie,不能删
    refresh_cookie(client, cfg)

    data: dict[str, Any] = dict(DEFAULT_READ_DATA)
    index = 1
    # 【保活策略】last_time 初始偏移 30 秒,伪造"已读 30 秒",不能删
    last_time = int(time.time()) - SECONDS_PER_READ
    total = cfg.read_num
    logger.info("需要阅读 %d 次。", total)

    # 熔断计数器：连续失败次数（任一成功分支会清零对应计数器）
    no_synckey_streak = 0
    cookie_fail_streak = 0

    # 运行 metrics 累计计数器
    synckey_success = 0
    no_synckey_fix_triggered = 0
    fix_retry_success = 0
    cookie_refresh_count = 1  # 启动时已刷新 1 次
    circuit_breaker_triggered = False
    last_printed_index = 0  # 进度打印去重：仅在 index 变化时打印

    while index <= total:
        # 【保活策略】删除上次签名,防止用旧 s 字段(服务器会拒)
        data.pop("s", None)
        # 【保活策略】b/c 随机选择,模拟"翻不同书不同章节",不能改成固定值
        data["b"] = random.choice(cfg.books) if cfg.books else data["b"]
        data["c"] = random.choice(cfg.chapters) if cfg.chapters else data["c"]
        this_time = int(time.time())
        data["ct"] = this_time
        # 【保活策略】rt = this_time - last_time,约 30 秒,模拟真实阅读停留
        data["rt"] = this_time - last_time
        # 【保活策略】ts 加 0~1000ms jitter,rn 纯随机,防风控识别
        data["ts"] = int(this_time * 1000) + random.randint(0, 1000)
        data["rn"] = random.randint(0, 1000)
        # 【保活策略】sg/s 签名,服务器校验请求合法性,不能改算法
        sign_request(data, SIGN_KEY)

        if refresh_print and index != last_printed_index:
            last_printed_index = index
            refresh_print(
                f"阅读进度: 第 {index}/{total} 次，已阅读 {(index - 1) * 0.5:.1f} 分钟"
            )
        logger.debug("data: %s", data)

        response = client.post(
            READ_URL,
            data=json.dumps(data, separators=(",", ":")),
            timeout=READ_TIMEOUT,
        )
        res_data = response.json()
        logger.debug("response: %s", res_data)

        if "succ" in res_data:
            # succ 分支：cookie 仍有效，清零 cookie 失败计数
            cookie_fail_streak = 0
            if "synckey" in res_data:
                last_time = this_time
                index += 1
                no_synckey_streak = 0
                synckey_success += 1
                time.sleep(READ_INTERVAL_SECONDS)
            else:
                no_synckey_streak += 1
                if no_synckey_streak >= MAX_NO_SYNCKEY:
                    msg = (
                        f"连续 {MAX_NO_SYNCKEY} 次无 synckey 修复无效，任务中止"
                        f"（已完成 {index - 1}/{total} 次）。"
                        "通常是 cookie 失效或触发风控，请检查 WEREAD_WEB_CURL"
                    )
                    logger.error(msg)
                    circuit_breaker_triggered = True
                    raise ReadFailedError(msg)
                logger.info(
                    "第 %d/%d 次：阅读上下文未同步，已自动修复并重试", index, total
                )
                fix_no_synckey(client, cfg)
                no_synckey_fix_triggered += 1
                # 修复后立即重试一次 read（重新签名，因为 ts/rn 需要更新）
                # 这样不会丢失本次阅读进度
                retry_time = int(time.time())
                data["ct"] = retry_time
                data["rt"] = retry_time - last_time
                data["ts"] = int(retry_time * 1000) + random.randint(0, 1000)
                data["rn"] = random.randint(0, 1000)
                sign_request(data, SIGN_KEY)
                retry_response = client.post(
                    READ_URL,
                    data=json.dumps(data, separators=(",", ":")),
                    timeout=READ_TIMEOUT,
                )
                retry_data = retry_response.json()
                if "synckey" in retry_data:
                    logger.info("第 %d/%d 次：修复成功，继续阅读", index, total)
                    last_time = retry_time
                    index += 1
                    no_synckey_streak = 0
                    synckey_success += 1
                    fix_retry_success += 1
                    time.sleep(READ_INTERVAL_SECONDS)
                    continue
                # 重试仍无 synckey，短暂退避后进入下一轮循环
                backoff_log = (
                    logger.warning
                    if no_synckey_streak >= MAX_NO_SYNCKEY - 1
                    else logger.info
                )
                backoff_log(
                    "第 %d/%d 次：修复未生效，%ds 后重试（连续 %d/%d 次）",
                    index,
                    total,
                    CIRCUIT_BREAKER_BACKOFF,
                    no_synckey_streak,
                    MAX_NO_SYNCKEY,
                )
                time.sleep(CIRCUIT_BREAKER_BACKOFF)
        else:
            # 无 succ：cookie 过期，清零 synckey 计数
            no_synckey_streak = 0
            cookie_fail_streak += 1
            if cookie_fail_streak >= MAX_COOKIE_FAIL:
                msg = (
                    f"连续 {MAX_COOKIE_FAIL} 次 cookie 过期，熔断退出。"
                    f"已完成 {index - 1}/{total} 次。"
                )
                logger.error(msg)
                circuit_breaker_triggered = True
                raise CookieExpiredError(msg)
            logger.warning("cookie 已过期，尝试刷新...")
            refresh_cookie(client, cfg)
            cookie_refresh_count += 1
            time.sleep(CIRCUIT_BREAKER_BACKOFF)

    logger.info("阅读脚本已完成。")
    return ReadResult(
        completed_count=index - 1,
        total_minutes=(index - 1) * 0.5,
        synckey_success=synckey_success,
        no_synckey_fix_triggered=no_synckey_fix_triggered,
        fix_retry_success=fix_retry_success,
        cookie_refresh_count=cookie_refresh_count,
        circuit_breaker_triggered=circuit_breaker_triggered,
    )
