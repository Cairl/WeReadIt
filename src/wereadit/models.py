"""数据模型：用 dataclass 表达请求数据与响应数据，替代裸 dict 访问。

类型注解便于 IDE 自动补全与静态检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReadResult:
    """阅读循环执行结果。"""

    completed_count: int  # 成功完成的阅读次数
    total_minutes: float  # 累计阅读时长（分钟）

    @property
    def is_full_completed(self) -> bool:
        """是否完成了全部阅读次数（由调用方判断阈值）。"""
        return self.completed_count > 0


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
