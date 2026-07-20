"""阅读奖励兑换核心逻辑。

从原 exchange.py 迁移，重构点：
- 合并 _query_awards 和 _exchange_single_award 为通用 _call_exchange
- 用 Award/AwardChoice dataclass 替代裸 dict 访问
- 通过 HttpClient 发请求，复用 TCP 连接
- 重试改为指数退避

业务逻辑保持不变：
- 查询所有奖励 -> 过滤可领取 -> 按策略逐个兑换 -> 返回摘要
"""

from __future__ import annotations

import logging
import time
from typing import Any

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
)
from wereadit.exceptions import ExchangeError
from wereadit.infra.http import HttpClient
from wereadit.models import Award, AwardChoice

logger = logging.getLogger(__name__)


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


def exchange_awards(
    client: HttpClient,
    cfg: Config,
) -> str:
    """查询并兑换阅读奖励。

    Args:
        client: HTTP 客户端
        cfg: 运行时配置（包含 access_token / vid / exchange_award / platform）

    Returns:
        兑换结果摘要字符串（用于推送）
    """
    auth_token = cfg.weread_access_token
    vid = cfg.cookies.get("wr_vid", "")
    if not vid:
        logger.warning("cookie 中未找到 wr_vid，跳过兑换")
        return "兑换奖励失败: cookie 中未找到 wr_vid"

    strategy = _parse_strategy(cfg.exchange_award)
    platform_name = "iOS" if cfg.weread_platform == PLATFORM_IOS else "Android"

    # Token 自动续期：如果配置了 WEREAD_LOGIN_CURL，重放 /login 请求刷新 skey
    # 注意：web wr_skey 不能用于 App 接口（2026-07-21 已证实两套独立体系，
    # wr_skey 完整长度仅 8 位，与 App skey 不同），已移除该路径
    from wereadit.core.token_refresher import refresh_app_token

    if cfg.weread_login_curl:
        new_token = refresh_app_token(cfg.weread_login_curl)
        if new_token:
            auth_token = new_token
        else:
            logger.warning("Token 刷新失败，降级使用原 token")

    # 排查 token 过快过期：记录本次使用的 token 前 8 位，便于对应 GitHub Secrets
    token_preview = auth_token[:8] if auth_token else ""
    logger.info(
        "兑换开始: 平台=%s, vid=%s, token=%s...",
        platform_name, vid, token_preview,
    )

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
    # 其他 ExchangeError 转字符串返回；网络异常等非 ExchangeError 直接抛出由上层兜底。
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
        return f"兑换奖励失败: {exc}"

    reading_time = award_data.get("readingTime", 0)
    reading_day = award_data.get("readingDay", 0)
    raw_awards = award_data.get("readtimeAwards", []) + award_data.get("readdayAwards", [])
    awards = [Award.from_dict(a) for a in raw_awards]

    logger.info(
        "本周阅读 %d 天, %.1f 小时, 共 %d 个奖励",
        reading_day,
        reading_time / 3600,
        len(awards),
    )

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

    logger.info("阅读奖励兑换完成 (%s)", platform_name)
    summary = (
        f"阅读奖励兑换完成 ({platform_name})\n"
        f"本周阅读: {reading_day} 天 / {reading_time / 3600:.1f} 小时\n"
        f"兑换: {exchanged_coin} 书币, {exchanged_card} 天体验卡\n"
        f"跳过: {skipped}, 失败: {failed}"
    )
    return summary
