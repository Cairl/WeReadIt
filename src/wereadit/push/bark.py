"""Bark 推送渠道。

Bark 是 iOS 上的推送 App，通过简单的 HTTP 接口接收通知。
- 官方服务器：https://api.day.app
- 自建服务器：通过 BARK_SERVER 环境变量配置
- 推送方式：POST JSON 到 {server}/{device_key}，body 含 title/body
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

    device_key 由 cfg.bark_key 提供（环境变量 BARK），
    服务器地址由 cfg.bark_server 提供（环境变量 BARK_SERVER，默认官方）。
    """

    @with_retry()
    def send(self, content: str, is_success: bool = True) -> bool:
        # token 既可以是纯 device_key（配合 bark_server 拼接），
        # 也可以是完整 URL（https://api.day.app/<key>[/]），自动识别
        token = (self.token or "").strip()
        if token.startswith(("http://", "https://")):
            url = token.rstrip("/")
        else:
            server = self.cfg.bark_server.rstrip("/")
            url = f"{server}/{token.strip('/')}"
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
