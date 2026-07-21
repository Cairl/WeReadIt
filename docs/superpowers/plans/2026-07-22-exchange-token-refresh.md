# 兑换 Token 自动续期修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `/login` 重放续期链路（自适应提取 + 配置体检 + 诊断直推 + 阅读前刷新 + 补刷保险），实现兑换书币的准零手动托管。

**Architecture:** 重写 `token_refresher.py`：`RefreshResult` 结构化返回（token + 命中字段名 + 人话诊断），递归 JSON 提取替代顶层猜测，`diagnose_login_curl` 静态体检；`app.py` 把刷新挪到阅读前并用 `dataclasses.replace` 注入新 token；`exchanger.py` 删除内联刷新，改为接收外部注入的 refresher 回调做补刷保险。

**Tech Stack:** Python 3.10+ / requests / pytest / ruff

**Spec:** `docs/superpowers/specs/2026-07-22-exchange-token-refresh-design.md`

## Global Constraints

- Python 3.10+，类型注解一律 `from __future__ import annotations`
- 每个 Task 完成后 `pytest tests/` 与 `ruff check src/ tests/` 必须全过
- 提交信息格式 `[模块] 简要描述`（参考 git log 现有风格）
- 推送与日志中 token 只显示前 8 位（脱敏）
- 禁止触碰保活策略代码（AGENTS.md「Keepalive Strategy」列出项）
- 禁止裸 `except:`，项目异常定义在 `wereadit/exceptions.py`
- 比 spec 多出的一处实现细节（已纳入设计意图范围内）：`RefreshResult` 增加 `token_key` 字段记录命中字段名，用于 iOS/Android 平台错位校验（防 iOS login curl 配 Android token 的交叉错位）

---

### Task 1: RefreshResult + 递归提取 + 结构摘要

**Files:**
- Modify: `src/wereadit/core/token_refresher.py`
- Test: `tests/test_token_refresher.py`

**Interfaces:**
- Consumes: 现有 `parse_curl_full`（不改动）
- Produces:
  - `RefreshResult(token: str | None = None, token_key: str = "", diagnosis: str = "")`，属性 `.ok -> bool`
  - `_find_token_in_json(obj: object, depth: int = 0) -> tuple[str | None, str]`
  - `_extract_token_from_response(response: requests.Response) -> tuple[str | None, str]`（签名变更：返回 (token, 命中字段名)）
  - `_summarize_structure(obj: object) -> str`

- [ ] **Step 1: 写失败测试**

在 `tests/test_token_refresher.py` 顶部 import 区替换为（注意：本步**同时删除**旧的 `TestRefreshAppToken` 与 `TestRefreshAppTokenViaWeb` 两个测试类 —— 它们将在 Task 3 被整体重写，删除后本 Task 结束全量测试即绿）：

```python
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
    refresh_app_token,
)
from wereadit.infra.curl_parser import parse_curl_full
```

在 `TestExtractTokenFromResponse` 类之前新增三个测试类：

```python
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
```

并把 `TestExtractTokenFromResponse` 整个类替换为（适配新返回类型）：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_token_refresher.py -x -q
```

预期：ImportError（`RefreshResult` 等尚不存在）

- [ ] **Step 3: 实现**

把 `src/wereadit/core/token_refresher.py` 的模块 docstring 到 `_extract_token_from_response` 末尾（第 1-116 行）替换为：

```python
"""App 端 Token 自动续期。

微信读书 App 端 skey/accessToken 有效期极短（约 2 小时），无法长期使用。
本模块通过重放 App 端 /login 请求（i.weread.qq.com/login）实现自动续期：
/login 以请求 body 中的 deviceId 等长效设备凭证换取新 token，
抓包一次即可长期反复重放（社区实证）。

参考：https://www.ppanda.com/posts/tech/微信读书三方插件cookie失效问题修复

设计要点：
- login curl 重放使用独立的 requests 调用，不带 web 端 cookie，
  避免 web cookie 干扰 App 端认证（两套独立认证体系）。
- 响应中 skey/accessToken 的位置未公开，因此递归遍历响应 JSON
  （任意嵌套，深度限 5 层），外加响应 header、Set-Cookie 两路兜底。
- 刷新结果以 RefreshResult 返回：token + 命中字段名 + 人话诊断，
  诊断可直接进推送，无需翻 Actions 日志。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from wereadit.constants import (
    LOGIN_MAX_ATTEMPTS,
    LOGIN_RETRY_INTERVAL,
    LOGIN_TIMEOUT,
)
from wereadit.infra.curl_parser import parse_curl_full

logger = logging.getLogger(__name__)

# 响应中可能包含 token 的字段名（按优先级尝试）
_TOKEN_KEYS = ("skey", "accessToken", "access_token", "token")
# 递归提取/结构摘要的最大深度（防异常响应导致栈溢出）
_MAX_EXTRACT_DEPTH = 5
# 结构摘要最多输出的条目数
_MAX_SUMMARY_ITEMS = 20


@dataclass(frozen=True)
class RefreshResult:
    """Token 刷新结果。

    token 为 None 表示失败，此时 diagnosis 为人话诊断 + 下一步指引；
    token_key 记录命中字段名（如 "skey" / "accessToken"），供平台一致性校验。
    """

    token: str | None = None
    token_key: str = ""
    diagnosis: str = ""

    @property
    def ok(self) -> bool:
        return self.token is not None


def _find_token_in_json(obj: object, depth: int = 0) -> tuple[str | None, str]:
    """递归查找 token 字段，返回 (token, 命中字段名)；未找到返回 (None, "")。"""
    if depth > _MAX_EXTRACT_DEPTH:
        return None, ""
    if isinstance(obj, dict):
        for key in _TOKEN_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value, key
        for value in obj.values():
            found, key = _find_token_in_json(value, depth + 1)
            if found:
                return found, key
    elif isinstance(obj, list):
        for item in obj:
            found, key = _find_token_in_json(item, depth + 1)
            if found:
                return found, key
    return None, ""


def _extract_token_from_response(response: requests.Response) -> tuple[str | None, str]:
    """从响应中提取 token，返回 (token, 命中字段名)。

    尝试以下位置（按优先级）：
    1. 响应体 JSON（递归任意嵌套）
    2. 响应 header（不区分大小写）
    3. Set-Cookie
    """
    # 1. 响应体 JSON（递归）
    try:
        data = response.json()
    except ValueError:
        data = None
    if data is not None:
        token, key = _find_token_in_json(data)
        if token:
            return token, key

    # 2. 响应 header（不区分大小写）
    for key in _TOKEN_KEYS:
        value = response.headers.get(key) or response.headers.get(key.capitalize())
        if value:
            return value, key

    # 3. Set-Cookie
    set_cookie = response.headers.get("Set-Cookie", "")
    for part in set_cookie.split(";"):
        part = part.strip()
        for key in _TOKEN_KEYS:
            if part.startswith(f"{key}="):
                return part[len(key) + 1 :], key

    return None, ""


def _collect_structure(obj: object, prefix: str, depth: int, items: list[str]) -> None:
    """递归收集 JSON 键路径:类型（只收键名与类型，不收值，脱敏）。"""
    if len(items) >= _MAX_SUMMARY_ITEMS or depth > _MAX_EXTRACT_DEPTH:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if len(items) >= _MAX_SUMMARY_ITEMS:
                return
            path = f"{prefix}.{key}" if prefix else str(key)
            items.append(f"{path}:{type(value).__name__}")
            _collect_structure(value, path, depth + 1, items)
    elif isinstance(obj, list):
        for index, item in enumerate(obj[:3]):  # 列表只展开前 3 个元素
            if len(items) >= _MAX_SUMMARY_ITEMS:
                return
            path = f"{prefix}[{index}]"
            items.append(f"{path}:{type(item).__name__}")
            _collect_structure(item, path, depth + 1, items)


def _summarize_structure(obj: object) -> str:
    """生成 JSON 键路径:类型 摘要（如 "errcode:int, data.user.skey:str"）。"""
    items: list[str] = []
    _collect_structure(obj, "", 0, items)
    return ", ".join(items) if items else "(空)"
```

注意：

1. **import 区不动旧内容**：`refresh_app_token` 旧实现与 `refresh_app_token_via_web` 仍引用 `json`、`HttpClient`、`COOKIE_DATA_VARIANTS`、`RENEW_TIMEOUT`、`RENEW_URL`，本 Task 全部保留（Task 3 删函数时同步清理）；只在 import 区追加 `from dataclasses import dataclass`。
2. `constants.py` 中新常量尚不存在，先在 `src/wereadit/constants.py` 第 17 行 `LOGIN_TIMEOUT = 15` 之后插入（本 Task 只定义不 import，Task 3 才在 token_refresher 中引用）：

```python
# App 端 /login 重试参数（刷新 token，网络异常时退避重试）
LOGIN_MAX_ATTEMPTS = 3
LOGIN_RETRY_INTERVAL = 5
```

3. 旧 `refresh_app_token` 需最小适配新提取签名（完整重写见 Task 3）：`new_token = _extract_token_from_response(response)` 改为 `new_token, _ = _extract_token_from_response(response)`；`return new_token`（成功分支）改为 `return RefreshResult(token=new_token) if new_token else RefreshResult()`；其余 `return None` 改为 `return RefreshResult()`。旧提取函数末尾的 `return None` 不在此列（该函数已被整体替换）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_token_refresher.py tests/test_exchanger.py -q && python -m ruff check src/wereadit/core/token_refresher.py tests/test_token_refresher.py
```

预期：全部 PASS（旧 TestRefreshAppToken/TestRefreshAppTokenViaWeb 已删，test_exchanger 不受影响），ruff 无告警

- [ ] **Step 5: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/core/token_refresher.py src/wereadit/constants.py tests/test_token_refresher.py && git commit -m "[exchange] RefreshResult 结构化结果 + 递归 token 提取 + 响应结构摘要"
```

---

### Task 2: diagnose_login_curl 配置体检

**Files:**
- Modify: `src/wereadit/core/token_refresher.py`
- Test: `tests/test_token_refresher.py`

**Interfaces:**
- Consumes: Task 1 的 `parse_curl_full`
- Produces: `diagnose_login_curl(login_curl: str) -> str`（空串 = 通过；非空 = 人话诊断 + 修正指引）

- [ ] **Step 1: 写失败测试**

先在 `tests/test_token_refresher.py` 的 import 区给 `from wereadit.core.token_refresher import (...)` 列表追加 `diagnose_login_curl`，然后在 `TestExtractTokenFromResponse` 类之后新增：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_token_refresher.py -q -k TestDiagnoseLoginCurl
```

预期：FAIL（`diagnose_login_curl` 不存在或行为不符）

- [ ] **Step 3: 实现**

在 `src/wereadit/core/token_refresher.py` 的 `_summarize_structure` 之后追加：

```python
def diagnose_login_curl(login_curl: str) -> str:
    """静态体检 login curl（不发请求），返回诊断；空串表示通过。

    校验项：
    1. 非空且能解析出 URL
    2. URL 是 /login 请求（重放其他请求无法换新 token）
    3. body 含 deviceId（长效设备凭证，是 /login 换新 token 的依据）
    """
    if not login_curl.strip():
        return "WEREAD_LOGIN_CURL 为空，请按 README「Token 自动续期」一节配置"

    url, _, _, body = parse_curl_full(login_curl)
    if not url:
        return (
            "无法从 WEREAD_LOGIN_CURL 解析出 URL，"
            "请确认 Secret 中是完整的 cURL 命令（抓包工具「复制为 cURL (Bash)」）"
        )
    if "/login" not in url:
        return (
            f"抓到的请求不是 /login 而是 {url}。"
            "请按 README 抓包指引，在 App 触发 Token 刷新（杀掉 App 重新打开）时，"
            "抓取 i.weread.qq.com/login 请求"
        )
    if "deviceId" not in body:
        return (
            "/login 请求 body 中缺少 deviceId（长效设备凭证），重放无法换新 Token。"
            "请抓取 App 冷启动（杀掉 App 重新打开）时的 /login 请求，确保 body 含 deviceId"
        )
    return ""
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_token_refresher.py -q -k TestDiagnoseLoginCurl
```

预期：5 个测试全 PASS

- [ ] **Step 5: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/core/token_refresher.py tests/test_token_refresher.py && git commit -m "[exchange] login curl 静态体检：非 /login、缺 deviceId 提前给出修正指引"
```

---

### Task 3: refresh_app_token 主流程重写 + 删除 via_web 死代码

**Files:**
- Modify: `src/wereadit/core/token_refresher.py`
- Test: `tests/test_token_refresher.py`

**Interfaces:**
- Consumes: Task 1 的 `RefreshResult` / `_extract_token_from_response` / `_summarize_structure`；constants 的 `LOGIN_MAX_ATTEMPTS` / `LOGIN_RETRY_INTERVAL` / `LOGIN_TIMEOUT`
- Produces: `refresh_app_token(login_curl: str) -> RefreshResult`（供 Task 4/5 使用）

- [ ] **Step 1: 写失败测试**

把 `tests/test_token_refresher.py` 中 `TestRefreshAppToken` 和 `TestRefreshAppTokenViaWeb` 两个类整体替换为：

```python
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

    def test_invalid_curl_no_url(self) -> None:
        result = refresh_app_token("curl -H 'vid: 12345'")
        assert result.ok is False
        assert "URL" in result.diagnosis
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_token_refresher.py -q -k TestRefreshAppToken
```

预期：FAIL（旧实现返回 `str | None`，无重试/诊断）

- [ ] **Step 3: 实现**

把 `src/wereadit/core/token_refresher.py` 中 `refresh_app_token` 函数与 `refresh_app_token_via_web` 函数（含其间的全部内容）整体替换为：

```python
def refresh_app_token(login_curl: str) -> RefreshResult:
    """重放 /login 请求刷新 App 端 skey/accessToken。

    用户抓包 App 的 /login 请求（i.weread.qq.com/login），配置为环境变量。
    脚本重放该请求，从响应中提取新的 skey/accessToken。

    错误四分类：
    - 配置错误（解析不出 URL）：不重试
    - 网络错误（异常 / HTTP 5xx）：指数退避重试（最多 LOGIN_MAX_ATTEMPTS 次）
    - 服务端拒绝（HTTP 4xx / errcode 非 0）：不重试，指引重新抓包
    - 结构未知（200 但提取不到 token）：不重试，诊断含响应结构摘要

    Args:
        login_curl: /login 请求的 cURL 命令（抓包工具「复制为 Bash」）

    Returns:
        RefreshResult：成功含新 token 与命中字段名；失败含人话诊断
    """
    url, headers, cookies, body = parse_curl_full(login_curl)
    if not url:
        return RefreshResult(
            diagnosis="login curl 解析失败：未找到 URL，请检查 WEREAD_LOGIN_CURL 是否为完整 cURL 命令"
        )

    logger.info("刷新 App Token: POST %s", url)
    last_network_error = ""
    for attempt in range(LOGIN_MAX_ATTEMPTS):
        try:
            response = requests.post(
                url,
                data=body if body else None,
                headers=headers,
                cookies=cookies,
                timeout=LOGIN_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_network_error = str(exc)
            logger.warning(
                "刷新 App Token 网络异常（第 %d/%d 次）: %s",
                attempt + 1,
                LOGIN_MAX_ATTEMPTS,
                exc,
            )
            if attempt < LOGIN_MAX_ATTEMPTS - 1:
                time.sleep(LOGIN_RETRY_INTERVAL * (2**attempt))
            continue

        if response.status_code >= 500:
            last_network_error = f"HTTP {response.status_code}"
            logger.warning(
                "刷新 App Token 服务端错误（第 %d/%d 次）: HTTP %s",
                attempt + 1,
                LOGIN_MAX_ATTEMPTS,
                response.status_code,
            )
            if attempt < LOGIN_MAX_ATTEMPTS - 1:
                time.sleep(LOGIN_RETRY_INTERVAL * (2**attempt))
            continue

        if response.status_code >= 400:
            logger.warning(
                "刷新 App Token 被服务端拒绝: HTTP %s, 响应体=%s",
                response.status_code,
                response.text[:500],
            )
            return RefreshResult(
                diagnosis=(
                    f"login 凭证已被服务端拒绝 (HTTP {response.status_code})，"
                    "请重新抓包更新 WEREAD_LOGIN_CURL"
                )
            )

        # 检查 errcode（weread 约定 errcode==0 或缺失为成功）
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            errcode = data.get("errcode")
            if errcode not in (None, 0):
                errmsg = data.get("errmsg", "unknown")
                logger.warning(
                    "刷新 App Token 失败: errcode=%s, errmsg=%s", errcode, errmsg
                )
                return RefreshResult(
                    diagnosis=(
                        f"login 凭证已失效 (errcode={errcode}, {errmsg})，"
                        "请重新抓包更新 WEREAD_LOGIN_CURL"
                    )
                )

        token, token_key = _extract_token_from_response(response)
        if token:
            logger.info(
                "App Token 刷新成功, 字段=%s, 新 token=%s...", token_key, token[:8]
            )
            return RefreshResult(token=token, token_key=token_key)

        structure = _summarize_structure(data)
        logger.warning("刷新 App Token: 响应 200 但未找到 token, 结构=%s", structure)
        return RefreshResult(
            diagnosis=(
                f"/login 响应 200 但未找到 token，响应结构: {structure}。"
                "请把此信息反馈给开发者适配新的响应格式"
            )
        )

    return RefreshResult(
        diagnosis=(
            f"刷新 App Token 网络异常（重试 {LOGIN_MAX_ATTEMPTS} 次均失败）: {last_network_error}。"
            "本次为网络问题，明日自动重试"
        )
    )
```

同时调整模块顶部 import：

- 删除：`import json`、`from wereadit.infra.http import HttpClient`
- 新增：`import time`
- constants import 列表：移除 `COOKIE_DATA_VARIANTS, RENEW_TIMEOUT, RENEW_URL`，追加 `LOGIN_MAX_ATTEMPTS, LOGIN_RETRY_INTERVAL`（Task 1 已在 constants.py 定义），最终为 `from wereadit.constants import LOGIN_MAX_ATTEMPTS, LOGIN_RETRY_INTERVAL, LOGIN_TIMEOUT`

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_token_refresher.py -q && python -m ruff check src/wereadit/core/token_refresher.py tests/test_token_refresher.py
```

预期：全部 PASS，ruff 无告警（重点确认无 `json`/`HttpClient` 等未使用 import 残留）

- [ ] **Step 5: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/core/token_refresher.py tests/test_token_refresher.py && git commit -m "[exchange] /login 重放重写：RefreshResult 四分类诊断 + 网络退避重试，删除已证伪的 via_web 路径"
```

---

### Task 4: exchanger 适配外部注入 token + 补刷保险

**Files:**
- Modify: `src/wereadit/core/exchanger.py`
- Modify: `src/wereadit/constants.py`
- Test: `tests/test_exchanger.py`

**Interfaces:**
- Consumes: Task 3 的 `RefreshResult`
- Produces: `exchange_awards(client: HttpClient, cfg: Config, *, refresher: Callable[[], RefreshResult] | None = None, token_refreshed_at: float | None = None) -> str`（供 Task 5 调用）

- [ ] **Step 1: 写失败测试**

在 `tests/test_exchanger.py` 顶部 import 区追加：

```python
import time
from wereadit.core.token_refresher import RefreshResult
```

删除两个 `_mock_token_refresher` fixture（`TestExchangeAwards` 与 `TestExchangeLogging` 中各一个）—— exchanger 将不再调用 token_refresher。

在 `TestExchangeAwards` 类之后新增：

```python
class TestExchangeTokenRefresh:
    """补刷保险：token 年龄超阈值时兑换前调 refresher 补刷。"""

    def test_refresh_triggered_when_token_old(self, mock_client: MagicMock) -> None:
        """token 年龄 > TOKEN_MAX_AGE_SECONDS：补刷并用新 token 兑换。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        refresher = MagicMock(
            return_value=RefreshResult(token="new_token_123456", token_key="accessToken")
        )
        exchange_awards(
            mock_client,
            cfg,
            refresher=refresher,
            token_refreshed_at=time.time() - 6000,
        )
        refresher.assert_called_once()
        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["accessToken"] == "new_token_123456"

    def test_refresh_not_triggered_when_token_fresh(self, mock_client: MagicMock) -> None:
        """token 年龄 < 阈值：不补刷。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        refresher = MagicMock()
        exchange_awards(
            mock_client,
            cfg,
            refresher=refresher,
            token_refreshed_at=time.time() - 100,
        )
        refresher.assert_not_called()
        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["accessToken"] == "test_token"

    def test_refresh_failure_keeps_old_token(self, mock_client: MagicMock) -> None:
        """补刷失败：沿用原 token 继续兑换。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        refresher = MagicMock(return_value=RefreshResult(diagnosis="网络异常"))
        exchange_awards(
            mock_client,
            cfg,
            refresher=refresher,
            token_refreshed_at=time.time() - 6000,
        )
        _, kwargs = mock_client.post.call_args
        assert kwargs["headers"]["accessToken"] == "test_token"

    def test_no_refresher_no_crash_when_token_old(self, mock_client: MagicMock) -> None:
        """refresher 为 None 时即使 token 很旧也不补刷、不崩溃。"""
        cfg = _make_cfg()
        query_resp = _mock_award_data()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = query_resp
        mock_client.post.return_value = mock_response

        result = exchange_awards(
            mock_client,
            cfg,
            token_refreshed_at=time.time() - 6000,
        )
        assert "兑换奖励失败" not in result
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_exchanger.py -q -k TestExchangeTokenRefresh
```

预期：FAIL（`exchange_awards` 不接受 `refresher` 参数）

- [ ] **Step 3: 实现**

`src/wereadit/constants.py` 中 `EXCHANGE_RETRY_INTERVAL = 5` 之后追加：

```python
# 兑换 Token 补刷阈值（秒）：阅读前刷新后，若兑换前 token 年龄超过该值则补刷一次
# （App token 有效期约 2 小时，90 分钟留 30 分钟余量）
TOKEN_MAX_AGE_SECONDS = 90 * 60
```

`src/wereadit/core/exchanger.py` 改动：

1. import 区：`from typing import Any` 改为：

```python
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wereadit.core.token_refresher import RefreshResult
```

constants import 列表追加 `TOKEN_MAX_AGE_SECONDS`。

2. `exchange_awards` 签名与函数开头（原第 139-178 行，含内联刷新块）替换为：

```python
def exchange_awards(
    client: HttpClient,
    cfg: Config,
    *,
    refresher: Callable[[], RefreshResult] | None = None,
    token_refreshed_at: float | None = None,
) -> str:
    """查询并兑换阅读奖励。

    Args:
        client: HTTP 客户端
        cfg: 运行时配置（token 应由调用方在阅读前刷新并注入）
        refresher: 可选的 token 刷新回调（补刷保险用）
        token_refreshed_at: token 刷新时刻（time.time() 返回值），与 refresher
            配合；兑换前 token 年龄超过 TOKEN_MAX_AGE_SECONDS 时调 refresher 补刷

    Returns:
        兑换结果摘要字符串（用于推送）
    """
    auth_token = cfg.weread_access_token
    vid = cfg.cookies.get("wr_vid", "")
    if not vid:
        logger.warning("cookie 中未找到 wr_vid，跳过兑换")
        return "兑换奖励失败: cookie 中未找到 wr_vid"

    # 补刷保险：阅读耗时过长导致 token 年龄接近 2 小时有效期时，兑换前再刷一次
    if (
        refresher is not None
        and token_refreshed_at is not None
        and time.time() - token_refreshed_at > TOKEN_MAX_AGE_SECONDS
    ):
        logger.info("token 年龄超过 %ds，兑换前补刷...", TOKEN_MAX_AGE_SECONDS)
        refresh_result = refresher()
        if refresh_result.ok:
            auth_token = refresh_result.token
            logger.info("补刷成功, 新 token=%s...", auth_token[:8])
        else:
            logger.warning("补刷失败，沿用原 token: %s", refresh_result.diagnosis)

    strategy = _parse_strategy(cfg.exchange_award)
    platform_name = "iOS" if cfg.weread_platform == PLATFORM_IOS else "Android"

    # 排查 token 过快过期：记录本次使用的 token 前 8 位，便于对应 GitHub Secrets
    token_preview = auth_token[:8] if auth_token else ""
    logger.info(
        "兑换开始: 平台=%s, vid=%s, token=%s...",
        platform_name, vid, token_preview,
    )
```

（即：删除原内联刷新块与 `from wereadit.core.token_refresher import refresh_app_token` 函数内 import；查询及以下逻辑保持原样不动。）

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_exchanger.py -q && python -m ruff check src/wereadit/core/exchanger.py tests/test_exchanger.py
```

预期：全部 PASS，ruff 无告警

- [ ] **Step 5: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/core/exchanger.py src/wereadit/constants.py tests/test_exchanger.py && git commit -m "[exchange] 兑换 token 改为外部注入 + 年龄超 90 分钟补刷保险"
```

---

### Task 5: app.py 编排 —— 阅读前刷新 + 平台校验 + 诊断入推送

**Files:**
- Modify: `src/wereadit/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: Task 2 的 `diagnose_login_curl`、Task 3 的 `refresh_app_token` / `RefreshResult`、Task 4 的 `exchange_awards(client, cfg, *, refresher, token_refreshed_at)`
- Produces: `_replace_token(cfg: Config, new_token: str) -> Config`、`_token_key_matches_platform(token_key: str, platform: str) -> bool`（模块私有，供测试）

- [ ] **Step 1: 写失败测试**

在 `tests/test_app.py` 末尾追加：

```python
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
```

并在 import 区把 `from wereadit.constants import ERRCODE_TOKEN_EXPIRED` 保持不变（已存在）。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_app.py -q -k TestMainTokenRefresh
```

预期：FAIL（app.py 尚无刷新编排、`exchange_awards` 调用无新参数）

- [ ] **Step 3: 实现**

`src/wereadit/app.py` 改动：

1. import 区替换为：

```python
from __future__ import annotations

import dataclasses
import logging
import time
import traceback
from functools import partial

from wereadit.config import Config, load_config
from wereadit.constants import ERRCODE_TOKEN_EXPIRED, PLATFORM_IOS
from wereadit.exceptions import CookieExpiredError, ExchangeError, ReadFailedError
from wereadit.infra.http import HttpClient
from wereadit.push import push

logger = logging.getLogger(__name__)


def _replace_token(cfg: Config, new_token: str) -> Config:
    """按平台替换 Config 中对应的 token 字段（frozen dataclass 用 replace 派生新实例）。"""
    if cfg.weread_platform == PLATFORM_IOS:
        return dataclasses.replace(cfg, weread_ios_token=new_token)
    return dataclasses.replace(cfg, weread_android_token=new_token)


def _token_key_matches_platform(token_key: str, platform: str) -> bool:
    """校验刷新得到的 token 字段名与兑换平台是否匹配。

    iOS 登录响应下发 skey，Android 下发 accessToken；
    错位说明 login curl 与兑换 Token 抓自不同平台的设备。
    """
    if platform == PLATFORM_IOS:
        return token_key == "skey"
    return token_key != "skey"
```

2. `main()` 中 try 块开头（`# 阅读循环` 之前）插入刷新段，并把 `read_books` 调用行改为使用（可能被替换的）cfg：

```python
    try:
        # 兑换 Token 自动续期：阅读前刷新，保证兑换时 token 年龄在 2 小时窗口内
        # （旧设计在兑换前刷新，阅读 60+ 分钟后 token 年龄贴近有效期边缘）
        refresh_diagnosis = ""
        refresher = None
        token_refreshed_at = None
        if cfg.weread_access_token and cfg.weread_login_curl:
            from wereadit.core.token_refresher import (
                diagnose_login_curl,
                refresh_app_token,
            )

            curl_diagnosis = diagnose_login_curl(cfg.weread_login_curl)
            if curl_diagnosis:
                logger.warning("WEREAD_LOGIN_CURL 体检不过: %s", curl_diagnosis)
                refresh_diagnosis = f"Token 自动刷新已跳过：{curl_diagnosis}"
            else:
                refresher = partial(refresh_app_token, cfg.weread_login_curl)
                refresh_result = refresher()
                if refresh_result.ok and _token_key_matches_platform(
                    refresh_result.token_key, cfg.weread_platform
                ):
                    cfg = _replace_token(cfg, refresh_result.token)
                    token_refreshed_at = time.time()
                    logger.info(
                        "兑换 Token 已在阅读前刷新: %s...", refresh_result.token[:8]
                    )
                elif refresh_result.ok:
                    refresh_diagnosis = (
                        f"刷新得到的凭证类型 ({refresh_result.token_key}) 与兑换平台不匹配，"
                        "WEREAD_LOGIN_CURL 与兑换 Token 似乎抓自不同平台的设备，"
                        "请统一为同一台设备的抓包"
                    )
                    logger.warning(refresh_diagnosis)
                else:
                    refresh_diagnosis = refresh_result.diagnosis
                    logger.warning("阅读前刷新 Token 失败: %s", refresh_diagnosis)

        # 阅读循环
        from wereadit.core.reader import read_books

        result = read_books(client, cfg, refresh_print=refresh_print)
```

3. 兑换调用改为传新参数：

```python
                exchange_summary = exchange_awards(
                    client,
                    cfg,
                    refresher=refresher,
                    token_refreshed_at=token_refreshed_at,
                )
```

4. `-2012` 分支的 `exchange_summary` 文案替换为：

```python
                    if refresh_diagnosis:
                        guidance = "根因见下方 Token 自动续期诊断。"
                    elif cfg.weread_login_curl:
                        guidance = "请重新抓包更新 Secret 中的 Token。"
                    else:
                        guidance = (
                            "未配置自动续期，请重新抓包更新 Secret 中的 Token，"
                            "或按 README 配置 WEREAD_LOGIN_CURL 实现自动续期。"
                        )
                    exchange_summary = (
                        f"兑换奖励失败: {platform_label} 已过期。{guidance}\n"
                        f"过期 Token 前 8 位: {token_preview}..."
                    )
```

（删除原 `exchange_summary = (...)` 赋值块，保留 `exit_code = 1` 与 `has_failure = True`。）

5. 推送内容拼接处（`if exchange_summary:` 之后）追加诊断：

```python
        if exchange_summary:
            push_content += f"\n\n{exchange_summary}"
        if refresh_diagnosis:
            push_content += f"\n\nToken 自动续期诊断：{refresh_diagnosis}"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/ -q && python -m ruff check src/ tests/
```

预期：全部测试 PASS（含 test_keepalive.py 35 个回归），ruff 无告警

- [ ] **Step 5: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/app.py tests/test_app.py && git commit -m "[exchange] 阅读前刷新 token + 平台错位校验 + 续期诊断直推推送"
```

---

### Task 6: 文档同步（README / AGENTS.md / changelog.md）

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: Task 1-5 的最终行为
- Produces: 无代码接口

- [ ] **Step 1: README 更新**

1. 「Token 自动续期」抓包步骤（约第 84-89 行）替换为：

```markdown
### Token 自动续期（推荐）

App Token 有效期仅约 2 小时，手动抓包无法覆盖每日定时运行。配置 `/login` 重放后，脚本每次运行会在**阅读开始前**自动刷新 Token（兑换时 token 年龄保持在有效期窗口内；若阅读耗时过长，兑换前还会补刷一次）。

抓包要点（决定成败）：

1. **杀掉微信读书 App 重新打开**（冷启动会触发 /login 刷新请求）。
2. 用抓包工具捕获 `i.weread.qq.com/login` 请求，**确认请求 body 中含 `deviceId`**（长效设备凭证，是重放换新 token 的依据；缺了它重放必然失败）。
3. 将该请求复制为 cURL (Bash) 格式。
4. 配置到 Secret `WEREAD_LOGIN_CURL`。

注意：`WEREAD_LOGIN_CURL` 与兑换 Token（`WEREAD_ANDROID_TOKEN` / `WEREAD_IOS_TOKEN`）必须抓自**同一平台**的设备（iOS 下发 skey，Android 下发 accessToken，交叉配置会被平台校验拦截）。

配置后无需再手动更新 Token。脚本启动时会对 curl 做静态体检（是否 /login、是否含 deviceId），刷新失败时会把诊断与下一步指引**直接写进推送消息**，无需翻 Actions 日志。
```

2. 配置表第 38 行 `WEREAD_LOGIN_CURL` 的说明列改为：

```markdown
| `WEREAD_LOGIN_CURL` | Secret | 选填 | - | App 端 `/login` 请求 cURL（body 须含 `deviceId`），用于 Token 自动续期（推荐配置） |
```

3. 在「Token 自动续期」节末尾追加兜底方案说明：

```markdown
> 兜底思路（本项目未实现）：若 `/login` 重放被服务端彻底关闭，社区方案是手机端用 Quantumult X / 快捷指令定时拦截 App 的 skey 并调用 GitHub API 更新 Secrets。依赖手机常开与抓包 App，仅作为最后手段记录在案。
```

- [ ] **Step 2: AGENTS.md 更新**

1. Architecture 一节中 `token_refresher.py` 行替换为：

```
│   └── token_refresher.py # App 端 Token 续期（/login 重放 + 配置体检 + 四分类诊断）
```

2. Key Design Decisions 中「兑换 Token 自动续期」条替换为：

```markdown
- **兑换 Token 自动续期**：App 端 skey/accessToken 有效期仅约 2 小时。`token_refresher.py` 重放 `i.weread.qq.com/login`（body 中 deviceId 为长效凭证，抓包一次可长期重放）换新 token；`app.py` 在**阅读开始前**刷新并用 `dataclasses.replace` 注入新 cfg（兑换时 token 年龄远离 2 小时边缘）；`exchanger.py` 删除内联刷新，改收 `refresher` 回调，token 年龄 > `TOKEN_MAX_AGE_SECONDS`（90 分钟）时补刷。刷新结果 `RefreshResult` 带四分类人话诊断（配置/网络/服务端拒绝/结构未知），随推送直发。响应 token 位置未公开，递归提取 JSON（深度限 5 层）+ header + Set-Cookie 三路兜底。web wr_skey 复用路径 2026-07-21 已实测证伪（与 App skey 不同体系），勿恢复。
```

3. Changelog 节「添加」列表最上方插入：

```markdown
- **兑换 Token 续期自诊断**: `token_refresher.py` 重写为 `RefreshResult` 结构化返回（token + 命中字段名 + 人话诊断）；新增 `diagnose_login_curl` 静态体检（非 /login、缺 deviceId 提前给修正指引）；响应 token 递归提取（任意嵌套，深度限 5 层）替代顶层猜测；网络异常/HTTP 5xx 指数退避重试 3 次；刷新诊断随推送直发，无需翻 Actions 日志。
```

「修复」列表最上方插入：

```markdown
- **兑换 Token 刷新时机**: 刷新从"阅读 60+ 分钟后、兑换前"挪到阅读开始前，兑换时 token 年龄远离 2 小时有效期边缘；`exchanger.py` 新增补刷保险（token 年龄 > 90 分钟时兑换前再刷一次）；iOS/Android 平台错位配置（iOS curl 配 Android token）会被校验拦截并提示。
```

「优化」列表最上方插入：

```markdown
- **删除已证伪的 web wr_skey 复用路径**: 移除 `refresh_app_token_via_web` 及其 4 个测试（07-21 实测 wr_skey 与 App skey 不同体系，保留会误导）。
```

- [ ] **Step 3: changelog.md 更新**

按现有三分类格式（新增/修复/优化）在文件顶部日期段追加 2026-07-22 条目，内容同 Step 2 的三条（合并为面向用户的表述）：

```markdown
## 2026-07-22

### 新增

- 兑换 Token 续期自诊断：login curl 配置体检、响应递归提取、网络退避重试、诊断随推送直发

### 修复

- 兑换 Token 刷新时机前移至阅读前，新增 90 分钟补刷保险与 iOS/Android 平台错位校验

### 优化

- 删除已证伪的 web wr_skey 复用死代码
```

（若 changelog.md 顶部格式不同，遵循其现有格式，保持三分类与条目内容不变。）

- [ ] **Step 4: 全量验证 + Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/ -q && python -m ruff check src/ tests/ && git add README.md AGENTS.md changelog.md && git commit -m "[docs] 兑换 Token 续期自诊断：抓包要点、推送文案与设计决策同步"
```

预期：测试与 ruff 全过后完成提交

---

## Self-Review 记录

- **Spec 覆盖**：自适应提取(Task 1)、配置体检(Task 2)、RefreshResult+重试+四分类(Task 3)、exchanger 适配+补刷(Task 4)、app.py 编排+诊断直推+文案(Task 5)、死代码清理(Task 3)、文档(Task 6) —— spec 各节均有对应 Task。
- **类型一致性**：`RefreshResult(token, token_key, diagnosis)` / `.ok`、`diagnose_login_curl -> str`、`refresh_app_token -> RefreshResult`、`exchange_awards(client, cfg, *, refresher, token_refreshed_at) -> str`、`_extract_token_from_response -> tuple[str | None, str]` 在定义与使用处一致；`LOGIN_MAX_ATTEMPTS`/`LOGIN_RETRY_INTERVAL`(Task 1 加常量) 在 Task 3 使用；`TOKEN_MAX_AGE_SECONDS`(Task 4) 在 Task 4 使用。
- **占位符**：无 TBD/TODO，所有代码步骤含完整代码与精确命令。
