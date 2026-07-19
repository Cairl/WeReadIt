"""加密与签名工具。

对应原 main.py 中的 encode_data / cal_hash 两个函数。
保留原始算法以保证业务逻辑不变。
"""

from __future__ import annotations

import hashlib
import urllib.parse


def encode_data(data: dict) -> str:
    """数据编码：按键名字典序拼接为 k=v 形式，value 做 URL 编码。"""
    return "&".join(
        f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys())
    )


def cal_hash(input_string: str) -> str:
    """计算微信读书接口的校验和哈希。

    保留原始变量命名（_7032f5 / _cc1055 / _19094e）以便与逆向代码对照。
    """
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1

    while _19094e > 0:
        _7032f5 = 0x7FFFFFFF & (_7032f5 ^ ord(input_string[_19094e]) << (length - _19094e) % 30)
        _cc1055 = 0x7FFFFFFF & (_cc1055 ^ ord(input_string[_19094e - 1]) << _19094e % 30)
        _19094e -= 2

    return hex(_7032f5 + _cc1055)[2:].lower()


def sign_request(data: dict, key: str) -> dict:
    """为阅读请求计算签名并回填 sg/s 字段。

    Args:
        data: 待签名的请求数据（含 ts/rn 字段）
        key: 签名盐

    Returns:
        回填了 sg 与 s 字段的 data（原对象被修改）。
    """
    data["sg"] = hashlib.sha256(f"{data['ts']}{data['rn']}{key}".encode()).hexdigest()
    data["s"] = cal_hash(encode_data(data))
    return data
