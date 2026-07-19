"""ServerChan 推送渠道。"""

from __future__ import annotations

import json
import logging

from wereadit.constants import PUSH_TIMEOUT
from wereadit.push.base import Pusher, with_retry
from wereadit.push.registry import register

logger = logging.getLogger(__name__)

_SERVERCHAN_URL = "https://sctapi.ftqq.com/{}.send"


@register("serverchan")
class ServerChanPusher(Pusher):
    """ServerChan（Server 酱）推送。"""

    @with_retry()
    def send(self, content: str, is_success: bool = True) -> bool:
        url = _SERVERCHAN_URL.format(self.token)
        title = f"WeReadIt-{'成功' if is_success else '失败'}"
        response = self.client.post(
            url,
            data=json.dumps({"title": title, "desp": content}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=PUSH_TIMEOUT,
        )
        response.raise_for_status()
        logger.info("ServerChan 响应: %s", response.text)
        return True
