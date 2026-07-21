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


class TestMainTokenRefresh:
    """阅读前刷新 token 的编排：体检 -> 刷新 -> replace cfg -> 诊断入推送。"""

    _LOGIN_CURL = (
        "curl 'https://i.weread.qq.com/login' "
        "--data-raw '{\"deviceId\":\"dev1\"}'"
    )

    def _run_main(self, cfg: Config, call_order: list[str] | None = None):
        """以 mock 跑 main()，返回 (exit_code, mock_push, mock_exchange)。"""
        from wereadit.core.token_refresher import RefreshResult

        def _refresh_side_effect(*args):
            if call_order is not None:
                call_order.append("refresh")
            return RefreshResult(token="new_token_123456", token_key="accessToken")

        def _read_side_effect(*args, **kwargs):
            if call_order is not None:
                call_order.append("read")
            return _mock_read_result()

        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch(
                "wereadit.core.reader.read_books",
                side_effect=_read_side_effect,
            ),
            patch(
                "wereadit.core.exchanger.exchange_awards",
                return_value="兑换完成",
            ) as mock_exchange,
            patch("wereadit.app.push") as mock_push,
            patch(
                "wereadit.core.token_refresher.refresh_app_token",
                side_effect=_refresh_side_effect,
            ) as mock_refresh,
        ):
            exit_code = main()
        return exit_code, mock_push, mock_exchange, mock_refresh

    def test_refresh_before_reading_and_replace_cfg(self) -> None:
        """刷新发生在阅读之前，exchange_awards 收到的 cfg 已是新 token。"""
        call_order: list[str] = []
        cfg = _make_cfg(weread_login_curl=self._LOGIN_CURL)
        exit_code, _, mock_exchange, _ = self._run_main(cfg, call_order)

        assert exit_code == 0
        assert call_order == ["refresh", "read"]
        used_cfg = mock_exchange.call_args.args[1]
        assert used_cfg.weread_android_token == "new_token_123456"

    def test_exchange_receives_refresher_args(self) -> None:
        """exchange_awards 收到 refresher 回调与刷新时刻。"""
        cfg = _make_cfg(weread_login_curl=self._LOGIN_CURL)
        _, _, mock_exchange, _ = self._run_main(cfg)

        kwargs = mock_exchange.call_args.kwargs
        assert callable(kwargs["refresher"])
        assert isinstance(kwargs["token_refreshed_at"], float)

    def test_refresh_skipped_when_curl_unhealthy(self) -> None:
        """体检不过：不发起刷新，诊断进推送。"""
        cfg = _make_cfg(weread_login_curl="curl 'https://i.weread.qq.com/readdetail'")
        exit_code, mock_push, _, mock_refresh = self._run_main(cfg)

        assert exit_code == 0
        mock_refresh.assert_not_called()
        push_content = mock_push.call_args.args[0]
        assert "不是 /login" in push_content

    def test_refresh_failure_diagnosis_in_push(self) -> None:
        """刷新失败 + 兑换 -2012：推送含刷新诊断，cfg 用原 token。"""
        from wereadit.core.token_refresher import RefreshResult

        cfg = _make_cfg(weread_login_curl=self._LOGIN_CURL)
        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch("wereadit.core.reader.read_books", return_value=_mock_read_result()),
            patch(
                "wereadit.core.exchanger.exchange_awards",
                side_effect=ExchangeError("token expired", ERRCODE_TOKEN_EXPIRED),
            ) as mock_exchange,
            patch("wereadit.app.push") as mock_push,
            patch(
                "wereadit.core.token_refresher.refresh_app_token",
                return_value=RefreshResult(diagnosis="网络异常（重试 3 次均失败）"),
            ),
        ):
            exit_code = main()

        assert exit_code == 1
        used_cfg = mock_exchange.call_args.args[1]
        assert used_cfg.weread_android_token == "test_token"  # 未被替换
        push_content = mock_push.call_args.args[0]
        assert "网络异常" in push_content
        assert "根因" in push_content

    def test_platform_mismatch_not_replaced(self) -> None:
        """iOS curl 配 Android token（错位）：不替换 cfg，诊断进推送。"""
        from wereadit.core.token_refresher import RefreshResult

        cfg = _make_cfg(weread_login_curl=self._LOGIN_CURL)  # Android 平台
        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch("wereadit.core.reader.read_books", return_value=_mock_read_result()),
            patch(
                "wereadit.core.exchanger.exchange_awards",
                return_value="兑换完成",
            ) as mock_exchange,
            patch("wereadit.app.push") as mock_push,
            patch(
                "wereadit.core.token_refresher.refresh_app_token",
                return_value=RefreshResult(token="ios_skey_123456", token_key="skey"),
            ),
        ):
            main()

        used_cfg = mock_exchange.call_args.args[1]
        assert used_cfg.weread_android_token == "test_token"  # 未被替换
        push_content = mock_push.call_args.args[0]
        assert "不匹配" in push_content

    def test_no_login_curl_no_refresh(self) -> None:
        """未配置 login curl：刷新段整体跳过，现有行为不变。"""
        cfg = _make_cfg()  # weread_login_curl 默认 ""
        exit_code, _, mock_exchange, mock_refresh = self._run_main(cfg)

        assert exit_code == 0
        mock_refresh.assert_not_called()
        kwargs = mock_exchange.call_args.kwargs
        assert kwargs["refresher"] is None
        assert kwargs["token_refreshed_at"] is None
