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

import enum
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


class ReadStatus(enum.Enum):
    """单次 read 的结果分类。"""

    SYNCED = "synced"               # 首次尝试即含 synckey
    SYNCED_VIA_FIX = "synced_via_fix"  # 无 synckey → fix 后重试成功
    NO_SYNCKEY = "no_synckey"       # fix 后重试仍无 synckey
    COOKIE_EXPIRED = "cookie_expired"  # 无 succ（cookie 失效）


def _prepare_data(data: dict[str, Any], cfg: Config, last_time: int) -> int:
    """构造单次 read 请求体并返回当前时间戳（保活字段全部保留）。"""
    now = int(time.time())
    data.pop("s", None)
    data["b"] = random.choice(cfg.books) if cfg.books else data["b"]
    data["c"] = random.choice(cfg.chapters) if cfg.chapters else data["c"]
    data["ct"] = now
    data["rt"] = now - last_time
    data["ts"] = int(now * 1000) + random.randint(0, 1000)
    data["rn"] = random.randint(0, 1000)
    sign_request(data, SIGN_KEY)
    return now


def _read_once(
    client: HttpClient, cfg: Config, data: dict[str, Any], last_time: int
) -> tuple[ReadStatus, int, bool]:
    """执行一次 read；无 synckey 时 fix 后内重试一次。

    返回 (状态, 成功时间戳 or 本次时间戳, 是否经 fix 重试成功)。
    """
    now = _prepare_data(data, cfg, last_time)
    res = client.post(
        READ_URL,
        data=json.dumps(data, separators=(",", ":")),
        timeout=READ_TIMEOUT,
    )
    res_data = res.json()
    if "succ" not in res_data:
        return (ReadStatus.COOKIE_EXPIRED, now, False)
    if "synckey" in res_data:
        return (ReadStatus.SYNCED, now, False)
    # 无 synckey → fix 后内重试一次
    fix_no_synckey(client, cfg)
    retry_now = _prepare_data(data, cfg, last_time)
    retry_res = client.post(
        READ_URL,
        data=json.dumps(data, separators=(",", ":")),
        timeout=READ_TIMEOUT,
    )
    retry_data = retry_res.json()
    if "succ" in retry_data and "synckey" in retry_data:
        return (ReadStatus.SYNCED_VIA_FIX, retry_now, True)
    return (ReadStatus.NO_SYNCKEY, retry_now, False)


def _warmup(client: HttpClient, cfg: Config, data: dict[str, Any]) -> tuple[int, int]:
    """预热阶段：循环 _read_once 直到 synckey 出现，不计入阅读次数。

    返回 (成功时的 last_time, 尝试次数)。熔断规则与主循环一致。
    """
    logger.info("开始预热：建立阅读上下文（不计入阅读次数）")
    last_time = int(time.time()) - SECONDS_PER_READ
    no_synckey_streak = 0
    cookie_fail_streak = 0
    attempts = 0
    while True:
        attempts += 1
        status, now, via_fix = _read_once(client, cfg, data, last_time)
        if status is ReadStatus.SYNCED or status is ReadStatus.SYNCED_VIA_FIX:
            if via_fix:
                logger.info("预热：阅读上下文未同步，已自动修复并重试")
                logger.info("预热：修复成功，上下文已建立（尝试 %d 次）。", attempts)
            else:
                logger.info("预热成功，上下文已建立（尝试 %d 次）。", attempts)
            return (now, attempts)
        if status is ReadStatus.COOKIE_EXPIRED:
            cookie_fail_streak += 1
            if cookie_fail_streak >= MAX_COOKIE_FAIL:
                msg = f"预热阶段连续 {MAX_COOKIE_FAIL} 次 cookie 过期，熔断退出。"
                logger.error(msg)
                raise CookieExpiredError(msg)
            logger.warning("预热：cookie 已过期，尝试刷新...")
            refresh_cookie(client, cfg)
            time.sleep(CIRCUIT_BREAKER_BACKOFF)
            continue
        # NO_SYNCKEY
        no_synckey_streak += 1
        if no_synckey_streak >= MAX_NO_SYNCKEY:
            msg = (
                f"预热阶段连续 {MAX_NO_SYNCKEY} 次无 synckey 修复无效，任务中止"
                f"（已完成 0/{cfg.read_num} 次）。请检查 WEREAD_WEB_CURL"
            )
            logger.error(msg)
            raise ReadFailedError(msg)
        logger.info("预热：阅读上下文未同步，已自动修复并重试")
        backoff_log = (
            logger.warning if no_synckey_streak >= MAX_NO_SYNCKEY - 1 else logger.info
        )
        backoff_log(
            "预热：修复未生效，%ds 后重试（连续 %d/%d 次）",
            CIRCUIT_BREAKER_BACKOFF,
            no_synckey_streak,
            MAX_NO_SYNCKEY,
        )
        time.sleep(CIRCUIT_BREAKER_BACKOFF)


def read_books(client: HttpClient, cfg: Config) -> ReadResult:
    """执行阅读循环。

    Returns:
        ReadResult：完成次数与累计分钟数

    Raises:
        ReadFailedError: 连续 MAX_NO_SYNCKEY 次无 synckey（预热或主循环熔断）
        CookieExpiredError: 连续 MAX_COOKIE_FAIL 次 cookie 过期（熔断）

    【保活策略 - 多项关键设计不能改】
    - 启动强制 refresh_cookie: 上线握手,不能删
    - last_time = now - SECONDS_PER_READ: 伪造"已读 30 秒",不能删
    - data.pop("s"): 每次循环开头删除旧签名,不能删
    - b/c 随机选择: 模拟翻不同书,不能改成固定值
    - ts/rn jitter: 风控规避,不能去掉随机
    - sleep(READ_INTERVAL_SECONDS): 30 秒固定节奏,不能调快
    - 失败后不 sleep 不递增 index: 本次重试不计入进度
    """
    # 启动强制刷新（【保活策略】不能删）
    refresh_cookie(client, cfg)

    data: dict[str, Any] = dict(DEFAULT_READ_DATA)
    total = cfg.read_num
    logger.info("需要阅读 %d 次。", total)

    # 熔断计数器
    no_synckey_streak = 0
    cookie_fail_streak = 0

    # 运行 metrics 累计计数器（仅统计主循环 120 次；预热不计入）
    synckey_success = 0
    no_synckey_fix_triggered = 0
    fix_retry_success = 0
    cookie_refresh_count = 1  # 启动时已刷新 1 次
    circuit_breaker_triggered = False
    warmup_attempts = 0
    last_printed_index = 0  # 进度打印去重：仅在 index 变化时打印

    # 预热阶段：建立上下文，不计入阅读次数
    last_time, warmup_attempts = _warmup(client, cfg, data)

    # 主循环：120 次干净阅读
    index = 1
    while index <= total:
        status, now, via_fix = _read_once(client, cfg, data, last_time)
        if status is ReadStatus.SYNCED or status is ReadStatus.SYNCED_VIA_FIX:
            last_time = now
            no_synckey_streak = 0
            cookie_fail_streak = 0
            if index != last_printed_index:
                last_printed_index = index
                logger.info(
                    "阅读进度: 第 %d/%d 次，已阅读 %.1f 分钟",
                    index, total, (index - 1) * 0.5,
                )
            index += 1
            synckey_success += 1
            if via_fix:
                fix_retry_success += 1
                logger.info("第 %d/%d 次：修复成功，继续阅读", index - 1, total)
            time.sleep(READ_INTERVAL_SECONDS)
            continue
        if status is ReadStatus.COOKIE_EXPIRED:
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
            continue
        # NO_SYNCKEY（预热后应极少发生；保留兜底）
        no_synckey_streak += 1
        no_synckey_fix_triggered += 1
        if no_synckey_streak >= MAX_NO_SYNCKEY:
            msg = (
                f"连续 {MAX_NO_SYNCKEY} 次无 synckey 修复无效，任务中止"
                f"（已完成 {index - 1}/{total} 次）。"
                "通常是 cookie 失效或触发风控，请检查 WEREAD_WEB_CURL"
            )
            logger.error(msg)
            circuit_breaker_triggered = True
            raise ReadFailedError(msg)
        logger.info("第 %d/%d 次：阅读上下文未同步，已自动修复并重试", index, total)
        backoff_log = (
            logger.warning if no_synckey_streak >= MAX_NO_SYNCKEY - 1 else logger.info
        )
        backoff_log(
            "第 %d/%d 次：修复未生效，%ds 后重试（连续 %d/%d 次）",
            index, total, CIRCUIT_BREAKER_BACKOFF,
            no_synckey_streak, MAX_NO_SYNCKEY,
        )
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
        warmup_done=True,
        warmup_attempts=warmup_attempts,
    )
