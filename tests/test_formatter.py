"""formatter 测试：验证推送消息格式化逻辑。

覆盖：
- 成功路径（含/不含可选字段）
- 部分成功（兑换失败）
- 致命错误（阅读失败）
- 跳过兑换
- 诊断信息附加
"""

from __future__ import annotations

from wereadit.push.formatter import PushMessage, format_push_message


class TestFormatSuccess:
    """成功路径渲染。"""

    def test_full_success_with_all_fields(self) -> None:
        """所有字段都有值时完整渲染（严格按用户格式，单一区块）。"""
        msg = PushMessage(
            is_success=True,
            account="12345",
            read_minutes=60,
            weekly_read_seconds=27060,  # 7 小时 31 分钟
            keep_reading_days=128,
            coin_balance=9.92,
            exchanged_coin=2,
            exchanged_card=1,
            platform_note="平台自识别：iOS",
            metrics_summary="本次统计：成功 120 次",
        )
        text = format_push_message(msg)

        assert "WeReadIt 执行完成" in text
        assert "账号：12345" in text
        assert "状态：成功" in text
        assert "本轮阅读：60 分钟" in text
        assert "本周阅读：7 小时 31 分钟" in text
        assert "连续阅读：128 天" in text
        assert "书币钱包：9.92 (+2)" in text
        assert "平台自识别：iOS" in text
        assert "本次统计：成功 120 次" in text
        # 不应有额外区块标题
        assert "兑换奖励" not in text
        assert "运行统计" not in text

    def test_success_without_optional_fields(self) -> None:
        """无可选字段（连续阅读/书币余额）时对应行省略。"""
        msg = PushMessage(
            is_success=True,
            account="12345",
            read_minutes=30,
            weekly_read_seconds=3600,
            exchanged_coin=1,
        )
        text = format_push_message(msg)

        assert "状态：成功" in text
        assert "本轮阅读：30 分钟" in text
        assert "本周阅读：1 小时" in text
        # 无可选字段时不应出现这两行
        assert "连续阅读" not in text
        assert "书币钱包" not in text

    def test_minimal_success(self) -> None:
        """最小成功：只有阅读，无兑换数据。"""
        msg = PushMessage(
            is_success=True,
            account="12345",
            read_minutes=60,
            metrics_summary="统计信息",
        )
        text = format_push_message(msg)

        assert "WeReadIt 执行完成" in text
        assert "状态：成功" in text
        assert "本轮阅读：60 分钟" in text
        # 无本周阅读数据时不展示
        assert "本周阅读" not in text
        assert "兑换失败" not in text

    def test_account_fallback_when_empty(self) -> None:
        """账号为空时显示（未配置）。"""
        msg = PushMessage(is_success=True, account="", read_minutes=60)
        text = format_push_message(msg)
        assert "账号：（未配置）" in text


class TestFormatPartial:
    """部分成功（阅读成功但兑换失败）。"""

    def test_partial_with_exchange_error(self) -> None:
        """兑换失败时状态为部分成功，错误信息直接展示。"""
        msg = PushMessage(
            is_success=False,
            is_partial=True,
            account="12345",
            read_minutes=60,
            exchange_error="cookie 中未找到 wr_vid",
        )
        text = format_push_message(msg)

        assert "WeReadIt 执行完成" in text
        assert "状态：部分成功" in text
        assert "兑换失败：cookie 中未找到 wr_vid" in text
        # 不应有"兑换奖励"区块标题
        assert "兑换奖励" not in text

    def test_partial_with_exchange_skipped(self) -> None:
        """跳过兑换时展示已跳过说明。"""
        msg = PushMessage(
            is_success=True,
            is_partial=True,
            account="12345",
            read_minutes=60,
            exchange_skipped=True,
        )
        text = format_push_message(msg)

        assert "状态：部分成功" in text
        assert "兑换已跳过（未配置兑换 Token）" in text


class TestFormatFatalError:
    """致命错误（阅读未完成）。"""

    def test_fatal_error_uses_failure_title(self) -> None:
        """致命错误用失败标题，状态后直接展示错误信息。"""
        msg = PushMessage(
            account="12345",
            fatal_error="阅读熔断：连续 3 次 cookie 过期",
        )
        text = format_push_message(msg)

        assert "WeReadIt 执行失败" in text
        assert "状态：失败" in text
        assert "阅读熔断：连续 3 次 cookie 过期" in text
        # 致命错误时不展示阅读数据
        assert "本轮阅读" not in text

    def test_fatal_error_with_diagnosis(self) -> None:
        """致命错误带 Token 续期诊断。"""
        msg = PushMessage(
            account="12345",
            fatal_error="Cookie 刷新失败",
            refresh_diagnosis="网络异常",
        )
        text = format_push_message(msg)

        assert "WeReadIt 执行失败" in text
        assert "Token 续期诊断：网络异常" in text


class TestFormatDiagnostics:
    """诊断信息附加（不加区块标题）。"""

    def test_refresh_diagnosis_appended(self) -> None:
        """Token 续期诊断追加在末尾。"""
        msg = PushMessage(
            is_success=True,
            account="12345",
            read_minutes=60,
            refresh_diagnosis="login 凭证已失效，请重新抓包",
        )
        text = format_push_message(msg)

        assert "Token 续期诊断：login 凭证已失效，请重新抓包" in text

    def test_platform_note_appended(self) -> None:
        """平台自识别说明追加在末尾。"""
        msg = PushMessage(
            is_success=True,
            account="12345",
            read_minutes=60,
            platform_note="平台自识别：Android",
        )
        text = format_push_message(msg)

        assert "平台自识别：Android" in text


class TestFormatDuration:
    """时长格式化边界。"""

    def test_zero_seconds(self) -> None:
        """0 秒时不展示本周阅读行。"""
        msg = PushMessage(
            is_success=True, account="x", read_minutes=60, weekly_read_seconds=0,
        )
        text = format_push_message(msg)
        assert "本周阅读" not in text

    def test_only_minutes(self) -> None:
        """不足 1 小时只显示分钟。"""
        msg = PushMessage(
            is_success=True,
            account="x",
            read_minutes=60,
            weekly_read_seconds=1800,  # 30 分钟
        )
        text = format_push_message(msg)
        assert "本周阅读：30 分钟" in text

    def test_only_hours(self) -> None:
        """整点小时只显示小时。"""
        msg = PushMessage(
            is_success=True,
            account="x",
            read_minutes=60,
            weekly_read_seconds=7200,  # 2 小时
        )
        text = format_push_message(msg)
        assert "本周阅读：2 小时" in text
