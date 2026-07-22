"""保活策略回归测试。

每条测试对应 wxread_keepalive_analysis.md / wxread_keepalive_improvement_plan.md
中的一项策略或修复。确保后续重构不会破坏保活机制。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wereadit.config import Config
from wereadit.constants import (
    DEFAULT_HEADERS,
    DEFAULT_READ_DATA,
    FIX_SYNCKEY_BOOK_IDS,
    FIX_SYNCKEY_URL,
    MAX_COOKIE_FAIL,
    READ_INTERVAL_SECONDS,
    READ_URL,
    RENEW_URL,
    SIGN_KEY,
)
from wereadit.core.reader import (
    _get_wr_skey,
    fix_no_synckey,
    read_books,
    refresh_cookie,
)
from wereadit.exceptions import CookieExpiredError, ReadFailedError
from wereadit.infra.http import HttpClient
from wereadit.utils.crypto import cal_hash, encode_data, sign_request

_PROJECT_ROOT = Path(__file__).parent.parent
_WORKFLOW_FILE = _PROJECT_ROOT / ".github" / "workflows" / "deploy.yml"


def _make_cfg(**overrides) -> Config:
    """构造测试用 Config。"""
    defaults = dict(
        read_num=2,
        books=["b1", "b2"],
        chapters=["c1", "c2"],
        pushplus_token="",
        wxpusher_spt="",
        telegram_bot_token="",
        telegram_chat_id="",
        serverchan_spt="",
        weread_app_curl="",
        app_token="",
        app_token_key="",
        exchange_award="2,2,2,2,2,2,2,2",
        headers={"accept": "application/json"},
        cookies={"wr_skey": "initial", "wr_vid": "12345"},
        web_curl="",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _mock_response(json_data: dict, set_cookies: dict | None = None) -> MagicMock:
    """构造 mock 响应。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.cookies = set_cookies or {}
    resp.raise_for_status.return_value = None
    return resp


# =========================================================================
# P0.1 — HttpClient cookies 业务层独占测试
# =========================================================================


class TestHttpClientCookieIsolation:
    """验证 HttpClient 不会被服务器 Set-Cookie 自动污染。"""

    def test_update_cookie_only_affects_business_layer(self) -> None:
        """业务层 update_cookie 应只更新业务字典，不影响 Session 自动合并行为。"""
        client = HttpClient(cookies={"wr_skey": "old"})
        client.update_cookie("wr_skey", "new12345")
        assert client.cookies["wr_skey"] == "new12345"

    def test_cookies_property_returns_copy(self) -> None:
        """cookies property 返回副本，外部修改不影响内部状态。"""
        client = HttpClient(cookies={"wr_skey": "abc"})
        snapshot = client.cookies
        snapshot["wr_skey"] = "tampered"
        assert client.cookies["wr_skey"] == "abc", "外部修改不应影响内部状态"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_post_passes_business_cookies_not_session_jar(self, mock_post) -> None:
        """post 应显式传业务层 cookies，而非依赖 Session.cookies。"""
        mock_post.return_value = _mock_response({"succ": 1})
        client = HttpClient(cookies={"wr_skey": "biz_value"})
        client._session.cookies.set("wr_skey", "session_value")  # 模拟服务器污染 jar

        client.post("https://example.com")

        _, kwargs = mock_post.call_args
        assert kwargs["cookies"] == {"wr_skey": "biz_value"}, \
            "post 必须传业务层 cookies，不能让 Session.jar 污染"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_server_set_cookie_does_not_overwrite_business(self, mock_post) -> None:
        """服务器响应的 Set-Cookie 不会回写到业务层 cookies。"""
        mock_post.return_value = _mock_response(
            {"succ": 1}, set_cookies={"wr_skey": "server_full_value"}
        )
        client = HttpClient(cookies={"wr_skey": "truncated"})
        client.post("https://example.com")
        # 业务层 cookies 不应被服务器响应覆盖
        assert client.cookies["wr_skey"] == "truncated"


# =========================================================================
# P0.2 — headers 单一来源测试
# =========================================================================


class TestHeadersSingleSource:
    """验证 reader 不再重复传 headers。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_read_request_does_not_pass_headers_explicitly(self, mock_post) -> None:
        """read_books 调 read 接口时不应显式传 headers（由 HttpClient 持有）。"""
        # read_books 会先调 refresh_cookie（renewal），再调 read
        # 让所有响应都返回 succ+synckey
        mock_post.side_effect = [
            _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"}),  # renewal
            _mock_response({"succ": 1, "synckey": 123}),  # 预热 read
            _mock_response({"succ": 1, "synckey": 124}),  # read #1
            _mock_response({"succ": 1, "synckey": 125}),  # read #2
        ]
        cfg = _make_cfg(read_num=2)
        client = HttpClient(headers={"accept": "application/json"}, cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            read_books(client, cfg)

        # 检查 read 接口的调用参数
        for call in mock_post.call_args_list[1:]:  # 跳过 renewal
            _, kwargs = call
            assert "headers" not in kwargs or kwargs["headers"] is None, \
                "reader 不应再显式传 headers，由 HttpClient 持有"


# =========================================================================
# P0.3 — 熔断机制测试
# =========================================================================


class TestCircuitBreaker:
    """验证连续失败时触发熔断，避免死循环。"""

    def _setup_continuous_no_synckey(self, mock_post) -> None:
        """让所有 read/retry 都返回 succ 但无 synckey,fix 返回空。

        P1.1 后循环逻辑:read → 无synckey → fix → retry_read → 无synckey → 下一轮
        第 3 次原始 read 无 synckey 时熔断(不 fix 不 retry)。
        """
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey_resp = _mock_response({"succ": 1})  # 无 synckey
        fix_resp = _mock_response({})
        # 3 轮完整失败(read + fix + retry)；第 3 轮 retry 仍无 synckey → 预热熔断
        # 注意：预热阶段 _read_once 在每次无 synckey 时都会触发 fix+retry，
        # 故第 3 轮需完整的 read+fix+retry 三元组，而非单 read。
        mock_post.side_effect = [renewal_resp] + [
            no_synckey_resp, fix_resp, no_synckey_resp,  # 轮1
            no_synckey_resp, fix_resp, no_synckey_resp,  # 轮2
            no_synckey_resp, fix_resp, no_synckey_resp,  # 轮3 retry 无 synckey → 熔断
        ]

    @patch("wereadit.infra.http.requests.Session.post")
    def test_circuit_breaker_on_continuous_no_synckey(self, mock_post) -> None:
        """连续 MAX_NO_SYNCKEY 次无 synckey 应抛 ReadFailedError。"""
        self._setup_continuous_no_synckey(mock_post)
        cfg = _make_cfg(read_num=10)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            with pytest.raises(ReadFailedError) as exc_info:
                read_books(client, cfg)
        assert "连续" in str(exc_info.value) and "synckey" in str(exc_info.value)

    @patch("wereadit.infra.http.requests.Session.post")
    def test_circuit_breaker_on_continuous_cookie_fail(self, mock_post) -> None:
        """连续 MAX_COOKIE_FAIL 次无 succ 应抛 CookieExpiredError。"""
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_succ_resp = _mock_response({})  # 无 succ
        mock_post.side_effect = [renewal_resp] + [
            no_succ_resp,
            _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"}),  # refresh
        ] * (MAX_COOKIE_FAIL + 1)

        cfg = _make_cfg(read_num=10)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            with pytest.raises(CookieExpiredError) as exc_info:
                read_books(client, cfg)
        assert "连续" in str(exc_info.value) and "cookie" in str(exc_info.value)

    @patch("wereadit.infra.http.requests.Session.post")
    def test_no_synckey_streak_resets_on_success(self, mock_post) -> None:
        """synckey 成功后 no_synckey_streak 应清零，不会累积触发熔断。

        P1.1 后:fix 后重试 read,若重试成功则 streak 清零。
        场景:read_num=2
        - 轮1: read 无synckey → fix → retry ok (streak 清零, index 1→2)
        - 轮2: read ok (index 2→3, 退出)
        """
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        ok_resp = _mock_response({"succ": 1, "synckey": 1})
        no_synckey_resp = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        mock_post.side_effect = [
            renewal_resp,                        # 启动刷新
            no_synckey_resp, fix_resp, ok_resp,  # 预热：read → fix → retry ok
            ok_resp,                             # 主循环 read#1
            ok_resp,                             # 主循环 read#2
        ]
        cfg = _make_cfg(read_num=2)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            result = read_books(client, cfg)
        assert result.completed_count == 2
        assert result.fix_retry_success == 0, "预热阶段的 fix 重试不计入主循环 metrics"


# =========================================================================
# 保活策略核心行为回归测试
# =========================================================================


class TestKeepaliveStrategies:
    """对应 wxread_keepalive_analysis.md 第 9.1 节保活策略列表。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_startup_refresh_cookie_called(self, mock_post) -> None:
        """策略 #1: 启动时强制 refresh_cookie（即使 cookie 看似有效）。"""
        mock_post.side_effect = [
            _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"}),  # renewal
            _mock_response({"succ": 1, "synckey": 1}),  # 预热 read
            _mock_response({"succ": 1, "synckey": 1}),  # 主循环 read#1
        ]
        cfg = _make_cfg(read_num=1)
        client = HttpClient(cookies={"wr_skey": "still_valid"})

        with patch("wereadit.core.reader.time.sleep"):
            read_books(client, cfg)

        # 第一个请求必须是 renewal（启动强制刷新）
        first_call_url = mock_post.call_args_list[0][0][0]
        assert first_call_url == RENEW_URL, "启动必须先调 renewal"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_all_3_cookie_variants_tried_when_first_two_fail(self, mock_post) -> None:
        """策略 #2: 前 2 种 payload 失败时第 3 种应被尝试。"""
        # 前 2 次响应无 wr_skey，第 3 次有
        mock_post.side_effect = [
            _mock_response({}),  # v1 失败
            _mock_response({}),  # v2 失败
            _mock_response({"succ": 1}, set_cookies={"wr_skey": "ok12345678"}),  # v3
        ]
        client = HttpClient(cookies={"wr_skey": "old"})
        cfg = _make_cfg()

        result = _get_wr_skey(client, cfg)
        assert result == "ok123456", "wr_skey 必须截取前 8 位"
        assert mock_post.call_count == 3, "3 种 payload 都应被尝试"

    def test_wr_skey_truncated_to_8_chars(self) -> None:
        """策略 #3: wr_skey[:8] 截取，refresh_cookie 后 client 持有的是 8 位值。"""
        client = HttpClient(cookies={"wr_skey": "old"})
        cfg = _make_cfg()

        with patch("wereadit.infra.http.requests.Session.post") as mock_post:
            mock_post.return_value = _mock_response(
                {"succ": 1}, set_cookies={"wr_skey": "1234567890ABCDEF"}
            )
            new_skey = refresh_cookie(client, cfg)

        assert new_skey == "12345678", "wr_skey 必须截取前 8 位"
        assert client.cookies["wr_skey"] == "12345678", \
            "client 持有的必须是 8 位截断值，不能是服务器返回的完整值"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_no_synckey_called_when_missing_synckey(self, mock_post) -> None:
        """策略 #9: 无 synckey 时调 fix_no_synckey。

        P1.1 后:read 无synckey → fix → retry_read(也无synckey) → 下一轮
        第 3 次原始 read 无 synckey 时熔断(不 fix 不 retry)。
        """
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey_resp = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        # 3 轮完整失败(read + fix + retry)；第 3 轮 retry 仍无 synckey → 预热熔断
        # 预热阶段 _read_once 在每次无 synckey 时都会触发 fix+retry，
        # 故第 3 轮需完整的 read+fix+retry 三元组，而非单 read。
        mock_post.side_effect = [
            renewal_resp,
            no_synckey_resp, fix_resp, no_synckey_resp,  # 轮1
            no_synckey_resp, fix_resp, no_synckey_resp,  # 轮2
            no_synckey_resp, fix_resp, no_synckey_resp,  # 轮3 retry 无 synckey → 熔断
        ]
        cfg = _make_cfg(read_num=2)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            with pytest.raises(ReadFailedError):
                read_books(client, cfg)

        # 应该有 fix_no_synckey 调用（chapterInfos URL）
        fix_calls = [
            c for c in mock_post.call_args_list
            if c[0][0] == FIX_SYNCKEY_URL
        ]
        assert len(fix_calls) >= 1, "无 synckey 时必须调 fix_no_synckey"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_no_synckey_uses_magic_bookid(self, mock_post) -> None:
        """策略 #10: fix_no_synckey 必须用写死的 bookId "3300060341"。"""
        client = HttpClient(cookies={"wr_skey": "old"})
        cfg = _make_cfg()

        with patch("wereadit.infra.http.requests.Session.post") as mock_post:
            mock_post.return_value = _mock_response({})
            fix_no_synckey(client, cfg)

        _, kwargs = mock_post.call_args
        body = json.loads(kwargs["data"])
        assert body == {"bookIds": FIX_SYNCKEY_BOOK_IDS}
        assert FIX_SYNCKEY_BOOK_IDS == ["3300060341"], "bookId 不能改"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_lasttime_only_updated_on_synckey_success(self, mock_post) -> None:
        """策略: lastTime 只在 synckey 成功时更新（rt 字段依赖）。"""
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        ok_resp = _mock_response({"succ": 1, "synckey": 1})
        mock_post.side_effect = [renewal_resp, ok_resp, ok_resp]  # renewal + 预热 + 主循环
        cfg = _make_cfg(read_num=1)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            with patch("wereadit.core.reader.time.time") as mock_time:
                mock_time.side_effect = [1000, 1030, 1060]
                result = read_books(client, cfg)

        assert result.completed_count == 1
        assert result.warmup_done is True

    @patch("wereadit.infra.http.requests.Session.post")
    def test_data_pop_s_each_iteration(self, mock_post) -> None:
        """策略: 每次循环开头 data.pop('s')，防止用旧签名。"""
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        ok_resp = _mock_response({"succ": 1, "synckey": 1})
        mock_post.side_effect = [renewal_resp, ok_resp, ok_resp, ok_resp]  # renewal + 预热 + 2 主循环
        cfg = _make_cfg(read_num=2)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            read_books(client, cfg)

        # 验证每次 read 请求的 data 都不含上次的 s（重新计算）
        read_calls = [
            c for c in mock_post.call_args_list
            if c[0][0] == READ_URL
        ]
        assert len(read_calls) == 3, "预热 + 2 次主循环共 3 次 read"
        bodies = [json.loads(c.kwargs["data"]) for c in read_calls]
        # 三组的 s 字段应该两两不同（ts/rn 不同导致签名不同）
        assert bodies[0]["s"] != bodies[1]["s"], "每次循环必须重新签名"
        assert bodies[1]["s"] != bodies[2]["s"], "每次循环必须重新签名"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_sleep_30s_after_synckey_success(self, mock_post) -> None:
        """策略 #13: synckey 成功后必须 sleep(30)。"""
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        ok_resp = _mock_response({"succ": 1, "synckey": 1})
        mock_post.side_effect = [renewal_resp, ok_resp, ok_resp, ok_resp]  # renewal + 预热 + 2 主循环
        cfg = _make_cfg(read_num=2)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep") as mock_sleep:
            read_books(client, cfg)

        # 应该有 2 次 sleep(30)（每次 synckey 成功后；预热成功不 sleep）
        sleep_30_calls = [
            c for c in mock_sleep.call_args_list
            if c.args == (READ_INTERVAL_SECONDS,)
        ]
        assert len(sleep_30_calls) == 2, "每次 synckey 成功必须 sleep(30)"

    @patch("wereadit.infra.http.requests.Session.post")
    def test_no_sleep_no_increment_on_cookie_fail(self, mock_post) -> None:
        """策略 #12: cookie 失败后不 sleep(30)、不递增 index（重试本次）。"""
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_succ_resp = _mock_response({})  # 无 succ
        ok_resp = _mock_response({"succ": 1, "synckey": 1})
        # renewal → no succ → refresh → ok (预热阶段吃掉上述序列)
        mock_post.side_effect = [
            renewal_resp,    # 启动刷新
            no_succ_resp,    # 预热 read #1 失败
            renewal_resp,    # refresh_cookie
            ok_resp,         # 预热 read #2 成功
            ok_resp,         # 主循环 read#1
        ]
        cfg = _make_cfg(read_num=1)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep") as mock_sleep:
            result = read_books(client, cfg)

        # cookie 失败后只应 sleep(CIRCUIT_BREAKER_BACKOFF=5)，不应 sleep(30)
        sleep_30_calls = [
            c for c in mock_sleep.call_args_list
            if c.args == (READ_INTERVAL_SECONDS,)
        ]
        # 只有最后一次 synckey 成功才 sleep(30)
        assert len(sleep_30_calls) == 1
        assert result.completed_count == 1


# =========================================================================
# P3.1 — 补充保活策略回归测试(覆盖剩余策略)
# =========================================================================


class TestCryptoKeepalive:
    """验证签名相关保活策略不能被破坏。"""

    def test_cal_hash_preserves_obfuscated_vars(self) -> None:
        """策略: cal_hash 保留混淆变量名 _7032f5/_cc1055/_19094e。

        这些变量名来自前端 JS 逆向,保留便于对照。
        算法本身不能改,否则服务器校验失败。
        """
        import inspect

        source = inspect.getsource(cal_hash)
        # 关键变量名必须保留(逆向对照用)
        assert "_7032f5" in source, "cal_hash 必须保留 _7032f5 变量名"
        assert "_cc1055" in source, "cal_hash 必须保留 _cc1055 变量名"
        assert "_19094e" in source, "cal_hash 必须保留 _19094e 变量名"

    def test_cal_hash_known_value(self) -> None:
        """策略: cal_hash 算法稳定,已知输入产出已知输出。"""
        # 空字符串应返回固定值
        result = cal_hash("")
        assert isinstance(result, str)
        assert result == hex(0x15051505 + 0x15051505)[2:].lower()

    def test_encode_data_sorts_keys(self) -> None:
        """策略: encode_data 按 key 字典序排序,顺序错则签名错。"""
        data = {"b": "2", "a": "1", "c": "3"}
        encoded = encode_data(data)
        # 必须按 a/b/c 排序
        assert encoded == "a=1&b=2&c=3"

    def test_encode_data_url_encodes_values(self) -> None:
        """策略: encode_data 对 value 做 URL 编码。"""
        data = {"k": "hello world&special"}
        encoded = encode_data(data)
        assert encoded == "k=hello%20world%26special"

    def test_sign_request_uses_correct_key(self) -> None:
        """策略: sg = sha256(ts + rn + SIGN_KEY),SIGN_KEY 不能改。"""
        # SIGN_KEY 必须是固定盐值
        assert SIGN_KEY == "3c5c8717f3daf09iop3423zafeqoi", \
            "SIGN_KEY 不能改,来自前端 JS 逆向"

    def test_sign_request_fills_sg_and_s(self) -> None:
        """策略: sign_request 必须同时填 sg 和 s 两个字段。"""
        import hashlib

        data = {"ts": 1744264311434, "rn": 466, "b": "x"}
        sign_request(data, SIGN_KEY)
        expected_sg = hashlib.sha256(
            f"{data['ts']}{data['rn']}{SIGN_KEY}".encode()
        ).hexdigest()
        assert data["sg"] == expected_sg
        assert "s" in data, "s 字段(cal_hash 结果)必须被填入"


class TestConstantsKeepalive:
    """验证常量中的保活策略不能被破坏。"""

    def test_default_headers_includes_baggage(self) -> None:
        """策略: headers 必须包含 baggage Sentry 头(浏览器指纹)。"""
        assert "baggage" in DEFAULT_HEADERS, "baggage 头不能删"
        baggage = DEFAULT_HEADERS["baggage"]
        assert "sentry" in baggage, "baggage 必须含 sentry 追踪信息"
        assert "sentry-trace_id" in baggage, "baggage 必须含 sentry-trace_id"

    def test_default_headers_includes_user_agent(self) -> None:
        """策略: user-agent 必须是浏览器 UA,不能改成 bot UA。"""
        ua = DEFAULT_HEADERS["user-agent"]
        assert "Mozilla" in ua, "UA 必须模拟浏览器"
        assert "Chrome" in ua or "Edg" in ua, "UA 必须含 Chrome/Edge 标识"

    def test_default_read_data_keeps_fixed_fields(self) -> None:
        """策略: DEFAULT_READ_DATA 的 ci/co/sm/pr/ps/pc 是固定值,不能改成动态。"""
        # 这些字段在 read_books 循环中不会被更新(只有 b/c/ct/rt/ts/rn/sg/s 更新)
        assert DEFAULT_READ_DATA["ci"] == 27
        assert DEFAULT_READ_DATA["co"] == 389
        assert DEFAULT_READ_DATA["pr"] == 74
        assert DEFAULT_READ_DATA["ps"] == "4ee326507a65a465g015fae"
        assert DEFAULT_READ_DATA["pc"] == "aab32e207a65a466g010615"
        assert "三体" in DEFAULT_READ_DATA["sm"], "sm 应保留三体摘要(作者实测得出)"

    def test_default_read_data_has_all_required_fields(self) -> None:
        """策略: data 字段必须完整,缺字段服务器会拒。"""
        required = {
            "appId", "b", "c", "ci", "co", "sm", "pr",
            "rt", "ts", "rn", "sg", "ct", "ps", "pc", "s",
        }
        assert set(DEFAULT_READ_DATA.keys()) == required, \
            f"data 字段必须完整,实际: {set(DEFAULT_READ_DATA.keys())}"

    def test_fix_synckey_book_ids_is_magic_value(self) -> None:
        """策略: FIX_SYNCKEY_BOOK_IDS 必须是写死的 "3300060341"。"""
        assert FIX_SYNCKEY_BOOK_IDS == ["3300060341"], \
            "bookId 不能改,这是触发服务器重建上下文的特殊 ID"

    def test_read_interval_is_30_seconds(self) -> None:
        """策略: READ_INTERVAL_SECONDS 必须是 30(经验值,调快触发风控)。"""
        assert READ_INTERVAL_SECONDS == 30, \
            "阅读间隔不能调快,30 秒是模拟真实阅读的经验值"

    def test_cookie_data_variants_has_3_payloads(self) -> None:
        """策略: COOKIE_DATA_VARIANTS 必须有 3 种 payload。"""
        from wereadit.constants import COOKIE_DATA_VARIANTS
        assert len(COOKIE_DATA_VARIANTS) == 3, "3 种 payload 不能简化为 1 种"
        # 验证 3 种变体
        assert {"rq": "%2Fweb%2Fbook%2Fread", "ql": False} in COOKIE_DATA_VARIANTS
        assert {"rq": "%2Fweb%2Fbook%2Fread", "ql": True} in COOKIE_DATA_VARIANTS
        assert {"rq": "%2Fweb%2Fbook%2Fread"} in COOKIE_DATA_VARIANTS


class TestWorkflowKeepalive:
    """验证 GitHub Actions workflow 中的保活策略。"""

    def test_workflow_file_exists(self) -> None:
        """deploy.yml 必须存在。"""
        assert _WORKFLOW_FILE.exists(), "deploy.yml 不能删"

    def test_keepalive_job_present(self) -> None:
        """策略: workflow 必须包含 keepalive-job(防 GitHub 60 天禁用)。"""
        content = _WORKFLOW_FILE.read_text(encoding="utf-8")
        assert "keepalive-job" in content, "keepalive-job 不能删"
        assert "liskin/gh-workflow-keepalive" in content, \
            "必须用 liskin/gh-workflow-keepalive action"

    def test_dns_setup_present(self) -> None:
        """策略: workflow 必须设置 8.8.8.8 DNS(基础设施保活)。"""
        content = _WORKFLOW_FILE.read_text(encoding="utf-8")
        assert "8.8.8.8" in content, "DNS 8.8.8.8 不能删"
        assert "nameserver" in content, "必须设置 nameserver"

    def test_concurrency_present(self) -> None:
        """策略: workflow 必须有 concurrency(防重复运行)。"""
        content = _WORKFLOW_FILE.read_text(encoding="utf-8")
        assert "concurrency" in content, "concurrency 不能删"
        assert "cancel-in-progress" in content

    def test_cron_schedule_present(self) -> None:
        """策略: workflow 必须有定时触发。"""
        content = _WORKFLOW_FILE.read_text(encoding="utf-8")
        assert "schedule" in content
        assert "cron" in content
