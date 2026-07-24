"""配置加载。

从环境变量读取配置，返回不可变 Config dataclass。
所有配置项有合理默认值，未配置时使用默认值。

设计原则：
- 纯函数 load_config()，无副作用
- Config 是 dataclass，字段不可变（frozen=True）
- 业务数据（书籍/章节）从 schemas/books.json 加载，与配置逻辑分离
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wereadit.constants import (
    DEFAULT_COOKIES,
    DEFAULT_EXCHANGE_AWARD,
    DEFAULT_HEADERS,
    DEFAULT_READ_NUM,
    PLATFORM_ANDROID,
    PLATFORM_IOS,
)
from wereadit.infra.curl_parser import parse_curl

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).parent / "schemas"


def _load_books() -> tuple[list[str], list[str]]:
    """从 schemas/books.json 加载书籍与章节 ID 列表。"""
    books_path = _SCHEMAS_DIR / "books.json"
    with books_path.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return list(data.get("book", [])), list(data.get("chapter", []))


def _env(name: str, default: str = "") -> str:
    """读取环境变量，未设置时返回 default。"""
    return os.getenv(name) or default


def _env_renamed(new_name: str, old_name: str) -> str:
    """读取重命名后的环境变量，新名优先，旧名作为向后兼容 fallback。

    Secret 重命名后老用户可能仍配置旧名，命中旧名时发 deprecated 警告，
    引导用户改名，避免长期保留旧名引用。
    """
    value = os.getenv(new_name)
    if value:
        return value
    old_value = os.getenv(old_name)
    if old_value:
        logger.warning(
            "环境变量 %s 已废弃，请改用 %s。当前仍读取旧名以保持兼容。",
            old_name,
            new_name,
        )
        return old_value
    return ""


@dataclass(frozen=True)
class Config:
    """运行时配置。

    所有从环境变量或文件加载的配置项集中在此。
    frozen=True 保证配置加载后不被意外修改。
    """

    # 阅读参数
    read_num: int
    books: list[str] = field(default_factory=list)
    chapters: list[str] = field(default_factory=list)

    # 推送参数
    pushplus_token: str = ""
    wxpusher_spt: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    serverchan_spt: str = ""
    bark_key: str = ""

    # 兑换参数
    exchange_award: str = DEFAULT_EXCHANGE_AWARD
    weread_app_curl: str = ""

    # 运行时注入（非环境变量）：/login 重放刷新得到的 App token 与命中字段名
    app_token: str = ""
    app_token_key: str = ""

    # HTTP 请求参数
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    web_curl: str = ""

    def token_for(self, method: str) -> str:
        """根据推送方式返回对应的 token。"""
        method = (method or "").lower()
        token_map = {
            "pushplus": self.pushplus_token,
            "wxpusher": self.wxpusher_spt,
            "telegram": self.telegram_bot_token,
            "serverchan": self.serverchan_spt,
            "bark": self.bark_key,
        }
        return token_map.get(method, "")

    @property
    def push_method(self) -> str:
        """自动检测已配置的推送渠道。

        按优先级检测各渠道的 token/凭证，首个已配置的即为激活渠道。
        若为 telegram，需同时配置 bot_token 和 chat_id。
        """
        if self.pushplus_token:
            return "pushplus"
        if self.wxpusher_spt:
            return "wxpusher"
        if self.telegram_bot_token and self.telegram_chat_id:
            return "telegram"
        if self.serverchan_spt:
            return "serverchan"
        if self.bark_key:
            return "bark"
        return ""

    @property
    def weread_access_token(self) -> str:
        """兑换用 App token（运行时由 /login 重放注入，见 app.py）。"""
        return self.app_token

    @property
    def weread_platform(self) -> str:
        """平台由刷新命中字段名派生：skey → iOS，accessToken → Android。"""
        if self.app_token_key == "skey":
            return PLATFORM_IOS
        return PLATFORM_ANDROID


def load_config() -> Config:
    """从环境变量加载配置。

    优先级：环境变量 > 默认值。
    若提供 WEREAD_WEB_CURL，则从中解析 headers/cookies；否则使用默认模板。
    """
    books, chapters = _load_books()

    web_curl = _env("WEREAD_WEB_CURL")
    if web_curl:
        headers, cookies = parse_curl(web_curl)
    else:
        logger.warning(
            "未配置 WEREAD_WEB_CURL，使用默认 cookies 模板。"
            "生产环境必须配置自己的 web_curl，否则请求会被服务器拒绝。"
            "本地调试可参考 README.md 抓包步骤。"
        )
        headers, cookies = dict(DEFAULT_HEADERS), dict(DEFAULT_COOKIES)

    read_num_raw = _env("READ_NUM", str(DEFAULT_READ_NUM))
    try:
        read_num = int(read_num_raw)
    except ValueError:
        read_num = DEFAULT_READ_NUM

    return Config(
        read_num=read_num,
        books=books,
        chapters=chapters,
        pushplus_token=_env_renamed("PUSHPLUS", "PUSHPLUS_TOKEN"),
        wxpusher_spt=_env_renamed("WXPUSHER", "WXPUSHER_SPT"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        serverchan_spt=_env_renamed("SERVERCHAN", "SERVERCHAN_SPT"),
        bark_key=_env("BARK_PUSHER"),
        weread_app_curl=_env("WEREAD_APP_CURL"),
        exchange_award=_env("EXCHANGE_AWARD", DEFAULT_EXCHANGE_AWARD),
        headers=headers,
        cookies=cookies,
        web_curl=web_curl,
    )
