# WeReadIt

微信读书自动阅读脚本。通过模拟官网 `read` 接口刷阅读时长与挑战赛保持天数，支持自动刷新 Cookie、兑换阅读奖励、多渠道推送结果。

## 功能特性

- 自动阅读，默认 20 分钟（可配置）
- Cookie 自动刷新，一次部署长期运行
- 自动兑换每周阅读奖励（书币/体验卡，可选）
- 支持 PushPlus / WxPusher / Telegram / ServerChan 四种推送
- 适合 GitHub Actions 或服务器定时运行

## 快速开始

### 1. 抓包 read 接口

在 [微信读书官网](https://weread.qq.com/) 搜索任意书籍（推荐《三体》），打开阅读并翻页。用浏览器开发者工具抓到 `read` 接口 `https://weread.qq.com/web/book/read`，确认返回：

```json
{"succ": 1, "synckey": 564589834}
```

右键复制该请求为 cURL (Bash) 格式备用。

### 2. 配置 GitHub Secrets / Variables

在仓库 **Settings -> Secrets and variables -> Actions** 中配置：

**Secrets（必填）**

| key | 说明 |
| --- | --- |
| `WEREADIT_CURL_BASH` | 上一步复制的 cURL 命令 |
| `PUSH_METHOD` | 推送渠道：`pushplus` / `wxpusher` / `telegram` / `serverchan` |

**Secrets（按推送渠道填）**

| key | 适用渠道 | 获取地址 |
| --- | --- | --- |
| `PUSHPLUS_TOKEN` | pushplus | https://www.pushplus.plus/uc.html |
| `WXPUSHER_SPT` | wxpusher | https://wxpusher.zjiecode.com/docs/#/?id=获取spt |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | telegram | BotFather 创建机器人 |
| `SERVERCHAN_SPT` | serverchan | https://sct.ftqq.com/sendkey |

**Variables（可选）**

| key | 默认值 | 说明 |
| --- | --- | --- |
| `READ_NUM` | `40` | 阅读次数（每次 30 秒，40 次 = 20 分钟） |
| `WEREAD_PLATFORM` | `android` | 兑换平台：`android` / `ios` |
| `EXCHANGE_AWARD` | `2,2,2,2,2,2,2,2` | 兑换策略，8 位逗号分隔，`0`=不兑/`1`=体验卡/`2`=书币 |

**Secrets（兑换功能，可选）**

| key | 说明 |
| --- | --- |
| `WEREAD_ACCESS_TOKEN` | APP 端认证 token，未配置则跳过兑换。Android 填 `accessToken`，iOS 填 `skey` |

> iOS 用户建议设置 `WEREAD_PLATFORM=ios`，否则领取的书币无法在 iOS APP 使用。

### 3. 运行

推送到 GitHub 即可。默认每天北京时间 01:00 自动运行，也可在 Actions 页面手动触发。

## 兑换奖励的 token 抓包

兑换功能需要 APP 端认证 token（网页 cookie 调不通该接口）：

1. 安卓用 Reqable / HTTP Canary，iOS 用 Stream / ProxyPin
2. 安装 CA 证书后启动抓包
3. 打开微信读书 APP 正常使用
4. 在抓包工具里按域名 `i.weread.qq.com` 筛选任意请求
5. Android 查看请求头 `accessToken`，iOS 查看 `skey`
6. 配置到 Secret `WEREAD_ACCESS_TOKEN`，并在 Variables 设置 `WEREAD_PLATFORM`

token 有有效期（实测数天），过期后兑换功能会推送「登录超时」通知，重新抓包即可。`vid` 无需单独配置，脚本会从网页 cookie 的 `wr_vid` 自动提取。

## 本地运行

```bash
git clone <仓库地址>
cd WeReadIt
pip install -r requirements.txt

# 通过环境变量配置（推荐）
export WEREADIT_CURL_BASH='curl ...'
export PUSH_METHOD=pushplus
export PUSHPLUS_TOKEN=xxx
python main.py
```

也可直接编辑 `src/wereadit/constants.py` 中的 `DEFAULT_HEADERS` 和 `DEFAULT_COOKIES`，省去环境变量。

## 项目结构

```
WeReadIt/
├── main.py                    # 入口（兼容 python main.py）
├── src/wereadit/
│   ├── app.py                 # 编排：阅读 -> 兑换 -> 推送
│   ├── config.py              # 配置加载（Config dataclass）
│   ├── constants.py           # URL / 加密盐 / 默认值
│   ├── core/
│   │   ├── reader.py          # 阅读循环 + cookie 刷新
│   │   └── exchanger.py       # 奖励兑换
│   ├── infra/
│   │   ├── http.py            # HttpClient（requests.Session 封装）
│   │   └── curl_parser.py     # cURL 命令解析
│   ├── push/                  # 推送渠道（策略模式 + 注册表）
│   │   ├── base.py
│   │   ├── registry.py
│   │   └── {pushplus,wxpusher,telegram,serverchan}.py
│   ├── utils/{crypto,logging}.py
│   └── schemas/books.json     # 书籍/章节列表
└── tests/                     # 单元测试
```

新增推送渠道只需在 `src/wereadit/push/` 下新建一个文件，用 `@register("xxx")` 装饰即可，无需改动主流程。

## 开发

```bash
pip install -r requirements-dev.txt
pytest tests/                 # 37 个单元测试
ruff check src/ tests/        # 代码风格检查
```

## 注意事项

- 只需完成挑战赛签到可将 `READ_NUM` 设为 `2`（1 分钟）
- 默认阅读《三体》，换其他书籍自行测试时长是否累计
- Cookie 失效后脚本会自动刷新，刷新失败会推送通知
- 推送失败有 5 次重试，间隔 3-6 分钟随机

## 许可

仅供学习交流使用，请勿用于商业用途。
