"""Bark 推送渠道。

Bark 是 iOS 上的推送 App，通过简单的 HTTP 接口接收通知。
- 配置方式：环境变量 BARK_PUSHER 填完整 URL（如 https://api.day.app/<device_key>）
- 推送方式：POST JSON 到 BARK_PUSHER 指定的 URL，body 含 title/body
"""

from __future__ import annotations

import logging

from wereadit.constants import PUSH_TIMEOUT
from wereadit.push.base import Pusher, with_retry
from wereadit.push.registry import register

logger = logging.getLogger(__name__)


@register("bark")
class BarkPusher(Pusher):
    """Bark 推送。

    BARK_PUSHER 环境变量填完整 URL（如 https://api.day.app/<device_key>），
    末尾斜杠可选。不再支持 server+key 分离配置。
    """

    @with_retry()
    def send(self, content: str, is_success: bool = True) -> bool:
        url = (self.token or "").strip().rstrip("/")
        title = f"WeReadIt-{'成功' if is_success else '失败'}"
        response = self.client.post(
            url,
            json={"title": title, "body": content},
            headers={"Content-Type": "application/json"},
            timeout=PUSH_TIMEOUT,
        )
        response.raise_for_status()
        logger.info("Bark 响应: %s", response.text)
        return True
