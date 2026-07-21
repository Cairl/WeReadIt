"""token_refresher 测试：验证 /login 重放续期逻辑。

覆盖：
- parse_curl_full 解析 URL/headers/cookies/body
- _find_token_in_json 递归提取（嵌套/列表/深度限制）
- _extract_token_from_response 从响应体 JSON / 响应 header / Set-Cookie 提取 token
- _summarize_structure 结构摘要（脱敏）
- RefreshResult 结构化结果
- diagnose_login_curl 配置体检
- refresh_app_token 重放、重试与四分类诊断
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests

from wereadit.core.token_refresher import (
    RefreshResult,
    _extract_token_from_response,
    _find_token_in_json,
    _summarize_structure,
    diagnose_login_curl,
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


class TestRefreshResult:
    """RefreshResult 结构化结果。"""

    def test_ok_when_token_present(self) -> None:
        result = RefreshResult(token="abc123456789", token_key="skey")
        assert result.ok is True
        assert result.diagnosis == ""

    def test_not_ok_when_token_missing(self) -> None:
        result = RefreshResult(diagnosis="失败原因")
        assert result.ok is False
        assert result.token is None


class TestFindTokenInJson:
    """_find_token_in_json 递归提取 token。"""

    def test_top_level_skey(self) -> None:
        token, key = _find_token_in_json({"skey": "top_skey"})
        assert token == "top_skey"
        assert key == "skey"

    def test_nested_access_token(self) -> None:
        token, key = _find_token_in_json({"data": {"accessToken": "nested_token"}})
        assert token == "nested_token"
        assert key == "accessToken"

    def test_three_level_nesting(self) -> None:
        token, key = _find_token_in_json(
            {"data": {"user": {"login": {"skey": "deep_skey"}}}}
        )
        assert token == "deep_skey"
        assert key == "skey"

    def test_inside_list(self) -> None:
        token, key = _find_token_in_json({"items": [{"x": 1}, {"access_token": "list_tok"}]})
        assert token == "list_tok"
        assert key == "access_token"

    def test_priority_order_at_same_level(self) -> None:
        """同层按 _TOKEN_KEYS 优先级：skey 优先于 token。"""
        token, key = _find_token_in_json({"token": "low_prio", "skey": "high_prio"})
        assert token == "high_prio"
        assert key == "skey"

    def test_empty_value_skipped(self) -> None:
        """空串/None 值不算命中，继续找。"""
        token, _ = _find_token_in_json({"skey": "", "data": {"skey": "real"}})
        assert token == "real"

    def test_depth_limit_returns_none(self) -> None:
        """超过 5 层嵌套不再深入。"""
        obj: dict = {}
        current = obj
        for _ in range(7):
            current["next"] = {}
            current = current["next"]
        current["skey"] = "too_deep"
        token, key = _find_token_in_json(obj)
        assert token is None
        assert key == ""

    def test_no_token_returns_none(self) -> None:
        token, key = _find_token_in_json({"data": {"name": "x"}})
        assert token is None
        assert key == ""


class TestSummarizeStructure:
    """_summarize_structure 生成键路径:类型 摘要（脱敏）。"""

    def test_flat_dict(self) -> None:
        summary = _summarize_structure({"errcode": 0, "errmsg": "ok"})
        assert "errcode:int" in summary
        assert "errmsg:str" in summary

    def test_nested_path(self) -> None:
        summary = _summarize_structure({"data": {"user": {"name": "x"}}})
        assert "data.user.name:str" in summary

    def test_values_not_leaked(self) -> None:
        """具体值不出现在摘要中。"""
        summary = _summarize_structure({"nickname": "张三", "skey": "secret123"})
        assert "张三" not in summary
        assert "secret123" not in summary

    def test_list_limited_to_first_three(self) -> None:
        summary = _summarize_structure({"items": [1, 2, 3, 4, 5]})
        assert "items[2]:int" in summary
        assert "items[3]" not in summary


class TestExtractTokenFromResponse:
    """_extract_token_from_response 从响应各位置提取 token，返回 (token, 命中字段名)。"""

    def test_extract_from_json_body_skey(self) -> None:
        response = MagicMock()
        response.json.return_value = {"skey": "new_skey_123", "other": "xxx"}
        assert _extract_token_from_response(response) == ("new_skey_123", "skey")

    def test_extract_from_nested_json(self) -> None:
        response = MagicMock()
        response.json.return_value = {"errcode": 0, "data": {"accessToken": "nested_at"}}
        assert _extract_token_from_response(response) == ("nested_at", "accessToken")

    def test_extract_from_header(self) -> None:
        response = MagicMock()
        response.json.side_effect = ValueError()
        response.headers = {"skey": "header_skey"}
        assert _extract_token_from_response(response) == ("header_skey", "skey")

    def test_extract_from_header_capitalized(self) -> None:
        response = MagicMock()
        response.json.side_effect = ValueError()
        response.headers = {"Skey": "header_skey_cap"}
        assert _extract_token_from_response(response) == ("header_skey_cap", "skey")

    def test_extract_from_set_cookie(self) -> None:
        response = MagicMock()
        response.json.side_effect = ValueError()
        response.headers = {"Set-Cookie": "skey=cookie_skey; Path=/; HttpOnly"}
        assert _extract_token_from_response(response) == ("cookie_skey", "skey")

    def test_no_token_returns_none(self) -> None:
        response = MagicMock()
        response.json.return_value = {"other": "xxx"}
        response.headers = {}
        assert _extract_token_from_response(response) == (None, "")

    def test_json_not_dict_returns_none(self) -> None:
        response = MagicMock()
        response.json.return_value = ["not", "a", "dict"]
        response.headers = {}
        assert _extract_token_from_response(response) == (None, "")


class TestDiagnoseLoginCurl:
    """diagnose_login_curl 静态体检（不发请求），空串 = 通过。"""

    _GOOD_CURL = (
        "curl 'https://i.weread.qq.com/login' "
        "-H 'vid: 12345' "
        "--data-raw '{\"deviceId\":\"dev1\",\"deviceName\":\"iPhone\"}'"
    )

    def test_good_curl_passes(self) -> None:
        assert diagnose_login_curl(self._GOOD_CURL) == ""

    def test_empty_curl(self) -> None:
        diagnosis = diagnose_login_curl("   ")
        assert "为空" in diagnosis

    def test_no_url(self) -> None:
        diagnosis = diagnose_login_curl("curl -H 'vid: 12345'")
        assert "URL" in diagnosis

    def test_not_login_url(self) -> None:
        curl = (
            "curl 'https://i.weread.qq.com/readdetail' "
            "--data-raw '{\"deviceId\":\"dev1\"}'"
        )
        diagnosis = diagnose_login_curl(curl)
        assert "不是 /login" in diagnosis
        assert "readdetail" in diagnosis

    def test_missing_device_id(self) -> None:
        curl = (
            "curl 'https://i.weread.qq.com/login' "
            "--data-raw '{\"deviceName\":\"iPhone\"}'"
        )
        diagnosis = diagnose_login_curl(curl)
        assert "deviceId" in diagnosis
        assert "冷启动" in diagnosis


class TestRefreshAppToken:
    """refresh_app_token 重放 /login 请求刷新 token，返回 RefreshResult。"""

    _LOGIN_CURL = (
        "curl 'https://i.weread.qq.com/login' "
        "-H 'vid: 12345' "
        "--data-raw '{\"deviceId\":\"dev1\"}'"
    )

    @patch("wereadit.core.token_refresher.requests.post")
    def test_success_from_json(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"skey": "fresh_skey_abc"}
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is True
        assert result.token == "fresh_skey_abc"
        assert result.token_key == "skey"
        assert result.diagnosis == ""
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.args[0] == "https://i.weread.qq.com/login"
        assert call_kwargs.kwargs["data"] == '{"deviceId":"dev1"}'

    @patch("wereadit.core.token_refresher.requests.post")
    def test_success_from_nested_json(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"errcode": 0, "data": {"accessToken": "nested_at"}}
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is True
        assert result.token == "nested_at"
        assert result.token_key == "accessToken"

    @patch("wereadit.core.token_refresher.requests.post")
    def test_success_from_header(self, mock_post: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError()
        mock_response.headers = {"skey": "header_fresh_skey"}
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is True
        assert result.token == "header_fresh_skey"

    @patch("wereadit.core.token_refresher.requests.post")
    def test_errcode_rejected_no_retry(self, mock_post: MagicMock) -> None:
        """响应 errcode 非 0：凭证失效，不重试。"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"errcode": -2012, "errmsg": "登录超时"}
        mock_response.headers = {}
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is False
        assert "-2012" in result.diagnosis
        assert "重新抓包" in result.diagnosis
        mock_post.assert_called_once()

    @patch("wereadit.core.token_refresher.requests.post")
    def test_http_4xx_rejected_no_retry(self, mock_post: MagicMock) -> None:
        """HTTP 4xx：服务端拒绝，不重试。"""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "unauthorized"
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is False
        assert "401" in result.diagnosis
        assert "重新抓包" in result.diagnosis
        mock_post.assert_called_once()

    @patch("wereadit.core.token_refresher.requests.post")
    def test_unknown_structure_diagnosis(self, mock_post: MagicMock) -> None:
        """200 但递归提取不到 token：诊断含响应结构摘要。"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": {"user": {"name": "x"}}}
        mock_response.headers = {}
        mock_post.return_value = mock_response

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is False
        assert "未找到 token" in result.diagnosis
        assert "data.user.name:str" in result.diagnosis
        # 脱敏：具体值不出现
        assert '"x"' not in result.diagnosis

    @patch("wereadit.core.token_refresher.time.sleep")
    @patch("wereadit.core.token_refresher.requests.post")
    def test_network_retry_then_success(
        self, mock_post: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """首次网络异常，退避后第二次成功。"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"skey": "retry_skey"}
        mock_post.side_effect = [requests.RequestException("timeout"), mock_response]

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is True
        assert result.token == "retry_skey"
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(5)

    @patch("wereadit.core.token_refresher.time.sleep")
    @patch("wereadit.core.token_refresher.requests.post")
    def test_network_all_attempts_fail(
        self, mock_post: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """三次均网络异常：诊断含网络异常说明，退避间隔 5s/10s。"""
        mock_post.side_effect = requests.RequestException("timeout")

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is False
        assert "网络异常" in result.diagnosis
        assert "明日自动重试" in result.diagnosis
        assert mock_post.call_count == 3
        assert [c.args[0] for c in mock_sleep.call_args_list] == [5, 10]

    @patch("wereadit.core.token_refresher.time.sleep")
    @patch("wereadit.core.token_refresher.requests.post")
    def test_http_5xx_retried(
        self, mock_post: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """HTTP 5xx 视同网络类，退避重试后成功。"""
        mock_500 = MagicMock()
        mock_500.status_code = 500
        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {"skey": "after_500"}
        mock_post.side_effect = [mock_500, mock_ok]

        result = refresh_app_token(self._LOGIN_CURL)
        assert result.ok is True
        assert result.token == "after_500"
        assert mock_post.call_count == 2
        mock_sleep.assert_called_once_with(5)

    def test_invalid_curl_no_url(self) -> None:
        result = refresh_app_token("curl -H 'vid: 12345'")
        assert result.ok is False
        assert "URL" in result.diagnosis
