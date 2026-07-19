# 微信读书自动兑换阅读奖励 - 设计文档

> 日期：2026-07-19
> 状态：待审查

## 1. 概述

在现有 WeReadIt 项目基础上新增「自动兑换阅读奖励」功能。每天阅读脚本完成后，自动查询可领取的阅读奖励（时长奖励 + 天数奖励），按用户配置的策略（默认全兑书币）逐个兑换，结果合并到现有推送渠道。

## 2. 调研结论（已通过 7 轮探针 + HAR 抓包验证）

### 2.1 接口

- **URL**：`POST https://i.weread.qq.com/weekly/exchange`
- 查询和兑换共用同一个 URL，通过请求体字段区分

### 2.2 认证方式

APP 端用以下 header 认证（**不是 cookie，也不是 skey**）：

```
accessToken: <APP端 accessToken>
vid: <用户 vid>
baseapi: 34
appver: 8.2.6.10163989
User-Agent: WeRead/8.2.6 WRBrand/other Dalvik/2.1.0 (Linux; U; Android 14; ...)
osver: 14
channelId: 0
basever: 8.2.6.10163989
Content-Type: application/json; charset=UTF-8
```

关键发现：
- `accessToken` 是 APP 端专属会话密钥，跟网页 `wr_skey` 是两套独立体系（值不同）
- `vid` 跟网页 cookie 里的 `wr_vid` **完全相同**（都是用户唯一标识），可从现有 `WEREADIT_CURL_BASH` 提取，零增量配置
- 网页 cookie 调 `i.weread.qq.com` 接口必然返回 401（7 轮探针证实），必须用 APP 端 `accessToken`

### 2.3 请求体

查询（列出所有奖励）：
```json
{
  "awardLevelId": 0,
  "isExchangeAward": 0,
  "isVisitReadGoal": 1,
  "unread": 0,
  "pf": "wechat_wx-2001-android-100-weread",
  "awardChoiceType": 0
}
```

兑换指定奖励：
```json
{
  "awardLevelId": <具体奖励等级ID>,
  "isExchangeAward": 1,
  "isVisitReadGoal": 1,
  "unread": 0,
  "pf": "wechat_wx-2001-android-100-weread",
  "awardChoiceType": <1=体验卡 | 2=书币>
}
```

### 2.4 响应结构

```json
{
  "readingTime": 28614,       // 本周阅读时长（秒）
  "readingDay": 7,            // 本周阅读天数
  "readtimeAwards": [...],    // 时长奖励数组（5 个）
  "readdayAwards": [...],     // 天数奖励数组（3 个）
  ...
}
```

每个奖励对象：
```json
{
  "awardLevelId": 4,           // 奖励等级 ID
  "awardStatus": 1,            // 1=可领取, 2=已领取
  "awardLevelDesc": "读 5 分钟",
  "awardChoices": [
    {"choiceType": 1, "awardNum": 1, "canChoice": 1},  // 体验卡
    {"choiceType": 2, "awardNum": 1, "canChoice": 1}   // 书币
  ]
}
```

### 2.5 奖励等级 ID 顺序

按响应中出现的顺序，8 个奖励等级 ID 为：`[4, 5, 1, 2, 3, 11, 12, 13]`

| 序号 | awardLevelId | 描述 | 体验卡 | 书币 |
|------|--------------|------|--------|------|
| 0 | 4 | 读 5 分钟 | 1 天 | 1 |
| 1 | 5 | 读 30 分钟 | 1 天 | 1 |
| 2 | 1 | 读 1 小时 | 1 天 | 2 |
| 3 | 2 | 读 3 小时 | 2 天 | 2 |
| 4 | 3 | 读 5 小时 | 2 天 | 2 |
| 5 | 11 | 读 2 天 | 2 天 | 2 |
| 6 | 12 | 读 4 天 | 2 天 | 4 |
| 7 | 13 | 读 7 天 | 2 天 | 6 |

全兑书币每周最多 **20 书币**；全兑体验卡每周最多 **13 天体验卡**。

## 3. 架构设计

### 3.1 文件改动

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `exchange.py` | 新增 | 兑换功能核心模块 |
| `config.py` | 修改 | 新增 `WEREAD_ACCESS_TOKEN` 和 `EXCHANGE_AWARD` 配置项 |
| `main.py` | 修改 | 阅读完成后调用兑换，结果合并到推送 |
| `README.md` | 修改 | 补充新配置项说明 |

### 3.2 exchange.py 模块设计

```python
# exchange.py 核心接口
def exchange_awards(access_token, vid, exchange_strategy, push_method):
    """
    查询并兑换阅读奖励。
    
    Args:
        access_token: APP 端 accessToken
        vid: 用户 vid（从网页 cookie 的 wr_vid 提取）
        exchange_strategy: 8 位字符串，如 "2,2,2,2,2,2,2,2"
        push_method: 推送方式
    
    Returns:
        str: 兑换结果摘要（用于推送）
    """
```

模块内部流程：
1. 构造 APP 风格 header（accessToken + vid + APP 元数据）
2. 调 weekly/exchange 查询接口
3. 解析响应，合并 readtimeAwards + readdayAwards
4. 过滤出 `awardStatus == 1`（可领取）的奖励
5. 按 `AWARD_LEVEL_IDS = [4, 5, 1, 2, 3, 11, 12, 13]` 顺序，对照用户策略逐个兑换
6. 对每个奖励：检查 `awardChoices` 里对应 `choiceType` 的 `canChoice == 1`，调兑换接口
7. 汇总成功/失败/跳过数量，返回摘要字符串

### 3.3 错误处理

| 场景 | 错误码/表现 | 处理 |
|------|------------|------|
| accessToken 过期 | `-2012` 登录超时 | 推送通知「accessToken 已过期，请重新抓包」，跳过兑换 |
| 奖励不可兑换 | `canChoice != 1` | 跳过该奖励，记录日志 |
| 兑换请求失败 | 网络异常/超时 | 重试 3 次（间隔 5 秒），仍失败则跳过 |
| 奖励已领取 | `awardStatus == 2` | 跳过（正常情况，每周重置后才会重新可领） |
| 未配置 accessToken | `WEREAD_ACCESS_TOKEN` 为空 | 跳过兑换，记录日志「未配置 accessToken，跳过兑换」 |

### 3.4 与现有代码的集成

`main.py` 在阅读循环结束后、推送前插入兑换调用：

```python
# main.py 现有阅读逻辑结束后
logging.info("阅读脚本已完成。")

# 新增：兑换阅读奖励
exchange_summary = ""
if WEREAD_ACCESS_TOKEN:
    from exchange import exchange_awards
    logging.info("开始兑换阅读奖励...")
    try:
        exchange_summary = exchange_awards(
            WEREAD_ACCESS_TOKEN, cookies.get('wr_vid', ''),
            EXCHANGE_AWARD, PUSH_METHOD
        )
    except Exception as exc:
        logging.error("兑换奖励失败: %s", exc)
        exchange_summary = f"兑换奖励失败: {exc}"
else:
    logging.info("未配置 WEREAD_ACCESS_TOKEN，跳过兑换。")

# 修改推送内容，合并阅读结果 + 兑换结果
if PUSH_METHOD not in (None, ''):
    push_content = f"WeReadIt 自动阅读完成。\n阅读时长：{(index - 1) * 0.5} 分钟。"
    if exchange_summary:
        push_content += f"\n\n{exchange_summary}"
    push(push_content, PUSH_METHOD, is_success=True)
```

## 4. 配置设计

### 4.1 新增环境变量

| key | 必填 | 默认值 | 说明 |
|-----|------|--------|------|
| `WEREAD_ACCESS_TOKEN` | 否 | 空 | APP 端 accessToken，未配置则跳过兑换。从 APP 抓包获取 |
| `EXCHANGE_AWARD` | 否 | `"2,2,2,2,2,2,2,2"` | 8 位兑换策略，0=不兑/1=体验卡/2=书币，对应 8 个奖励等级 |

### 4.2 config.py 改动

```python
# 兑换奖励配置
WEREAD_ACCESS_TOKEN = "" or os.getenv("WEREAD_ACCESS_TOKEN")
EXCHANGE_AWARD = "2,2,2,2,2,2,2,2" or os.getenv("EXCHANGE_AWARD")
```

### 4.3 accessToken 获取方式（写入 README）

1. 手机安装抓包工具（Reqable / HTTP Canary / Stream）
2. 安装 CA 证书，启动抓包
3. 打开微信读书 APP，正常使用（任意触发一次 i.weread.qq.com 请求即可）
4. 在抓包工具里找任意一个 `i.weread.qq.com` 请求，查看请求头里的 `accessToken` 字段值
5. 复制 accessToken 值，配置到 GitHub Secret `WEREAD_ACCESS_TOKEN`

注意：accessToken 有有效期（具体时长未知，实测至少几天）。过期后兑换功能会推送「accessToken 已过期」通知，重新抓包更新即可。

## 5. 已知限制与未来改进

### 5.1 当前限制

1. **accessToken 过期需手动重抓**：当前版本不支持自动续期。过期后推送通知，用户需重新抓包。
2. **accessToken 获取流程未完全逆向**：HAR 里没抓到 `/login` 请求，不清楚 accessToken 的获取接口。未来补抓 /login 后可实现自动续期。
3. **每周重置**：奖励每周一 00:00 重置，周日 24:00 前必须兑换。脚本每天检查不会错过窗口，但如果脚本连续一周不运行则会错过。

### 5.2 未来改进方向

1. **accessToken 自动续期**：补抓 `/login` 请求，搞清获取流程后，实现类似 `refresh_cookie()` 的自动续期机制。
2. **兑换结果详细推送**：当前只推送摘要，未来可推送每个奖励的兑换详情。
3. **多账号支持**：如果项目未来支持多账号，兑换功能也需要支持每个账号独立配置 accessToken。

## 6. 测试策略

### 6.1 验证步骤（实现后执行）

1. **查询验证**：用真实 accessToken 调查询接口，确认返回 8 个奖励
2. **单奖励兑换验证**：用最小的奖励（awardLevelId=4，1 书币）做一次真实兑换，确认接口工作正常
3. **完整流程验证**：运行 `python main.py`，确认阅读 + 兑换 + 推送全流程跑通
4. **异常验证**：用错误的 accessToken 跑一次，确认错误处理和推送通知正常

### 6.2 回归测试

- 不配置 `WEREAD_ACCESS_TOKEN` 时，确认兑换被跳过，阅读流程不受影响
- 配置错误的 accessToken 时，确认推送「过期」通知，阅读流程不受影响
