"""config_check 配置检查：各检查分支、报告格式、退出码。"""

from __future__ import annotations

from unittest.mock import patch

from wereadit.config import Config
from wereadit.config_check import main
from wereadit.core.token_refresher import RefreshResult


def _make_cfg(**overrides) -> Config:
    defaults = dict(
        read_num=120,
        books=["b1"],
        chapters=["c1"],
        pushplus_token="push_token",
        wxpusher_spt="",
        telegram_bot_token="",
        telegram_chat_id="",
        serverchan_spt="",
        app_token="",
        app_token_key="",
        weread_app_curl=(
            "curl 'https://i.weread.qq.com/login' "
            "--data-raw '{\"deviceId\":\"dev1\"}'"
        ),
        exchange_award="2,2,2,2,2,2,2,2",
        headers={},
        cookies={"wr_skey": "abc", "wr_vid": "12345"},
        web_curl="curl 'https://weread.qq.com/web/book/read'",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _run(cfg: Config, refresh_result: RefreshResult | None = None):
    """跑 config_check.main()，返回 (exit_code, mock_push)。"""
    if refresh_result is None:
        refresh_result = RefreshResult(token="tok_12345678", token_key="skey")
    with (
        patch("wereadit.config_check.load_config", return_value=cfg),
        patch("wereadit.config_check.HttpClient"),
        patch("wereadit.config_check.push") as mock_push,
        patch(
            "wereadit.core.token_refresher.refresh_app_token",
            return_value=refresh_result,
        ),
    ):
        exit_code = main()
    return exit_code, mock_push


class TestConfigCheck:
    def test_all_ok_returns_0(self) -> None:
        exit_code, mock_push = _run(_make_cfg())
        assert exit_code == 0
        report = mock_push.call_args.args[0]
        assert "[正常] WEREAD_WEB_CURL" in report
        assert "[正常] WEREAD_APP_CURL" in report
        assert "平台自识别为 iOS" in report
        assert "READ_NUM=120" in report
        assert "全部检查通过" in report
        assert mock_push.call_args.kwargs["is_success"] is True

    def test_web_curl_missing(self) -> None:
        cfg = _make_cfg(web_curl="", cookies={})
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        report = mock_push.call_args.args[0]
        assert "[异常] WEREAD_WEB_CURL：未配置" in report

    def test_web_curl_missing_cookie_keys(self) -> None:
        cfg = _make_cfg(cookies={"wr_skey": "abc"})  # 缺 wr_vid
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        assert "wr_vid" in mock_push.call_args.args[0]

    def test_app_curl_missing(self) -> None:
        cfg = _make_cfg(weread_app_curl="")
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        assert "[异常] WEREAD_APP_CURL：未配置" in mock_push.call_args.args[0]

    def test_app_curl_unhealthy(self) -> None:
        cfg = _make_cfg(weread_app_curl="curl 'https://i.weread.qq.com/readdetail'")
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        assert "不是 /login" in mock_push.call_args.args[0]

    def test_app_curl_refresh_failed(self) -> None:
        cfg = _make_cfg()
        exit_code, mock_push = _run(
            cfg, RefreshResult(diagnosis="login 凭证已失效 (errcode=-2012)")
        )
        assert exit_code == 1
        assert "[异常] WEREAD_APP_CURL" in mock_push.call_args.args[0]
        assert "-2012" in mock_push.call_args.args[0]

    def test_no_push_channel_skips_push(self) -> None:
        cfg = _make_cfg(pushplus_token="")
        exit_code, mock_push = _run(cfg)
        assert exit_code == 0  # 推送为可选项，不配置不影响检查结果
        mock_push.assert_not_called()

    def test_app_curl_android_platform(self) -> None:
        """token_key=accessToken 时报告平台自识别为 Android。"""
        cfg = _make_cfg()
        exit_code, mock_push = _run(
            cfg, RefreshResult(token="at_12345678", token_key="accessToken")
        )
        assert exit_code == 0
        assert "平台自识别为 Android" in mock_push.call_args.args[0]
