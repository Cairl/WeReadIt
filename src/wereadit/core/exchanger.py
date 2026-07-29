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
    BALANCE_URL,
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
    MEMBER_CARD_SUMMARY_URL,
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


@dataclass
class ExchangeResult:
    """兑换结果（结构化）。

    成功时 error 为空；失败时 error 非空，其余字段为零值。
    可选字段 keep_reading_days 由响应决定是否有值，无值时为 None，
    formatter 会自动跳过对应行。
    """

    reading_time: int = 0  # 本周阅读时长（秒）
    reading_day: int = 0  # 本周阅读天数
    exchanged_coin: int = 0  # 兑换的书币数
    exchanged_card: int | None = None  # 兑换的体验卡天数（None=未兑换体验卡）
    skipped: int = 0  # 跳过的奖励数
    failed: int = 0  # 兑换失败的奖励数
    platform: str = ""  # 平台标识（iOS / Android）
    keep_reading_days: int | None = None  # 连续阅读天数（可选）
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


def _collect_all_numeric_fields(
    data: dict[str, Any], prefix: str = "", depth: int = 0
) -> dict[str, Any]:
    """递归收集响应中所有数值字段，键为点分路径（如 'wallet.bookCoin'）。

    诊断用途：接口响应不符合预期时，用 WARNING 输出全部数值字段，
    让用户在默认 INFO 日志下即可定位问题（如字段改名/结构变化），
    不必开 DEBUG。深度限 3 层。
    """
    if depth > 3:
        return {}
    result: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[path] = value
        elif isinstance(value, dict):
            result.update(_collect_all_numeric_fields(value, path, depth + 1))
    return result


def _fetch_json(
    client: HttpClient,
    method: str,
    url: str,
    name: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """发请求并返回 JSON dict；任何失败都记 WARNING 并返回 None。

    失败判定：网络异常 / 非 200 / 响应非 JSON / errcode 或 errCode 非 0。
    注意 weread 部分接口（如 /web/pay/balance）错误字段是大写驼峰 errCode，
    只查 errcode 会把错误响应当成功响应放行（2026-07-30 实证）。
    """
    try:
        if method == "POST":
            resp = client.post(url, json=json_body, timeout=EXCHANGE_TIMEOUT)
        else:
            resp = client.get(url, timeout=EXCHANGE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 查询失败不应影响主流程
        logger.warning("%s请求异常: %s", name, exc)
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.warning(
            "%s响应非 JSON: HTTP=%s, 原文=%s", name, resp.status_code, resp.text[:500]
        )
        return None

    if not isinstance(data, dict):
        logger.warning("%s响应非对象: HTTP=%s, 原文=%s", name, resp.status_code, resp.text[:500])
        return None

    errcode = data.get("errcode", data.get("errCode"))
    if resp.status_code != 200 or errcode not in (None, 0):
        logger.warning(
            "%s失败: HTTP=%s, errcode=%s, 数值字段=%s",
            name,
            resp.status_code,
            errcode,
            _collect_all_numeric_fields(data),
        )
        return None
    return data


def _pick_number(*vals: object) -> float | None:
    """优先取非零数值，其次 0 值，都无效返回 None。"""
    for v in vals:
        if isinstance(v, int | float) and not isinstance(v, bool) and v > 0:
            return float(v)
    for v in vals:
        if isinstance(v, int | float) and not isinstance(v, bool) and v >= 0:
            return float(v)
    return None


def query_coin_balance(
    client: HttpClient, cfg: Config
) -> tuple[float | None, float | None, int | None]:
    """独立查询书币余额与体验卡剩余（不依赖兑换）。

    2026-07-30 实证结论（config-check 余额探测对真实账号全量字段拍平，
    并经 consumeHistory 交易记录交叉核对金额变动）：

    - /web/pay/balance（POST，web cookie 认证）：
      balance / giftBalance = 本端（iOS）书币钱包余额（含赠币，与 App
      「我-账户」顶部书币数字一致，随兑换/消费实时变动）；
      peerBalance / peerGiftBalance = 对端（Android）余额 / 赠币；
      welfare.expiredTime = 体验卡剩余秒数；
      welfare.showExpiredTime = 体验卡到期时间戳。
    - /web/pay/memberCardSummary（GET）：remainTime = 体验卡剩余秒数
      （与 welfare.expiredTime 同值），expiredTime = 到期时间戳。
      体验卡以此接口为准，welfare 字段作兜底。
    - 赠币没有独立的实时总额接口：/pay/consumeHistory 每条记录的 gift
      字段才是赠币拆分（按条标注 expiringTime），推送只能展示含赠币的
      书币余额。
    - /weekly/exchange 查询与兑换响应均不含余额字段（实测拍平确认），
      余额只能由此处独立查询获得。

    Returns:
        (coin_balance, card_remain_seconds, card_expire_ts)：
        书币余额 / 体验卡剩余秒数 / 体验卡到期时间戳；获取失败的项为 None
    """
    # ---- 书币余额（/web/pay/balance 是唯一来源）----
    coin_balance: float | None = None
    welfare_seconds: float | None = None
    welfare_expire_ts: int | None = None
    balance_data = _fetch_json(
        client,
        "POST",
        BALANCE_URL,
        "书币余额查询",
        json_body={"zoneid": "1", "release": "1", "pf": "weread_wx-2001-iap-2001-iphone"},
    )
    if balance_data is not None:
        gift = balance_data.get("giftBalance")
        total = balance_data.get("balance")
        peer = balance_data.get("peerBalance")
        if cfg.weread_platform == PLATFORM_IOS:
            coin_balance = _pick_number(gift, total, peer)
        else:
            coin_balance = _pick_number(peer, total, gift)
        if coin_balance is None:
            logger.warning(
                "书币余额查询响应未识别到余额字段，数值字段=%s",
                _collect_all_numeric_fields(balance_data),
            )
        # 体验卡兜底字段（memberCardSummary 失败时用）
        welfare = balance_data.get("welfare")
        if isinstance(welfare, dict):
            w_time = welfare.get("expiredTime")
            if isinstance(w_time, int | float) and not isinstance(w_time, bool):
                welfare_seconds = float(w_time)
            w_expire = welfare.get("showExpiredTime")
            if isinstance(w_expire, int | float) and not isinstance(w_expire, bool):
                welfare_expire_ts = int(w_expire)

    # ---- 体验卡（memberCardSummary 为准，welfare 兜底）----
    card_seconds: float | None = None
    card_expire_ts: int | None = None
    pf = "ios" if cfg.weread_platform == PLATFORM_IOS else "android"
    card_data = _fetch_json(
        client, "GET", f"{MEMBER_CARD_SUMMARY_URL}?pf={pf}", "体验卡查询"
    )
    if card_data is not None:
        remain = card_data.get("remainTime")
        if isinstance(remain, int | float) and not isinstance(remain, bool):
            card_seconds = float(remain)
        expire = card_data.get("expiredTime")
        if isinstance(expire, int | float) and not isinstance(expire, bool):
            card_expire_ts = int(expire)

    if card_seconds is None:
        card_seconds = welfare_seconds
    if card_expire_ts is None:
        card_expire_ts = welfare_expire_ts

    return coin_balance, card_seconds, card_expire_ts


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
    raw_awards = award_data.get("readtimeAwards", []) + award_data.get("readdayAwards", [])
    awards = [Award.from_dict(a) for a in raw_awards]

    # 逐个兑换
    exchanged_card = 0
    exchange_happened = False  # 是否发生过成功兑换（区分"未发生兑换"与"真实0"）
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
            exchange_happened = True
            if choice_type == CHOICE_CARD:
                exchanged_card += choice.award_num
            else:
                exchanged_coin += choice.award_num
        else:
            logger.error("兑换 %s 失败（重试 %d 次）", award.award_level_desc, EXCHANGE_MAX_RETRY)
            failed += 1

    # 无可兑换奖励：查询成功但无实际兑换（awards 为空或全部被策略/状态跳过），
    # 追加状态回显，避免"开始兑换阅读奖励..."之后无任何后续日志
    if exchanged_coin == 0 and exchanged_card == 0 and failed == 0:
        logger.info("无需兑换阅读奖励")

    return ExchangeResult(
        reading_time=reading_time,
        reading_day=reading_day,
        exchanged_coin=exchanged_coin,
        exchanged_card=exchanged_card if exchange_happened else None,
        skipped=skipped,
        failed=failed,
        platform=platform_name,
        keep_reading_days=keep_reading_days,
    )
