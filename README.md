# WeReadIt

微信读书自动阅读脚本。

通过模拟微信读书 Web `read` 接口完成阅读，支持自动刷新 Cookie、自动兑换每周奖励、多渠道消息推送，适合 GitHub Actions 或服务器长期运行。

## 功能特性

- 自动阅读（默认 60 分钟，可配置）
- Cookie 自动刷新，部署后长期运行
- 自动兑换每周阅读奖励（书币 / 无限卡，可选）
- 支持 PushPlus、WxPusher、Telegram、ServerChan 推送
- 支持 GitHub Actions、服务器定时任务

## 快速开始

### 1. 获取 `read` 请求

1. 登录微信读书网页版。
2. 打开任意书籍（推荐《三体》）开始阅读并翻页。
3. 使用浏览器开发者工具抓取：

```
POST https://weread.qq.com/web/book/read
```

复制该请求为 **cURL (Bash)**。

### 2. 配置 Secrets and Variables

仓库进入 **Settings → Secrets and variables → Actions**。

| 配置项                 | 类型     | 要求 | 默认值              | 说明                                           |
| ---------------------- | -------- | ---- | ------------------- | ---------------------------------------------- |
| `WEREAD_CURL_BASH`     | Secret   | 必填 | -                   | 第 1 步复制的 cURL 命令                        |
| `WEREAD_ANDROID_TOKEN` | Secret   | 选填 | -                   | Android `accessToken`，用于兑换奖励             |
| `WEREAD_IOS_TOKEN`     | Secret   | 选填 | -                   | iOS `skey`，用于兑换奖励                        |
| `WEREAD_LOGIN_CURL` | Secret | 选填 | - | App 端 `/login` 请求 cURL（body 须含 `deviceId`），用于 Token 自动续期（推荐配置） |
| `READ_NUM`             | Variable | 选填 | `120`               | 阅读次数（120 次 ≈ 60 分钟）                   |
| `EXCHANGE_AWARD`       | Variable | 选填 | `2,2,2,2,2,2,2,2`  | 兑换策略：`0`=不兑换，`1`=体验卡，`2`=书币     |

### 3. 运行

推送到 GitHub 后即可运行。

默认每天 **北京时间 00:00** 自动执行，支持在 GitHub Actions 页面手动触发。

### 4. 配置推送

| 配置项               | 类型   | 要求 | 默认值 | 说明                                    |
| -------------------- | ------ | ---- | ------ | --------------------------------------- |
| `PUSHPLUS`           | Secret | 选填 | -      | PushPlus 推送 token                     |
| `WXPUSHER`           | Secret | 选填 | -      | WxPusher 推送 token                     |
| `TELEGRAM_BOT_TOKEN` | Secret | 选填 | -      | Telegram Bot token（需同时配置 CHAT_ID） |
| `TELEGRAM_CHAT_ID`   | Secret | 选填 | -      | Telegram 会话 ID（需同时配置 BOT_TOKEN） |
| `SERVERCHAN`         | Secret | 选填 | -      | ServerChan 推送 token                   |

## 奖励兑换

网页 Cookie 无法调用奖励兑换接口，需要使用微信读书 App 的认证 Token。

### 抓取 Token

抓包步骤：

1. Android 推荐使用 Reqable，iOS 推荐使用 ProxyPin。
2. 安装并信任抓包证书。
3. 打开微信读书 App。
4. 抓取 `i.weread.qq.com` 任意请求。
5. 获取：
   - Android：请求头 `accessToken`
   - iOS：请求头 `skey`
6. 按需配置到：
   - `WEREAD_ANDROID_TOKEN`
   - `WEREAD_IOS_TOKEN`

> App Token 有效期仅约 2 小时，强烈建议配置下方的 Token 自动续期。`wr_vid` 会自动从网页 Cookie 获取，无需额外配置。

### Token 自动续期（推荐）

App Token 有效期仅约 2 小时，手动抓包无法覆盖每日定时运行。配置 `/login` 重放后，脚本每次运行会在**阅读开始前**自动刷新 Token（兑换时 token 年龄保持在有效期窗口内；若阅读耗时过长，兑换前还会补刷一次）。

抓包要点（决定成败）：

1. **杀掉微信读书 App 重新打开**（冷启动会触发 /login 刷新请求）。
2. 用抓包工具捕获 `i.weread.qq.com/login` 请求，**确认请求 body 中含 `deviceId`**（长效设备凭证，是重放换新 token 的依据；缺了它重放必然失败）。
3. 将该请求复制为 cURL (Bash) 格式。
4. 配置到 Secret `WEREAD_LOGIN_CURL`。

注意：`WEREAD_LOGIN_CURL` 与兑换 Token（`WEREAD_ANDROID_TOKEN` / `WEREAD_IOS_TOKEN`）必须抓自**同一平台**的设备（iOS 下发 skey，Android 下发 accessToken，交叉配置会被平台校验拦截）。

配置后无需再手动更新 Token。脚本启动时会对 curl 做静态体检（是否 /login、是否含 deviceId），刷新失败时会把诊断与下一步指引**直接写进推送消息**，无需翻 Actions 日志。

> 兜底思路（本项目未实现）：若 `/login` 重放被服务端彻底关闭，社区方案是手机端用 Quantumult X / 快捷指令定时拦截 App 的 skey 并调用 GitHub API 更新 Secrets。依赖手机常开与抓包 App，仅作为最后手段记录在案。

## 本地运行

```bash
git clone <repository>
cd WeReadIt

pip install -r requirements.txt

export WEREAD_CURL_BASH='curl ...'

# 可选
export PUSHPLUS=xxxx

python main.py
```

或直接修改 `src/wereadit/constants.py` 中的默认请求头和 Cookie 进行本地调试。

## 致谢

项目灵感及部分代码实现参考自 [findmover/wxread](https://github.com/findmover/wxread)，由衷感谢原作者技术支持。

## 免责声明

- 本项目**仅供学习交流**，严禁用于商业用途或任何违反微信读书服务条款的行为。
- 本项目与**微信读书（weread.qq.com）及其运营方腾讯公司无任何关联**，未获得其任何形式的授权或认可。
- 使用本项目可能违反微信读书用户协议，包括但不限于禁止使用自动化脚本的规定。使用者应自行承担由此产生的全部风险和后果，包括但不限于 **账号封禁、阅读时长清零、奖励回收**等。
- 项目维护者**不承担任何责任**，不因任何人使用本项目或其衍生作品而产生的任何直接或间接损失负责。
- 使用者应当在充分了解上述风险的前提下，自行决定是否使用本项目。开始使用即视为已阅读并接受本免责声明的全部内容。
- 本项目不提供任何担保，包括但不限于可用性、稳定性、安全性的担保。
