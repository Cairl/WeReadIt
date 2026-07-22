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
from typing import TYPE_CHECKING

from wereadit.config import Config, load_config
from wereadit.constants import ERRCODE_TOKEN_EXPIRED, PLATFORM_IOS
from wereadit.exceptions import CookieExpiredError, ExchangeError, ReadFailedError
from wereadit.infra.http import HttpClient
from wereadit.push import push

if TYPE_CHECKING:
    from wereadit.core.token_refresher import RefreshResult

logger = logging.getLogger(__name__)


def _inject_app_token(cfg: Config, refresh_result: RefreshResult) -> Config:
    """把刷新得到的 token 注入 Config（平台由 token_key 自动派生）。"""
    return dataclasses.replace(
        cfg, app_token=refresh_result.token, app_token_key=refresh_result.token_key
    )


def main() -> int:
    """主入口：阅读 -> 兑换 -> 推送。

    Returns:
        进程退出码：0=成功，1=失败
    """
    from wereadit.utils.logging import setup_logging

    setup_logging()
    cfg = load_config()
    client = HttpClient(headers=cfg.headers, cookies=cfg.cookies)

    push_method = cfg.push_method
    exchange_summary = ""
    exit_code = 0
    has_failure = False  # 推送状态标志：兑换失败但阅读成功时为 False；Token 过期等致命错误为 True

    try:
        # 兑换 Token：阅读前由 /login 重放刷新生成，平台从命中字段名自识别
        refresh_diagnosis = ""
        platform_note = ""
        refresher = None
        token_refreshed_at = None
        if cfg.weread_app_curl:
            from wereadit.core.token_refresher import (
                diagnose_login_curl,
                refresh_app_token,
            )

            curl_diagnosis = diagnose_login_curl(cfg.weread_app_curl)
            if curl_diagnosis:
                logger.warning("WEREAD_APP_CURL 体检不过: %s", curl_diagnosis)
                refresh_diagnosis = f"Token 自动刷新已跳过：{curl_diagnosis}"
            else:
                refresher = partial(refresh_app_token, cfg.weread_app_curl)
                refresh_result = refresher()
                if refresh_result.ok:
                    cfg = _inject_app_token(cfg, refresh_result)
                    token_refreshed_at = time.time()
                    platform_note = (
                        f"平台自识别：{'iOS' if cfg.weread_platform == PLATFORM_IOS else 'Android'}"
                    )
                    logger.info(
                        "兑换 Token 已在阅读前刷新: %s...（%s）",
                        refresh_result.token[:8],
                        platform_note,
                    )
                else:
                    refresh_diagnosis = refresh_result.diagnosis
                    logger.warning("阅读前刷新 Token 失败: %s", refresh_diagnosis)

        # 阅读循环
        from wereadit.core.reader import read_books

        result = read_books(client, cfg)
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
                        "iOS" if cfg.weread_platform == PLATFORM_IOS else "Android"
                    )
                    logger.error("兑换 Token 已过期: %s", exc)
                    if refresh_diagnosis:
                        guidance = "根因见下方 Token 自动续期诊断。"
                    elif cfg.weread_app_curl:
                        guidance = "请重新抓包更新 WEREAD_APP_CURL（杀 App 冷启动抓 /login，body 须含 deviceId）。"
                    else:
                        guidance = "未配置 WEREAD_APP_CURL 自动续期，请按 README 配置。"
                    exchange_summary = (
                        f"兑换奖励失败: {platform_label} Token 已过期。{guidance}\n"
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
            logger.info("无可用兑换 Token（WEREAD_APP_CURL 未配置或刷新失败），跳过兑换。")
            if refresh_diagnosis:
                # 配了 APP_CURL 但刷新失败：兑换目标未达成，标记为可见失败
                exit_code = 1
                has_failure = True

        if exchange_summary:
            push_content += f"\n\n{exchange_summary}"
        if refresh_diagnosis:
            push_content += f"\n\nToken 自动续期诊断：{refresh_diagnosis}"
        if platform_note:
            push_content += f"\n\n{platform_note}"

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
