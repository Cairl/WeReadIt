"""pytest 共享 fixtures。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 把 src/ 加入 sys.path，兼容未 pip install 的场景
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def mock_client() -> MagicMock:
    """Mock HttpClient，用于测试 push/exchanger。"""
    client = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.text = '{"success":true}'
    response.json.return_value = {"success": True}
    response.raise_for_status.return_value = None
    response.ok = True
    client.post.return_value = response
    client.get.return_value = response
    return client


@pytest.fixture
def sample_curl_bash() -> str:
    """示例 curl 命令，包含 -H Cookie 与多个 header。"""
    return (
        "curl 'https://weread.qq.com/web/book/read' "
        "-H 'accept: application/json, text/plain, */*' "
        "-H 'accept-language: zh-CN,zh;q=0.9' "
        "-H 'user-agent: Mozilla/5.0 TestAgent' "
        "-H 'Cookie: wr_skey=abc12345; wr_vid=987654; RK=oxEY1bTnXf'"
    )
