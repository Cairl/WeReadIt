# WeReadIt

## 项目介绍

这是一个微信读书自动阅读脚本，主要用于在阅读挑战赛中刷时长和保持天数。通过对微信读书官网接口的抓包和 JS 逆向分析实现。

功能特性：

- **阅读时长调节**：默认计入排行榜和挑战赛，时长可调节，默认为 20 分钟。
- **定时运行推送**：可部署在 GitHub Action/服务器上，支持每天定时运行并推送结果。
- **Cookie 自动更新**：脚本能自动获取并更新 Cookie，一次部署后无需额外操作。
- **轻量化设计**：无需额外硬件，到点自动运行。

---

## 操作步骤

### 抓包准备

在 [微信读书](https://weread.qq.com/) 搜索【三体】，点开阅读，点击下一页进行抓包，抓到 `read` 接口 `https://weread.qq.com/web/book/read`，如果返回格式正常（如：

```json
{
  "succ": 1,
  "synckey": 564589834
}
```

右键复制为 Bash 格式。

### GitHub Action 部署运行

在仓库 **Settings** -> **Secrets and variables** -> **Actions** 中配置：

**Repository secrets（必填）：**

| key | Value | 说明 |
| --- | --- | --- |
| `WEREADIT_CURL_BASH` | `read` 接口 `curl_bash` 数据 | 必须提供有效指令 |
| `PUSH_METHOD` | `pushplus` / `wxpusher` / `telegram` / `serverchan` | 推送方式，4 选 1 |

**Repository secrets（按需）：**

| key | Value | 说明 |
| --- | --- | --- |
| `PUSHPLUS_TOKEN` | PushPlus 的 token | 当 `PUSH_METHOD=pushplus` 时必填，[获取地址](https://www.pushplus.plus/uc.html) |
| `WXPUSHER_SPT` | WxPusher 的 token | 当 `PUSH_METHOD=wxpusher` 时必填，[获取地址](https://wxpusher.zjiecode.com/docs/#/?id=获取spt) |
| `TELEGRAM_BOT_TOKEN` | 机器人 token | 当 `PUSH_METHOD=telegram` 时必填 |
| `TELEGRAM_CHAT_ID` | 群组 ID | 当 `PUSH_METHOD=telegram` 时必填 |
| `SERVERCHAN_SPT` | ServerChan 的 SendKey | 当 `PUSH_METHOD=serverchan` 时必填，[获取地址](https://sct.ftqq.com/sendkey) |
| `WEREAD_ACCESS_TOKEN` | APP 端认证 token | 兑换阅读奖励时必填，未配置则跳过兑换。Android 填 `accessToken` 值，iOS 填 `skey` 值。获取方式见下方[认证 token 抓包指引](#认证-token-抓包指引) |

**Repository variables（可选）：**

| key | Value | 说明 |
| --- | --- | --- |
| `READ_NUM` | 阅读次数（每次 30 秒） | 阅读时长，默认 20 分钟 |
| `WEREAD_PLATFORM` | `android` 或 `ios` | 兑换平台，决定认证方式和书币归属。默认 `android`。**iOS 用户建议设为 `ios`**，否则领取的书币无法在 iOS APP 使用 |
| `EXCHANGE_AWARD` | 兑换策略 | 8 位逗号分隔，`0`=不兑/`1`=体验卡/`2`=书币，对应奖励等级 `[4,5,1,2,3,11,12,13]`，默认 `2,2,2,2,2,2,2,2`（全兑书币） |

### 认证 token 抓包指引

兑换阅读奖励功能需要 APP 端认证 token，网页 cookie 调不通该接口。Android 和 iOS 的认证方式不同：

| 平台 | 认证 header | token 来源 | pf 标识 |
| --- | --- | --- | --- |
| Android | `accessToken` | Android APP 抓包 | `wechat_wx-2001-android-100-weread` |
| iOS | `skey` | iOS APP 抓包 | `weread_wx-2001-iap-2001-iphone` |

**重要**：Android 平台领取的书币无法在 iOS APP 使用（微信读书对书币做了平台归属限制）。如果你的主要设备是 iOS，请配置 `WEREAD_PLATFORM=ios` 并抓取 iOS APP 的 skey。

获取步骤：

1. **安装抓包工具**：安卓推荐 Reqable 或 HTTP Canary，iOS 推荐 Stream 或 ProxyPin
2. **安装 CA 证书**：按抓包工具引导安装，HTTPS 抓包必需
3. **启动抓包**：打开抓包工具开始记录流量
4. **打开微信读书 APP**：正常使用即可（浏览书架、打开任意一本书等）
5. **筛选请求**：在抓包工具里按域名 `i.weread.qq.com` 筛选，找任意一个请求
6. **提取认证 token**：
   - Android：查看请求头里的 `accessToken` 字段值
   - iOS：查看请求头里的 `skey` 字段值
7. **配置**：将 token 值配置到 GitHub Secret `WEREAD_ACCESS_TOKEN`，并在 variables 里设置 `WEREAD_PLATFORM` 为对应平台

注意：认证 token 有有效期（实测至少数天），过期后兑换功能会推送「登录超时」通知，重新抓包更新即可。`vid` 无需单独配置，脚本会自动从网页 cookie 的 `wr_vid` 提取。

### 服务器运行（本地部署）

在你的服务器上有 Python 运行环境即可，使用 `cron` 定义自动运行。

步骤：

1. 克隆项目：`git clone <仓库地址>`
2. 配置 `config.py` 里的 `headers`、`cookies`、`READ_NUM`、`PUSH_METHOD` 以及对应推送方式的 token
3. 安装依赖：`pip install requests`
4. 测试运行：`python main.py`
5. 设置 cron 定时任务

---

## 注意事项

1. **签到次数调整**：只需签到完成挑战赛可以将 `READ_NUM` 次数从 40 调整为 2，每次为 30 秒。
2. **解决阅读时间问题**：建议保留 `config.py` 中的 `data` 字段，默认阅读三体，其它书籍自行测试。
3. **GitHub Action 部署**：使用环境变量配置；本地部署直接修改 `config.py` 即可。
4. **推送**：pushplus 推送增加重试机制；wxpusher 支持极简推送方式。

---

## 字段解释

| 字段 | 示例值 | 解释 |
| --- | --- | --- |
| `appId` | `"wbxxxxxxxxxxxxxxxxxxxxxxxx"` | 应用的唯一标识符。 |
| `b` | `"ce032b305a9bc1ce0b0dd2a"` | 书籍或章节的唯一标识符。 |
| `c` | `"0723244023c072b030ba601"` | 内容的唯一标识符，可能是页面或具体段落。 |
| `ci` | `60` | 章节或部分的索引。 |
| `co` | `336` | 内容的具体位置或页码。 |
| `sm` | `"[插图]威慑纪元61年，执剑人在一棵巨树"` | 当前阅读的内容描述或摘要。 |
| `pr` | `65` | 页码或段落索引。 |
| `rt` | `88` | 阅读时长或阅读进度。 |
| `ts` | `1727580815581` | 时间戳，表示请求发送的具体时间（毫秒级）。 |
| `rn` | `114` | 随机数或请求编号，用于标识唯一的请求。 |
| `sg` | `"bfdf7de2fe1673546ca079e2f02b79b937901ef789ed5ae16e7b43fb9e22e724"` | 安全签名，用于验证请求的合法性和完整性。 |
| `ct` | `1727580815` | 时间戳，表示请求发送的具体时间（秒级）。 |
| `ps` | `"xxxxxxxxxxxxxxxxxxxxxxxx"` | 用户标识符或会话标识符，用于追踪用户或会话。 |
| `pc` | `"xxxxxxxxxxxxxxxxxxxxxxxx"` | 设备标识符或客户端标识符，用于标识用户的设备或客户端。 |
| `s` | `"fadcb9de"` | 校验和或哈希值，用于验证请求数据的完整性。 |
