# exchange.py 自动兑换阅读奖励
import logging
import time

import requests

logger = logging.getLogger(__name__)

# 接口地址
EXCHANGE_URL = "https://i.weread.qq.com/weekly/exchange"

# 平台标识
PLATFORM_ANDROID = "android"
PLATFORM_IOS = "ios"

# Android 平台常量（从 Android HAR 抓包确认）
ANDROID_UA = (
    "WeRead/8.2.6 WRBrand/other Dalvik/2.1.0 "
    "(Linux; U; Android 14; 25102RKBEC Build/UQ1A.240205.07021608)"
)
ANDROID_BASEAPI = "34"
ANDROID_APPVER = "8.2.6.10163989"
ANDROID_OSVER = "14"
ANDROID_CHANNEL_ID = "0"
ANDROID_BASEVER = "8.2.6.10163989"
ANDROID_PF = "wechat_wx-2001-android-100-weread"

# iOS 平台常量（从 iOS HAR 抓包确认）
IOS_UA = "WeRead/10.2.0 (iPhone; iOS 26.5.2; Scale/3.00)"
IOS_BASEVER = "10.2.0.85"
IOS_V = "10.2.0.85"
IOS_CHANNEL_ID = "AppStore"
IOS_PF = "weread_wx-2001-iap-2001-iphone"

# 奖励等级 ID 顺序（对应 EXCHANGE_AWARD 策略字符串的 8 个位置）
# 顺序：5 个时长奖励（4,5,1,2,3）+ 3 个天数奖励（11,12,13）
AWARD_LEVEL_IDS = [4, 5, 1, 2, 3, 11, 12, 13]

# 兑换选择类型
CHOICE_NONE = 0  # 不兑换
CHOICE_CARD = 1  # 体验卡
CHOICE_COIN = 2  # 书币

# accessToken/skey 过期错误码
ERRCODE_TOKEN_EXPIRED = -2012

# 兑换重试次数
MAX_RETRY = 3
RETRY_INTERVAL = 5  # 秒


class ExchangeError(Exception):
    """兑换异常，携带 errcode 便于上层判断"""

    def __init__(self, message, errcode=None):
        super().__init__(message)
        self.errcode = errcode


def _build_headers(auth_token, vid, platform):
    """根据平台构造 APP 端请求头

    Args:
        auth_token: Android 平台为 accessToken 值，iOS 平台为 skey 值
        vid: 用户 vid
        platform: PLATFORM_ANDROID 或 PLATFORM_IOS
    """
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


def _get_pf(platform):
    """根据平台返回 pf 标识"""
    if platform == PLATFORM_IOS:
        return IOS_PF
    return ANDROID_PF


def _query_awards(auth_token, vid, platform):
    """查询可兑换的阅读奖励列表"""
    headers = _build_headers(auth_token, vid, platform)
    body = {
        "awardLevelId": 0,
        "isExchangeAward": 0,
        "isVisitReadGoal": 1,
        "unread": 0,
        "pf": _get_pf(platform),
        "awardChoiceType": 0,
    }
    response = requests.post(EXCHANGE_URL, headers=headers, json=body, timeout=15)
    data = response.json()
    if response.status_code != 200 or "errcode" in data:
        errcode = data.get("errcode", "unknown")
        errmsg = data.get("errmsg", "unknown")
        raise ExchangeError(
            f"查询奖励失败: HTTP {response.status_code}, errcode={errcode}, errmsg={errmsg}",
            errcode,
        )
    return data


def _exchange_single_award(auth_token, vid, platform, award_level_id, choice_type):
    """兑换单个奖励"""
    headers = _build_headers(auth_token, vid, platform)
    body = {
        "awardLevelId": award_level_id,
        "isExchangeAward": 1,
        "isVisitReadGoal": 1,
        "unread": 0,
        "pf": _get_pf(platform),
        "awardChoiceType": choice_type,
    }
    response = requests.post(EXCHANGE_URL, headers=headers, json=body, timeout=15)
    data = response.json()
    if response.status_code != 200 or "errcode" in data:
        errcode = data.get("errcode", "unknown")
        errmsg = data.get("errmsg", "unknown")
        raise ExchangeError(
            f"兑换奖励失败: HTTP {response.status_code}, errcode={errcode}, errmsg={errmsg}",
            errcode,
        )
    return data


def _parse_strategy(strategy_str):
    """解析兑换策略字符串，返回 {award_level_id: choice_type} 映射"""
    if not strategy_str:
        strategy_str = "2,2,2,2,2,2,2,2"
    parts = [int(x.strip()) for x in strategy_str.split(",")]
    if len(parts) != len(AWARD_LEVEL_IDS):
        raise ValueError(
            f"兑换策略格式错误: 需要 {len(AWARD_LEVEL_IDS)} 个值, 得到 {len(parts)}"
        )
    return {AWARD_LEVEL_IDS[i]: parts[i] for i in range(len(AWARD_LEVEL_IDS))}


def exchange_awards(auth_token, vid, exchange_strategy, platform=PLATFORM_ANDROID):
    """
    查询并兑换阅读奖励。

    Args:
        auth_token: APP 端认证 token
                    - Android 平台为 accessToken 值
                    - iOS 平台为 skey 值
        vid: 用户 vid（从网页 cookie 的 wr_vid 提取）
        exchange_strategy: 8 位兑换策略字符串，如 "2,2,2,2,2,2,2,2"
        platform: 平台标识，PLATFORM_ANDROID 或 PLATFORM_IOS

    Returns:
        str: 兑换结果摘要

    Raises:
        ExchangeError: 认证 token 过期或查询失败时抛出
    """
    strategy = _parse_strategy(exchange_strategy)
    platform_name = "iOS" if platform == PLATFORM_IOS else "Android"
    logging.info("兑换平台: %s", platform_name)

    # 查询
    award_data = _query_awards(auth_token, vid, platform)

    reading_time = award_data.get("readingTime", 0)
    reading_day = award_data.get("readingDay", 0)
    all_awards = award_data.get("readtimeAwards", []) + award_data.get("readdayAwards", [])

    logging.info(
        "本周阅读 %d 天, %.1f 小时, 共 %d 个奖励",
        reading_day,
        reading_time / 3600,
        len(all_awards),
    )

    # 逐个兑换
    exchanged_card = 0
    exchanged_coin = 0
    skipped = 0
    failed = 0

    for award in all_awards:
        award_level_id = award.get("awardLevelId")
        award_status = award.get("awardStatus")
        award_desc = award.get("awardLevelDesc", "")

        if award_status != 1:
            logging.debug("跳过 %s (awardLevelId=%s): status=%s", award_desc, award_level_id, award_status)
            skipped += 1
            continue

        choice_type = strategy.get(award_level_id, CHOICE_NONE)
        if choice_type == CHOICE_NONE:
            logging.info("跳过 %s: 策略为不兑换", award_desc)
            skipped += 1
            continue

        # 找对应的选择项
        choices = award.get("awardChoices", [])
        choice = next((c for c in choices if c.get("choiceType") == choice_type), None)
        if not choice or choice.get("canChoice") != 1:
            logging.warning("跳过 %s: choiceType=%s 不可兑换", award_desc, choice_type)
            skipped += 1
            continue

        award_num = choice.get("awardNum", 0)
        choice_name = "体验卡" if choice_type == CHOICE_CARD else "书币"

        # 执行兑换（带重试）
        success = False
        for attempt in range(MAX_RETRY):
            try:
                _exchange_single_award(auth_token, vid, platform, award_level_id, choice_type)
                success = True
                break
            except ExchangeError as exc:
                if exc.errcode == ERRCODE_TOKEN_EXPIRED:
                    raise  # token 过期，直接向上抛
                logging.warning("兑换 %s 第 %d/%d 次失败: %s", award_desc, attempt + 1, MAX_RETRY, exc)
                if attempt < MAX_RETRY - 1:
                    time.sleep(RETRY_INTERVAL)
            except requests.RequestException as exc:
                logging.warning("兑换 %s 网络异常第 %d/%d 次: %s", award_desc, attempt + 1, MAX_RETRY, exc)
                if attempt < MAX_RETRY - 1:
                    time.sleep(RETRY_INTERVAL)

        if success:
            logging.info("兑换 %s 成功: %d %s", award_desc, award_num, choice_name)
            if choice_type == CHOICE_CARD:
                exchanged_card += award_num
            else:
                exchanged_coin += award_num
        else:
            logging.error("兑换 %s 失败（重试 %d 次）", award_desc, MAX_RETRY)
            failed += 1

    summary = (
        f"阅读奖励兑换完成 ({platform_name})\n"
        f"本周阅读: {reading_day} 天 / {reading_time / 3600:.1f} 小时\n"
        f"兑换: {exchanged_coin} 书币, {exchanged_card} 天体验卡\n"
        f"跳过: {skipped}, 失败: {failed}"
    )
    logging.info(summary)
    return summary
