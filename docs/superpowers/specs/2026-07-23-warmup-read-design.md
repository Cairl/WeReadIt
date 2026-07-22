# 预热首轮阅读 + 日志格式统一 设计文档

- 日期：2026-07-23
- 项目：WeReadIt（微信读书自动阅读脚本）
- 状态：已与用户确认，待审阅

## 1. 背景与问题

GitHub Actions 运行日志里，首次阅读必然出现一段"突兀的、不格式化的回显"：

```
阅读进度: 第 1/120 次，已阅读 0.0 分钟
2026-07-22 17:09:54,446 - INFO     - wereadit.core.reader - 第 1/120 次：阅读上下文未同步，已自动修复并重试
2026-07-22 17:09:55,072 - INFO     - wereadit.core.reader - 第 1/120 次：修复未生效，5s 后重试（连续 1/3 次）
阅读进度: 第 2/120 次，已阅读 0.5 分钟
阅读进度: 第 3/120 次，已阅读 1.0 分钟
```

问题归因（已读源码确认）：

1. **重试/修复只发生在首次阅读，且挤占第 1 次计数**：全新会话首读无 synckey，触发 `fix_no_synckey` + 内重试。这段"预热噪音"夹在 120 次正式阅读里，且同一 `index=1` 在一轮内被打印了 3 次（进度条 + 两条 INFO），措辞"修复未生效"之后其实第 2 次就成功，有误导。
2. **进度回显不格式化**：`阅读进度: ...` 由 `make_refresh_print()` 用裸 `print("\r...\033[K", ...)` 打到 stdout，**没有走 logging 框架**，因此没有 `时间 - 级别 - 模块名` 前缀。在 GitHub Actions（非 TTY）里成为一行突兀的裸文本，与周围带格式的 INFO 行不一致。

## 2. 目标

- 把"建立阅读上下文"的 fix/重试噪音隔离到一个**独立的预热阶段**，不计入 120 次正式阅读。
- 120 次正式阅读的日志保持干净、连续、可读。
- 进度回显与所有日志**同一格式**（带时间戳/级别/模块名）。
- 不改动任何保活策略语义与"禁止删除"的代码。

## 3. 非目标（YAGNI）

- 不改变 `read_num` 配置语义（仍是 120 次正式阅读）。
- 不重写推送、兑换、token 续期逻辑。
- 不引入 tqdm/rich 等进度条库（本次只做格式统一）。

## 4. 方案：A —— 预热阶段 + 进度回显走 logging

### 4.1 组件与职责

将 `reader.py` 中内联的"签名 → 发 read → 判 synckey → fix+重试"抽为可复用单元：

- **`_prepare_data(data, cfg, last_time) -> now`**
  - 保留全部保活字段逻辑：`data.pop("s", None)`、`b/c` 随机、`ct=now`、`rt=now-last_time`、`ts` 加 0~1000ms jitter、`rn` 随机、`sign_request(data, SIGN_KEY)`。
  - 返回 `now = int(time.time())`，供调用方维护 `last_time`。
- **`_read_once(client, cfg, data, last_time) -> tuple[ReadStatus, now]`**
  - `ReadStatus` 为 `enum.Enum`：`SYNCED` / `NO_SYNCKEY` / `COOKIE_EXPIRED`。
  - 调用 `_prepare_data` 发一次 read：
    - 无 `succ` → 返回 `(COOKIE_EXPIRED, now)`。
    - 有 `succ` 且含 `synckey` → 返回 `(SYNCED, now)`。
    - 有 `succ` 无 `synckey` → 调 `fix_no_synckey(client, cfg)`，**内重试一次**（重新 `_prepare_data`，`last_time` 不变）；成功 `(SYNCED, now2)`，否则 `(NO_SYNCKEY, now2)`。
- **`_warmup(client, cfg, data) -> last_time`**
  - 内部 `data = dict(DEFAULT_READ_DATA)` 独立创建，与主循环 `data` 互不影响；仅通过返回的 `last_time` 交接上下文。
  - 循环 `_read_once` 直到 `SYNCED`，**不计入 120 次**。
  - 日志统一带 `预热` 前缀，走 `logger`（格式化）。
  - 返回成功时的 `now` 作为主循环起始 `last_time`。
  - 熔断规则同主循环（见 4.3）。
- **`read_books(client, cfg) -> ReadResult`**（去掉 `refresh_print` 参数）
  - 先 `refresh_cookie(client, cfg)`（保留启动强制刷新）。
  - 再 `last_time = _warmup(...)`。
  - 主循环跑 `cfg.read_num`（120）次干净阅读。

### 4.2 数据流

- `read_num` 保持 120 不变。"120+1" = 120 正式 + 1 次不计数的预热首轮。
- 预热成功把 `last_time` 上下文交给主循环，保证第 1 次正式阅读的 `rt` 正确。
- `ReadResult` 新增字段：`warmup_done: bool`、`warmup_attempts: int`。
- 现有指标 `synckey_success` / `no_synckey_fix_triggered` / `fix_retry_success` / `cookie_refresh_count` / `circuit_breaker_triggered` 只统计主循环 120 次；预热那次成功读取不计入"已完成次数"。

### 4.3 错误处理

- `_warmup` 复用同一套熔断：
  - `NO_SYNCKEY` 连续 `MAX_NO_SYNCKEY`(3) 次 → 抛 `ReadFailedError`，文案带"预热阶段"标识。
  - `COOKIE_EXPIRED` 连续 `MAX_COOKIE_FAIL`(3) 次 → 抛 `CookieExpiredError`。
- 区别：失败**更早、带"预热"字样**，且不会污染 120 次的进度日志。
- 主循环若极端情况下仍遇 `NO_SYNCKEY`（上下文冷掉），保留 fix+重试兜底，行为同现在（保证健壮性）。

### 4.4 进度回显（解决"不格式化"）

- `read_books` 内进度打印由 `refresh_print(f"阅读进度: ...")` 改为：
  `logger.info("阅读进度: 第 %d/%d 次，已阅读 %.1f 分钟", index, total, (index - 1) * 0.5)`。
- 保留 `last_printed_index` 去重（fix 重试期间 index 不变，避免重复打印）。
- `app.py` 调用改为 `read_books(client, cfg)`（不再传 `refresh_print`）。
- `make_refresh_print` 与 `_RefreshSafeHandler` 暂保留函数不动（避免扩大改动面），但不再用于进度回显；后续可清理。

### 4.5 保活策略约束（绝对不能改）

重构仅移动/复用代码，**语义与禁止删除项全部保留**：
- 启动强制 `refresh_cookie` ✅
- 3 种 `COOKIE_DATA_VARIANTS` ✅
- `fix_no_synckey`、`FIX_SYNCKEY_BOOK_IDS=["3300060341"]` ✅
- `last_time = now - SECONDS_PER_READ`（伪造已读 30 秒）✅
- `data.pop("s")` ✅
- `DEFAULT_READ_DATA` 固定字段 ✅
- `time.sleep(READ_INTERVAL_SECONDS)`（30s）✅
- `baggage` Sentry 头、`keepalive-job`、DNS `8.8.8.8`、`cal_hash`/`encode_data` 算法均不在此改动范围 ✅
- 修改前自检问题："会不会影响 wr_skey 续期 / synckey 同步 / 上下文重建 / 风控规避？" → 均不影响。

## 5. 测试

- **新增预热单测**（`tests/test_reader.py` 或扩展现有）：
  - `_warmup` 首次即 synckey 成功，返回 `now`。
  - `_warmup` 首读无 synckey，fix 后重试成功。
  - `_warmup` 连续 `MAX_NO_SYNCKEY` 次失败 → 抛 `ReadFailedError`。
  - `_warmup` `COOKIE_EXPIRED` 连续超限 → 抛 `CookieExpiredError`。
- **新增主循环隔离测试**：mock 让上下文已建立，断言主循环 120 次**不触发** `fix_no_synckey`。
- **回归必须绿**：`pytest tests/ -v` 与 `ruff check src/ tests/`；现有 `tests/test_keepalive.py` 35 个保活回归测试全绿（重构未改保活语义）。

## 6. 预期日志效果（改造后）

```
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 需要阅读 120 次。
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 开始预热：建立阅读上下文（不计入阅读次数）
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 预热：阅读上下文未同步，已自动修复并重试
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 预热成功，上下文已建立。
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 阅读进度: 第 1/120 次，已阅读 0.0 分钟
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 阅读进度: 第 2/120 次，已阅读 0.5 分钟
...
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 阅读进度: 第 120/120 次，已阅读 59.5 分钟
2026-07-23 xx:xx:xx,xxx - INFO     - wereadit.core.reader - 阅读脚本已完成。
```

进度行带完整日志前缀，fix 噪音被隔离在"预热"段落，不再穿插 120 次正式阅读。

## 7. 实施步骤（概要，供 writing-plans 细化）

1. 在 `reader.py` 新增 `ReadStatus` 枚举、`_prepare_data`、`_read_once`、`_warmup`。
2. 重构 `read_books`：去掉 `refresh_print` 参数；先 `refresh_cookie` → `_warmup` → 主循环；进度改 `logger.info`。
3. `ReadResult` 增加 `warmup_done` / `warmup_attempts` 字段，`summary()` 一并输出。
4. `app.py` 调用改为 `read_books(client, cfg)`。
5. 补单测；跑 `pytest` + `ruff` 确认全绿。
