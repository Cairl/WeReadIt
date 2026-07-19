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

确认返回：

```json
{"succ":1}
```

复制该请求为 **cURL (Bash)**。

### 2. 配置 GitHub Secrets

仓库进入 **Settings → Secrets and variables → Actions**。

**必填**

| Secret             | 说明                   |
| ------------------ | ---------------------- |
| `WEREAD_CURL_BASH` | 上一步复制的 cURL 命令 |

**推送（按需配置）**

| Secret                                    | 渠道       |
| ----------------------------------------- | ---------- |
| `PUSHPLUS_TOKEN`                          | PushPlus   |
| `WXPUSHER_SPT`                            | WxPusher   |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram   |
| `SERVERCHAN_SPT`                          | ServerChan |

如果同时配置多个推送渠道，将按以下顺序选择：

```
PushPlus → WxPusher → Telegram → ServerChan
```

**兑换奖励（按需配置）**

| Secret                 | 说明                  |
| ---------------------- | --------------------- |
| `WEREAD_ANDROID_TOKEN` | Android `accessToken` |
| `WEREAD_IOS_TOKEN`     | iOS `skey`            |

### 3. 配置 Variables（可选）

| Variable         | 默认值            | 说明                                           |
| ---------------- | ----------------- | ---------------------------------------------- |
| `READ_NUM`       | `120`             | 阅读次数（120 次 ≈ 60 分钟）                   |
| `EXCHANGE_AWARD` | `2,2,2,2,2,2,2,2` | 奖励兑换策略，`0`=不兑换，`1`=体验卡，`2`=书币 |

### 4. 运行

推送到 GitHub 后即可运行。

默认每天 **北京时间 00:00** 自动执行，支持在 GitHub Actions 页面手动触发。

---

## 奖励兑换

网页 Cookie 无法调用奖励兑换接口，需要使用微信读书 App 的认证 Token。

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

> Token 通常有效数天，过期后需重新抓包。`wr_vid` 会自动从网页 Cookie 获取，无需额外配置。

## 本地运行

```bash
git clone <repository>
cd WeReadIt

pip install -r requirements.txt

export WEREAD_CURL_BASH='curl ...'

# 可选
export PUSHPLUS_TOKEN=xxxx

python main.py
```

或直接修改 `src/wereadit/constants.py` 中的默认请求头和 Cookie 进行本地调试。

## 配置说明

| 配置项           | 默认值            | 说明                      |
| ---------------- | ----------------- | ------------------------- |
| `READ_NUM`       | `120`             | 默认阅读约 60 分钟        |
| `EXCHANGE_AWARD` | `2,2,2,2,2,2,2,2` | 默认全部兑换书币          |
| 默认书籍         | 《三体》          | 可自行修改                |
| Cookie           | 自动刷新          | 失效后自动更新            |
| 推送失败         | 最多重试 5 次     | 每次间隔 3～6 分钟        |
| 挑战赛模式       | `READ_NUM=2`      | 阅读约 1 分钟即可完成签到 |

## 项目结构

```text
WeReadIt/
├── main.py
├── src/
│   └── wereadit/
│       ├── core/         # 阅读、奖励兑换
│       ├── infra/        # HTTP、cURL 解析
│       ├── push/         # 推送渠道
│       ├── utils/        # 工具函数
│       ├── config.py
│       └── app.py
├── tests/
└── requirements*.txt
```

新增推送渠道只需在 `src/wereadit/push/` 下新增实现，并使用：

```python
@register("your_channel")
```

即可自动注册，无需修改主流程。

## 开发

```bash
pip install -r requirements-dev.txt

pytest

ruff check src tests
```

## 保活策略说明

本项目基于 wxread 重构,**完整保留了 21 项保活策略**。详见 [`wxread_keepalive_analysis.md`](./wxread_keepalive_analysis.md)。

### 绝对不要删除的代码

即使看起来"没有实际业务意义",以下代码都是保活机制,**删除会导致长期运行失效**:

| 代码 | 位置 | 作用 |
|------|------|------|
| 启动强制 `refresh_cookie()` | `core/reader.py` | 上线握手,告知服务器客户端在线 |
| 3 种 `COOKIE_DATA_VARIANTS` | `constants.py` | 应对 renewal 接口版本变化 |
| `fix_no_synckey()` | `core/reader.py` | 调 chapterInfos 触发服务器重建上下文 |
| `FIX_SYNCKEY_BOOK_IDS = ["3300060341"]` | `constants.py` | 写死的特殊 bookId,触发上下文重建 |
| `last_time = now - SECONDS_PER_READ` | `core/reader.py` | 伪造"已读 30 秒" |
| `data.pop("s")` | `core/reader.py` | 删除上次签名,防止用旧 s |
| `DEFAULT_READ_DATA` 固定字段(ci/co/sm/pr/ps/pc) | `constants.py` | 上下文指纹,实测必须固定 |
| `time.sleep(READ_INTERVAL_SECONDS)` | `core/reader.py` | 30 秒固定节奏,调快触发风控 |
| `baggage` Sentry 头 | `constants.py` | 浏览器指纹,真实浏览器才有 |
| `keepalive-job` | `.github/workflows/deploy.yml` | 防 GitHub Actions 60 天自动禁用 |
| `DNS 8.8.8.8` | `.github/workflows/deploy.yml` | 基础设施保活,解决 DNS 解析 |
| `cal_hash` / `encode_data` 混淆变量名 | `utils/crypto.py` | 服务器签名校验,不能改算法 |

### 修改前必读

修改任何保活策略相关代码前,请先阅读 [`wxread_keepalive_analysis.md`](./wxread_keepalive_analysis.md) 对应章节,确认改动不会破坏:

- wr_skey 续期机制
- synckey 状态同步
- 服务器上下文重建
- 风控规避

改进计划与已实施记录见 [`wxread_keepalive_improvement_plan.md`](./wxread_keepalive_improvement_plan.md)。

## 致谢

项目灵感及部分代码实现参考自 [findmover/wxread](https://github.com/findmover/wxread)，由衷感谢原作者技术支持。

## 免责声明

- 本项目**仅供学习交流**，严禁用于商业用途或任何违反微信读书服务条款的行为。
- 本项目与**微信读书（weread.qq.com）及其运营方腾讯公司无任何关联**，未获得其任何形式的授权或认可。
- 使用本项目可能违反微信读书用户协议，包括但不限于禁止使用自动化脚本的规定。使用者应自行承担由此产生的全部风险和后果，包括但不限于 **账号封禁、阅读时长清零、奖励回收**等。
- 项目维护者**不承担任何责任**，不因任何人使用本项目或其衍生作品而产生的任何直接或间接损失负责。
- 使用者应当在充分了解上述风险的前提下，自行决定是否使用本项目。开始使用即视为已阅读并接受本免责声明的全部内容。
- 本项目不提供任何担保，包括但不限于可用性、稳定性、安全性的担保。
