"""推送渠道注册表与工厂。

用装饰器注册代替 if-elif 链：
    @register("pushplus")
    class PushPlusPusher(Pusher): ...

新增渠道零修改主流程。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from wereadit.config import Config
from wereadit.infra.http import HttpClient
from wereadit.push.base import Pusher

logger = logging.getLogger(__name__)

# 渠道注册表：method_name -> Pusher 子类
_PUSHERS: dict[str, type[Pusher]] = {}


def register(name: str) -> Callable[[type[Pusher]], type[Pusher]]:
    """类装饰器：注册推送渠道。

    Usage:
        @register("pushplus")
        class PushPlusPusher(Pusher): ...
    """

    def decorator(cls: type[Pusher]) -> type[Pusher]:
        cls.name = name
        _PUSHERS[name.lower()] = cls
        return cls

    return decorator


def get_pusher(method: str, client: HttpClient, cfg: Config) -> Pusher | None:
    """根据 method 名称获取 Pusher 实例。

    Args:
        method: 渠道名（pushplus/wxpusher/telegram/serverchan）
        client: HTTP 客户端
        cfg: 配置（含 token 与其他字段）

    Returns:
        Pusher 实例，未注册或 token 为空时返回 None
    """
    if not method:
        return None
    method_lower = str(method).lower()
    cls = _PUSHERS.get(method_lower)
    if cls is None:
        logger.warning(
            "无效的通知渠道 '%s'，已跳过推送。支持：%s",
            method,
            ", ".join(sorted(_PUSHERS.keys())),
        )
        return None
    token = cfg.token_for(method_lower)
    if not token and method_lower != "telegram":
        # telegram 同时需要 bot_token 和 chat_id，下面由 Pusher 自行判断
        logger.warning("渠道 %s 未配置 token，跳过推送", method_lower)
        return None
    return cls(client=client, cfg=cfg)


def push(
    content: str,
    method: str,
    client: HttpClient,
    cfg: Config,
    is_success: bool = True,
) -> bool:
    """统一推送入口。

    Args:
        content: 推送内容
        method: 渠道名
        client: HTTP 客户端
        cfg: 配置
        is_success: 业务是否成功

    Returns:
        是否发送成功
    """
    if not method:
        logger.warning("未配置推送渠道，跳过推送。")
        return False
    pusher = get_pusher(method, client, cfg)
    if pusher is None:
        return False
    return pusher.send(content, is_success=is_success)


# 触发各渠道模块导入，完成注册（必须放在注册表定义之后）
from wereadit.push import (  # noqa: E402
    bark,  # noqa: F401
    pushplus,  # noqa: F401
    serverchan,  # noqa: F401
    telegram,  # noqa: F401
    wxpusher,  # noqa: F401
)
