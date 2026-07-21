# Secrets 配置傻瓜化 + 日志回显优化设计

日期：2026-07-22
状态：已确认（五段设计均已获用户批准）

## 背景与目标

兑换 Token 自动续期链路已验证成功（Actions run 29858555579：`/login` 重放 → 提取 skey → 平台校验 → 兑换书币成功）。在此基础上做配置傻瓜化与日志可读性优化，四件套：

1. **彻底删除手动 token**：`WEREAD_ANDROID_TOKEN` / `WEREAD_IOS_TOKEN` 从代码与文档中完全移除，兑换 token 完全由 `WEREAD_APP_CURL` 运行时刷新生成
2. **Secret 命名重构（无旧名兼容）**：`WEREAD_CURL_BASH` → `WEREAD_WEB_CURL`、`WEREAD_LOGIN_CURL` → `WEREAD_APP_CURL`
3. **配置检查按钮**：独立 workflow，静态体检 + 真实重放验证，结果推送
4. **日志回显优化**：进度重复打印修复 + synckey 常态日志简化

用户已确认的约束：不做旧名兼容；代码、README、AGENTS.md、changelog.md、deploy.yml、全部日志/推送文案不留旧名痕迹，一切按当前状态重写；git 提交历史与 `docs/superpowers/` 过程文档不动。

**用户需手动操作**：GitHub Secrets 删旧建新（`WEREAD_CURL_BASH` → `WEREAD_WEB_CURL`、`WEREAD_LOGIN_CURL` → `WEREAD_APP_CURL`，值不变；删除两个 token Secret）。

## 1. 配置模型（彻底删除版）

最终 Secret 清单：

| Secret | 状态 | 说明 |
|---|---|---|
| `WEREAD_WEB_CURL` | 必填 | 网页端 read 请求抓包，阅读 + `wr_vid` 来源 |
| `WEREAD_APP_CURL` | 推荐 | App 端 `/login` 抓包（含 deviceId），自动续期 + 平台自识别 |
| `PUSHPLUS` 等推送渠道 | 选填 | 配置检查结果推送所需 |

`Config` 改动：

- 删除 `weread_android_token` / `weread_ios_token` 字段，环境变量不再读取
- `curl_bash` 字段改名 `web_curl`；`weread_login_curl` 改名 `weread_app_curl`
- 新增运行时字段 `app_token: str = ""`（刷新注入的 token）、`app_token_key: str = ""`（命中字段名）
- `weread_access_token` property 改为直接返回 `app_token`
- `weread_platform` property 改为从 `app_token_key` 派生（`"skey"` → iOS，其余 → Android）
- exchanger 消费接口（`cfg.weread_access_token` / `cfg.weread_platform`）签名不变，零改动

`deploy.yml`：env 引用改新名，删除两个 token env 行。

## 2. 平台自识别流程（无种子版）

```
if cfg.weread_app_curl:                    # 门控只看 APP_CURL
    diagnose_login_curl 体检
      ├─ 不过 → 诊断存下（不刷新）
      └─ 通过 → refresh_app_token 重放
            ├─ 成功 → 按 token_key 注入：
            │         dataclasses.replace(cfg, app_token=token, app_token_key=token_key)
            │         日志/推送明示"平台自识别：iOS（依据响应字段 skey）"
            └─ 失败 → 诊断存下
→ 阅读 → 兑换（cfg.weread_access_token 为空则跳过兑换 + 诊断推送）
```

要点：

- 平台完全由 `token_key` 决定，无"种子错位校验"分支（整体删除 `_token_key_matches_platform` 与 `_replace_token` 旧实现）
- 刷新失败无降级：兑换跳过，推送呈现刷新诊断
- Android 双字段误判风险：防御为推送明示识别依据，误判需用户反馈后适配（iOS 实测命中 skey，Android 预期单字段 accessToken，风险可接受）
- `app.py` 中 platform_label、-2012 文案，以及 `token_refresher.py` 的 `diagnose_login_curl` 指引文案等所有引用旧 Secret 名处全部焕新

## 3. 配置检查按钮

新增 `.github/workflows/config-check.yml`（仅 `workflow_dispatch`），按钮位置：仓库页 → Actions → 左侧"WeReadIt 配置检查" → 右侧 "Run workflow"。

新增 `src/wereadit/config_check.py`，入口 `python -m wereadit.config_check`：

```
WEREAD_WEB_CURL
  ├─ 未配 → [异常] 阅读功能无法工作 + 抓包指引
  └─ 已配 → parse_curl 解析 → cookies 含 wr_skey、wr_vid → [正常] 显示 vid
WEREAD_APP_CURL
  ├─ 未配 → [异常] 兑换无法自动续期 + 抓包指引
  └─ 已配 → diagnose_login_curl 静态体检
      ├─ 不过 → [异常] 修正指引
      └─ 通过 → 真实重放一次 /login
          ├─ 成功 → [正常] 平台自识别 + token 前 8 位
          └─ 失败 → [异常] RefreshResult 诊断
推送渠道 → [正常] 显示检测到的渠道（未配则提示结果仅见日志）
运行参数 → [信息] 显示 READ_NUM、EXCHANGE_AWARD 当前值
```

推送文案样例：

```
WeReadIt 配置检查报告

[正常] WEREAD_WEB_CURL：解析成功，vid=200188697
[正常] WEREAD_APP_CURL：/login 重放成功，平台自识别为 iOS（依据响应字段 skey），token=5tUjfr38...
[正常] 推送渠道：pushplus
[信息] READ_NUM=120（约 60 分钟），EXCHANGE_AWARD=2,2,2,2,2,2,2,2

全部检查通过，托管就绪。
```

退出码：全部正常 0，任一异常 1。唯一发出的网络请求是 `/login` 重放验证（幂等无副作用）；不阅读、不兑换。

## 4. 日志回显优化

原则：行为逻辑一行不动（fix 调用、重试、退避、熔断阈值为保活红线），只动呈现。

**进度重复打印修复**：`refresh_print` 在 while 循环顶部，fix 退避后 `continue` 时 `index` 未变导致同一进度行二次打印。循环内记录上次打印的 `index`，仅在变化时打印。

**synckey 三行重构**：

| 场景 | 现状 | 新版 |
|---|---|---|
| 首次无 synckey，修复并重试 | `WARNING 无 synckey，尝试修复...` + `INFO fix_no_synckey 已调用，重试 read 接口...` | `INFO - 第 1/2 次：阅读上下文未同步，已自动修复并重试` |
| 修复后仍无 synckey，退避 | `WARNING 修复后重试仍无 synckey，退避 5s 后进入下一轮循环` | `INFO - 第 1/2 次：修复未生效，5s 后重试（连续 1/3 次）` |
| 连续失败第 2 次（逼近熔断） | 同上 | 同上内容，级别升 `WARNING` |
| 熔断（连续 3 次） | `ERROR 连续 3 次无 synckey，熔断退出。已完成 X/Y 次` | `ERROR - 连续 3 次修复无效，任务中止（已完成 X/Y 次）。通常是 cookie 失效或触发风控，请检查 WEREAD_WEB_CURL` |

要点：常态事件降为 INFO 一行；去掉 synckey/fix_no_synckey 实现术语；"第 X/Y 次"前缀与进度行对齐；连续次数保留（逼近熔断信号），2/3 升 WARNING。

**不动的部分**：cookie 过期刷新等其他日志；`ReadResult.summary()` 推送摘要。

## 5. 测试策略

- `test_config.py`（新增或更新）：新环境变量名读取、`app_token`/`app_token_key` 字段、`weread_access_token` 与 `weread_platform` 新派生逻辑
- `test_app.py`：门控只看 APP_CURL；无种子 + skey 注入 iOS、无种子 + accessToken 注入 Android；刷新失败且无 token 跳过兑换 + 诊断入推送；删除种子/错位相关旧用例
- `test_exchanger.py`：`_make_cfg` 适配新 Config 字段
- `test_config_check.py`（新）：各检查分支、报告格式、退出码
- reader 测试：进度打印去重（同 index 不重复打）、synckey 三处级别与文案断言
- 门槛：`pytest tests/` 与 `ruff check src/ tests/` 全过

## 文档范围

README（Secret 表精简、三步走配置流程、检查按钮指引、迁移说明）、AGENTS.md（架构/设计决策/Changelog 焕新）、changelog.md、deploy.yml、新建 config-check.yml —— 全部按当前状态写，无旧名痕迹。git 历史与 `docs/superpowers/` 过程文档不动。

## 上线验证

合并推送后：① 用户改 Secrets（删旧建新 + 删 token）；② Actions 点"WeReadIt 配置检查"→ Run workflow，推送报告应全 [正常]；③ 主 workflow 手动触发一次，验证阅读 + 兑换 + 新日志格式。
