"""推送消息格式化。

把 ReadResult + ExchangeResult + 诊断信息组装成统一的推送文案，
替代原先散落在 app.py 中的字符串拼接。

格式（严格按用户要求，单一区块，不加额外标题/分隔线）：
    WeReadIt 执行完成

    运行结果
    ────────────────────
    账号：xxxx
    状态：成功

    本轮阅读：60 分钟
    本周阅读：7 小时 31 分钟
    连续阅读：128 天
    书币钱包：9.92 (+2)

可选字段（连续阅读 / 书币钱包余额）由调用方按数据是否可得决定填不填，
未填则该行自动省略，不会出现空占位。诊断信息追加在主体之后，不加区块标题。
"""

from __future__ import annotations

from dataclasses import dataclass

# 分隔线：与示例保持一致（20 个 U+2500）
_DIVIDER = "\u2500" * 20


@dataclass
class PushMessage:
    """推送消息内容。

    所有字段都有默认值，调用方按需填充。formatter 按字段是否有值决定是否
    渲染对应行，避免出现空占位。
    """

    # 基础信息
    is_success: bool = True  # 整体是否成功（影响标题与状态行）
    is_partial: bool = False  # 阅读成功但兑换失败/未兑换
    account: str = ""  # 账号标识（wr_vid）

    # 阅读数据
    read_minutes: float = 0.0  # 本轮阅读时长（分钟）
    weekly_read_seconds: int = 0  # 本周阅读时长（秒）
    keep_reading_days: int | None = None  # 连续阅读天数（可选）

    # 兑换数据
    exchanged_coin: int = 0  # 兑换的书币数
    exchanged_card: int = 0  # 兑换的体验卡天数
    coin_balance: float | None = None  # 书币钱包余额（可选）
    exchange_error: str = ""  # 兑换错误描述（非空表示兑换失败）
    exchange_skipped: bool = False  # 是否跳过兑换（无 token 等）

    # 诊断信息（可选，追加在主体之后）
    refresh_diagnosis: str = ""  # Token 自动续期诊断
    platform_note: str = ""  # 平台自识别说明
    metrics_summary: str = ""  # 阅读运行 metrics 摘要

    # 致命错误（阅读未完成等，非空时优先级最高）
    fatal_error: str = ""


def _format_duration(seconds: int) -> str:
    """把秒数格式化为 'X 小时 Y 分钟'。

    0 或负数返回 '0 分钟'；不足 1 小时只显示分钟。
    """
    if seconds <= 0:
        return "0 分钟"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0 and minutes > 0:
        return f"{hours} 小时 {minutes} 分钟"
    if hours > 0:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


def _format_status(msg: PushMessage) -> str:
    """状态行文案。"""
    if msg.fatal_error:
        return "失败"
    if msg.is_partial:
        return "部分成功"
    if msg.is_success:
        return "成功"
    return "失败"


def format_push_message(msg: PushMessage) -> str:
    """渲染推送消息为最终文本。

    严格按用户要求的单一区块格式：标题 -> 运行结果 -> 账号/状态 -> 阅读数据。
    诊断信息追加在主体之后，不加额外区块标题或分隔线。
    """
    title = "WeReadIt 执行失败" if msg.fatal_error else "WeReadIt 执行完成"

    # 主体：标题 + 运行结果 + 账号/状态 + 数据行
    lines = [
        title,
        "",
        "运行结果",
        _DIVIDER,
        f"账号：{msg.account or '（未配置）'}",
        f"状态：{_format_status(msg)}",
    ]

    if msg.fatal_error:
        # 致命错误：状态行后直接展示错误信息
        lines.extend(["", msg.fatal_error])
    else:
        # 成功/部分成功：展示阅读与兑换数据
        lines.append("")
        lines.append(f"本轮阅读：{msg.read_minutes:.0f} 分钟")

        # 本周阅读（有时长才展示）
        if msg.weekly_read_seconds > 0:
            lines.append(f"本周阅读：{_format_duration(msg.weekly_read_seconds)}")

        # 连续阅读（可选字段，有值才展示）
        if msg.keep_reading_days is not None:
            lines.append(f"连续阅读：{msg.keep_reading_days} 天")

        # 书币钱包（有余额才展示；兑换增量作为 (+N) 后缀）
        if msg.coin_balance is not None:
            balance_str = f"{msg.coin_balance:.2f}"
            if msg.exchanged_coin > 0:
                balance_str += f" (+{msg.exchanged_coin})"
            lines.append(f"书币钱包：{balance_str}")

        # 兑换失败/跳过（部分成功时展示原因）
        if msg.exchange_error:
            lines.extend(["", f"兑换失败：{msg.exchange_error}"])
        elif msg.exchange_skipped:
            lines.extend(["", "兑换已跳过（未配置兑换 Token）"])

    # 诊断信息追加在最后，不加区块标题
    if msg.platform_note:
        lines.extend(["", msg.platform_note])

    if msg.refresh_diagnosis:
        lines.extend(["", f"Token 续期诊断：{msg.refresh_diagnosis}"])

    if msg.metrics_summary:
        lines.extend(["", msg.metrics_summary])

    return "\n".join(lines)
