"""应用编排：阅读 -> 兑换 -> 推送。

把原 main.py 顶层的脚本式逻辑封装为 main() 函数，
便于：1) 被 __main__.py 调用；2) 被单元测试 import；3) 被其他场景复用。

顶层异常兜底：所有未捕获异常统一推送通知，避免静默失败。
"""

from __future__ import annotations

import dataclasses
import logging
import time
import traceback
from functools import partial

from wereadit.config import Config, load_config
from wereadit.constants import ERRCODE_TOKEN_EXPIRED, PLATFORM_IOS
from wereadit.exceptions import CookieExpiredError, ExchangeError, ReadFailedError
from wereadit.infra.http import HttpClient
from wereadit.push import push

logger = logging.getLogger(__name__)


def _replace_token(cfg: Config, new_token: str) -> Config:
    """按平台替换 Config 中对应的 token 字段（frozen dataclass 用 replace 派生新实例）。"""
    if cfg.weread_platform == PLATFORM_IOS:
        return dataclasses.replace(cfg, weread_ios_token=new_token)
    return dataclasses.replace(cfg, weread_android_token=new_token)


def _token_key_matches_platform(token_key: str, platform: str) -> bool:
    """校验刷新得到的 token 字段名与兑换平台是否匹配。

    iOS 登录响应下发 skey，Android 下发 accessToken；
    错位说明 login curl 与兑换 Token 抓自不同平台的设备。
    """
    if platform == PLATFORM_IOS:
        return token_key == "skey"
    return token_key != "skey"


def main() -> int:
    """主入口：阅读 -> 兑换 -> 推送。

    Returns:
        进程退出码：0=成功，1=失败
    """
    from wereadit.utils.logging import make_refresh_print

    refresh_print = make_refresh_print()
    cfg = load_config()
    client = HttpClient(headers=cfg.headers, cookies=cfg.cookies)

    push_method = cfg.push_method
    exchange_summary = ""
    exit_code = 0
    has_failure = False  # 推送状态标志：兑换失败但阅读成功时为 False；Token 过期等致命错误为 True

    try:
        # 兑换 Token 自动续期：阅读前刷新，保证兑换时 token 年龄在 2 小时窗口内
        # （旧设计在兑换前刷新，阅读 60+ 分钟后 token 年龄贴近有效期边缘）
        refresh_diagnosis = ""
        refresher = None
        token_refreshed_at = None
        if cfg.weread_access_token and cfg.weread_login_curl:
            from wereadit.core.token_refresher import (
                diagnose_login_curl,
                refresh_app_token,
            )

            curl_diagnosis = diagnose_login_curl(cfg.weread_login_curl)
            if curl_diagnosis:
                logger.warning("WEREAD_LOGIN_CURL 体检不过: %s", curl_diagnosis)
                refresh_diagnosis = f"Token 自动刷新已跳过：{curl_diagnosis}"
            else:
                refresher = partial(refresh_app_token, cfg.weread_login_curl)
                refresh_result = refresher()
                if refresh_result.ok and _token_key_matches_platform(
                    refresh_result.token_key, cfg.weread_platform
                ):
                    cfg = _replace_token(cfg, refresh_result.token)
                    token_refreshed_at = time.time()
                    logger.info(
                        "兑换 Token 已在阅读前刷新: %s...", refresh_result.token[:8]
                    )
                elif refresh_result.ok:
                    refresh_diagnosis = (
                        f"刷新得到的凭证类型 ({refresh_result.token_key}) 与兑换平台不匹配，"
                        "WEREAD_LOGIN_CURL 与兑换 Token 似乎抓自不同平台的设备，"
                        "请统一为同一台设备的抓包"
                    )
                    logger.warning(refresh_diagnosis)
                else:
                    refresh_diagnosis = refresh_result.diagnosis
                    logger.warning("阅读前刷新 Token 失败: %s", refresh_diagnosis)

        # 阅读循环
        from wereadit.core.reader import read_books

        result = read_books(client, cfg, refresh_print=refresh_print)
        push_content = (
            f"WeReadIt 自动阅读完成。\n"
            f"阅读时长：{result.total_minutes} 分钟。\n"
            f"{result.summary()}"
        )

        # 兑换阅读奖励
        if cfg.weread_access_token:
            logger.info("开始兑换阅读奖励...")
            try:
                from wereadit.core.exchanger import exchange_awards

                exchange_summary = exchange_awards(
                    client,
                    cfg,
                    refresher=refresher,
                    token_refreshed_at=token_refreshed_at,
                )
            except ExchangeError as exc:
                if exc.errcode == ERRCODE_TOKEN_EXPIRED:
                    # 排查 token 过快过期：告警中明确平台 + token 前 8 位，
                    # 便于用户对应 GitHub Secrets 并追踪是否为同一 token 反复过期
                    token_preview = cfg.weread_access_token[:8] if cfg.weread_access_token else ""
                    platform_label = (
                        "iOS (WEREAD_IOS_TOKEN)"
                        if cfg.weread_platform == PLATFORM_IOS
                        else "Android (WEREAD_ANDROID_TOKEN)"
                    )
                    logger.error("兑换 Token 已过期: %s", exc)
                    if refresh_diagnosis:
                        guidance = "根因见下方 Token 自动续期诊断。"
                    elif cfg.weread_login_curl:
                        guidance = "请重新抓包更新 Secret 中的 Token。"
                    else:
                        guidance = (
                            "未配置自动续期，请重新抓包更新 Secret 中的 Token，"
                            "或按 README 配置 WEREAD_LOGIN_CURL 实现自动续期。"
                        )
                    exchange_summary = (
                        f"兑换奖励失败: {platform_label} 已过期。{guidance}\n"
                        f"过期 Token 前 8 位: {token_preview}..."
                    )
                    exit_code = 1
                    has_failure = True
                else:
                    logger.error("兑换奖励异常: %s", exc)
                    exchange_summary = f"兑换奖励失败: {exc}"
            except Exception as exc:  # noqa: BLE001
                logger.error("兑换奖励异常: %s", exc)
                exchange_summary = f"兑换奖励失败: {exc}"
        else:
            logger.info("未配置 WEREAD_ACCESS_TOKEN，跳过兑换。")

        if exchange_summary:
            push_content += f"\n\n{exchange_summary}"
        if refresh_diagnosis:
            push_content += f"\n\nToken 自动续期诊断：{refresh_diagnosis}"

        # 推送成功通知
        if push_method:
            logger.info("开始推送...")
            push(push_content, push_method, client, cfg, is_success=not has_failure)
        else:
            logger.info("未配置推送渠道，跳过推送。")

    except CookieExpiredError as exc:
        logger.error("Cookie 刷新失败：%s", exc)
        if push_method:
            push(str(exc), push_method, client, cfg, is_success=False)
        exit_code = 1

    except ReadFailedError as exc:
        logger.error("阅读熔断：%s", exc)
        if push_method:
            push(f"WeReadIt 阅读熔断：{exc}", push_method, client, cfg, is_success=False)
        exit_code = 1

    except Exception as exc:  # noqa: BLE001
        logger.error("未捕获异常：%s\n%s", exc, traceback.format_exc())
        if push_method:
            push(f"WeReadIt 运行失败：{exc}", push_method, client, cfg, is_success=False)
        exit_code = 1

    finally:
        client.close()

    return exit_code
