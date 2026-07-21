# 兑换 Token 自动续期修复设计

日期：2026-07-22
状态：已确认（三段设计均已获用户批准）

## 背景与问题

App 端 `skey`/`accessToken` 有效期不足 2 小时（服务端硬约束），而 GitHub Actions 每天仅运行一次，手动抓包的 `WEREAD_ANDROID_TOKEN` / `WEREAD_IOS_TOKEN` 到运行时必然过期，兑换报 `errcode=-2012`。

现有 `WEREAD_LOGIN_CURL` 重放路径（2026-07-21 上线）实测依然失败，失败环节未知（提取失败 / 请求被拒 / token 无效）。web wr_skey 复用路径已于 2026-07-21 实测证伪（wr_skey 完整值仅 8 位，与 App skey 属不同凭证体系，HTTP 401 / errcode=-2012）。

**已确认的事实地基**：

1. App token 寿命 < 2 小时，不可能延长。自动兑换的唯一出路是每次运行前自动换新 token。
2. `/login` 重放原理可行（社区实证）：请求 body 中的 `deviceId` 等长效设备凭证是换新 skey 的依据，抓一次 curl 可长期反复重放。前提是抓对那次请求且提取逻辑找对位置。
3. web 端无替代路径：两套凭证体系已证伪，且网页版无"时长换书币"入口。
4. 社区的终极全自动方案（手机端 quanx/Tasker 定时抓 skey 推送）依赖手机常开与付费 App，作为兜底记录在案，本期不实现。

**用户目标**：零手动维护的全自动托管 —— 自动兑换书币 + 凭证自动续期；不愿翻 Actions 日志，方案必须自带诊断与上报能力。

## 方案概述（方案 A）

修好 `/login` 重放 + 全链路自诊断。核心改动：自适应提取、配置体检、诊断直推、刷新时序前移、补刷保险、死代码清理。

## 架构与组件改动

### 1. `core/token_refresher.py`（核心重写）

- **`RefreshResult` 结构化返回**：`refresh_app_token` 返回 `RefreshResult(token: str | None, diagnosis: str)` 替代 `str | None`。成功时 diagnosis 为空；失败时 diagnosis 是人话诊断 + 下一步指引，可直接进推送。
- **自适应提取 `_find_token_in_json`**：递归遍历响应 JSON（任意嵌套，深度限 5 层），字段名候选 `skey` / `accessToken` / `access_token` / `token`；保留响应 header、Set-Cookie 两路兜底。
- **配置体检 `diagnose_login_curl`**：静态校验（不发请求）—— URL 是否为 `i.weread.qq.com/login`、body 是否含 `deviceId`、URL/body 是否解析得出。任一不过，返回具体修正指引。
- **网络重试**：请求异常时指数退避重试 2 次（5s、10s），共最多 3 次尝试。

### 2. `app.py`（编排调整）

- 刷新时机从"阅读约 60 分钟后、兑换前"挪到**阅读开始前**：体检 → 刷新 → 用 `dataclasses.replace(cfg, ...)` 按平台替换对应 token 字段生成新 cfg → 阅读 → 兑换。兑换时 token 年龄降到 60 分钟量级，远离 2 小时窗口边缘。
- 刷新状态/诊断汇总进推送内容。

### 3. `core/exchanger.py`（保险 + 适配）

- 适配 `RefreshResult`；删除内联刷新调用（token 改由 app.py 注入）。
- **补刷保险**：记录刷新时刻，兑换前若 token 年龄 > 90 分钟（READ_NUM 调大时）且 login curl 可用，补刷一次再兑。

### 4. 死代码清理

- 删除 `refresh_app_token_via_web` 及其全部测试（已实测证伪，保留会误导）。
- 同步更新 `token_refresher.py` 模块 docstring 与 AGENTS.md 中"瀑布式两条路径"的过时描述。

## 数据流

```
load_config
  → diagnose_login_curl（静态体检，不发请求）
      ├─ 不通过 → 记 warning，diagnosis 存下，跳过刷新，直接阅读
      └─ 通过 → refresh_app_token（阅读前，最多 3 次尝试）
            ├─ 成功 → replace(cfg, 平台对应 token 字段 = 新 token)
            └─ 失败 → 沿用原 cfg，diagnosis 存下
  → read_books（行为完全不变）
  → exchange_awards
      ├─ token 年龄 > 90min 且 login curl 可用 → 补刷一次
      ├─ 兑换成功 → 现有摘要
      └─ errcode=-2012 → 现有告警 + 附上 refresh 诊断
  → push（阅读结果 + 兑换结果 + 刷新状态/诊断）
```

## 错误处理

| 失败类型 | 判定 | 行为 | 推送中的指引 |
|---|---|---|---|
| 配置错误 | 体检不过（非 /login URL、缺 deviceId、解析失败） | 不重试，直接降级 | "curl 抓错了，正确抓法：..." |
| 网络错误 | 请求异常 | 退避重试 2 次（5s、10s） | 仍失败则"本次网络异常，明日自动重试" |
| 服务端拒绝 | HTTP 4xx 或响应 errcode | 不重试（重放无意义） | "login 凭证已失效，需重新抓包" + errcode |
| 结构未知 | 200 但递归提取不到 token | 不重试 | 响应键结构摘要 + "请把此信息反馈给开发者" |

**脱敏边界**：推送与日志中 token 只显示前 8 位；"结构未知"类诊断只打响应 JSON 的键路径与值类型（如 `data.user.skey:str`），不打完整值。

**行为变更**：刷新失败降级用原 token 时，兑换几乎必然 -2012（原 token 早已过期）。新推送文案改为**先呈现刷新诊断**（根因是刷新失败而非 token 本身），避免误导重新抓一个其实没问题的 curl。

## 测试策略

- `tests/test_token_refresher.py`（主要更新）
  - 递归提取：嵌套 `{"data": {"skey": ...}}`、三层嵌套、列表内嵌套、深度超限返回 None
  - `RefreshResult`：成功/各失败类型的 diagnosis 内容断言
  - `diagnose_login_curl`：正常 curl、非 /login URL、缺 deviceId、解析失败四分支
  - 网络重试：首次异常二次成功、三次全失败、重试间隔符合退避
  - 删除 `refresh_app_token_via_web` 的全部测试（4 个）
- `tests/test_exchanger.py`：适配 token 外部注入的新接口；补刷保险分支（token 年龄 > 90min 触发二次刷新）
- `tests/test_app.py`（若存在编排测试）：断言刷新发生在阅读之前、刷新失败时推送包含诊断
- 验收门槛：`pytest tests/` 全过 + `ruff check src/ tests/` 全过

## 文档同步

- README：抓包指引强化"必须抓到含 deviceId 的 /login"；新推送文案示例
- AGENTS.md：架构与关键设计决策更新
- changelog.md：按 cairl 规范三分类（新增/修复/优化）

## 范围外（本期不做）

- 方案 B（手机端定时抓取 + GitHub Secrets 自更新）：仅在 README 记录为兜底思路
- 本地调试 CLI 工具：诊断直推已覆盖"不翻日志"需求，YAGNI
- 兑换频次/时序的更大改动（如每周只兑一次）：与 token 问题正交，不混入本期

## 上线验证

合并后在 Actions 手动触发一次 `workflow_dispatch`，推送消息直接告知结果类别：成功则书币到账；失败则推送带诊断和下一步动作。

**预期管理**：修好后需最后再抓一次正确的 `/login` curl 配到 Secrets，之后只要不换设备、不重新登录微信，即可一直自动续期（准零手动）。
