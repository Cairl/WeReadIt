"""token_refresher 测试：验证 /login 重放与 web wr_skey 续期逻辑。

覆盖：
- parse_curl_full 解析 URL/headers/cookies/body
- _extract_token_from_response 从响应体 JSON / 响应 header / Set-Cookie 提取 token
- refresh_app_token 重放 /login 请求与异常处理
- refresh_app_token_via_web 通过 web renewal 获取 wr_skey 完整值
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from wereadit.core.token_refresher import (
    _extract_token_from_response,
    refresh_app_token,
)
from wereadit.infra.curl_parser import parse_curl_full


class TestParseCurlFull:
    """parse_curl_full 解析 URL/headers/cookies/body。"""

    def test_parse_url_headers_cookies_body(self) -> None:
        curl = (
            "curl 'https://i.weread.qq.com/login' "
            "-H 'vid: 12345' "
            "-H 'Content-Type: application/json' "
            "-H 'Cookie: wr_skey=abc; wr_vid=12345' "
            "--data-raw '{\"deviceId\":\"dev1\",\"deviceName\":\"iPhone\"}'"
        )
        url, headers, cookies, body = parse_curl_full(curl)
        assert url == "https://i.weread.qq.com/login"
        assert headers["vid"] == "12345"
        assert headers["Content-Type"] == "application/json"
        assert cookies["wr_skey"] == "abc"
        assert "deviceId" in body

    def test_parse_with_d_flag(self) -> None:
        curl = "curl 'https://i.weread.qq.com/login' -d '{\"key\":\"value\"}'"
        url, _, _, body = parse_curl_full(curl)
        assert url == "https://i.weread.qq.com/login"
        assert body == '{"key":"value"}'

    def test_parse_with_data_binary(self) -> None:
        curl = (
            "curl 'https://i.weread.qq.com/login' "
            "--data-binary '{\"binary\":true}'"
        )
        _, _, _, body = parse_curl_full(curl)
        assert body == '{"binary":true}'

    def test_no_body_returns_empty(self) -> None:
        curl = "curl 'https://i.weread.qq.com/login' -H 'vid: 12345'"
        _, _, _, body = parse_curl_full(curl)
        assert body == ""

    def test_no_url_returns_empty(self) -> None:
        curl = "curl -H 'vid: 12345'"
        url, _, _, _ = parse_curl_full(curl)
        assert url == ""


class TestExtractTokenFromResponse:
    """_extract_token_from_response 从响应各位置提取 token。"""

    def test_extract_from_json_body_skey(self) -> None:
        response = MagicMock()
        response.json.return_value = {"skey": "new_skey_123", "other": "xxx"}
        assert _extract_token_from_response(response) == "new_skey_123"

    def test_extract_from_json_body_access_token(self) -> None:
        response = MagicMock()
        response.json.return_value = {"accessToken": "new_access_token"}
        assert _extract_token_from_response(response) == "new_access_token"

    def test_extract_from_header(self) -> None:
        response = MagicMock()
        response.json.side_effect = ValueError()
        response.headers = {"skey": "header_skey"}
        assert _extract_token_from_response(response) == "header_skey"

    def test_extract_from_header_capitalized(self) -> None:
        response = MagicMock()
        response.json.side_effect = ValueError()
        response.headers = {"Skey": "header_skey_cap"}
        assert _extract_token_from_response(response) == "header_skey_cap"

    def test_extract_from_set_cookie(self) -> None:
        response = MagicMock()
        response.json.side_effect = ValueError()
        response.headers = {"Set-Cookie": "skey=cookie_skey; Path=/; HttpOnly"}
        assert _extract_token_from_response(response) == "cookie_skey"

    def test_no_token_returns_none(self) -> None:
        response = MagicMock()
        response.json.return_value = {"other": "xxx"}
        response.headers = {}
        assert _extract_token_from_response(response) is None

    def test_json_not_dict_returns_none(self) -> None:
        response = MagicMock()
        response.json.return_value = ["not", "a", "dict"]
        response.headers = {}
        assert _extract_token_from_response(response) is None


class TestRefreshAppToken:
    """refresh_app_token 重放 /login 请求刷新 token。"""

    _LOGIN_CURL = (
        "curl 'https://i.weread.qq.com/login' "
        "-H 'vid: 12345' "
        "--data-raw '{\"deviceId\":\"dev1\"}'"
    )

    @patch("wereadit.core.token_refresher.requests.post")
    def test_refresh_success_from_json(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"skey": "fresh_skey_abc"}
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result == "fresh_skey_abc"
        mock_post.assert_called_once()
        # 验证请求参数
        call_kwargs = mock_post.call_args
        assert call_kwargs.args[0] == "https://i.weread.qq.com/login"
        assert call_kwargs.kwargs["data"] == '{"deviceId":"dev1"}'

    @patch("wereadit.core.token_refresher.requests.post")
    def test_refresh_success_from_header(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.side_effect = ValueError()
        mock_response.headers = {"skey": "header_fresh_skey"}
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result == "header_fresh_skey"

    @patch("wereadit.core.token_refresher.requests.post")
    def test_refresh_failure_no_token(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"other": "xxx"}
        mock_response.headers = {}
        mock_response.status_code = 200
        mock_response.text = "{}"
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result is None

    @patch("wereadit.core.token_refresher.requests.post")
    def test_refresh_network_error_returns_none(self, mock_post: MagicMock) -> None:
        mock_post.side_effect = requests.RequestException("timeout")

        result = refresh_app_token(self._LOGIN_CURL)
        assert result is None

    def test_invalid_curl_no_url_returns_none(self) -> None:
        result = refresh_app_token("curl -H 'vid: 12345'")
        assert result is None


class TestRefreshAppTokenViaWeb:
    """refresh_app_token_via_web 通过 web renewal 获取 wr_skey 完整值。"""

    def test_success_returns_full_wr_skey(self) -> None:
        """renewal 响应含 wr_skey 时返回完整值（不截断）。"""
        from wereadit.core.token_refresher import refresh_app_token_via_web

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.cookies = {"wr_skey": "abcdef1234567890"}
        mock_client.post.return_value = mock_response

        result = refresh_app_token_via_web(mock_client)
        assert result == "abcdef1234567890"
        assert len(result) == 16  # 完整值，不是截断的 8 位

    def test_no_wr_skey_in_first_variant_tries_next(self) -> None:
        """第一种 payload 无 wr_skey 时尝试下一种。"""
        from wereadit.core.token_refresher import refresh_app_token_via_web

        mock_client = MagicMock()
        resp1 = MagicMock()
        resp1.cookies = {}
        resp2 = MagicMock()
        resp2.cookies = {"wr_skey": "skey_from_variant2"}
        mock_client.post.side_effect = [resp1, resp2, resp2]

        result = refresh_app_token_via_web(mock_client)
        assert result == "skey_from_variant2"

    def test_all_variants_fail_returns_none(self) -> None:
        """所有 payload 都无 wr_skey 时返回 None。"""
        from wereadit.core.token_refresher import refresh_app_token_via_web

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.cookies = {}
        mock_client.post.return_value = mock_response

        result = refresh_app_token_via_web(mock_client)
        assert result is None

    def test_network_error_returns_none(self) -> None:
        """网络异常时返回 None。"""
        from wereadit.core.token_refresher import refresh_app_token_via_web

        mock_client = MagicMock()
        mock_client.post.side_effect = requests.RequestException("timeout")

        result = refresh_app_token_via_web(mock_client)
        assert result is None
