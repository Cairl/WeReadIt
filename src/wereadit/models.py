"""数据模型：用 dataclass 表达请求数据与响应数据，替代裸 dict 访问。

类型注解便于 IDE 自动补全与静态检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReadResult:
    """阅读循环执行结果。

    包含运行 metrics,用于推送时展示账号健康度。
    """
    # 基础结果
    completed_count: int  # 成功完成的阅读次数
    total_minutes: float  # 累计阅读时长（分钟）

    # 运行 metrics（用于评估账号健康度与保活策略生效情况）
    synckey_success: int = 0  # synckey 成功次数(直接成功 + fix 后重试成功)
    no_synckey_fix_triggered: int = 0  # fix_no_synckey 触发次数
    fix_retry_success: int = 0  # fix 后重试 read 成功的次数
    cookie_refresh_count: int = 0  # refresh_cookie 触发次数(含启动 1 次)
    circuit_breaker_triggered: bool = False  # 是否触发熔断

    @property
    def is_full_completed(self) -> bool:
        """是否完成了全部阅读次数（由调用方判断阈值）。"""
        return self.completed_count > 0

    def summary(self) -> str:
        """生成 metrics 摘要文本,用于推送内容。"""
        return (
            f"本次统计：成功 {self.synckey_success} 次 / "
            f"fix 触发 {self.no_synckey_fix_triggered} 次 / "
            f"fix 重试成功 {self.fix_retry_success} 次 / "
            f"cookie 刷新 {self.cookie_refresh_count} 次"
        )


@dataclass
class AwardChoice:
    """奖励选项（体验卡 / 书币）。"""

    choice_type: int
    award_num: int = 0
    can_choice: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AwardChoice:
        return cls(
            choice_type=int(data.get("choiceType", 0)),
            award_num=int(data.get("awardNum", 0)),
            can_choice=bool(data.get("canChoice", 0) == 1),
        )


@dataclass
class Award:
    """单个奖励。"""

    award_level_id: int
    award_status: int  # 1=可领取, 2=已领取
    award_level_desc: str = ""
    choices: list[AwardChoice] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Award:
        return cls(
            award_level_id=int(data.get("awardLevelId", 0)),
            award_status=int(data.get("awardStatus", 0)),
            award_level_desc=str(data.get("awardLevelDesc", "")),
            choices=[AwardChoice.from_dict(c) for c in data.get("awardChoices", [])],
        )

    def find_choice(self, choice_type: int) -> AwardChoice | None:
        """查找指定类型的奖励选项。"""
        return next((c for c in self.choices if c.choice_type == choice_type), None)
