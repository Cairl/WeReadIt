"""push 注册表与工厂测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wereadit.config import Config
from wereadit.push.base import Pusher
from wereadit.push.registry import _PUSHERS, get_pusher, push


def _make_cfg(**overrides) -> Config:
    """构造测试用 Config。

    push_method / weread_access_token / weread_platform 是 @property，
    不能作为构造参数，由对应 token 字段自动派生。
    """
    defaults = dict(
        read_num=2,
        books=["b1"],
        chapters=["c1"],
        pushplus_token="",
        wxpusher_spt="",
        telegram_bot_token="",
        telegram_chat_id="",
        serverchan_spt="",
        bark_key="",
        weread_app_curl="",
        app_token="",
        app_token_key="",
        exchange_award="2,2,2,2,2,2,2,2",
        headers={},
        cookies={},
        web_curl="",
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestRegistry:
    def test_all_channels_registered(self) -> None:
        """5 个渠道都应已注册。"""
        assert set(_PUSHERS.keys()) == {
            "pushplus", "wxpusher", "telegram", "serverchan", "bark"
        }

    def test_get_pusher_unknown_method(self, mock_client: MagicMock) -> None:
        """未知渠道返回 None。"""
        cfg = _make_cfg()
        assert get_pusher("unknown", mock_client, cfg) is None

    def test_get_pusher_empty_method(self, mock_client: MagicMock) -> None:
        """空 method 返回 None。"""
        cfg = _make_cfg()
        assert get_pusher("", mock_client, cfg) is None

    def test_get_pusher_pushplus(self, mock_client: MagicMock) -> None:
        cfg = _make_cfg(pushplus_token="tok123")
        pusher = get_pusher("pushplus", mock_client, cfg)
        assert pusher is not None
        assert pusher.token == "tok123"
        assert pusher.name == "pushplus"

    def test_get_pusher_case_insensitive(self, mock_client: MagicMock) -> None:
        cfg = _make_cfg(pushplus_token="tok123")
        assert get_pusher("PushPlus", mock_client, cfg) is not None

    def test_get_pusher_missing_token(self, mock_client: MagicMock) -> None:
        """非 telegram 渠道缺 token 返回 None。"""
        cfg = _make_cfg(pushplus_token="")
        assert get_pusher("pushplus", mock_client, cfg) is None


class TestPushFunction:
    def test_push_empty_method(self, mock_client: MagicMock) -> None:
        cfg = _make_cfg()
        assert push("content", "", mock_client, cfg) is False

    def test_push_unknown_method(self, mock_client: MagicMock) -> None:
        cfg = _make_cfg()
        assert push("content", "unknown", mock_client, cfg) is False

    def test_push_pushplus_success(self, mock_client: MagicMock) -> None:
        cfg = _make_cfg(pushplus_token="tok")
        result = push("hello", "pushplus", mock_client, cfg)
        assert result is True
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert "pushplus.plus/send" in args[0]
        body = kwargs["json"]
        assert body["token"] == "tok"
        assert "成功" in body["title"]

    def test_push_bark_success(self, mock_client: MagicMock) -> None:
        """bark 推送应 POST 到 BARK_PUSHER 指定的完整 URL，body 含 title/body。"""
        cfg = _make_cfg(bark_key="https://api.day.app/device_key_123")
        result = push("hello", "bark", mock_client, cfg)
        assert result is True
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        assert args[0] == "https://api.day.app/device_key_123"
        body = kwargs["json"]
        assert "成功" in body["title"]
        assert body["body"] == "hello"

    def test_push_bark_full_url_trailing_slash(self, mock_client: MagicMock) -> None:
        """完整 URL 末尾带斜杠时应去除。"""
        cfg = _make_cfg(bark_key="https://api.day.app/aZybLbnT5XhyUhkPxLBQGn/")
        result = push("hi", "bark", mock_client, cfg)
        assert result is True
        args, _ = mock_client.post.call_args
        assert args[0] == "https://api.day.app/aZybLbnT5XhyUhkPxLBQGn"


class TestPusherBase:
    def test_pusher_abc_cannot_instantiate(self, mock_client: MagicMock) -> None:
        """Pusher 是抽象基类，不能直接实例化。"""
        cfg = _make_cfg()
        with pytest.raises(TypeError):
            Pusher(mock_client, cfg)
