"""config 加载与派生测试：新环境变量名、app_token/app_token_key 派生逻辑。"""

from __future__ import annotations

from unittest.mock import patch

from wereadit.config import Config, load_config
from wereadit.constants import PLATFORM_ANDROID, PLATFORM_IOS


class TestLoadConfigEnvNames:
    """环境变量新名读取（无旧名兼容）。"""

    def test_web_curl_read(self) -> None:
        env = {
            "WEREAD_WEB_CURL": (
                "curl 'https://weread.qq.com/web/book/read' "
                "-H 'Cookie: wr_skey=abc12345; wr_vid=12345'"
            )
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.web_curl.startswith("curl")
        assert cfg.cookies["wr_vid"] == "12345"

    def test_app_curl_read(self) -> None:
        env = {"WEREAD_APP_CURL": "curl 'https://i.weread.qq.com/login'"}
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.weread_app_curl == "curl 'https://i.weread.qq.com/login'"

    def test_old_names_ignored(self) -> None:
        """旧名 WEREAD_CURL_BASH / WEREAD_LOGIN_CURL 不再读取。"""
        env = {
            "WEREAD_CURL_BASH": (
                "curl 'https://weread.qq.com/web/book/read' -H 'Cookie: wr_vid=1'"
            ),
            "WEREAD_LOGIN_CURL": "curl 'https://i.weread.qq.com/login'",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.web_curl == ""
        assert cfg.weread_app_curl == ""


class TestTokenDerivation:
    """app_token/app_token_key 与 property 派生。"""

    def test_access_token_returns_app_token(self) -> None:
        cfg = Config(read_num=1, app_token="tok123")
        assert cfg.weread_access_token == "tok123"

    def test_platform_ios_when_key_skey(self) -> None:
        cfg = Config(read_num=1, app_token="tok", app_token_key="skey")
        assert cfg.weread_platform == PLATFORM_IOS

    def test_platform_android_when_key_access_token(self) -> None:
        cfg = Config(read_num=1, app_token="tok", app_token_key="accessToken")
        assert cfg.weread_platform == PLATFORM_ANDROID

    def test_platform_default_android_when_empty(self) -> None:
        cfg = Config(read_num=1)
        assert cfg.weread_platform == PLATFORM_ANDROID
        assert cfg.weread_access_token == ""
