"""应用编排：阅读 -> 兑换 -> 推送。

把原 main.py 顶层的脚本式逻辑封装为 main() 函数，
便于：1) 被 __main__.py 调用；2) 被单元测试 import；3) 被其他场景复用。

顶层异常兜底：所有未捕获异常统一推送通知，避免静默失败。
"""

from __future__ import annotations

import logging
import traceback

from wereadit.config import load_config
from wereadit.constants import ERRCODE_TOKEN_EXPIRED
from wereadit.exceptions import CookieExpiredError, ExchangeError, ReadFailedError
from wereadit.infra.http import HttpClient
from wereadit.push import push

logger = logging.getLogger(__name__)


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

                exchange_summary = exchange_awards(client, cfg)
            except ExchangeError as exc:
                if exc.errcode == ERRCODE_TOKEN_EXPIRED:
                    logger.error("兑换 Token 已过期: %s", exc)
                    exchange_summary = (
                        "兑换奖励失败: WEREAD_ANDROID_TOKEN / WEREAD_IOS_TOKEN 已过期，"
                        "请重新抓包更新 Secret 中的 Token。"
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
