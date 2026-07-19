"""PushPlus 推送渠道。"""

from __future__ import annotations

import logging

from wereadit.constants import PUSH_TIMEOUT
from wereadit.push.base import Pusher, with_retry
from wereadit.push.registry import register

logger = logging.getLogger(__name__)

_PUSHPLUS_URL = "https://www.pushplus.plus/send"


@register("pushplus")
class PushPlusPusher(Pusher):
    """PushPlus 推送。"""

    @with_retry()
    def send(self, content: str, is_success: bool = True) -> bool:
        title = f"WeReadIt-{'成功' if is_success else '失败'}"
        response = self.client.post(
            _PUSHPLUS_URL,
            json={"token": self.token, "title": title, "content": content},
            headers={"Content-Type": "application/json"},
            timeout=PUSH_TIMEOUT,
        )
        response.raise_for_status()
        logger.info("PushPlus 响应: %s", response.text)
        return True
