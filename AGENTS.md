# AGENTS.md

## Project Overview

WeReadIt 是微信读书自动阅读脚本，通过模拟 `weread.qq.com` 的 `read` 接口刷阅读时长。支持 Cookie 自动刷新、兑换阅读奖励、多渠道推送结果。在 GitHub Actions 上定时运行。

代码思路来源于 [findmover/wxread](https://github.com/findmover/wxread)，在此版本上进行了重构和功能扩展。

## Setup Commands

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt   # 开发依赖（pytest + ruff）
```

## Development Workflow

```bash
python main.py       # 直接运行
python -m wereadit   # 模块方式运行
```

环境变量配置见 README.md。本地调试可直接编辑 `src/wereadit/constants.py` 中的 `DEFAULT_HEADERS` / `DEFAULT_COOKIES`。

## Testing Instructions

```bash
pytest tests/                          # 全部单元测试
pytest tests/ -v                       # 详细输出
pytest tests/test_keepalive.py -v      # 保活策略回归测试
ruff check src/ tests/                 # 代码检查
```

## Code Style

- Python 3.10+，类型注解使用 `from __future__ import annotations`
- lint: ruff，配置在 `pyproject.toml`
- 命名：文件名下划线，类名 PascalCase，函数/变量 snake_case
- 异常：项目异常定义在 `wereadit/exceptions.py`，禁止 `except:` 裸捕获

## Architecture

```
src/wereadit/
├── app.py          # 编排入口：阅读 -> 兑换 -> 推送
├── config_check.py # 配置检查入口（静态体检 + /login 重放验证，供 config-check.yml 调用）
├── config.py       # Config frozen dataclass + property 派生（平台从 app_token_key 自识别）
├── constants.py    # URL、加密盐、默认值、平台常量（带【保活策略】注释）
├── core/
│   ├── reader.py   # 阅读循环 + Cookie 刷新 + 熔断 + fix 后重试
│   ├── exchanger.py # 奖励兑换（token 外部注入 + 年龄超 90 分钟补刷保险）
│   └── token_refresher.py # App 端 Token 续期（/login 重放 + 配置体检 + 四分类诊断）
├── infra/
│   ├── http.py     # HttpClient（Session 复用 TCP，cookies 业务层独占）
│   └── curl_parser.py # cURL 命令解析（parse_curl + parse_curl_full）
├── push/           # 推送渠道（策略模式 + @register 注册表）
│   ├── base.py
│   ├── registry.py
│   └── {pushplus,wxpusher,telegram,serverchan}.py
├── utils/          # crypto, logging
└── schemas/        # books.json（书籍/章节列表）
```

新增推送渠道：在 `push/` 下新建文件，用 `@register("xxx")` 装饰 `Pusher` 子类即可，无需改动主流程。

## Key Design Decisions

- **Config 是 frozen dataclass**：加载后不可变，推送渠道通过 @property 自动检测；兑换 token 不走环境变量，由 app.py 阅读前 /login 重放刷新后 dataclasses.replace 注入 app_token/app_token_key，weread_access_token/weread_platform 两 property 从其派生（skey → iOS）。
- **推送注册表**：`push/registry.py` 用装饰器注册代替 if-elif 链。
- **Cookie 刷新**：失效后自动调 `login/renewal` 接口刷新，3 种 payload 变体 + 多轮重试 + 指数退避，提高网络抖动下的恢复能力。
- **熔断机制**：连续 `MAX_NO_SYNCKEY=3` 次无 synckey 抛 `ReadFailedError`，连续 `MAX_COOKIE_FAIL=3` 次 cookie 失败抛 `CookieExpiredError`，避免死循环卡死 GitHub Actions。app.py 捕获后推送告警。
- **fix 后重试 read**：`fix_no_synckey` 后立即重试一次 read（重新签名），成功则计入本次进度，不丢失阅读请求。
- **HttpClient cookies 业务层独占**：`Session` 仅用于 TCP 复用，cookies 存在 `self._cookies` 字典，每次请求显式传 `cookies=`，避免服务器 `Set-Cookie` 自动覆盖业务层 `wr_skey[:8]` 截断。
- **兑换重试**：指数退避，最多 3 次，token 过期会抛出 `CookieExpiredError` 并推送通知。
- **兑换 Token 自动续期**：App 端 skey/accessToken 有效期仅约 2 小时。`token_refresher.py` 重放 `i.weread.qq.com/login`（body 中 deviceId 为长效凭证，抓包一次可长期重放）换新 token；`app.py` 在**阅读开始前**刷新并用 `dataclasses.replace` 注入新 cfg（兑换时 token 年龄远离 2 小时边缘）；`exchanger.py` 删除内联刷新，改收 `refresher` 回调，token 年龄 > `TOKEN_MAX_AGE_SECONDS`（90 分钟）时补刷。刷新结果 `RefreshResult` 带四分类人话诊断（配置/网络/服务端拒绝/结构未知），随推送直发。响应 token 位置未公开，递归提取 JSON（深度限 5 层）+ header + Set-Cookie 三路兜底。web wr_skey 复用路径 2026-07-21 已实测证伪（与 App skey 不同体系），勿恢复。

## Keepalive Strategy

本项目基于 wxread 重构，**完整保留了 21 项保活策略**。修改任何保活相关代码前必读：

- `wxread_keepalive_analysis.md` — 13 类保活策略深度分析
- `wxread_keepalive_improvement_plan.md` — 改进计划与实施记录
- `tests/test_keepalive.py` — 35 个保活策略回归测试（修改后必跑）

**绝对不要删除的代码**（即使看起来无业务意义）：启动强制 `refresh_cookie`、3 种 `COOKIE_DATA_VARIANTS`、`fix_no_synckey`、`FIX_SYNCKEY_BOOK_IDS=["3300060341"]`、`last_time = now - 30`、`data.pop("s")`、`DEFAULT_READ_DATA` 固定字段、`time.sleep(30)`、`baggage` Sentry 头、`keepalive-job`、DNS `8.8.8.8`、`cal_hash`/`encode_data` 算法。

修改前先问："这个改动会不会影响 wr_skey 续期 / synckey 同步 / 上下文重建 / 风控规避？" 如果答案是"可能"，就不要改。

## CI/CD

GitHub Actions 定时触发（`deploy.yml`），北京时间每天 00:00 运行。环境变量通过 Secrets/Variables 注入，详见 README。

## Changelog

### 添加

- **配置检查 workflow**: 新增 `config-check.yml`（手动触发）与 `config_check.py` 入口：WEREAD_WEB_CURL 解析与关键 cookie 校验、WEREAD_APP_CURL 静态体检 + 真实 /login 重放验证（平台自识别 + token 前 8 位）、推送渠道检测、READ_NUM/EXCHANGE_AWARD 显示；报告直推手机，退出码反映整体状态。
- **兑换 Token 续期自诊断**: `token_refresher.py` 重写为 `RefreshResult` 结构化返回（token + 命中字段名 + 人话诊断）；新增 `diagnose_login_curl` 静态体检（非 /login、缺 deviceId 提前给修正指引）；响应 token 递归提取（任意嵌套，深度限 5 层）替代顶层猜测；网络异常/HTTP 5xx 指数退避重试 3 次；刷新诊断随推送直发，无需翻 Actions 日志。
- **兑换 Token 自动续期**: 新增 `token_refresher.py`，提供两条续期路径：① 重放 App 端 `/login` 请求（需配置 `WEREAD_APP_CURL`，但 skey 刷新请求难抓）② 通过 web 端 `login/renewal` 获取 `wr_skey` 完整值尝试作为 App skey（全自动，无需手动操作）；`exchanger.py` 兑换前按瀑布式依次尝试两条路径，全失败降级用原 token；`curl_parser.py` 新增 `parse_curl_full` 解析 URL+body。
- **兑换 Token 过期排查日志**: `exchanger.py` 与 `app.py` 在兑换流程记录 token 前 8 位/平台/HTTP 状态码/errcode/响应体，token 过期告警中明确平台标识与 token 前 8 位，便于对应 GitHub Secrets 定位是自然失效还是风控作废。

### 修复

- **阅读日志回显**: 进度打印去重（fix 退避后同一进度不再重复输出）；synckey 常态日志三行合并为一行人话（INFO），连续失败 2/3 次升 WARNING，熔断文案保留排查信息；不含 synckey/fix_no_synckey 实现术语。
- **兑换 Token 刷新时机**: 刷新从"阅读 60+ 分钟后、兑换前"挪到阅读开始前，兑换时 token 年龄远离 2 小时有效期边缘；`exchanger.py` 新增补刷保险（token 年龄 > 90 分钟时兑换前再刷一次）；iOS/Android 平台错位配置（iOS curl 配 Android token）会被校验拦截并提示。（2026-07-22 起由平台自识别取代）
- **synckey 修复流程日志**: `reader.py` 中 `fix_no_synckey` 后重试失败分支无日志输出，补 `INFO - fix_no_synckey 已调用，重试 read 接口...` 与 `WARNING - 修复后重试仍无 synckey，退避 Xs 后进入下一轮循环`；同步去掉"（连续第 X/Y 次）""（第 X 次修复）"等冗余次数回显。
- **兑换奖励 Token 过期告警**: `app.py` 区分 `ExchangeError(errcode==-2012)`，Token 过期时 `exit_code=1` 且推送以 `is_success=False` 发送，消息明确提示重新抓包更新 `WEREAD_APP_CURL`（兑换 Token 由其自动生成）；`exchanger.py` 查询与兑换均 re-raise Token 过期异常，不再被 `except Exception` 兜底静默吞掉或以成功状态推送。
- **Secret 旧名兼容**: `config.py` 新增 `_env_renamed()`，PUSHPLUS / WXPUSHER / SERVERCHAN 新名优先，旧名（`PUSHPLUS_TOKEN` / `WXPUSHER_SPT` / `SERVERCHAN_SPT`）作为 fallback 仍可读取，命中旧名发 deprecated 警告，老用户升级后推送不再因 Secret 名不匹配而静默失效。
- **配置检查不再视推送为异常**: 移除 `config_check.py` 的 `_check_push`，推送渠道为可选项，未配置时不再判为 `[异常]`、不再影响退出码（仍为 0）；配置了则照常推送报告，未配置则仅见日志。

### 优化

- **Secret 精简改名（无旧名兼容）**: `WEREAD_CURL_BASH`→`WEREAD_WEB_CURL`、`WEREAD_LOGIN_CURL`→`WEREAD_APP_CURL`；`WEREAD_ANDROID_TOKEN`/`WEREAD_IOS_TOKEN` 彻底删除（兑换 token 完全由 /login 重放生成，平台自识别）；刷新失败且无 token 时跳过兑换并以失败状态推送（exit_code=1），不再用必然过期的种子 token 空跑一次。
- **删除已证伪的 web wr_skey 复用路径**: 移除 `refresh_app_token_via_web` 及其 4 个测试（07-21 实测 wr_skey 与 App skey 不同体系，保留会误导）。
- **重试次数统一为 3 次**: `MAX_NO_SYNCKEY` 5→3（无 synckey 熔断阈值）、`REFRESH_COOKIE_MAX_ROUNDS` 2→3（cookie 刷新重试轮数）；`PUSH_MAX_ATTEMPTS` / `EXCHANGE_MAX_RETRY` / `MAX_COOKIE_FAIL` 已是 3 不变。cookie 刷新总耗时从 ~36s 增至 ~96s（多一轮 60s 退避）。

## Pull Request Guidelines

- 标题格式：`[模块] 简要描述`，如 `[push] 新增钉钉推送渠道`
- 提交前确保 `pytest tests/` 和 `ruff check src/ tests/` 通过
