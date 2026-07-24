"""reader 日志回显：进度走 logger + 预热阶段隔离 + synckey 常态日志简化。"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from wereadit.config import Config
from wereadit.core.reader import read_books
from wereadit.infra.http import HttpClient


def _make_cfg(**overrides) -> Config:
    defaults = dict(
        read_num=2,
        books=["b1"],
        chapters=["c1"],
        headers={},
        cookies={"wr_skey": "old"},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _mock_response(json_data, set_cookies=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.cookies = set_cookies or {}
    return resp


class TestProgressViaLogger:
    """进度回显走 logger（统一格式），且 index 不变时不重复打印。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_progress_not_repeated_on_retry(self, mock_post, caplog) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,  # 预热：修复后重试成功
            ok_resp,  # 主循环 read#1
            ok_resp,  # 主循环 read#2
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        progress = [r.message for r in caplog.records if r.message.startswith("阅读进度:")]
        assert progress == [
            "阅读进度: 第 1/2 次，当前阅读 0.5 分钟",
            "阅读进度: 第 2/2 次，当前阅读 1.0 分钟",
        ]


class TestSynckeyLogPresentation:
    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_log_merged_info_once(self, mock_post, caplog) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,
            ok_resp,
            ok_resp,
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        # 预热成功时只打印"正在阅读预热"，不打印"预热成功"，也不打印修复细节
        assert any(m == "正在阅读预热" for m in caplog.messages)
        assert not any("预热成功" in m for m in caplog.messages)
        assert not any("已自动修复并重试" in m for m in caplog.messages)
        assert not any("尝试修复" in m for m in caplog.messages)
        assert not any("fix_no_synckey 已调用" in m for m in caplog.messages)
        assert not any(
            r.levelno == logging.WARNING and "修复未生效" in r.message
            for r in caplog.records
        )

    @patch("wereadit.infra.http.requests.Session.post")
    def test_backoff_info_then_warning_on_second_streak(
        self, mock_post, caplog
    ) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, no_synckey,  # 预热轮1：streak=1，退避
            no_synckey, fix_resp, no_synckey,  # 预热轮2：streak=2，退避（WARNING）
            no_synckey, fix_resp, ok_resp,    # 预热轮3：修复后重试成功
            ok_resp,                           # 主循环 read#1
        ]

        cfg = _make_cfg(read_num=1)
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        backoff = [r for r in caplog.records if "修复未生效" in r.message]
        assert len(backoff) == 2
        assert backoff[0].levelno == logging.INFO
        assert "连续 1/3 次" in backoff[0].message
        assert backoff[1].levelno == logging.WARNING
        assert "连续 2/3 次" in backoff[1].message

    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_retry_success_log(self, mock_post, caplog) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,
            ok_resp,
            ok_resp,
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        assert not any("预热成功" in r.message for r in caplog.records)
        assert not any("synckey 修复成功" in r.message for r in caplog.records)
