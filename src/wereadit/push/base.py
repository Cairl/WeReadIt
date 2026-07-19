"""Pusher 抽象基类与重试装饰器。

消除原 push.py 中 4 处重复的重试逻辑：所有渠道共享 with_retry。
"""

from __future__ import annotations

import functools
import logging
import random
import time
from abc import ABC, abstractmethod

from wereadit.config import Config
from wereadit.constants import (
    PUSH_MAX_ATTEMPTS,
    PUSH_RETRY_MAX_WAIT,
    PUSH_RETRY_MIN_WAIT,
)
from wereadit.infra.http import HttpClient

logger = logging.getLogger(__name__)


def with_retry(
    attempts: int = PUSH_MAX_ATTEMPTS,
    min_wait: int = PUSH_RETRY_MIN_WAIT,
    max_wait: int = PUSH_RETRY_MAX_WAIT,
):
    """推送重试装饰器。

    失败后随机等待 [min_wait, max_wait] 秒重试，最多 attempts 次。
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    if fn(*args, **kwargs):
                        return True
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "%s 第 %d/%d 次失败: %s", fn.__name__, attempt + 1, attempts, exc
                    )
                if attempt < attempts - 1:
                    sleep_time = random.randint(min_wait, max_wait)
                    logger.info("%d 秒后重试...", sleep_time)
                    time.sleep(sleep_time)
            return False

        return wrapper

    return decorator


class Pusher(ABC):
    """推送渠道抽象基类。

    每个具体渠道实现 send 方法即可。
    持有 cfg 引用，可按需取自己需要的字段（token / chat_id 等）。
    """

    name: str = "abstract"

    def __init__(self, client: HttpClient, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg

    @property
    def token(self) -> str:
        """默认从 cfg 中按渠道名取 token，子类可覆盖。"""
        return self.cfg.token_for(self.name)

    @abstractmethod
    def send(self, content: str, is_success: bool = True) -> bool:
        """发送推送。

        Args:
            content: 推送内容
            is_success: 业务是否成功（影响标题）

        Returns:
            是否发送成功
        """
        raise NotImplementedError
