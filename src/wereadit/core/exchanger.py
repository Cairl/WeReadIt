"""阅读奖励兑换核心逻辑。

从原 exchange.py 迁移，重构点：
- 合并 _query_awards 和 _exchange_single_award 为通用 _call_exchange
- 用 Award/AwardChoice dataclass 替代裸 dict 访问
- 通过 HttpClient 发请求，复用 TCP 连接
- 重试改为指数退避

业务逻辑保持不变：
- 查询所有奖励 -> 过滤可领取 -> 按策略逐个兑换 -> 返回 ExchangeResult
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wereadit.core.token_refresher import RefreshResult

from wereadit.config import Config
from wereadit.constants import (
    ANDROID_APPVER,
    ANDROID_BASEAPI,
    ANDROID_BASEVER,
    ANDROID_CHANNEL_ID,
    ANDROID_OSVER,
    ANDROID_PF,
    ANDROID_UA,
    AWARD_LEVEL_IDS,
    CHOICE_CARD,
    CHOICE_NONE,
    ERRCODE_TOKEN_EXPIRED,
    EXCHANGE_MAX_RETRY,
    EXCHANGE_RETRY_INTERVAL,
    EXCHANGE_TIMEOUT,
    EXCHANGE_URL,
    IOS_BASEVER,
    IOS_CHANNEL_ID,
    IOS_PF,
    IOS_UA,
    IOS_V,
    PLATFORM_IOS,
    TOKEN_MAX_AGE_SECONDS,
)
from wereadit.exceptions import ExchangeError
from wereadit.infra.http import HttpClient
from wereadit.models import Award, AwardChoice

logger = logging.getLogger(__name__)

# 响应中可能表示"连续阅读天数"的字段名（按优先级尝试，取首个非空值）
_KEEP_READING_KEYS = (
    "keepReadingDays",
    "continuousReadDays",
    "totalReadDay",
    "totalReadDays",
)
# 响应中可能表示"书币钱包余额"的字段名（按优先级尝试）
_COIN_BALANCE_KEYS = (
    "bookCoin",
    "bookCoinBalance",
    "walletCoin",
    "userCoin",
    "coin",
)


@dataclass
class ExchangeResult:
    """兑换结果（结构化）。

    成功时 error 为空；失败时 error 非空，其余字段为零值。
    可选字段（keep_reading_days / coin_balance）由响应决定是否有值，
    无值时为 None，formatter 会自动跳过对应行。
    """

    reading_time: int = 0  # 本周阅读时长（秒）
    reading_day: int = 0  # 本周阅读天数
    exchanged_coin: int = 0  # 兑换的书币数
    exchanged_card: int = 0  # 兑换的体验卡天数
    skipped: int = 0  # 跳过的奖励数
    failed: int = 0  # 兑换失败的奖励数
    platform: str = ""  # 平台标识（iOS / Android）
    keep_reading_days: int | None = None  # 连续阅读天数（可选）
    coin_balance: float | None = None  # 书币钱包余额（可选）
    error: str = ""  # 兑换错误描述（非空表示兑换失败）


def _build_headers(auth_token: str, vid: str, platform: str) -> dict[str, str]:
    """根据平台构造 APP 端请求头。"""
    if platform == PLATFORM_IOS:
        return {
            "skey": auth_token,
            "vid": str(vid),
            "channelid": IOS_CHANNEL_ID,
            "basever": IOS_BASEVER,
            "v": IOS_V,
            "User-Agent": IOS_UA,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "zh-Hans-CN;q=1",
        }
    # Android 默认
    return {
        "accessToken": auth_token,
        "vid": str(vid),
        "baseapi": ANDROID_BASEAPI,
        "appver": ANDROID_APPVER,
        "User-Agent": ANDROID_UA,
        "osver": ANDROID_OSVER,
        "channelId": ANDROID_CHANNEL_ID,
        "basever": ANDROID_BASEVER,
        "Content-Type": "application/json; charset=UTF-8",
    }


def _get_pf(platform: str) -> str:
    """根据平台返回 pf 标识。"""
    if platform == PLATFORM_IOS:
        return IOS_PF
    return ANDROID_PF


def _call_exchange(
    client: HttpClient,
    auth_token: str,
    vid: str,
    platform: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """统一的兑换接口调用，处理 errcode 检查。

    查询和兑换共用此函数，由 body 中的字段区分。

    排查日志：失败时打印 HTTP 状态码、errcode、errmsg、响应体片段，
    用于定位 token 过快过期是自然失效还是风控作废。
    """
    headers = _build_headers(auth_token, vid, platform)
    logger.debug("兑换请求 body: %s", body)
    response = client.post(
        EXCHANGE_URL,
        json=body,
        headers=headers,
        timeout=EXCHANGE_TIMEOUT,
    )
    data = response.json()
    if response.status_code != 200 or "errcode" in data:
        errcode = data.get("errcode", "unknown")
        errmsg = data.get("errmsg", "unknown")
        # 排查 token 过快过期：记录完整失败信息（响应体截断到 500 字符避免刷屏）
        logger.warning(
            "兑换接口失败: HTTP=%s, errcode=%s, errmsg=%s, token=%s..., 响应体=%s",
            response.status_code,
            errcode,
            errmsg,
            auth_token[:8] if auth_token else "",
            str(data)[:500],
        )
        raise ExchangeError(
            f"兑换接口失败: HTTP {response.status_code}, errcode={errcode}, errmsg={errmsg}",
            errcode if isinstance(errcode, int) else None,
        )
    return data


def _parse_strategy(strategy_str: str) -> dict[int, int]:
    """解析兑换策略字符串，返回 {award_level_id: choice_type} 映射。"""
    if not strategy_str:
        strategy_str = "2,2,2,2,2,2,2,2"
    parts = [int(x.strip()) for x in strategy_str.split(",")]
    if len(parts) != len(AWARD_LEVEL_IDS):
        raise ValueError(
            f"兑换策略格式错误: 需要 {len(AWARD_LEVEL_IDS)} 个值, 得到 {len(parts)}"
        )
    return {AWARD_LEVEL_IDS[i]: parts[i] for i in range(len(AWARD_LEVEL_IDS))}


def _extract_keep_reading_days(award_data: dict[str, Any]) -> int | None:
    """从容错字段名列表中提取连续阅读天数，未找到返回 None。"""
    for key in _KEEP_READING_KEYS:
        value = award_data.get(key)
        if isinstance(value, int | float) and value > 0:
            return int(value)
    return None


def _extract_coin_balance(award_data: dict[str, Any]) -> float | None:
    """从容错字段名列表中提取书币钱包余额，未找到返回 None。

    微信读书书币余额可能以整数（书币数）或小数（元）形式返回，统一转 float。
    """
    for key in _COIN_BALANCE_KEYS:
        value = award_data.get(key)
        if isinstance(value, int | float) and value > 0:
            return float(value)
    return None


def exchange_awards(
    client: HttpClient,
    cfg: Config,
    *,
    refresher: Callable[[], RefreshResult] | None = None,
    token_refreshed_at: float | None = None,
) -> ExchangeResult:
    """查询并兑换阅读奖励。

    Args:
        client: HTTP 客户端
        cfg: 运行时配置（token 应由调用方在阅读前刷新并注入）
        refresher: 可选的 token 刷新回调（补刷保险用）
        token_refreshed_at: token 刷新时刻（time.time() 返回值），与 refresher
            配合；兑换前 token 年龄超过 TOKEN_MAX_AGE_SECONDS 时调 refresher 补刷

    Returns:
        ExchangeResult：结构化兑换结果，成功时 error 为空，失败时 error 非空。

    Raises:
        ExchangeError: Token 过期（errcode==-2012），由调用方处理告警。
    """
    auth_token = cfg.weread_access_token
    vid = cfg.cookies.get("wr_vid", "")
    if not vid:
        logger.warning("cookie 中未找到 wr_vid，跳过兑换")
        return ExchangeResult(error="cookie 中未找到 wr_vid")

    # 补刷保险：阅读耗时过长导致 token 年龄接近 2 小时有效期时，兑换前再刷一次
    if (
        refresher is not None
        and token_refreshed_at is not None
        and time.time() - token_refreshed_at > TOKEN_MAX_AGE_SECONDS
    ):
        logger.info("token 年龄超过 %ds，兑换前补刷...", TOKEN_MAX_AGE_SECONDS)
        refresh_result = refresher()
        if refresh_result.ok:
            auth_token = refresh_result.token
            logger.info("补刷成功, 新 token=%s...", auth_token[:8])
        else:
            logger.warning("补刷失败，沿用原 token: %s", refresh_result.diagnosis)

    strategy = _parse_strategy(cfg.exchange_award)
    platform_name = "iOS" if cfg.weread_platform == PLATFORM_IOS else "Android"

    # 排查 token 过快过期：记录本次使用的 token 前 8 位，便于对应 GitHub Secrets
    token_preview = auth_token[:8] if auth_token else ""

    # 查询
    query_body = {
        "awardLevelId": 0,
        "isExchangeAward": 0,
        "isVisitReadGoal": 1,
        "unread": 0,
        "pf": _get_pf(cfg.weread_platform),
        "awardChoiceType": 0,
    }
    # 查询失败不重试（与兑换循环不同）：Token 过期 re-raise 由 app.py 处理，
    # 其他 ExchangeError 转 ExchangeResult.error 返回；网络异常等非 ExchangeError
    # 直接抛出由上层兜底。
    try:
        award_data = _call_exchange(client, auth_token, vid, cfg.weread_platform, query_body)
    except ExchangeError as exc:
        if exc.errcode == ERRCODE_TOKEN_EXPIRED:
            logger.warning(
                "查询奖励时 Token 已过期 (errcode=%s), token=%s..., 请重新抓包更新 Secret",
                exc.errcode, token_preview,
            )
            raise
        logger.error("查询奖励失败: %s", exc)
        return ExchangeResult(error=str(exc))

    reading_time = award_data.get("readingTime", 0)
    reading_day = award_data.get("readingDay", 0)
    keep_reading_days = _extract_keep_reading_days(award_data)
    coin_balance = _extract_coin_balance(award_data)
    raw_awards = award_data.get("readtimeAwards", []) + award_data.get("readdayAwards", [])
    awards = [Award.from_dict(a) for a in raw_awards]

    # 逐个兑换
    exchanged_card = 0
    exchanged_coin = 0
    skipped = 0
    failed = 0

    for award in awards:
        if award.award_status != 1:
            logger.debug(
                "跳过 %s (awardLevelId=%s): status=%s",
                award.award_level_desc,
                award.award_level_id,
                award.award_status,
            )
            skipped += 1
            continue

        choice_type = strategy.get(award.award_level_id, CHOICE_NONE)
        if choice_type == CHOICE_NONE:
            logger.info("跳过 %s: 策略为不兑换", award.award_level_desc)
            skipped += 1
            continue

        choice: AwardChoice | None = award.find_choice(choice_type)
        if not choice or not choice.can_choice:
            logger.warning(
                "跳过 %s: choiceType=%s 不可兑换", award.award_level_desc, choice_type
            )
            skipped += 1
            continue

        choice_name = "体验卡" if choice_type == CHOICE_CARD else "书币"

        # 执行兑换（带指数退避重试）
        success = False
        for attempt in range(EXCHANGE_MAX_RETRY):
            try:
                exchange_body = {
                    "awardLevelId": award.award_level_id,
                    "isExchangeAward": 1,
                    "isVisitReadGoal": 1,
                    "unread": 0,
                    "pf": _get_pf(cfg.weread_platform),
                    "awardChoiceType": choice_type,
                }
                _call_exchange(
                    client, auth_token, vid, cfg.weread_platform, exchange_body
                )
                success = True
                break
            except ExchangeError as exc:
                if exc.errcode == ERRCODE_TOKEN_EXPIRED:
                    logger.warning(
                        "兑换 %s 时 Token 已过期 (errcode=%s), token=%s..., 请重新抓包更新 Secret",
                        award.award_level_desc, exc.errcode, token_preview,
                    )
                    raise
                logger.warning(
                    "兑换 %s 第 %d/%d 次失败: %s",
                    award.award_level_desc,
                    attempt + 1,
                    EXCHANGE_MAX_RETRY,
                    exc,
                )
                if attempt < EXCHANGE_MAX_RETRY - 1:
                    time.sleep(EXCHANGE_RETRY_INTERVAL * (2**attempt))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "兑换 %s 网络异常第 %d/%d 次: %s",
                    award.award_level_desc,
                    attempt + 1,
                    EXCHANGE_MAX_RETRY,
                    exc,
                )
                if attempt < EXCHANGE_MAX_RETRY - 1:
                    time.sleep(EXCHANGE_RETRY_INTERVAL * (2**attempt))

        if success:
            logger.info(
                "兑换 %s 成功: %d %s", award.award_level_desc, choice.award_num, choice_name
            )
            if choice_type == CHOICE_CARD:
                exchanged_card += choice.award_num
            else:
                exchanged_coin += choice.award_num
        else:
            logger.error("兑换 %s 失败（重试 %d 次）", award.award_level_desc, EXCHANGE_MAX_RETRY)
            failed += 1

    return ExchangeResult(
        reading_time=reading_time,
        reading_day=reading_day,
        exchanged_coin=exchanged_coin,
        exchanged_card=exchanged_card,
        skipped=skipped,
        failed=failed,
        platform=platform_name,
        keep_reading_days=keep_reading_days,
        coin_balance=coin_balance,
    )
