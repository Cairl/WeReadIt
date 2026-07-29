"""formatter 测试：验证推送消息格式化逻辑。

覆盖：
- 成功路径（含/不含可选字段）
- 部分成功（兑换失败/跳过）
- 阅读失败（含错误信息）
- 平台识别信息位置
"""

from __future__ import annotations

from wereadit.push.formatter import PushMessage, format_push_message


class TestFormatSuccess:
    """成功路径渲染：阅读与兑换均成功。"""

    def test_full_success_with_all_fields(self) -> None:
        """阅读+兑换均成功，所有字段渲染完整。平台信息在账号之后。"""
        msg = PushMessage(
            timestamp="2026-07-26 18:00:00",
            account="12345",
            reading_success=True,
            read_minutes=60,
            weekly_read_seconds=27060,  # 7 小时 31 分钟
            keep_reading_days=128,
            exchange_success=True,
            exchanged_coin=2,
            exchanged_card=1,
            coin_balance=9.92,
            card_remain_seconds=153600,  # 1 天 18 小时 40 分钟
            platform_note="平台：iOS",
        )
        text = format_push_message(msg)

        assert "2026-07-26 18:00:00" in text
        # 平台信息在账号之后、阅读状态之前
        assert "账号：12345\n平台：iOS" in text
        assert "阅读状态：成功" in text
        assert "本轮阅读：1 小时" in text
        assert "本周阅读：7 小时 31 分钟" in text
        assert "连续阅读：128 天" in text
        assert "兑换状态：成功" in text
        assert "赠币数量：9.92 (+2)" in text
        # 体验卡剩余时长带天，括号加数带单位
        assert "体验天数：1 天 18 小时 40 分钟 (+1 天)" in text
        assert "约" not in text
        assert "到期" not in text
        # 不应有旧格式残留
        assert "执行完成" not in text
        assert "执行失败" not in text
        assert "书币钱包" not in text

    def test_success_without_optional_fields(self) -> None:
        """余额与体验卡剩余均未获取：赠币"未知 (+N)"，体验卡"未知"。"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=30,
            weekly_read_seconds=3600,
            exchange_success=True,
            exchanged_coin=1,
            exchanged_card=0,  # 发生了兑换但未获得体验卡
            # card_remain_seconds 未设（None）→ 体验卡显示"未知"
        )
        text = format_push_message(msg)

        assert "阅读状态：成功" in text
        assert "本轮阅读：30 分钟" in text
        assert "本周阅读：1 小时" in text
        assert "兑换状态：成功" in text
        # coin_balance 未设置（未获取到余额）时显示"未知 (+本次获得)"
        assert "赠币数量：未知 (+1)" in text
        assert "0.00" not in text
        # card_remain_seconds 未获取，体验卡显示"未知"
        assert "体验天数：未知" in text
        assert "连续阅读" not in text

    def test_no_exchange_data_shows_unknown(self) -> None:
        """未发生兑换（exchanged_card=None）：赠币与体验卡均显示"未知"。"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=60,
            exchange_success=True,
            exchanged_coin=0,
            exchanged_card=None,  # 未发生兑换
            coin_balance=None,  # 余额也未查到
        )
        text = format_push_message(msg)
        assert "赠币数量：未知" in text
        assert "体验天数：未知" in text
        assert "0.00" not in text
        assert "到期" not in text

    def test_card_duration_displayed_when_present(self) -> None:
        """体验卡有剩余秒数时按时长格式显示（+本次获得 天），不显示"未知"。"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=60,
            exchange_success=True,
            exchanged_coin=0,
            exchanged_card=1,  # 本次获得 1 天
            coin_balance=9.92,
            card_remain_seconds=153600,  # 1 天 18 小时 40 分钟
        )
        text = format_push_message(msg)
        assert "体验天数：1 天 18 小时 40 分钟 (+1 天)" in text
        assert "体验天数：未知" not in text
        assert "约" not in text
        assert "到期" not in text
        assert "书币余额" not in text
        assert "赠币数量：9.92" in text

    def test_minimal_success(self) -> None:
        """阅读成功，兑换跳过（未配置）"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=60,
            exchange_skipped=True,
        )
        text = format_push_message(msg)

        assert "阅读状态：成功" in text
        assert "本轮阅读：1 小时" in text
        assert "兑换状态：未配置" in text
        assert "本周阅读" not in text

    def test_account_fallback_when_empty(self) -> None:
        """账号为空时显示（未配置）。"""
        msg = PushMessage(
            reading_success=True, account="", read_minutes=60, exchange_skipped=True,
        )
        text = format_push_message(msg)
        assert "账号：（未配置）" in text

    def test_platform_note_absent_when_empty(self) -> None:
        """无平台信息时不展示对应行。"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=60,
            exchange_skipped=True,
            platform_note="",
        )
        text = format_push_message(msg)
        assert "平台" not in text


class TestFormatPartial:
    """部分成功：阅读成功但兑换失败或跳过。"""

    def test_partial_with_exchange_error(self) -> None:
        """兑换失败时状态独立展示。"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=60,
            exchange_success=False,
            exchange_error="cookie 中未找到 wr_vid",
        )
        text = format_push_message(msg)

        assert "阅读状态：成功" in text
        assert "本轮阅读：1 小时" in text
        assert "兑换状态：失败" in text
        assert "cookie 中未找到 wr_vid" in text

    def test_partial_with_exchange_skipped_and_error(self) -> None:
        """跳过兑换但配置了 APP_CURL（刷新失败场景），兑换显示失败。"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=60,
            exchange_skipped=True,
            exchange_error="Token 自动刷新已跳过：WEREAD_APP_CURL 中未找到 /login 请求",
        )
        text = format_push_message(msg)

        assert "兑换状态：失败" in text
        assert "WEREAD_APP_CURL" in text

    def test_partial_with_exchange_skipped_no_error(self) -> None:
        """跳过兑换且无诊断（纯未配置）。"""
        msg = PushMessage(
            account="12345",
            reading_success=True,
            read_minutes=60,
            exchange_skipped=True,
        )
        text = format_push_message(msg)

        assert "兑换状态：未配置" in text


class TestFormatReadingFailure:
    """阅读失败（阅读未完成，兑换未进行）。"""

    def test_reading_failure_cookie_expired(self) -> None:
        """阅读失败：Cookie 过期。"""
        msg = PushMessage(
            account="12345",
            reading_success=False,
            reading_error="Cookie 刷新失败：连续 3 次 cookie 刷新均失败",
        )
        text = format_push_message(msg)

        assert "阅读状态：失败" in text
        assert "Cookie 刷新失败" in text
        assert "兑换状态：失败" in text
        assert "阅读未完成，兑换未进行" in text
        assert "本轮阅读" not in text

    def test_reading_failure_read_failed(self) -> None:
        """阅读失败：阅读熔断。"""
        msg = PushMessage(
            account="12345",
            reading_success=False,
            reading_error="阅读熔断：连续 3 次无 synckey",
        )
        text = format_push_message(msg)

        assert "阅读状态：失败" in text
        assert "阅读熔断" in text
        assert "兑换状态：失败" in text
        assert "阅读未完成，兑换未进行" in text

    def test_reading_failure_general_exception(self) -> None:
        """阅读失败：未捕获异常。"""
        msg = PushMessage(
            account="12345",
            reading_success=False,
            reading_error="运行失败：HTTPConnectionPool: Max retries exceeded",
        )
        text = format_push_message(msg)

        assert "阅读状态：失败" in text
        assert "Max retries exceeded" in text
        assert "兑换状态：失败" in text
        assert "阅读未完成，兑换未进行" in text


class TestFormatDuration:
    """时长格式化边界。"""

    def test_zero_seconds(self) -> None:
        """0 秒时不展示本周阅读行。"""
        msg = PushMessage(
            reading_success=True,
            account="x",
            read_minutes=60,
            weekly_read_seconds=0,
            exchange_skipped=True,
        )
        text = format_push_message(msg)
        assert "本周阅读" not in text

    def test_only_minutes(self) -> None:
        """不足 1 小时只显示分钟。"""
        msg = PushMessage(
            reading_success=True,
            account="x",
            read_minutes=60,
            weekly_read_seconds=1800,  # 30 分钟
            exchange_skipped=True,
        )
        text = format_push_message(msg)
        assert "本周阅读：30 分钟" in text

    def test_only_hours(self) -> None:
        """整点小时只显示小时。"""
        msg = PushMessage(
            reading_success=True,
            account="x",
            read_minutes=60,
            weekly_read_seconds=7200,  # 2 小时
            exchange_skipped=True,
        )
        text = format_push_message(msg)
        assert "本周阅读：2 小时" in text
