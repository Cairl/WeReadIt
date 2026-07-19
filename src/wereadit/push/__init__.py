"""push 包：可插拔的推送渠道。

设计：
- base.py 定义 Pusher 抽象基类与 with_retry 装饰器
- 每个渠道独立文件，实现 Pusher 接口
- registry.py 提供注册表与 get_pusher 工厂

新增渠道只需：
1. 新建 push/xxx.py，定义 XxxxPusher(Pusher) 类
2. 用 @register("xxx") 装饰
3. 完成
"""

from wereadit.push.registry import get_pusher, push

__all__ = ["get_pusher", "push"]
