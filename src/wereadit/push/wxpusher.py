"""WxPusher 推送渠道（极简推送方式）。"""

from __future__ import annotations

import logging

from wereadit.constants import PUSH_TIMEOUT
from wereadit.push.base import Pusher, with_retry
from wereadit.push.registry import register

logger = logging.getLogger(__name__)

_WXPUSHER_SIMPLE_URL = "https://wxpusher.zjiecode.com/api/send/message/{}/{}"


@register("wxpusher")
class WxPusherPusher(Pusher):
    """WxPusher 极简推送。"""

    @with_retry()
    def send(self, content: str, is_success: bool = True) -> bool:
        url = _WXPUSHER_SIMPLE_URL.format(self.token, content)
        response = self.client.get(url, timeout=PUSH_TIMEOUT)
        response.raise_for_status()
        logger.info("WxPusher 响应: %s", response.text)
        return True
