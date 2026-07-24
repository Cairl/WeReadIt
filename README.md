# WeReadIt

微信读书自动阅读脚本。

通过模拟微信读书 Web `read` 接口完成阅读，支持自动刷新 Cookie、自动兑换每周奖励、多渠道消息推送，适合 GitHub Actions 或服务器长期运行。

## 功能特性

- 自动阅读（默认 60 分钟，可配置）
- Cookie 自动刷新，部署后长期运行
- 自动兑换每周阅读奖励（书币 / 无限卡，可选）
- 支持 PushPlus、WxPusher、Telegram、ServerChan、Bark 推送
- 支持 GitHub Actions、服务器定时任务

## 快速开始

### 1. 配置 Secrets and Variables

仓库进入 **Settings → Secrets and variables → Actions**。

| 配置项             | 类型     | 要求 | 默认值             | 说明                                                     |
| ------------------ | -------- | ---- | ------------------ | -------------------------------------------------------- |
| `WEREAD_WEB_CURL`  | Secret   | 必填 | -                  | 网页端 read 请求 cURL（第 1 步复制），阅读与 `wr_vid` 来源 |
| `WEREAD_APP_CURL`  | Secret   | 推荐 | -                  | App 端 `/login` 请求 cURL（body 须含 `deviceId`），兑换 Token 全自动续期与平台自识别 |
| `READ_NUM`         | Variable | 选填 | `120`              | 阅读次数（120 次 ≈ 60 分钟）                              |
| `EXCHANGE_AWARD`   | Variable | 选填 | `2,2,2,2,2,2,2,2` | 兑换策略：`0`=不兑换，`1`=体验卡，`2`=书币                 |

### 2. 运行

推送到 GitHub 后即可运行。

默认每天 **北京时间 00:00** 自动执行，支持在 GitHub Actions 页面手动触发。

### 3. 配置推送

| 配置项               | 类型   | 要求 | 默认值 | 说明                                    |
| -------------------- | ------ | ---- | ------ | --------------------------------------- |
| `PUSHPLUS`           | Secret | 选填 | -      | PushPlus 推送 token                     |
| `WXPUSHER`           | Secret | 选填 | -      | WxPusher 推送 token                     |
| `TELEGRAM_BOT_TOKEN` | Secret | 选填 | -      | Telegram Bot token（需同时配置 CHAT_ID） |
| `TELEGRAM_CHAT_ID`   | Secret | 选填 | -      | Telegram 会话 ID（需同时配置 BOT_TOKEN） |
| `SERVERCHAN`         | Secret | 选填 | -      | ServerChan 推送 token                   |
| `BARK_PUSHER`        | Secret | 选填 | -      | Bark 推送完整 URL（如 https://api.day.app/<key>） |

## 配置（三步走）

### 第 1 步：配置 WEREAD_WEB_CURL（阅读必需）

1. 浏览器登录 [微信读书网页版](https://weread.qq.com/)。
2. F12 打开开发者工具，进入 Network，随便翻开一本书。
3. 找到 `https://weread.qq.com/web/book/read` 请求，右键 → Copy → Copy as cURL (Bash)。
4. 配置到 Secret `WEREAD_WEB_CURL`。

> 网页 cookie 会自动续期，配一次长期有效。

### 第 2 步：配置 WEREAD_APP_CURL（兑换必需）

1. 杀掉微信读书 App 重新打开（冷启动会触发 /login 刷新请求）。
2. 用抓包工具捕获 `i.weread.qq.com/login` 请求，确认请求 body 中含 `deviceId`（长效设备凭证，缺了它重放必然失败）。
3. 将该请求复制为 cURL (Bash) 格式。
4. 配置到 Secret `WEREAD_APP_CURL`。

> 脚本每次运行会在阅读开始前自动重放 `/login` 刷新兑换 Token，平台（iOS/Android）从响应字段自动识别，无需任何其他配置。抓一次长期有效（不换设备、不重新登录即可）。

### 第 3 步：验证配置（配置检查按钮）

GitHub 仓库页 → 顶栏 `Actions` → 左侧选 **WeReadIt 配置检查** → 右侧 **Run workflow** → 几分钟后推送收到检查报告。全部 `[正常]` 即托管就绪；有 `[异常]` 则按报告内指引修正后重新检查。

### 迁移说明（2026-07-22 之前的老用户）

Secret 已改名并精简：

1. `WEREAD_CURL_BASH` → 改名 `WEREAD_WEB_CURL`（值不变，删旧建新）。
2. `WEREAD_LOGIN_CURL` → 改名 `WEREAD_APP_CURL`（值不变，删旧建新）。
3. `WEREAD_ANDROID_TOKEN` / `WEREAD_IOS_TOKEN` → 直接删除（兑换 Token 现已完全由 `WEREAD_APP_CURL` 自动生成）。

改完后点一次「配置检查」验证（见第 3 步）。

> 注意：改名完成前，兑换将静默跳过（阅读不受影响），以配置检查报告为准。

> 兜底思路（本项目未实现）：若 `/login` 重放被服务端彻底关闭，社区方案是手机端用 Quantumult X / 快捷指令定时拦截 App 的 skey 并调用 GitHub API 更新 Secrets。依赖手机常开与抓包 App，仅作为最后手段记录在案。

## 本地运行

```bash
git clone <repository>
cd WeReadIt

pip install -r requirements.txt

export WEREAD_WEB_CURL='curl ...'

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
