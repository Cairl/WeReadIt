"""reader 日志回显：进度去重 + synckey 常态日志简化（行为逻辑不变的回归保障）。"""

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


def _mock_response(json_data: dict, set_cookies: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.cookies = set_cookies or {}
    return resp


class TestProgressDeduplication:
    """进度打印：index 未变化时不重复打印。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_progress_not_repeated_on_retry(self, mock_post: MagicMock) -> None:
        """轮1修复未生效退避后，index 仍为 1，进度行不得二次打印。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, no_synckey,  # 轮1：修复未生效，退避
            ok_resp,  # 轮2（index 仍 1）：成功，index → 2
            ok_resp,  # 轮3（index 2）：成功，index → 3 退出
        ]

        prints: list[str] = []
        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with patch("wereadit.core.reader.time.sleep"):
            read_books(client, cfg, refresh_print=prints.append)

        assert prints == [
            "阅读进度: 第 1/2 次，已阅读 0.0 分钟",
            "阅读进度: 第 2/2 次，已阅读 0.5 分钟",
        ]


class TestSynckeyLogPresentation:
    """synckey 常态日志：合并为人话一行，级别随连续失败升级。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_log_merged_info_once(self, mock_post: MagicMock, caplog) -> None:
        """首次修复：一行 INFO，不再有 WARNING 旧文案与第二行 INFO。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,  # 轮1：修复后重试成功
            ok_resp,  # 轮2：成功退出
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        messages = [r.message for r in caplog.records]
        fix_logs = [m for m in messages if "已自动修复并重试" in m]
        assert fix_logs == ["第 1/2 次：阅读上下文未同步，已自动修复并重试"]
        assert not any("尝试修复" in m for m in messages)
        assert not any("fix_no_synckey 已调用" in m for m in messages)
        # 首次修复场景无 WARNING
        assert not any(
            r.levelno == logging.WARNING and "修复未生效" in r.message
            for r in caplog.records
        )

    @patch("wereadit.infra.http.requests.Session.post")
    def test_backoff_info_then_warning_on_second_streak(
        self, mock_post: MagicMock, caplog
    ) -> None:
        """修复未生效：streak=1 为 INFO，streak=2（逼近熔断）升 WARNING。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, no_synckey,  # 轮1：streak=1，退避
            no_synckey, fix_resp, no_synckey,  # 轮2：streak=2，退避（WARNING）
            ok_resp,  # 轮3：成功退出（streak 清零）
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
    def test_fix_retry_success_log(self, mock_post: MagicMock, caplog) -> None:
        """修复后重试成功：人话 INFO 行。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,
            ok_resp,
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        assert any("修复成功" in r.message for r in caplog.records)
        assert not any("synckey 修复成功" in r.message for r in caplog.records)
