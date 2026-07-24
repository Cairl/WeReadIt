"""exchanger 测试：用 mock HttpClient 验证兑换流程。"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest

from wereadit.config import Config
from wereadit.constants import (
    AWARD_LEVEL_IDS,
    CHOICE_COIN,
    ERRCODE_TOKEN_EXPIRED,
)
from wereadit.core.exchanger import _parse_strategy, exchange_awards
from wereadit.core.token_refresher import RefreshResult
from wereadit.exceptions import ExchangeError


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
        app_token="test_token",
        app_token_key="accessToken",
        weread_app_curl="",
        exchange_award="2,2,2,2,2,2,2,2",
        headers={},
        cookies={"wr_vid": "12345"},
        web_curl="",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _mock_award_data() -> dict:
    """构造查询响应：2 个可领取 + 1 个已领取。"""
    return {
        "readingTime": 1800,
        "readingDay": 1,
        "readtimeAwards": [
            {
                "awardLevelId": 4,
                "awardStatus": 1,
                "awardLevelDesc": "读 5 分钟",
                "awardChoices": [
                    {"choiceType": 1, "awardNum": 1, "canChoice": 1},
                    {"choiceType": 2, "awardNum": 1, "canChoice": 1},
                ],
            },
            {
                "awardLevelId": 5,
                "awardStatus": 1,
                "awardLevelDesc": "读 30 分钟",
                "awardChoices": [
                    {"choiceType": 1, "awardNum": 1, "canChoice": 1},
                    {"choiceType": 2, "awardNum": 1, "canChoice": 1},
                ],
            },
            {
                "awardLevelId": 1,
                "awardStatus": 2,
                "awardLevelDesc": "读 1 小时",
                "awardChoices": [],
            },
        ],
        "readdayAwards": [],
    }


class TestParseStrategy:
    def test_default_strategy(self) -> None:
        result = _parse_strategy("")
        assert result[4] == CHOICE_COIN
        assert result[13] == CHOICE_COIN

    def test_custom_strategy(self) -> None:
        result = _parse_strategy("1,0,2,1,0,2,1,0")
        assert result[AWARD_LEVEL_IDS[0]] == 1
        assert result[AWARD_LEVEL_IDS[1]] == 0
        assert result[AWARD_LEVEL_IDS[2]] == 2

    def test_wrong_length_raises(self) -> None:
        with pytest.raises(ValueError, match="格式错误"):
            _parse_strategy("2,2,2")

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_strategy("a,b,c,d,e,f,g,h")


class TestExchangeAwards:
    def test_missing_vid_returns_error(self, mock_client: MagicMock) -> None:
        cfg = _make_cfg(cookies={})
        result = exchange_awards(mock_client, cfg)
        assert "未找到 wr_vid" in result

    def test_successful_exchange(self, mock_client: MagicMock) -> None:
        """2 个可领 + 全兑书币：应兑换 2 个，跳过 1 个已领。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()

        # 第 1 次调用是查询，后续是兑换
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = [query_resp, {"ok": True}, {"ok": True}]
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert "兑换: 2 书币" in result
        assert "跳过: 1" in result
        assert "失败: 0" in result
        # 1 次查询 + 2 次兑换 = 3 次 post
        assert mock_client.post.call_count == 3

    def test_skip_when_strategy_none(self, mock_client: MagicMock) -> None:
        """策略为 0（不兑换）的奖励应跳过。"""
        cfg = _make_cfg(exchange_award="0,0,0,0,0,0,0,0")
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert "兑换: 0 书币" in result
        assert "跳过: 3" in result  # 2 个策略跳过 + 1 个已领取
        # 只查询，不兑换
        assert mock_client.post.call_count == 1

    def test_token_expired_raises(self, mock_client: MagicMock) -> None:
        """token 过期应直接抛出，不重试。"""
        cfg = _make_cfg()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "errcode": ERRCODE_TOKEN_EXPIRED,
            "errmsg": "登录超时",
        }
        mock_client.post.return_value = mock_response

        with pytest.raises(ExchangeError) as exc_info:
            exchange_awards(mock_client, cfg)
        assert exc_info.value.errcode == ERRCODE_TOKEN_EXPIRED

    def test_query_other_exchange_error_returns_string(self, mock_client: MagicMock) -> None:
        """查询抛非 Token 过期的 ExchangeError 时返回错误字符串，不 re-raise。"""
        cfg = _make_cfg()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "errcode": -999,
            "errmsg": "查询失败",
        }
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert "兑换奖励失败" in result
        assert "-999" in result

    def test_ios_platform(self, mock_client: MagicMock) -> None:
        """iOS 平台应使用 skey header。"""
        # 通过 app_token_key="skey" 触发 weread_platform=PLATFORM_IOS
        cfg = _make_cfg(app_token="ios_token", app_token_key="skey")
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert "iOS" in result
        # 检查 post 调用的 headers 参数
        _, kwargs = mock_client.post.call_args
        assert "skey" in kwargs["headers"]
        assert "accessToken" not in kwargs["headers"]


class TestExchangeTokenRefresh:
    """补刷保险：token 年龄超阈值时兑换前调 refresher 补刷。"""

    def test_refresh_triggered_when_token_old(self, mock_client: MagicMock) -> None:
        """token 年龄 > TOKEN_MAX_AGE_SECONDS：补刷并用新 token 兑换。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        refresher = MagicMock(
            return_value=RefreshResult(token="new_token_123456", token_key="accessToken")
        )
        exchange_awards(
            mock_client,
            cfg,
            refresher=refresher,
            token_refreshed_at=time.time() - 6000,
        )
        refresher.assert_called_once()
        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["accessToken"] == "new_token_123456"

    def test_refresh_not_triggered_when_token_fresh(self, mock_client: MagicMock) -> None:
        """token 年龄 < 阈值：不补刷。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        refresher = MagicMock()
        exchange_awards(
            mock_client,
            cfg,
            refresher=refresher,
            token_refreshed_at=time.time() - 100,
        )
        refresher.assert_not_called()
        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["accessToken"] == "test_token"

    def test_refresh_failure_keeps_old_token(self, mock_client: MagicMock) -> None:
        """补刷失败：沿用原 token 继续兑换。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        refresher = MagicMock(return_value=RefreshResult(diagnosis="网络异常"))
        exchange_awards(
            mock_client,
            cfg,
            refresher=refresher,
            token_refreshed_at=time.time() - 6000,
        )
        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["accessToken"] == "test_token"

    def test_no_refresher_no_crash_when_token_old(self, mock_client: MagicMock) -> None:
        """refresher 为 None 时即使 token 很旧也不补刷、不崩溃。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        result = exchange_awards(
            mock_client,
            cfg,
            token_refreshed_at=time.time() - 6000,
        )
        assert "兑换奖励失败" not in result


class TestExchangeLogging:
    """排查 token 过快过期：验证兑换流程的关键日志输出。

    覆盖 2026-07-21 新增的排查日志：
    - Token 过期时记录 WARNING 日志，包含 token 前 8 位
    - 兑换接口失败时记录 HTTP 状态码、errcode、errmsg、响应体片段

    注意：兑换开始的 INFO 日志（平台/vid/token）与本周阅读统计 INFO 日志
    已于 2026-07-25 简化删除，正常流程只保留"兑换 X 成功: Y Z"结果行。
    """

    def test_token_expired_logs_warning_with_preview(
        self, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Token 过期时应记录 WARNING 级别日志，包含 token 前 8 位。"""
        cfg = _make_cfg(app_token="abcdefgh1234567890")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "errcode": ERRCODE_TOKEN_EXPIRED,
            "errmsg": "登录超时",
        }
        mock_client.post.return_value = mock_response

        with caplog.at_level(logging.WARNING, logger="wereadit.core.exchanger"):
            with pytest.raises(ExchangeError):
                exchange_awards(mock_client, cfg)

        # 验证 WARNING 日志中包含 token 前 8 位和 "Token 已过期"
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("abcdefgh" in m for m in warning_messages)
        assert any("Token 已过期" in m for m in warning_messages)
        # 完整 token 不应出现在日志中（脱敏）
        assert "abcdefgh1234567890" not in caplog.text

    def test_call_exchange_failure_logs_details(
        self, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """兑换接口失败时应记录 HTTP 状态码、errcode、errmsg、响应体片段。"""
        cfg = _make_cfg()
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.json.return_value = {
            "errcode": -999,
            "errmsg": "风控拦截",
            "extra": "detail",
        }
        mock_client.post.return_value = mock_response

        with caplog.at_level(logging.WARNING, logger="wereadit.core.exchanger"):
            result = exchange_awards(mock_client, cfg)

        # 非 token 过期错误应返回字符串而非 raise
        assert "兑换奖励失败" in result
        # 验证 WARNING 日志中包含 HTTP 状态码、errcode、errmsg
        warning_text = caplog.text
        assert "403" in warning_text
        assert "-999" in warning_text
        assert "风控拦截" in warning_text
