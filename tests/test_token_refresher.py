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

from unittest.mock import MagicMock

from wereadit.core.token_refresher import (
    RefreshResult,
    _extract_token_from_response,
    _find_token_in_json,
    _summarize_structure,
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
