"""Telegram 推送渠道。

支持代理：若环境变量 http_proxy/https_proxy 存在，先走代理，失败再直连。
"""

from __future__ import annotations

import logging
import os

from wereadit.constants import PUSH_TIMEOUT
from wereadit.push.base import Pusher
from wereadit.push.registry import register

logger = logging.getLogger(__name__)

_TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"


@register("telegram")
class TelegramPusher(Pusher):
    """Telegram Bot 推送。"""

    @property
    def bot_token(self) -> str:
        return self.cfg.telegram_bot_token

    @property
    def chat_id(self) -> str:
        return self.cfg.telegram_chat_id

    def send(self, content: str, is_success: bool = True) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram 未配置 bot_token 或 chat_id，跳过推送")
            return False

        url = _TELEGRAM_URL.format(self.bot_token)
        payload = {"chat_id": self.chat_id, "text": content}
        proxies = {
            "http": os.getenv("http_proxy"),
            "https": os.getenv("https_proxy"),
        }

        # 优先走代理
        if any(proxies.values()):
            try:
                response = self.client.post(
                    url, json=payload, proxies=proxies, timeout=PUSH_TIMEOUT
                )
                logger.info("Telegram 响应: %s", response.text)
                response.raise_for_status()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("Telegram 代理发送失败: %s", exc)

        # 直连兜底
        try:
            response = self.client.post(url, json=payload, timeout=PUSH_TIMEOUT)
            response.raise_for_status()
            logger.info("Telegram 响应: %s", response.text)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Telegram 发送失败: %s", exc)
            return False
