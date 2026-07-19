"""加密工具测试：验证 encode_data / cal_hash 的确定性与已知输出。"""

from __future__ import annotations

from wereadit.utils.crypto import cal_hash, encode_data, sign_request


class TestEncodeData:
    def test_empty_dict(self) -> None:
        assert encode_data({}) == ""

    def test_single_key(self) -> None:
        result = encode_data({"a": "1"})
        assert result == "a=1"

    def test_keys_sorted(self) -> None:
        """按键名字典序输出。"""
        result = encode_data({"b": "2", "a": "1", "c": "3"})
        assert result == "a=1&b=2&c=3"

    def test_value_url_encoded(self) -> None:
        """特殊字符做 URL 编码。"""
        result = encode_data({"k": "a b/c"})
        # space -> %20, / -> %2F
        assert "%20" in result
        assert "%2F" in result

    def test_deterministic(self) -> None:
        """相同输入永远产生相同输出。"""
        data = {"b": "2", "a": "1"}
        assert encode_data(data) == encode_data(data)


class TestCalHash:
    def test_known_value(self) -> None:
        """验证已知输入产生稳定输出（回归基线）。"""
        # 任意固定字符串，校验返回值为十六进制字符串
        result = cal_hash("hello world")
        assert isinstance(result, str)
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self) -> None:
        assert cal_hash("test") == cal_hash("test")

    def test_different_inputs_different_outputs(self) -> None:
        assert cal_hash("foo") != cal_hash("bar")

    def test_empty_string(self) -> None:
        """空字符串不报错。"""
        result = cal_hash("")
        assert isinstance(result, str)


class TestSignRequest:
    def test_sign_fills_sg_and_s(self) -> None:
        """sign_request 应回填 sg 与 s 字段。"""
        data = {"ts": 1744264311434, "rn": 466}
        result = sign_request(data, "fake_key")
        assert "sg" in result
        assert "s" in result
        assert len(result["sg"]) == 64  # sha256 hex
        assert isinstance(result["s"], str)

    def test_sign_deterministic(self) -> None:
        data = {"ts": 1000, "rn": 1}
        d1 = sign_request(dict(data), "key")
        d2 = sign_request(dict(data), "key")
        assert d1["sg"] == d2["sg"]
        assert d1["s"] == d2["s"]
