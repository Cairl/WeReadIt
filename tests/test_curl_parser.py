"""curl 解析测试：验证 -H Cookie / -b 两种格式。"""

from __future__ import annotations

from wereadit.infra.curl_parser import parse_curl


class TestParseCurl:
    def test_parse_headers(self, sample_curl_bash: str) -> None:
        """应正确提取非 Cookie 的 header。"""
        headers, _ = parse_curl(sample_curl_bash)
        assert headers["accept"] == "application/json, text/plain, */*"
        assert headers["accept-language"] == "zh-CN,zh;q=0.9"
        assert headers["user-agent"] == "Mozilla/5.0 TestAgent"

    def test_cookie_extracted_from_h(self, sample_curl_bash: str) -> None:
        """从 -H 'Cookie: xxx' 提取 cookies。"""
        _, cookies = parse_curl(sample_curl_bash)
        assert cookies["wr_skey"] == "abc12345"
        assert cookies["wr_vid"] == "987654"
        assert cookies["RK"] == "oxEY1bTnXf"

    def test_cookie_header_removed_from_headers(self, sample_curl_bash: str) -> None:
        """headers 中不应残留 Cookie 字段。"""
        headers, _ = parse_curl(sample_curl_bash)
        assert not any(k.lower() == "cookie" for k in headers)

    def test_parse_b_flag(self) -> None:
        """从 -b 'xxx' 提取 cookies。"""
        curl = (
            "curl 'https://weread.qq.com/web/book/read' "
            "-H 'accept: application/json' "
            "-b 'wr_skey=xyz789; wr_vid=111'"
        )
        headers, cookies = parse_curl(curl)
        assert cookies["wr_skey"] == "xyz789"
        assert cookies["wr_vid"] == "111"
        assert "accept" in headers

    def test_b_flag_overrides_h_cookie(self) -> None:
        """同时存在 -H Cookie 和 -b 时，-b 优先。"""
        curl = (
            "curl 'https://x.com' "
            "-H 'Cookie: wr_skey=from_h' "
            "-b 'wr_skey=from_b'"
        )
        _, cookies = parse_curl(curl)
        assert cookies["wr_skey"] == "from_b"

    def test_empty_curl(self) -> None:
        """空字符串返回空字典。"""
        headers, cookies = parse_curl("")
        assert headers == {}
        assert cookies == {}

    def test_cookie_value_with_equals(self) -> None:
        """cookie value 含 = 时只切第一个。"""
        curl = "curl 'https://x.com' -H 'Cookie: k=v=1=2'"
        _, cookies = parse_curl(curl)
        assert cookies["k"] == "v=1=2"
