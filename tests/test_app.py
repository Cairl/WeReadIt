"""app.main() 测试：验证兑换异常分支的 exit_code 与 push is_success 一致性。

覆盖 2026-07-21 引入的 has_failure 标志逻辑：
- Token 过期 (errcode==-2012) → exit_code=1, push is_success=False
- 其他兑换错误 → exit_code=0, push is_success=True（阅读仍成功）
- 未配置 weread_access_token → 跳过兑换, exit_code=0, is_success=True
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from wereadit.app import main
from wereadit.config import Config
from wereadit.constants import ERRCODE_TOKEN_EXPIRED
from wereadit.exceptions import ExchangeError


def _make_cfg(**overrides) -> Config:
    """构造测试用 Config。

    默认配置 pushplus_token + weread_android_token，触发推送与兑换分支。
    """
    defaults = dict(
        read_num=2,
        books=["b1"],
        chapters=["c1"],
        pushplus_token="test_push_token",
        wxpusher_spt="",
        telegram_bot_token="",
        telegram_chat_id="",
        serverchan_spt="",
        weread_android_token="test_token",
        weread_ios_token="",
        exchange_award="2,2,2,2,2,2,2,2",
        headers={},
        cookies={"wr_vid": "12345"},
        curl_bash="",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _mock_read_result() -> MagicMock:
    """构造 mock ReadResult，供 read_books 返回。"""
    result = MagicMock()
    result.total_minutes = 30
    result.summary.return_value = "阅读统计: 30 分钟"
    return result


class TestMainExchangeErrorHandling:
    """验证 main() 对兑换异常的处理与 push 状态一致性。"""

    def test_token_expired_returns_1_and_push_failure(self) -> None:
        """Token 过期: exit_code=1, push is_success=False（告警不被掩盖）。"""
        cfg = _make_cfg()
        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch("wereadit.core.reader.read_books", return_value=_mock_read_result()),
            patch(
                "wereadit.core.exchanger.exchange_awards",
                side_effect=ExchangeError("token expired", ERRCODE_TOKEN_EXPIRED),
            ),
            patch("wereadit.app.push") as mock_push,
        ):
            exit_code = main()

        assert exit_code == 1
        mock_push.assert_called_once()
        assert mock_push.call_args.kwargs["is_success"] is False

    def test_exchange_other_error_returns_0_and_push_success(self) -> None:
        """其他兑换错误: exit_code=0, push is_success=True（阅读仍成功）。"""
        cfg = _make_cfg()
        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch("wereadit.core.reader.read_books", return_value=_mock_read_result()),
            patch(
                "wereadit.core.exchanger.exchange_awards",
                side_effect=ExchangeError("other error", -999),
            ),
            patch("wereadit.app.push") as mock_push,
        ):
            exit_code = main()

        assert exit_code == 0
        mock_push.assert_called_once()
        assert mock_push.call_args.kwargs["is_success"] is True

    def test_no_exchange_token_returns_0_and_push_success(self) -> None:
        """未配置 weread_access_token: 跳过兑换, exit_code=0, is_success=True。"""
        cfg = _make_cfg(weread_android_token="", weread_ios_token="")
        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch("wereadit.core.reader.read_books", return_value=_mock_read_result()),
            patch("wereadit.app.push") as mock_push,
        ):
            exit_code = main()

        assert exit_code == 0
        mock_push.assert_called_once()
        assert mock_push.call_args.kwargs["is_success"] is True
