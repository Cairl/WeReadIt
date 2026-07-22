# 预热首轮阅读 + 日志格式统一 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"建立阅读上下文"的 fix/重试噪音隔离到独立预热阶段（不计入 120 次），并将进度回显改为走 logging 框架以统一日志格式。

**Architecture:** 在 `reader.py` 中抽 `_prepare_data` / `_read_once` / `_warmup` 三个复用单元；`read_books` 改为 `refresh_cookie` → `_warmup`（返回 `last_time`，不计数）→ 主循环 120 次干净阅读；进度回显由裸 `print` 改为 `logger.info`。`ReadResult` 增加 `warmup_done` / `warmup_attempts`。`app.py` 改用 `setup_logging()` 配置日志。

**Tech Stack:** Python 3.10+，pytest，ruff，标准库 `enum` / `logging`。

## Global Constraints

- 启动强制 `refresh_cookie` 不能删。
- 3 种 `COOKIE_DATA_VARIANTS` 不能简化为 1 种。
- `fix_no_synckey`、`FIX_SYNCKEY_BOOK_IDS=["3300060341"]` 不能删。
- `last_time = now - SECONDS_PER_READ`（伪造已读 30 秒）不能删。
- `data.pop("s")` 每次循环开头必须执行。
- `DEFAULT_READ_DATA` 固定字段（ci/co/sm/pr/ps/pc）不能改。
- `time.sleep(READ_INTERVAL_SECONDS)`（30s）不能调快。
- `read_num` 保持 120 不变；预热那次成功读取不计入 `completed_count` / `synckey_success` / `fix_retry_success`。
- 进度回显必须走 `logging`（带 `时间 - 级别 - 模块名` 格式），不得再用裸 `print` / `refresh_print`。
- 正常 `main()` 流程日志 handler 必须由 `setup_logging()` 安装（`make_refresh_print` 的 handler 副作用不可直接删除，改为调用 `setup_logging()`）。
- 命名：函数/变量 snake_case，类 PascalCase；Python 3.10+ 类型注解用 `from __future__ import annotations`；`ruff` 通过。

---

## File Structure

- Modify: `src/wereadit/models.py` — `ReadResult` 增加 `warmup_done` / `warmup_attempts` 字段，`summary()` 展示。
- Modify: `src/wereadit/core/reader.py` — 新增 `ReadStatus` 枚举与 `_prepare_data` / `_read_once` / `_warmup`；重写 `read_books`（去掉 `refresh_print` 参数，进度走 logger）。`refresh_cookie` / `_get_wr_skey` / `fix_no_synckey` 保持不变。
- Modify: `src/wereadit/app.py` — `main()` 改用 `setup_logging()`，调用 `read_books(client, cfg)`。
- Rewrite: `tests/test_reader_logs.py` — 断言新日志文案（"预热：" 前缀）与进度走 logger。
- Modify: `tests/test_keepalive.py` — 调整受预热影响的 7 个测试的 mock 序列与断言（其余 28 个不变）。
- 保活策略测试文件 `tests/test_keepalive.py` 整体仍须全绿。

---

## Task 1: ReadResult 增加预热字段

**Files:**
- Modify: `src/wereadit/models.py:13-41`

**Interfaces:**
- 产生：`ReadResult.warmup_done: bool`、`ReadResult.warmup_attempts: int`，供 `reader.read_books` 填充、`app.py` 推送摘要展示。

- [ ] **Step 1: 写失败测试**

在 `tests/` 下新建 `tests/test_read_result_warmup.py`：

```python
from wereadit.models import ReadResult


def test_read_result_has_warmup_fields() -> None:
    r = ReadResult(
        completed_count=120,
        total_minutes=60.0,
        warmup_done=True,
        warmup_attempts=2,
    )
    assert r.warmup_done is True
    assert r.warmup_attempts == 2
    assert "预热" in r.summary()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_read_result_warmup.py -v`
Expected: FAIL（`ReadResult.__init__() got an unexpected keyword argument 'warmup_done'`）

- [ ] **Step 3: 修改 ReadResult**

`src/wereadit/models.py` 的 `ReadResult` 改为：

```python
@dataclass
class ReadResult:
    """阅读循环执行结果。

    包含运行 metrics,用于推送时展示账号健康度。
    """
    # 基础结果
    completed_count: int  # 成功完成的阅读次数
    total_minutes: float  # 累计阅读时长（分钟）

    # 运行 metrics（用于评估账号健康度与保活策略生效情况）
    synckey_success: int = 0  # synckey 成功次数(直接成功 + fix 后重试成功)
    no_synckey_fix_triggered: int = 0  # fix_no_synckey 触发次数
    fix_retry_success: int = 0  # fix 后重试 read 成功的次数
    cookie_refresh_count: int = 0  # refresh_cookie 触发次数(含启动 1 次)
    circuit_breaker_triggered: bool = False  # 是否触发熔断

    # 预热阶段（不计阅读次数）
    warmup_done: bool = False  # 预热是否成功建立上下文
    warmup_attempts: int = 0  # 预热尝试次数

    @property
    def is_full_completed(self) -> bool:
        """是否完成了全部阅读次数（由调用方判断阈值）。"""
        return self.completed_count > 0

    def summary(self) -> str:
        """生成 metrics 摘要文本,用于推送内容。"""
        return (
            f"本次统计：成功 {self.synckey_success} 次 / "
            f"fix 触发 {self.no_synckey_fix_triggered} 次 / "
            f"fix 重试成功 {self.fix_retry_success} 次 / "
            f"cookie 刷新 {self.cookie_refresh_count} 次"
            f"{' / 预热成功(尝试 ' + str(self.warmup_attempts) + ' 次)' if self.warmup_done else ''}"
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_read_result_warmup.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/wereadit/models.py tests/test_read_result_warmup.py
git commit -m "feat(models): ReadResult 增加 warmup_done/warmup_attempts 字段"
```

---

## Task 2: 重构 reader.py —— 预热阶段 + 进度走 logger

**Files:**
- Modify: `src/wereadit/core/reader.py:15-304`
- Rewrite: `tests/test_reader_logs.py`

**Interfaces:**
- 产生：`ReadStatus`（`enum.Enum`）、`_prepare_data(data, cfg, last_time) -> int`、`_read_once(client, cfg, data, last_time) -> tuple[ReadStatus, int, bool]`、`_warmup(client, cfg, data) -> tuple[int, int]`、`read_books(client, cfg) -> ReadResult`（**去掉 `refresh_print` 参数**）。
- 消费：Task 1 的 `ReadResult` 新字段；`fix_no_synckey`、`refresh_cookie`、`sign_request`、常量（均保持不变）。

- [ ] **Step 1: 改写 test_reader_logs.py 为新行为（先失败）**

`tests/test_reader_logs.py` 整文件替换为：

```python
"""reader 日志回显：进度走 logger + 预热阶段隔离 + synckey 常态日志简化。"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from wereadit.config import Config
from wereadit.core.reader import read_books
from wereadit.infra.http import HttpClient


def _make_cfg(**overrides) -> Config:
    defaults = dict(
        read_num=2,
        books=["b1"],
        chapters=["c1"],
        headers={},
        cookies={"wr_skey": "old"},
    )
    defaults.update(overrides)
    return Config(**defaults)


def _mock_response(json_data, set_cookies=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.cookies = set_cookies or {}
    return resp


class TestProgressViaLogger:
    """进度回显走 logger（统一格式），且 index 不变时不重复打印。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_progress_not_repeated_on_retry(self, mock_post, caplog) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,  # 预热：修复后重试成功
            ok_resp,  # 主循环 read#1
            ok_resp,  # 主循环 read#2
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        progress = [r.message for r in caplog.records if r.message.startswith("阅读进度:")]
        assert progress == [
            "阅读进度: 第 1/2 次，已阅读 0.0 分钟",
            "阅读进度: 第 2/2 次，已阅读 0.5 分钟",
        ]


class TestSynckeyLogPresentation:
    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_log_merged_info_once(self, mock_post, caplog) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,
            ok_resp,
            ok_resp,
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        fix_logs = [m for m in caplog.messages if "已自动修复并重试" in m]
        assert fix_logs == ["预热：阅读上下文未同步，已自动修复并重试"]
        assert not any("尝试修复" in m for m in caplog.messages)
        assert not any("fix_no_synckey 已调用" in m for m in caplog.messages)
        assert not any(
            r.levelno == logging.WARNING and "修复未生效" in r.message
            for r in caplog.records
        )

    @patch("wereadit.infra.http.requests.Session.post")
    def test_backoff_info_then_warning_on_second_streak(
        self, mock_post, caplog
    ) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, no_synckey,  # 预热轮1：streak=1，退避
            no_synckey, fix_resp, no_synckey,  # 预热轮2：streak=2，退避（WARNING）
            no_synckey, fix_resp, ok_resp,    # 预热轮3：修复后重试成功
            ok_resp,                           # 主循环 read#1
        ]

        cfg = _make_cfg(read_num=1)
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        backoff = [r for r in caplog.records if "修复未生效" in r.message]
        assert len(backoff) == 2
        assert backoff[0].levelno == logging.INFO
        assert "连续 1/3 次" in backoff[0].message
        assert backoff[1].levelno == logging.WARNING
        assert "连续 2/3 次" in backoff[1].message

    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_retry_success_log(self, mock_post, caplog) -> None:
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,
            ok_resp,
            ok_resp,
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        assert any("预热：修复成功" in r.message for r in caplog.records)
        assert not any("synckey 修复成功" in r.message for r in caplog.records)
```

- [ ] **Step 2: 运行新测试确认失败**

Run: `pytest tests/test_reader_logs.py -v`
Expected: FAIL（旧代码日志为 "第 1/2 次：阅读上下文未同步，已自动修复并重试"，且无 "预热：" 前缀；进度走 `refresh_print` 而非 logger）

- [ ] **Step 3: 实现 reader.py 重构**

`src/wereadit/core/reader.py`：
1. 文件顶部 `import` 增加 `import enum`。
2. 在 `fix_no_synckey` 之后、`read_books` 之前插入：

```python
class ReadStatus(enum.Enum):
    """单次 read 的结果分类。"""

    SYNCED = "synced"               # 首次尝试即含 synckey
    SYNCED_VIA_FIX = "synced_via_fix"  # 无 synckey → fix 后重试成功
    NO_SYNCKEY = "no_synckey"       # fix 后重试仍无 synckey
    COOKIE_EXPIRED = "cookie_expired"  # 无 succ（cookie 失效）


def _prepare_data(data: dict[str, Any], cfg: Config, last_time: int) -> int:
    """构造单次 read 请求体并返回当前时间戳（保活字段全部保留）。"""
    now = int(time.time())
    data.pop("s", None)
    data["b"] = random.choice(cfg.books) if cfg.books else data["b"]
    data["c"] = random.choice(cfg.chapters) if cfg.chapters else data["c"]
    data["ct"] = now
    data["rt"] = now - last_time
    data["ts"] = int(now * 1000) + random.randint(0, 1000)
    data["rn"] = random.randint(0, 1000)
    sign_request(data, SIGN_KEY)
    return now


def _read_once(
    client: HttpClient, cfg: Config, data: dict[str, Any], last_time: int
) -> tuple[ReadStatus, int, bool]:
    """执行一次 read；无 synckey 时 fix 后内重试一次。

    返回 (状态, 成功时间戳 or 本次时间戳, 是否经 fix 重试成功)。
    """
    now = _prepare_data(data, cfg, last_time)
    res = client.post(
        READ_URL,
        data=json.dumps(data, separators=(",", ":")),
        timeout=READ_TIMEOUT,
    )
    res_data = res.json()
    if "succ" not in res_data:
        return (ReadStatus.COOKIE_EXPIRED, now, False)
    if "synckey" in res_data:
        return (ReadStatus.SYNCED, now, False)
    # 无 synckey → fix 后内重试一次
    fix_no_synckey(client, cfg)
    retry_now = _prepare_data(data, cfg, last_time)
    retry_res = client.post(
        READ_URL,
        data=json.dumps(data, separators=(",", ":")),
        timeout=READ_TIMEOUT,
    )
    retry_data = retry_res.json()
    if "succ" in retry_data and "synckey" in retry_data:
        return (ReadStatus.SYNCED_VIA_FIX, retry_now, True)
    return (ReadStatus.NO_SYNCKEY, retry_now, False)


def _warmup(client: HttpClient, cfg: Config, data: dict[str, Any]) -> tuple[int, int]:
    """预热阶段：循环 _read_once 直到 synckey 出现，不计入阅读次数。

    返回 (成功时的 last_time, 尝试次数)。熔断规则与主循环一致。
    """
    logger.info("开始预热：建立阅读上下文（不计入阅读次数）")
    last_time = int(time.time()) - SECONDS_PER_READ
    no_synckey_streak = 0
    cookie_fail_streak = 0
    attempts = 0
    while True:
        attempts += 1
        status, now, via_fix = _read_once(client, cfg, data, last_time)
        if status is ReadStatus.SYNCED or status is ReadStatus.SYNCED_VIA_FIX:
            if via_fix:
                logger.info("预热：修复成功，上下文已建立（尝试 %d 次）。", attempts)
            else:
                logger.info("预热成功，上下文已建立（尝试 %d 次）。", attempts)
            return (now, attempts)
        if status is ReadStatus.COOKIE_EXPIRED:
            cookie_fail_streak += 1
            if cookie_fail_streak >= MAX_COOKIE_FAIL:
                msg = f"预热阶段连续 {MAX_COOKIE_FAIL} 次 cookie 过期，熔断退出。"
                logger.error(msg)
                raise CookieExpiredError(msg)
            logger.warning("预热：cookie 已过期，尝试刷新...")
            refresh_cookie(client, cfg)
            time.sleep(CIRCUIT_BREAKER_BACKOFF)
            continue
        # NO_SYNCKEY
        no_synckey_streak += 1
        if no_synckey_streak >= MAX_NO_SYNCKEY:
            msg = (
                f"预热阶段连续 {MAX_NO_SYNCKEY} 次无 synckey 修复无效，任务中止"
                f"（已完成 0/{cfg.read_num} 次）。请检查 WEREAD_WEB_CURL"
            )
            logger.error(msg)
            raise ReadFailedError(msg)
        logger.info("预热：阅读上下文未同步，已自动修复并重试")
        backoff_log = (
            logger.warning if no_synckey_streak >= MAX_NO_SYNCKEY - 1 else logger.info
        )
        backoff_log(
            "预热：修复未生效，%ds 后重试（连续 %d/%d 次）",
            CIRCUIT_BREAKER_BACKOFF,
            no_synckey_streak,
            MAX_NO_SYNCKEY,
        )
        time.sleep(CIRCUIT_BREAKER_BACKOFF)
```

3. 重写 `read_books`（替换原函数体，签名去掉 `refresh_print`）：

```python
def read_books(client: HttpClient, cfg: Config) -> ReadResult:
    """执行阅读循环。

    Returns:
        ReadResult：完成次数与累计分钟数

    Raises:
        ReadFailedError: 连续 MAX_NO_SYNCKEY 次无 synckey（预热或主循环熔断）
        CookieExpiredError: 连续 MAX_COOKIE_FAIL 次 cookie 过期（熔断）

    【保活策略 - 多项关键设计不能改】
    - 启动强制 refresh_cookie: 上线握手,不能删
    - last_time = now - SECONDS_PER_READ: 伪造"已读 30 秒",不能删
    - data.pop("s"): 每次循环开头删除旧签名,不能删
    - b/c 随机选择: 模拟翻不同书,不能改成固定值
    - ts/rn jitter: 风控规避,不能去掉随机
    - sleep(READ_INTERVAL_SECONDS): 30 秒固定节奏,不能调快
    - 失败后不 sleep 不递增 index: 本次重试不计入进度
    """
    # 启动强制刷新（【保活策略】不能删）
    refresh_cookie(client, cfg)

    data: dict[str, Any] = dict(DEFAULT_READ_DATA)
    total = cfg.read_num
    logger.info("需要阅读 %d 次。", total)

    # 熔断计数器
    no_synckey_streak = 0
    cookie_fail_streak = 0

    # 运行 metrics 累计计数器（仅统计主循环 120 次；预热不计入）
    synckey_success = 0
    no_synckey_fix_triggered = 0
    fix_retry_success = 0
    cookie_refresh_count = 1  # 启动时已刷新 1 次
    circuit_breaker_triggered = False
    warmup_attempts = 0
    last_printed_index = 0  # 进度打印去重：仅在 index 变化时打印

    # 预热阶段：建立上下文，不计入阅读次数
    last_time, warmup_attempts = _warmup(client, cfg, data)

    # 主循环：120 次干净阅读
    index = 1
    while index <= total:
        status, now, via_fix = _read_once(client, cfg, data, last_time)
        if status is ReadStatus.SYNCED or status is ReadStatus.SYNCED_VIA_FIX:
            last_time = now
            if index != last_printed_index:
                last_printed_index = index
                logger.info(
                    "阅读进度: 第 %d/%d 次，已阅读 %.1f 分钟",
                    index, total, (index - 1) * 0.5,
                )
            index += 1
            synckey_success += 1
            if via_fix:
                fix_retry_success += 1
                logger.info("第 %d/%d 次：修复成功，继续阅读", index - 1, total)
            time.sleep(READ_INTERVAL_SECONDS)
            continue
        if status is ReadStatus.COOKIE_EXPIRED:
            cookie_fail_streak += 1
            if cookie_fail_streak >= MAX_COOKIE_FAIL:
                msg = (
                    f"连续 {MAX_COOKIE_FAIL} 次 cookie 过期，熔断退出。"
                    f"已完成 {index - 1}/{total} 次。"
                )
                logger.error(msg)
                circuit_breaker_triggered = True
                raise CookieExpiredError(msg)
            logger.warning("cookie 已过期，尝试刷新...")
            refresh_cookie(client, cfg)
            cookie_refresh_count += 1
            time.sleep(CIRCUIT_BREAKER_BACKOFF)
            continue
        # NO_SYNCKEY（预热后应极少发生；保留兜底）
        no_synckey_streak += 1
        no_synckey_fix_triggered += 1
        if no_synckey_streak >= MAX_NO_SYNCKEY:
            msg = (
                f"连续 {MAX_NO_SYNCKEY} 次无 synckey 修复无效，任务中止"
                f"（已完成 {index - 1}/{total} 次）。"
                "通常是 cookie 失效或触发风控，请检查 WEREAD_WEB_CURL"
            )
            logger.error(msg)
            circuit_breaker_triggered = True
            raise ReadFailedError(msg)
        logger.info("第 %d/%d 次：阅读上下文未同步，已自动修复并重试", index, total)
        backoff_log = (
            logger.warning if no_synckey_streak >= MAX_NO_SYNCKEY - 1 else logger.info
        )
        backoff_log(
            "第 %d/%d 次：修复未生效，%ds 后重试（连续 %d/%d 次）",
            index, total, CIRCUIT_BREAKER_BACKOFF,
            no_synckey_streak, MAX_NO_SYNCKEY,
        )
        time.sleep(CIRCUIT_BREAKER_BACKOFF)

    logger.info("阅读脚本已完成。")
    return ReadResult(
        completed_count=index - 1,
        total_minutes=(index - 1) * 0.5,
        synckey_success=synckey_success,
        no_synckey_fix_triggered=no_synckey_fix_triggered,
        fix_retry_success=fix_retry_success,
        cookie_refresh_count=cookie_refresh_count,
        circuit_breaker_triggered=circuit_breaker_triggered,
        warmup_done=True,
        warmup_attempts=warmup_attempts,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_reader_logs.py -v`
Expected: PASS（4 个测试全过）

- [ ] **Step 5: 提交**

```bash
git add src/wereadit/core/reader.py tests/test_reader_logs.py
git commit -m "refactor(reader): 抽预热阶段 + 进度回显走 logger"
```

---

## Task 3: app.py 改用 setup_logging 并去掉 refresh_print

**Files:**
- Modify: `src/wereadit/app.py:37-91`

**Interfaces:**
- 消费：Task 2 的 `read_books(client, cfg)`（无 `refresh_print` 参数）；`setup_logging()`（`wereadit.utils.logging`）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_app.py` 中新增（依赖该文件已存在的 `_mock_read_result`）：

```python
from wereadit.config import Config


def test_main_calls_setup_logging():
    from unittest.mock import patch

    from wereadit.utils.logging import setup_logging

    cfg = Config(
        read_num=1, books=["b1"], chapters=["c1"],
        headers={}, cookies={"wr_skey": "x"},
    )
    with patch("wereadit.app.load_config", return_value=cfg), \
         patch("wereadit.app.HttpClient"), \
         patch("wereadit.app.read_books", return_value=_mock_read_result()), \
         patch("wereadit.utils.logging.setup_logging") as mock_setup:
        import wereadit.app as app_mod

        app_mod.main()

    mock_setup.assert_called_once()
```

> 目的：防止回归——正常 `main()` 流程必须仍安装日志 handler（原由 `make_refresh_print` 的副作用承担，现改由 `setup_logging`）。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_app.py::test_main_configures_logging_handler -v`
Expected: FAIL（`setup_logging` 未被调用）

- [ ] **Step 3: 修改 app.py**

`src/wereadit/app.py` 中：
- 删除第 43 行 `from wereadit.utils.logging import make_refresh_print` 与第 45 行 `refresh_print = make_refresh_print()`。
- 在第 43 行位置改为 `from wereadit.utils.logging import setup_logging`。
- `main()` 开头（在 `cfg = load_config()` 之前）插入 `setup_logging()`。
- 第 91 行 `result = read_books(client, cfg, refresh_print=refresh_print)` 改为 `result = read_books(client, cfg)`。

即关键片段：

```python
def main() -> int:
    from wereadit.utils.logging import setup_logging

    setup_logging()
    cfg = load_config()
    client = HttpClient(headers=cfg.headers, cookies=cfg.cookies)
    ...
    result = read_books(client, cfg)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_app.py -v`
Expected: PASS（`make_refresh_print` 的 patch 仍指向已存在的函数定义，不影响；`read_books` 为 mock，签名无关）

- [ ] **Step 5: 提交**

```bash
git add src/wereadit/app.py
git commit -m "refactor(app): 改用 setup_logging，去掉 refresh_print 参数"
```

---

## Task 4: 调整 test_keepalive.py 受预热影响的测试

**Files:**
- Modify: `tests/test_keepalive.py`（仅下列 7 个测试，其余 28 个不变）

**Interfaces:**
- 消费：Task 2 的 `read_books(client, cfg)` 行为——每次运行比旧代码多 1 次成功 read（预热那次），故所有 `read_books` 的 mock `side_effect` 需在原基础上**追加 1 个含 synckey 的成功响应**；日志文案 "第 1/N 次" 改为 "预热："；`fix_retry_success` 在预热阶段不计入。

> 规则：read_num=N 时，mock 需提供 1 个 renewal +（预热读取）+ N 个主循环 read 的响应。预热若直接拿到 synckey 则占 1 个 read 响应；若需 fix 则占 read+fix+retry 共 3 个 POST（与原"首读触发 fix"占用一致），但主循环仍多出 N 个 read。

需修改的 7 个测试（每个给出新 `side_effect` 与/或断言）：

### 4.1 test_read_request_does_not_pass_headers_explicitly

`side_effect` 改为（原 3 项 → 4 项，多 1 个预热 read）：

```python
        mock_post.side_effect = [
            _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"}),  # renewal
            _mock_response({"succ": 1, "synckey": 123}),  # 预热 read
            _mock_response({"succ": 1, "synckey": 124}),  # read #1
            _mock_response({"succ": 1, "synckey": 125}),  # read #2
        ]
```

断言 `for call in mock_post.call_args_list[1:]` 不变（仍检查所有 read 不显式传 headers）。

### 4.2 test_startup_refresh_cookie_called

`side_effect` 改为（多 1 个主循环 read）：

```python
        mock_post.side_effect = [
            _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"}),  # renewal
            _mock_response({"succ": 1, "synckey": 1}),  # 预热 read
            _mock_response({"succ": 1, "synckey": 1}),  # 主循环 read#1
        ]
```

断言（首个调用为 RENEW_URL）不变。

### 4.3 test_no_synckey_streak_resets_on_success

`side_effect` 改为（预热吃掉 fix 那次，主循环多 1 个 read）：

```python
        mock_post.side_effect = [
            renewal_resp,                        # 启动刷新
            no_synckey_resp, fix_resp, ok_resp,  # 预热：read → fix → retry ok
            ok_resp,                             # 主循环 read#1
            ok_resp,                             # 主循环 read#2
        ]
```

断言改为（预热 fix 不计入主循环 metrics）：

```python
        assert result.completed_count == 2
        assert result.fix_retry_success == 0, "预热阶段的 fix 重试不计入主循环 metrics"
```

### 4.4 test_lasttime_only_updated_on_synckey_success

该测试原无有效断言（仅 smoke）。扩成可运行：

```python
    @patch("wereadit.infra.http.requests.Session.post")
    def test_lasttime_only_updated_on_synckey_success(self, mock_post) -> None:
        """策略: lastTime 只在 synckey 成功时更新（rt 字段依赖）。"""
        renewal_resp = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        ok_resp = _mock_response({"succ": 1, "synckey": 1})
        mock_post.side_effect = [renewal_resp, ok_resp, ok_resp]  # renewal + 预热 + 主循环
        cfg = _make_cfg(read_num=1)
        client = HttpClient(cookies={"wr_skey": "old"})

        with patch("wereadit.core.reader.time.sleep"):
            with patch("wereadit.core.reader.time.time") as mock_time:
                mock_time.side_effect = [1000, 1030, 1060]
                result = read_books(client, cfg)

        assert result.completed_count == 1
        assert result.warmup_done is True
```

### 4.5 test_data_pop_s_each_iteration

`side_effect` 改为（多 1 个预热 read）：

```python
        mock_post.side_effect = [renewal_resp, ok_resp, ok_resp, ok_resp]  # renewal + 预热 + 2 主循环
```

断言改为：

```python
        read_calls = [c for c in mock_post.call_args_list if c[0][0] == READ_URL]
        assert len(read_calls) == 3, "预热 + 2 次主循环共 3 次 read"
        bodies = [json.loads(c.kwargs["data"]) for c in read_calls]
        assert bodies[0]["s"] != bodies[1]["s"]
        assert bodies[1]["s"] != bodies[2]["s"]
```

### 4.6 test_sleep_30s_after_synckey_success

`side_effect` 改为（多 1 个预热 read；预热成功不 sleep，故 sleep(30) 仍为 2 次）：

```python
        mock_post.side_effect = [renewal_resp, ok_resp, ok_resp, ok_resp]  # renewal + 预热 + 2 主循环
```

断言 `len(sleep_30_calls) == 2` 不变（预热成功不 sleep，仅主循环 2 次 synckey 成功 sleep）。

### 4.7 test_no_sleep_no_increment_on_cookie_fail

`side_effect` 改为（多 1 个主循环 read）：

```python
        mock_post.side_effect = [
            renewal_resp,    # 启动刷新
            no_succ_resp,    # 预热 read #1 失败
            renewal_resp,    # refresh_cookie
            ok_resp,         # 预热 read #2 成功
            ok_resp,         # 主循环 read#1
        ]
```

断言 `len(sleep_30_calls) == 1` 与 `result.completed_count == 1` 不变。

> 下列 3 个测试**无需修改**（预热复用了与原代码一致的失败循环，序列刚好被预热消费并如期熔断/触发）：
> - `test_circuit_breaker_on_continuous_no_synckey`（预热吃满 2 轮 + 第 3 轮 read 触发熔断）
> - `test_circuit_breaker_on_continuous_cookie_fail`（预热吃满 cookie 失败循环后熔断）
> - `test_fix_no_synckey_called_when_missing_synckey`（预热触发 2 次 fix，仍 `len>=1`）

- [ ] **Step 1: 运行受影响的 7 个测试确认通过**

Run: `pytest tests/test_keepalive.py -v`
Expected: 35 个测试全 PASS（7 个已调，28 个原样通过）

- [ ] **Step 2: 提交**

```bash
git add tests/test_keepalive.py
git commit -m "test(keepalive): 适配预热阶段带来的 mock 序列与日志文案变化"
```

---

## Task 5: 全量测试 + lint

**Files:**
- 无新增，仅验证。

- [ ] **Step 1: 运行全部 pytest**

Run: `pytest tests/ -v`
Expected: 全部 PASS（`test_reader_logs` 4 + `test_keepalive` 35 + `test_app` + `test_config` + `test_exchanger` + `test_read_result_warmup` 等）

- [ ] **Step 2: 运行 ruff**

Run: `ruff check src/ tests/`
Expected: 无错误（注意 `make_refresh_print` 在 `logging.py` 中保留但未使用不报 F 级错误；`app.py` 已移除其 import）

- [ ] **Step 3: 提交（若有 lint 修复）**

```bash
git add -A
git commit -m "style: ruff 修复"  # 仅当 Step 2 有修正时
```

---

## Self-Review 备注（已内联修正）

- 规格覆盖：预热（`_warmup`）、进度走 logger（`logger.info`）、`read_num` 不变、保活策略全部保留（函数未改语义）、`ReadResult` 新字段——均有对应 Task。
- 无占位符：所有 step 含完整代码或确切 mock 序列。
- 类型一致：`_read_once` 返回 `tuple[ReadStatus, int, bool]`；`_warmup` 返回 `tuple[int, int]`；`read_books` 返回 `ReadResult`；`ReadStatus` 枚举在 Task 2 定义并在 `_warmup` / `read_books` 一致使用。
- 关键风险已处理：`setup_logging()` 替代 `make_refresh_print` 的 handler 副作用，避免正常流程丢日志。
