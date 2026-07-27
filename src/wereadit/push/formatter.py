"""推送消息格式化。

把 ReadResult + ExchangeResult + 诊断信息组装成统一的推送文案，
替代原先散落在 app.py 中的字符串拼接。

格式（阅读与兑换独立展示，各按成功/失败展示详情或原因）：
    WeReadIt
    2026-07-26 18:00:00

    运行结果
    ────────────────────
    账号：xxxx

    阅读状态：成功
    本轮阅读：1 小时
    本周阅读：7 小时 31 分钟
    连续阅读：128 天

    兑换状态：成功
    赠币：9.92 (+2)
    体验卡：0 天

诊断信息追加在末尾，不加区块标题。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# 分隔线：20 个 U+2500
_DIVIDER = "\u2500" * 20


@dataclass
class PushMessage:
    """推送消息内容。

    阅读和兑换状态独立管理，互不影响。

    所有字段都有默认值，调用方按需填充。formatter 按字段是否有值决定是否
    渲染对应行，避免出现空占位。
    """

    # 基础信息
    timestamp: str = ""  # 时间戳（空则自动取当前时间 YYYY-MM-DD HH:MM:SS）
    account: str = ""  # 账号标识（wr_vid）

    # 阅读状态
    reading_success: bool = True  # 阅读是否成功
    reading_error: str = ""  # 阅读失败原因（成功时为空）
    read_minutes: float = 0.0  # 本轮阅读时长（分钟）
    weekly_read_seconds: int = 0  # 本周阅读时长（秒）
    keep_reading_days: int | None = None  # 连续阅读天数（可选）

    # 兑换状态
    exchange_success: bool = False  # 兑换是否成功（默认 False，兑换未发生时不影响显示）
    exchange_error: str = ""  # 兑换失败原因
    exchange_skipped: bool = False  # 是否跳过兑换（未配置 token 等）
    exchanged_coin: int = 0  # 兑换的书币数
    exchanged_card: int = 0  # 兑换的体验卡天数
    coin_balance: float | None = None  # 书币钱包余额（可选）

    # 诊断信息（可选，追加在末尾）
    refresh_diagnosis: str = ""  # Token 自动续期诊断
    platform_note: str = ""  # 平台自识别说明
    metrics_summary: str = ""  # 阅读运行 metrics 摘要


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


def format_push_message(msg: PushMessage) -> str:
    """渲染推送消息为最终文本。

    阅读和兑换独立展示，各自按成功/失败决定显示详情还是原因。
    诊断信息追加在末尾。
    """
    ts = msg.timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        ts,
        "",
        "运行结果",
        _DIVIDER,
        f"账号：{msg.account or '（未配置）'}",
    ]

    # 平台识别（紧接账号之后）
    if msg.platform_note:
        lines.append(msg.platform_note)

    # ---- 阅读区块 ----
    lines.append("")
    if msg.reading_success:
        lines.append("阅读状态：成功")
        lines.append(f"本轮阅读：{_format_duration(int(msg.read_minutes * 60))}")
        if msg.weekly_read_seconds > 0:
            lines.append(f"本周阅读：{_format_duration(msg.weekly_read_seconds)}")
        if msg.keep_reading_days is not None:
            lines.append(f"连续阅读：{msg.keep_reading_days} 天")
    else:
        lines.append("阅读状态：失败")
        if msg.reading_error:
            lines.append(msg.reading_error)

    # ---- 兑换区块 ----
    lines.append("")

    # 是否有兑换相关数据需要展示
    has_exchange_data = (
        msg.exchange_success
        or msg.exchange_skipped
        or bool(msg.exchange_error)
    )

    if not has_exchange_data:
        # 阅读失败导致兑换未进行
        lines.append("兑换状态：失败")
        if msg.reading_error:
            lines.append("阅读未完成，兑换未进行")
        else:
            lines.append("兑换未进行")
    elif msg.exchange_success:
        lines.append("兑换状态：成功")
        # 赠币行：兑换成功时始终展示当前书币余额
        balance = msg.coin_balance if msg.coin_balance is not None else 0.0
        balance_str = f"{balance:.2f}"
        if msg.exchanged_coin > 0:
            balance_str += f" (+{msg.exchanged_coin})"
        lines.append(f"赠币：{balance_str}")
        lines.append(f"体验卡：{msg.exchanged_card} 天")
    elif msg.exchange_skipped and not msg.exchange_error:
        lines.append("兑换状态：未配置")
    else:
        lines.append("兑换状态：失败")
        if msg.exchange_error:
            lines.append(msg.exchange_error)

    return "\n".join(lines)
