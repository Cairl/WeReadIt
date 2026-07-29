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
from wereadit.core.exchanger import _parse_strategy, exchange_awards, query_coin_balance
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
        assert "未找到 wr_vid" in result.error

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
        assert result.exchanged_coin == 2
        assert result.skipped == 1
        assert result.failed == 0
        assert result.error == ""
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
        assert result.exchanged_coin == 0
        assert result.skipped == 3  # 2 个策略跳过 + 1 个已领取
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

    def test_query_other_exchange_error_returns_result_with_error(
        self, mock_client: MagicMock
    ) -> None:
        """查询抛非 Token 过期的 ExchangeError 时返回带 error 的 ExchangeResult，不 re-raise。"""
        cfg = _make_cfg()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "errcode": -999,
            "errmsg": "查询失败",
        }
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert result.error
        assert "-999" in result.error

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
        assert result.platform == "iOS"
        # 检查 post 调用的 headers 参数
        _, kwargs = mock_client.post.call_args
        assert "skey" in kwargs["headers"]
        assert "accessToken" not in kwargs["headers"]

    def test_keep_reading_days_extracted(self, mock_client: MagicMock) -> None:
        """响应中的连续阅读天数应被提取到 ExchangeResult。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        query_resp["keepReadingDays"] = 128
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert result.keep_reading_days == 128

    def test_optional_fields_none_when_absent(self, mock_client: MagicMock) -> None:
        """响应中无连续阅读字段时，对应字段为 None。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert result.keep_reading_days is None

    def test_no_awards_logs_no_exchange_message(
        self, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """查询成功但无可兑换奖励（awards 为空）时输出"无需兑换阅读奖励"。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        query_resp["readtimeAwards"] = []
        query_resp["readdayAwards"] = []
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        with caplog.at_level(logging.INFO, logger="wereadit.core.exchanger"):
            result = exchange_awards(mock_client, cfg)

        assert result.exchanged_coin == 0
        assert result.exchanged_card is None
        assert result.failed == 0
        assert "无需兑换阅读奖励" in caplog.messages

    def test_card_exchange_returns_card_days(self, mock_client: MagicMock) -> None:
        """策略选体验卡（CHOICE_CARD=1）时 exchanged_card 返回实际天数。"""
        cfg = _make_cfg(exchange_award="1,1,1,1,1,1,1,1")  # 全选体验卡
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = [query_resp, {"ok": True}, {"ok": True}]
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert result.exchanged_card == 2  # 2 个可领取，每个 awardNum=1
        assert result.exchanged_coin == 0
        assert result.failed == 0

    def test_coin_only_exchange_returns_zero_card(self, mock_client: MagicMock) -> None:
        """策略选书币（发生兑换但未获得体验卡）时 exchanged_card 为 0（真实值）。"""
        cfg = _make_cfg()  # 默认全选书币
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = [query_resp, {"ok": True}, {"ok": True}]
        mock_client.post.return_value = mock_response

        result = exchange_awards(mock_client, cfg)
        assert result.exchanged_coin == 2
        assert result.exchanged_card == 0  # 发生了兑换，体验卡获得 0 天（真实值）


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
        assert result.error == ""


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

        # 非 token 过期错误应返回带 error 的 ExchangeResult 而非 raise
        assert result.error
        # 验证 WARNING 日志中包含 HTTP 状态码、errcode、errmsg
        warning_text = caplog.text
        assert "403" in warning_text
        assert "-999" in warning_text
        assert "风控拦截" in warning_text


class TestQueryCoinBalance:
    """独立查询：POST /web/pay/balance + GET /web/pay/memberCardSummary。

    返回 (coin_balance, card_remain_seconds, card_expire_ts)。
    字段语义经 2026-07-30 真实账号探测确认（见 exchanger.query_coin_balance
    docstring）；本组测试只验证提取/兜底/容错逻辑，mock 值均为中性任意数，
    不与任何真实账号数值挂钩。
    """

    @staticmethod
    def _setup(mock_client: MagicMock, balance_json: dict, card_json: dict) -> None:
        """配置 mock：post → /web/pay/balance，get → memberCardSummary。"""
        balance_resp = MagicMock()
        balance_resp.status_code = 200
        balance_resp.json.return_value = balance_json
        mock_client.post.return_value = balance_resp
        card_resp = MagicMock()
        card_resp.status_code = 200
        card_resp.json.return_value = card_json
        mock_client.get.return_value = card_resp

    def test_ios_picks_gift_balance(self, mock_client: MagicMock) -> None:
        """iOS 平台优先取 giftBalance（本端余额）。"""
        cfg = _make_cfg(app_token="ios_token", app_token_key="skey")
        self._setup(
            mock_client,
            {"giftBalance": 12.5, "balance": 12.5, "peerBalance": 7.0},
            {"errCode": -2012},  # 体验卡接口失败
        )
        coin, card_secs, card_ts = query_coin_balance(mock_client, cfg)
        assert coin == 12.5
        assert card_secs is None
        assert card_ts is None

    def test_android_picks_peer_balance(self, mock_client: MagicMock) -> None:
        """Android 平台优先取 peerBalance。"""
        cfg = _make_cfg()  # app_token_key="accessToken" -> Android
        self._setup(
            mock_client,
            {"giftBalance": 12.5, "peerBalance": 7.25},
            {"errCode": -2012},
        )
        coin, _, _ = query_coin_balance(mock_client, cfg)
        assert coin == 7.25

    def test_coin_fallback_to_balance_field(self, mock_client: MagicMock) -> None:
        """giftBalance 缺失时回退 balance 字段。"""
        cfg = _make_cfg(app_token="ios_token", app_token_key="skey")
        self._setup(mock_client, {"balance": 3.5}, {"errCode": -2012})
        coin, _, _ = query_coin_balance(mock_client, cfg)
        assert coin == 3.5

    def test_errcode_camel_case_returns_none(self, mock_client: MagicMock) -> None:
        """响应含大写驼峰 errCode（非 0）时判失败返回 None（回归：旧实现只查
        小写 errcode，会把 -2012 错误响应当成功放行）。"""
        cfg = _make_cfg()
        self._setup(mock_client, {"errCode": -2012, "errMsg": "登录超时"}, {})
        coin, card_secs, card_ts = query_coin_balance(mock_client, cfg)
        assert coin is None
        assert card_secs is None
        assert card_ts is None

    def test_network_exception_returns_none(self, mock_client: MagicMock) -> None:
        """请求异常时全部返回 None，不影响主流程。"""
        cfg = _make_cfg()
        mock_client.post.side_effect = Exception("network error")
        mock_client.get.side_effect = Exception("network error")
        coin, card_secs, card_ts = query_coin_balance(mock_client, cfg)
        assert coin is None
        assert card_secs is None
        assert card_ts is None

    def test_card_from_member_card_summary(self, mock_client: MagicMock) -> None:
        """体验卡优先取 memberCardSummary 的 remainTime（秒）与 expiredTime。"""
        cfg = _make_cfg()
        self._setup(
            mock_client,
            {"giftBalance": 12.5, "welfare": {"expiredTime": 3600}},
            {"remainTime": 9600, "expiredTime": 1785000000},
        )
        coin, card_secs, card_ts = query_coin_balance(mock_client, cfg)
        assert coin == 12.5
        assert card_secs == 9600.0
        assert card_ts == 1785000000

    def test_card_fallback_to_welfare(self, mock_client: MagicMock) -> None:
        """memberCardSummary 失败时回退 /web/pay/balance 的 welfare 字段。"""
        cfg = _make_cfg()
        self._setup(
            mock_client,
            {
                "giftBalance": 12.5,
                "welfare": {"expiredTime": 5400, "showExpiredTime": 1785000001},
            },
            {"errCode": -2012},
        )
        coin, card_secs, card_ts = query_coin_balance(mock_client, cfg)
        assert coin == 12.5
        assert card_secs == 5400.0
        assert card_ts == 1785000001
