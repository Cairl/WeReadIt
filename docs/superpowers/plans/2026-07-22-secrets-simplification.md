# Secrets 配置傻瓜化 + 日志回显优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 兑换 token 完全由 WEREAD_APP_CURL 运行时生成（彻底删除手动 token）、Secret 改名 WEREAD_WEB_CURL/WEREAD_APP_CURL（无旧名兼容）、新增配置检查按钮、阅读日志回显人话化。

**Architecture:** `config.py` 删除两个 token 字段，新增运行时字段 `app_token`/`app_token_key`，property 改从它们派生；`app.py` 刷新成功即 `dataclasses.replace` 注入；新增 `config_check.py` 独立检查入口 + `config-check.yml` workflow；`reader.py` 仅动呈现层（进度去重 + 日志文案/级别）。

**Tech Stack:** Python 3.10+ / requests / pytest / ruff / GitHub Actions YAML

**Spec:** `docs/superpowers/specs/2026-07-22-secrets-simplification-design.md`

## Global Constraints

- Python 3.10+，类型注解一律 `from __future__ import annotations`
- 每个 Task 完成后 `pytest tests/` 与 `ruff check src/ tests/` 必须全过
- 提交信息格式 `[模块] 简要描述`
- 推送与日志中 token 只显示前 8 位（脱敏）；日志/推送文案不使用 emoji
- **无旧名兼容**：代码、README、AGENTS.md、changelog.md、deploy.yml、全部日志/推送文案不得出现 `WEREAD_CURL_BASH`、`WEREAD_LOGIN_CURL`、`WEREAD_ANDROID_TOKEN`、`WEREAD_IOS_TOKEN` 字样
- reader.py 行为逻辑（fix 调用、重试、退避、熔断阈值、保活策略）一行不动，只动打印时机/文案/级别
- 熔断文案必须保留"连续"与"synckey"字样（test_keepalive.py:184 的回归断言依赖）

---

### Task 1: 兑换 token 来源切换（config + app + refresher 文案 + 测试适配）

**Files:**
- Modify: `src/wereadit/config.py`
- Modify: `src/wereadit/app.py`
- Modify: `src/wereadit/core/token_refresher.py`（仅 diagnose_login_curl 文案）
- Create: `tests/test_config.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_exchanger.py`

**Interfaces:**
- Produces（后续任务依赖）：
  - `Config.web_curl: str`、`Config.weread_app_curl: str`、`Config.app_token: str = ""`、`Config.app_token_key: str = ""`
  - `Config.weread_access_token -> str`（返回 `app_token`）、`Config.weread_platform -> str`（`app_token_key == "skey"` → `PLATFORM_IOS`，否则 `PLATFORM_ANDROID`）
  - `load_config()` 读 `WEREAD_WEB_CURL` / `WEREAD_APP_CURL`
  - `app.py` 私有 `_inject_app_token(cfg: Config, refresh_result: "RefreshResult") -> Config`

**语义决策（spec 未明确，本计划裁定）**：`refresh_diagnosis` 非空且无可用 token 时（配了 APP_CURL 但刷新失败），兑换跳过且 `exit_code = 1`、`has_failure = True` —— 兑换目标未达成必须是可见失败，不静默。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_config.py`：

```python
"""config 加载与派生测试：新环境变量名、app_token/app_token_key 派生逻辑。"""

from __future__ import annotations

from unittest.mock import patch

from wereadit.config import Config, load_config
from wereadit.constants import PLATFORM_ANDROID, PLATFORM_IOS


class TestLoadConfigEnvNames:
    """环境变量新名读取（无旧名兼容）。"""

    def test_web_curl_read(self) -> None:
        env = {
            "WEREAD_WEB_CURL": (
                "curl 'https://weread.qq.com/web/book/read' "
                "-H 'Cookie: wr_skey=abc12345; wr_vid=12345'"
            )
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.web_curl.startswith("curl")
        assert cfg.cookies["wr_vid"] == "12345"

    def test_app_curl_read(self) -> None:
        env = {"WEREAD_APP_CURL": "curl 'https://i.weread.qq.com/login'"}
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.weread_app_curl == "curl 'https://i.weread.qq.com/login'"

    def test_old_names_ignored(self) -> None:
        """旧名 WEREAD_CURL_BASH / WEREAD_LOGIN_CURL 不再读取。"""
        env = {
            "WEREAD_CURL_BASH": (
                "curl 'https://weread.qq.com/web/book/read' -H 'Cookie: wr_vid=1'"
            ),
            "WEREAD_LOGIN_CURL": "curl 'https://i.weread.qq.com/login'",
        }
        with patch.dict("os.environ", env, clear=True):
            cfg = load_config()
        assert cfg.web_curl == ""
        assert cfg.weread_app_curl == ""


class TestTokenDerivation:
    """app_token/app_token_key 与 property 派生。"""

    def test_access_token_returns_app_token(self) -> None:
        cfg = Config(read_num=1, app_token="tok123")
        assert cfg.weread_access_token == "tok123"

    def test_platform_ios_when_key_skey(self) -> None:
        cfg = Config(read_num=1, app_token="tok", app_token_key="skey")
        assert cfg.weread_platform == PLATFORM_IOS

    def test_platform_android_when_key_access_token(self) -> None:
        cfg = Config(read_num=1, app_token="tok", app_token_key="accessToken")
        assert cfg.weread_platform == PLATFORM_ANDROID

    def test_platform_default_android_when_empty(self) -> None:
        cfg = Config(read_num=1)
        assert cfg.weread_platform == PLATFORM_ANDROID
        assert cfg.weread_access_token == ""
```

`tests/test_app.py` 改动：

0. import 区：`from wereadit.constants import ERRCODE_TOKEN_EXPIRED` 改为 `from wereadit.constants import ERRCODE_TOKEN_EXPIRED, PLATFORM_IOS`。

1. `_make_cfg` 的 defaults 替换为（删除 `weread_android_token`/`weread_ios_token`/`weread_login_curl`，新增 `app_token`/`app_token_key`/`weread_app_curl`）：

```python
    defaults = dict(
        read_num=2,
        books=["b1"],
        chapters=["c1"],
        pushplus_token="test_push_token",
        wxpusher_spt="",
        telegram_bot_token="",
        telegram_chat_id="",
        serverchan_spt="",
        app_token="test_token",
        app_token_key="accessToken",
        weread_app_curl="",
        exchange_award="2,2,2,2,2,2,2,2",
        headers={},
        cookies={"wr_vid": "12345"},
        web_curl="",
    )
```

2. `test_no_exchange_token_returns_0_and_push_success` 的 `_make_cfg(weread_android_token="", weread_ios_token="")` 改为 `_make_cfg(app_token="", app_token_key="")`。

3. `TestMainTokenRefresh` 整个类替换为：

```python
class TestMainTokenRefresh:
    """阅读前刷新 token 的编排：体检 -> 刷新 -> 注入 -> 诊断入推送。"""

    _APP_CURL = (
        "curl 'https://i.weread.qq.com/login' "
        "--data-raw '{\"deviceId\":\"dev1\"}'"
    )

    def _run_main(self, cfg: Config, call_order: list[str] | None = None):
        """以 mock 跑 main()，返回 (exit_code, mock_push, mock_exchange, mock_refresh)。"""
        from wereadit.core.token_refresher import RefreshResult

        def _refresh_side_effect(*args):
            if call_order is not None:
                call_order.append("refresh")
            return RefreshResult(token="new_token_123456", token_key="accessToken")

        def _read_side_effect(*args, **kwargs):
            if call_order is not None:
                call_order.append("read")
            return _mock_read_result()

        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch(
                "wereadit.core.reader.read_books",
                side_effect=_read_side_effect,
            ),
            patch(
                "wereadit.core.exchanger.exchange_awards",
                return_value="兑换完成",
            ) as mock_exchange,
            patch("wereadit.app.push") as mock_push,
            patch(
                "wereadit.core.token_refresher.refresh_app_token",
                side_effect=_refresh_side_effect,
            ) as mock_refresh,
        ):
            exit_code = main()
        return exit_code, mock_push, mock_exchange, mock_refresh

    def test_refresh_before_reading_and_inject_cfg(self) -> None:
        """刷新发生在阅读之前，exchange_awards 收到的 cfg 已注入新 token。"""
        call_order: list[str] = []
        cfg = _make_cfg(app_token="", app_token_key="", weread_app_curl=self._APP_CURL)
        exit_code, _, mock_exchange, _ = self._run_main(cfg, call_order)

        assert exit_code == 0
        assert call_order == ["refresh", "read"]
        used_cfg = mock_exchange.call_args.args[1]
        assert used_cfg.app_token == "new_token_123456"
        assert used_cfg.app_token_key == "accessToken"

    def test_ios_platform_injected_from_skey(self) -> None:
        """token_key=skey 时注入为 iOS 平台，推送含自识别说明。"""
        from wereadit.core.token_refresher import RefreshResult

        cfg = _make_cfg(app_token="", app_token_key="", weread_app_curl=self._APP_CURL)
        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch("wereadit.core.reader.read_books", return_value=_mock_read_result()),
            patch(
                "wereadit.core.exchanger.exchange_awards",
                return_value="兑换完成",
            ) as mock_exchange,
            patch("wereadit.app.push") as mock_push,
            patch(
                "wereadit.core.token_refresher.refresh_app_token",
                return_value=RefreshResult(token="ios_skey_123456", token_key="skey"),
            ),
        ):
            main()

        used_cfg = mock_exchange.call_args.args[1]
        assert used_cfg.app_token_key == "skey"
        assert used_cfg.weread_platform == PLATFORM_IOS
        push_content = mock_push.call_args.args[0]
        assert "平台自识别：iOS（依据响应字段 skey）" in push_content

    def test_exchange_receives_refresher_args(self) -> None:
        """exchange_awards 收到 refresher 回调与刷新时刻。"""
        cfg = _make_cfg(weread_app_curl=self._APP_CURL)
        _, _, mock_exchange, _ = self._run_main(cfg)

        kwargs = mock_exchange.call_args.kwargs
        assert callable(kwargs["refresher"])
        assert isinstance(kwargs["token_refreshed_at"], float)

    def test_refresh_skipped_when_curl_unhealthy(self) -> None:
        """体检不过：不发起刷新，诊断进推送，exit_code=1。"""
        cfg = _make_cfg(
            app_token="",
            app_token_key="",
            weread_app_curl="curl 'https://i.weread.qq.com/readdetail'",
        )
        exit_code, mock_push, _, mock_refresh = self._run_main(cfg)

        assert exit_code == 1
        mock_refresh.assert_not_called()
        push_content = mock_push.call_args.args[0]
        assert "不是 /login" in push_content
        assert mock_push.call_args.kwargs["is_success"] is False

    def test_refresh_failure_no_token_exit_1(self) -> None:
        """刷新失败且无 token：跳过兑换，诊断进推送，exit_code=1。"""
        from wereadit.core.token_refresher import RefreshResult

        cfg = _make_cfg(app_token="", app_token_key="", weread_app_curl=self._APP_CURL)
        with (
            patch("wereadit.app.load_config", return_value=cfg),
            patch("wereadit.app.HttpClient"),
            patch("wereadit.utils.logging.make_refresh_print"),
            patch("wereadit.core.reader.read_books", return_value=_mock_read_result()),
            patch("wereadit.core.exchanger.exchange_awards") as mock_exchange,
            patch("wereadit.app.push") as mock_push,
            patch(
                "wereadit.core.token_refresher.refresh_app_token",
                return_value=RefreshResult(diagnosis="网络异常（重试 3 次均失败）"),
            ),
        ):
            exit_code = main()

        assert exit_code == 1
        mock_exchange.assert_not_called()
        push_content = mock_push.call_args.args[0]
        assert "网络异常" in push_content
        assert mock_push.call_args.kwargs["is_success"] is False

    def test_no_app_curl_no_refresh(self) -> None:
        """未配置 APP_CURL：刷新段整体跳过，行为与旧版一致。"""
        cfg = _make_cfg()  # weread_app_curl 默认 ""
        exit_code, _, mock_exchange, mock_refresh = self._run_main(cfg)

        assert exit_code == 0
        mock_refresh.assert_not_called()
        kwargs = mock_exchange.call_args.kwargs
        assert kwargs["refresher"] is None
        assert kwargs["token_refreshed_at"] is None
```

（原 `test_platform_mismatch_not_replaced` 与 `test_refresh_failure_diagnosis_in_push` 删除：错位概念不存在；刷新失败不再降级走兑换。）

`tests/test_exchanger.py` 的 `_make_cfg` defaults 替换为：

```python
    defaults = dict(
        read_num=2,
        books=["b1"],
        chapters=["c1"],
        pushplus_token="",
        wxpusher_spt="",
        telegram_bot_token="",
        telegram_chat_id="",
        serverchan_spt="",
        app_token="test_token",
        app_token_key="accessToken",
        weread_app_curl="",
        exchange_award="2,2,2,2,2,2,2,2",
        headers={},
        cookies={"wr_vid": "12345"},
        web_curl="",
    )
```

`test_ios_platform` 用例的 `_make_cfg(weread_android_token="", weread_ios_token="ios_token")` 改为 `_make_cfg(app_token="ios_token", app_token_key="skey")`。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/ -q -x
```

预期：大量 FAIL（Config 无 `app_token` 等字段，构造 TypeError）

- [ ] **Step 3: 实现**

**`src/wereadit/config.py`** 改动三处：

1. Config 字段区（兑换参数与 HTTP 参数段）替换为：

```python
    # 兑换参数
    exchange_award: str = DEFAULT_EXCHANGE_AWARD
    weread_app_curl: str = ""

    # 运行时注入（非环境变量）：/login 重放刷新得到的 App token 与命中字段名
    app_token: str = ""
    app_token_key: str = ""

    # HTTP 请求参数
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    web_curl: str = ""
```

2. 两个 property 替换为：

```python
    @property
    def weread_access_token(self) -> str:
        """兑换用 App token（运行时由 /login 重放注入，见 app.py）。"""
        return self.app_token

    @property
    def weread_platform(self) -> str:
        """平台由刷新命中字段名派生：skey → iOS，accessToken → Android。"""
        if self.app_token_key == "skey":
            return PLATFORM_IOS
        return PLATFORM_ANDROID
```

3. `load_config()`：局部变量 `curl_bash` 改名 `web_curl`；`_env("WEREAD_CURL_BASH")` 改为 `_env("WEREAD_WEB_CURL")`；未配置警告文案中 `WEREAD_CURL_BASH` 改为 `WEREAD_WEB_CURL`；return 语句中 `weread_login_curl=_env("WEREAD_LOGIN_CURL")` 删除、`weread_android_token`/`weread_ios_token` 两行删除，新增 `weread_app_curl=_env("WEREAD_APP_CURL")`，`curl_bash=curl_bash` 改为 `web_curl=web_curl`。

**`src/wereadit/app.py`** 改动：

1. 删除 `_replace_token` 与 `_token_key_matches_platform` 两个函数，新增：

```python
def _inject_app_token(cfg: Config, refresh_result: "RefreshResult") -> Config:
    """把刷新得到的 token 注入 Config（平台由 token_key 自动派生）。"""
    return dataclasses.replace(
        cfg, app_token=refresh_result.token, app_token_key=refresh_result.token_key
    )
```

（import 区加 `from typing import TYPE_CHECKING` 与 `if TYPE_CHECKING: from wereadit.core.token_refresher import RefreshResult`）

2. `main()` 刷新段（try 块开头）替换为：

```python
        # 兑换 Token：阅读前由 /login 重放刷新生成，平台从命中字段名自识别
        refresh_diagnosis = ""
        platform_note = ""
        refresher = None
        token_refreshed_at = None
        if cfg.weread_app_curl:
            from wereadit.core.token_refresher import (
                diagnose_login_curl,
                refresh_app_token,
            )

            curl_diagnosis = diagnose_login_curl(cfg.weread_app_curl)
            if curl_diagnosis:
                logger.warning("WEREAD_APP_CURL 体检不过: %s", curl_diagnosis)
                refresh_diagnosis = f"Token 自动刷新已跳过：{curl_diagnosis}"
            else:
                refresher = partial(refresh_app_token, cfg.weread_app_curl)
                refresh_result = refresher()
                if refresh_result.ok:
                    cfg = _inject_app_token(cfg, refresh_result)
                    token_refreshed_at = time.time()
                    platform_note = (
                        f"平台自识别：{'iOS' if cfg.weread_platform == PLATFORM_IOS else 'Android'}"
                        f"（依据响应字段 {refresh_result.token_key}）"
                    )
                    logger.info(
                        "兑换 Token 已在阅读前刷新: %s...（%s）",
                        refresh_result.token[:8],
                        platform_note,
                    )
                else:
                    refresh_diagnosis = refresh_result.diagnosis
                    logger.warning("阅读前刷新 Token 失败: %s", refresh_diagnosis)
```

3. 兑换 `else` 分支（原 `logger.info("未配置 WEREAD_ACCESS_TOKEN，跳过兑换。")`）替换为：

```python
        else:
            logger.info("无可用兑换 Token（WEREAD_APP_CURL 未配置或刷新失败），跳过兑换。")
            if refresh_diagnosis:
                # 配了 APP_CURL 但刷新失败：兑换目标未达成，标记为可见失败
                exit_code = 1
                has_failure = True
```

4. `-2012` 分支的 platform_label 与 guidance 替换为：

```python
                    platform_label = (
                        "iOS" if cfg.weread_platform == PLATFORM_IOS else "Android"
                    )
                    logger.error("兑换 Token 已过期: %s", exc)
                    if refresh_diagnosis:
                        guidance = "根因见下方 Token 自动续期诊断。"
                    elif cfg.weread_app_curl:
                        guidance = "请重新抓包更新 WEREAD_APP_CURL（杀 App 冷启动抓 /login，body 须含 deviceId）。"
                    else:
                        guidance = "未配置 WEREAD_APP_CURL 自动续期，请按 README 配置。"
                    exchange_summary = (
                        f"兑换奖励失败: {platform_label} Token 已过期。{guidance}\n"
                        f"过期 Token 前 8 位: {token_preview}..."
                    )
```

5. 推送拼接处（`if refresh_diagnosis:` 之后）追加：

```python
        if platform_note:
            push_content += f"\n\n{platform_note}"
```

**`src/wereadit/core/token_refresher.py`**：`diagnose_login_curl` 三处文案中的 `WEREAD_LOGIN_CURL` 全部改为 `WEREAD_APP_CURL`（"为空，请按 README..."、空 URL 提示、deviceId 缺失提示不变其余文字）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/ -q && python -m ruff check src/ tests/
```

预期：全部 PASS，ruff 无告警

- [ ] **Step 5: 全库旧名痕迹扫描**

```bash
cd "S:\Github Repositories\WeReadIt" && grep -rn "WEREAD_CURL_BASH\|WEREAD_LOGIN_CURL\|WEREAD_ANDROID_TOKEN\|WEREAD_IOS_TOKEN" src/ tests/
```

预期：无输出（deploy.yml、README、AGENTS.md 在 Task 4 处理）

- [ ] **Step 6: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/config.py src/wereadit/app.py src/wereadit/core/token_refresher.py tests/test_config.py tests/test_app.py tests/test_exchanger.py && git commit -m "[exchange] 兑换 token 完全由 /login 重放生成：删除手动 token，平台自识别，Secret 改名"
```

---

### Task 2: 配置检查入口与 workflow

**Files:**
- Create: `src/wereadit/config_check.py`
- Create: `.github/workflows/config-check.yml`
- Test: `tests/test_config_check.py`

**Interfaces:**
- Consumes: Task 1 的 `Config` 字段与 `load_config`；`token_refresher.diagnose_login_curl(curl) -> str`（空串=通过）、`refresh_app_token(curl) -> RefreshResult`（`.ok`/`.token`/`.token_key`/`.diagnosis`）；`wereadit.push.push(content, method, client, cfg, is_success=...)`
- Produces: `python -m wereadit.config_check` 入口（退出码 0=全部正常，1=任一异常）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_config_check.py`：

```python
"""config_check 配置检查：各检查分支、报告格式、退出码。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from wereadit.config import Config
from wereadit.config_check import main
from wereadit.core.token_refresher import RefreshResult


def _make_cfg(**overrides) -> Config:
    defaults = dict(
        read_num=120,
        books=["b1"],
        chapters=["c1"],
        pushplus_token="push_token",
        wxpusher_spt="",
        telegram_bot_token="",
        telegram_chat_id="",
        serverchan_spt="",
        app_token="",
        app_token_key="",
        weread_app_curl=(
            "curl 'https://i.weread.qq.com/login' "
            "--data-raw '{\"deviceId\":\"dev1\"}'"
        ),
        exchange_award="2,2,2,2,2,2,2,2",
        headers={},
        cookies={"wr_skey": "abc", "wr_vid": "12345"},
        web_curl="curl 'https://weread.qq.com/web/book/read'",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _run(cfg: Config, refresh_result: RefreshResult | None = None):
    """跑 config_check.main()，返回 (exit_code, mock_push)。"""
    if refresh_result is None:
        refresh_result = RefreshResult(token="tok_12345678", token_key="skey")
    with (
        patch("wereadit.config_check.load_config", return_value=cfg),
        patch("wereadit.config_check.HttpClient"),
        patch("wereadit.config_check.push") as mock_push,
        patch(
            "wereadit.core.token_refresher.refresh_app_token",
            return_value=refresh_result,
        ),
    ):
        exit_code = main()
    return exit_code, mock_push


class TestConfigCheck:
    def test_all_ok_returns_0(self) -> None:
        exit_code, mock_push = _run(_make_cfg())
        assert exit_code == 0
        report = mock_push.call_args.args[0]
        assert "[正常] WEREAD_WEB_CURL" in report
        assert "[正常] WEREAD_APP_CURL" in report
        assert "平台自识别为 iOS" in report
        assert "[正常] 推送渠道：pushplus" in report
        assert "READ_NUM=120" in report
        assert "全部检查通过" in report
        assert mock_push.call_args.kwargs["is_success"] is True

    def test_web_curl_missing(self) -> None:
        cfg = _make_cfg(web_curl="", cookies={})
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        report = mock_push.call_args.args[0]
        assert "[异常] WEREAD_WEB_CURL：未配置" in report

    def test_web_curl_missing_cookie_keys(self) -> None:
        cfg = _make_cfg(cookies={"wr_skey": "abc"})  # 缺 wr_vid
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        assert "wr_vid" in mock_push.call_args.args[0]

    def test_app_curl_missing(self) -> None:
        cfg = _make_cfg(weread_app_curl="")
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        assert "[异常] WEREAD_APP_CURL：未配置" in mock_push.call_args.args[0]

    def test_app_curl_unhealthy(self) -> None:
        cfg = _make_cfg(weread_app_curl="curl 'https://i.weread.qq.com/readdetail'")
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1
        assert "不是 /login" in mock_push.call_args.args[0]

    def test_app_curl_refresh_failed(self) -> None:
        cfg = _make_cfg()
        exit_code, mock_push = _run(
            cfg, RefreshResult(diagnosis="login 凭证已失效 (errcode=-2012)")
        )
        assert exit_code == 1
        assert "[异常] WEREAD_APP_CURL" in mock_push.call_args.args[0]
        assert "-2012" in mock_push.call_args.args[0]

    def test_no_push_channel_skips_push(self) -> None:
        cfg = _make_cfg(pushplus_token="")
        exit_code, mock_push = _run(cfg)
        assert exit_code == 1  # 无推送渠道计为异常项
        mock_push.assert_not_called()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_config_check.py -q
```

预期：FAIL（`wereadit.config_check` 模块不存在）

- [ ] **Step 3: 实现**

创建 `src/wereadit/config_check.py`：

```python
"""配置检查：静态体检 + 真实重放验证，结果推送。

入口：python -m wereadit.config_check
只读检查：不阅读、不兑换，唯一发出的请求是 /login 重放验证（幂等无副作用）。
"""

from __future__ import annotations

import logging
import sys

from wereadit.config import Config, load_config
from wereadit.infra.http import HttpClient
from wereadit.push import push
from wereadit.utils.logging import setup_logging

logger = logging.getLogger(__name__)


def _check_web_curl(cfg: Config) -> tuple[bool, str]:
    """检查 WEREAD_WEB_CURL：解析与关键 cookie。"""
    if not cfg.web_curl:
        return False, (
            "[异常] WEREAD_WEB_CURL：未配置，阅读功能无法工作。"
            "请按 README 抓取网页端 read 请求"
        )
    missing = [k for k in ("wr_skey", "wr_vid") if k not in cfg.cookies]
    if missing:
        return False, (
            f"[异常] WEREAD_WEB_CURL：cookie 中缺少 {', '.join(missing)}，"
            "请重新抓取完整 read 请求"
        )
    return True, f"[正常] WEREAD_WEB_CURL：解析成功，vid={cfg.cookies['wr_vid']}"


def _check_app_curl(cfg: Config) -> tuple[bool, str]:
    """检查 WEREAD_APP_CURL：静态体检 + 真实重放一次 /login。"""
    if not cfg.weread_app_curl:
        return False, (
            "[异常] WEREAD_APP_CURL：未配置，兑换无法自动续期。"
            "请按 README 抓取 App /login 请求（杀 App 冷启动，body 须含 deviceId）"
        )
    from wereadit.core.token_refresher import diagnose_login_curl, refresh_app_token

    diagnosis = diagnose_login_curl(cfg.weread_app_curl)
    if diagnosis:
        return False, f"[异常] WEREAD_APP_CURL：{diagnosis}"
    result = refresh_app_token(cfg.weread_app_curl)
    if not result.ok:
        return False, f"[异常] WEREAD_APP_CURL：{result.diagnosis}"
    platform = "iOS" if result.token_key == "skey" else "Android"
    return True, (
        f"[正常] WEREAD_APP_CURL：/login 重放成功，平台自识别为 {platform}"
        f"（依据响应字段 {result.token_key}），token={result.token[:8]}..."
    )


def _check_push(cfg: Config) -> tuple[bool, str]:
    """检查推送渠道配置。"""
    method = cfg.push_method
    if not method:
        return False, "[异常] 推送渠道：未配置，检查结果无法推送到手机（仅见日志）"
    return True, f"[正常] 推送渠道：{method}"


def main() -> int:
    """配置检查入口。返回 0（全部正常）或 1（任一异常）。"""
    setup_logging()
    cfg = load_config()

    results = [
        _check_web_curl(cfg),
        _check_app_curl(cfg),
        _check_push(cfg),
        (
            True,
            f"[信息] READ_NUM={cfg.read_num}（约 {cfg.read_num // 2} 分钟），"
            f"EXCHANGE_AWARD={cfg.exchange_award}",
        ),
    ]

    lines = [line for _, line in results]
    all_ok = all(ok for ok, _ in results)
    report = "WeReadIt 配置检查报告\n\n" + "\n".join(lines)
    if all_ok:
        report += "\n\n全部检查通过，托管就绪。"
    else:
        report += "\n\n存在异常项，请按上方指引修正后重新检查。"

    logger.info("\n%s", report)
    if cfg.push_method:
        client = HttpClient(headers=cfg.headers, cookies=cfg.cookies)
        try:
            push(report, cfg.push_method, client, cfg, is_success=all_ok)
        finally:
            client.close()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

创建 `.github/workflows/config-check.yml`：

```yaml
name: WeReadIt 配置检查

on:
  workflow_dispatch:  # 手动触发

jobs:
  check:
    runs-on: ubuntu-22.04
    environment: AutoRead  # 与主 workflow 同一环境，读取相同 Secrets

    steps:

    - name: Set DNS to Google's DNS
      run: |
        echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf
        echo "nameserver 8.8.4.4" | sudo tee -a /etc/resolv.conf

    - name: Checkout repository
      uses: actions/checkout@v2

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.10'

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt

    - name: Run config check
      env:
        WEREAD_WEB_CURL: ${{ secrets.WEREAD_WEB_CURL }}
        WEREAD_APP_CURL: ${{ secrets.WEREAD_APP_CURL }}
        PUSHPLUS: ${{ secrets.PUSHPLUS }}
        WXPUSHER: ${{ secrets.WXPUSHER }}
        TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
        TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        SERVERCHAN: ${{ secrets.SERVERCHAN }}
        READ_NUM: ${{ vars.READ_NUM }}
        EXCHANGE_AWARD: ${{ vars.EXCHANGE_AWARD }}
      run: python -m wereadit.config_check
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_config_check.py -q && python -m ruff check src/wereadit/config_check.py tests/test_config_check.py
```

预期：全部 PASS，ruff 无告警

- [ ] **Step 5: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/config_check.py .github/workflows/config-check.yml tests/test_config_check.py && git commit -m "[config] 配置检查 workflow：静态体检 + /login 重放验证，报告直推手机"
```

---

### Task 3: reader 日志回显优化

**Files:**
- Modify: `src/wereadit/core/reader.py:199-202, 224-235, 250-251, 259-264`
- Test: `tests/test_reader_logs.py`（新建）

**Interfaces:**
- Consumes: 无（独立任务）
- Produces: 无代码接口（仅呈现层变更）

**红线**：只改打印时机/文案/级别；fix 调用、重试、退避、熔断阈值、`index`/`last_time`/`streak` 流转一行不动。熔断文案必须保留"连续"与"synckey"字样（test_keepalive.py:184 断言依赖）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_reader_logs.py`：

```python
"""reader 日志回显：进度去重 + synckey 常态日志简化（行为逻辑不变的回归保障）。"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

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


def _mock_response(json_data: dict, set_cookies: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_data
    resp.cookies = set_cookies or {}
    return resp


class TestProgressDeduplication:
    """进度打印：index 未变化时不重复打印。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_progress_not_repeated_on_retry(self, mock_post: MagicMock) -> None:
        """轮1修复未生效退避后，index 仍为 1，进度行不得二次打印。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, no_synckey,  # 轮1：修复未生效，退避
            ok_resp,  # 轮2（index 仍 1）：成功，index → 2
            ok_resp,  # 轮3（index 2）：成功，index → 3 退出
        ]

        prints: list[str] = []
        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with patch("wereadit.core.reader.time.sleep"):
            read_books(client, cfg, refresh_print=prints.append)

        assert prints == [
            "阅读进度: 第 1/2 次，已阅读 0.0 分钟",
            "阅读进度: 第 2/2 次，已阅读 0.5 分钟",
        ]


class TestSynckeyLogPresentation:
    """synckey 常态日志：合并为人话一行，级别随连续失败升级。"""

    @patch("wereadit.infra.http.requests.Session.post")
    def test_fix_log_merged_info_once(self, mock_post: MagicMock, caplog) -> None:
        """首次修复：一行 INFO，不再有 WARNING 旧文案与第二行 INFO。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,  # 轮1：修复后重试成功
            ok_resp,  # 轮2：成功退出
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        messages = [r.message for r in caplog.records]
        fix_logs = [m for m in messages if "已自动修复并重试" in m]
        assert fix_logs == ["第 1/2 次：阅读上下文未同步，已自动修复并重试"]
        assert not any("尝试修复" in m for m in messages)
        assert not any("fix_no_synckey 已调用" in m for m in messages)
        # 首次修复场景无 WARNING
        assert not any(
            r.levelno == logging.WARNING and "修复未生效" in r.message
            for r in caplog.records
        )

    @patch("wereadit.infra.http.requests.Session.post")
    def test_backoff_info_then_warning_on_second_streak(
        self, mock_post: MagicMock, caplog
    ) -> None:
        """修复未生效：streak=1 为 INFO，streak=2（逼近熔断）升 WARNING。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, no_synckey,  # 轮1：streak=1，退避
            no_synckey, fix_resp, no_synckey,  # 轮2：streak=2，退避（WARNING）
            ok_resp,  # 轮3：成功退出（streak 清零）
        ]

        cfg = _make_cfg()
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
    def test_fix_retry_success_log(self, mock_post: MagicMock, caplog) -> None:
        """修复后重试成功：人话 INFO 行。"""
        renewal = _mock_response({"succ": 1}, set_cookies={"wr_skey": "new12345"})
        no_synckey = _mock_response({"succ": 1})
        fix_resp = _mock_response({})
        ok_resp = _mock_response({"succ": 1, "synckey": "abc"})
        mock_post.side_effect = [
            renewal,
            no_synckey, fix_resp, ok_resp,
            ok_resp,
        ]

        cfg = _make_cfg()
        client = HttpClient(cookies={"wr_skey": "old"})
        with (
            patch("wereadit.core.reader.time.sleep"),
            caplog.at_level(logging.INFO, logger="wereadit.core.reader"),
        ):
            read_books(client, cfg)

        assert any("修复成功" in r.message for r in caplog.records)
        assert not any("synckey 修复成功" in r.message for r in caplog.records)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/test_reader_logs.py -q
```

预期：FAIL（进度重复、旧文案仍在）

- [ ] **Step 3: 实现**

`src/wereadit/core/reader.py` 改动（仅呈现层）：

1. 循环变量区（`circuit_breaker_triggered = False` 之后）新增：

```python
    last_printed_index = 0  # 进度打印去重：仅在 index 变化时打印
```

2. 循环顶部进度打印（原 199-202 行）替换为：

```python
        if refresh_print and index != last_printed_index:
            last_printed_index = index
            refresh_print(
                f"阅读进度: 第 {index}/{total} 次，已阅读 {(index - 1) * 0.5:.1f} 分钟"
            )
```

3. 无 synckey 修复两行（原 232-235 行）替换为：

```python
                logger.info(
                    "第 %d/%d 次：阅读上下文未同步，已自动修复并重试", index, total
                )
                fix_no_synckey(client, cfg)
                no_synckey_fix_triggered += 1
```

4. 熔断 msg（原 225-228 行）替换为（保留"连续"与"synckey"字样）：

```python
                    msg = (
                        f"连续 {MAX_NO_SYNCKEY} 次无 synckey 修复无效，任务中止"
                        f"（已完成 {index - 1}/{total} 次）。"
                        "通常是 cookie 失效或触发风控，请检查 WEREAD_WEB_CURL"
                    )
```

5. fix 后重试成功日志（原 251 行 `logger.info("synckey 修复成功，继续阅读")`）替换为：

```python
                    logger.info("第 %d/%d 次：修复成功，继续阅读", index, total)
```

6. 退避日志（原 260-263 行）替换为：

```python
                backoff_log = (
                    logger.warning
                    if no_synckey_streak >= MAX_NO_SYNCKEY - 1
                    else logger.info
                )
                backoff_log(
                    "第 %d/%d 次：修复未生效，%ds 后重试（连续 %d/%d 次）",
                    index,
                    total,
                    CIRCUIT_BREAKER_BACKOFF,
                    no_synckey_streak,
                    MAX_NO_SYNCKEY,
                )
```

- [ ] **Step 4: 跑测试确认通过（含保活回归）**

```bash
cd "S:\Github Repositories\WeReadIt" && python -m pytest tests/ -q && python -m ruff check src/wereadit/core/reader.py tests/test_reader_logs.py
```

预期：全部 PASS（含 test_keepalive.py 35 个保活回归），ruff 无告警

- [ ] **Step 5: Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && git add src/wereadit/core/reader.py tests/test_reader_logs.py && git commit -m "[reader] 日志回显优化：进度打印去重，synckey 常态日志合并为人话并随连续失败升级"
```

---

### Task 4: 文档与主 workflow 同步

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `changelog.md`
- Modify: `.github/workflows/deploy.yml`

**Interfaces:**
- Consumes: Task 1-3 的最终行为
- Produces: 无代码接口

- [ ] **Step 1: deploy.yml env 区更新**

`.github/workflows/deploy.yml` 的 env 区替换为（删两个 token 行，改两个新名）：

```yaml
      env:
        WEREAD_WEB_CURL: ${{ secrets.WEREAD_WEB_CURL }}
        WEREAD_APP_CURL: ${{ secrets.WEREAD_APP_CURL }}
        PUSHPLUS: ${{ secrets.PUSHPLUS }}
        WXPUSHER: ${{ secrets.WXPUSHER }}
        TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
        TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        SERVERCHAN: ${{ secrets.SERVERCHAN }}
        READ_NUM: ${{ vars.READ_NUM }}
        EXCHANGE_AWARD: ${{ vars.EXCHANGE_AWARD }}
```

- [ ] **Step 2: README 重写配置相关章节**

1. 配置表（约第 33-40 行）替换为：

```markdown
| 配置项             | 类型     | 要求 | 默认值             | 说明                                                     |
| ------------------ | -------- | ---- | ------------------ | -------------------------------------------------------- |
| `WEREAD_WEB_CURL`  | Secret   | 必填 | -                  | 网页端 read 请求 cURL（第 1 步复制），阅读与 `wr_vid` 来源 |
| `WEREAD_APP_CURL`  | Secret   | 推荐 | -                  | App 端 `/login` 请求 cURL（body 须含 `deviceId`），兑换 Token 全自动续期与平台自识别 |
| `READ_NUM`         | Variable | 选填 | `120`              | 阅读次数（120 次 ≈ 60 分钟）                              |
| `EXCHANGE_AWARD`   | Variable | 选填 | `2,2,2,2,2,2,2,2` | 兑换策略：`0`=不兑换，`1`=体验卡，`2`=书币                 |
```

2. 抓包章节（web 端与 App 端两节）按"三步走"重写，并新增配置检查按钮与迁移说明，完整替换为：

```markdown
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

GitHub 仓库页 → 顶栏 `Actions` → 左侧选 **WeReadIt 配置检查** → 右侧 **Run workflow** → 几秒后推送收到检查报告。全部 `[正常]` 即托管就绪；有 `[异常]` 则按报告内指引修正后重新检查。

### 迁移说明（2026-07-22 之前的老用户）

Secret 已改名并精简：

1. `WEREAD_CURL_BASH` → 改名 `WEREAD_WEB_CURL`（值不变，删旧建新）。
2. `WEREAD_LOGIN_CURL` → 改名 `WEREAD_APP_CURL`（值不变，删旧建新）。
3. `WEREAD_ANDROID_TOKEN` / `WEREAD_IOS_TOKEN` → 直接删除（兑换 Token 现已完全由 `WEREAD_APP_CURL` 自动生成）。

改完后点一次「配置检查」验证（见第 3 步）。
```

3. 删除原「Token 自动续期」节（内容已被三步走覆盖），保留末尾的兜底 blockquote（手机端定时抓取方案）。

- [ ] **Step 3: AGENTS.md 更新**

1. Architecture 树：`config.py` 行改为 `# Config frozen dataclass + property 派生（平台从 app_token_key 自识别）`；`core/` 下 `exchanger.py` 行改为 `# 奖励兑换（token 外部注入 + 年龄超 90 分钟补刷保险）`；新增一行 `config_check.py` 位于 `app.py` 行之后：`├── config_check.py # 配置检查入口（静态体检 + /login 重放验证，供 config-check.yml 调用）`。
2. Key Design Decisions：
   - 「Config 是 frozen dataclass」条改为：`Config 是 frozen dataclass：加载后不可变，推送渠道通过 @property 自动检测；兑换 token 不走环境变量，由 app.py 阅读前 /login 重放刷新后 dataclasses.replace 注入 app_token/app_token_key，weread_access_token/weread_platform 两 property 从其派生（skey → iOS）。`
   - 「兑换 Token 自动续期」条中"若配置了 WEREAD_LOGIN_CURL"等旧名表述全部改为 `WEREAD_APP_CURL`，并删除关于手动 token 的历史描述。
3. Changelog 节「添加」最上方插入：

```markdown
- **配置检查 workflow**: 新增 `config-check.yml`（手动触发）与 `config_check.py` 入口：WEREAD_WEB_CURL 解析与关键 cookie 校验、WEREAD_APP_CURL 静态体检 + 真实 /login 重放验证（平台自识别 + token 前 8 位）、推送渠道检测、READ_NUM/EXCHANGE_AWARD 显示；报告直推手机，退出码反映整体状态。
```

「修复」最上方插入：

```markdown
- **阅读日志回显**: 进度打印去重（fix 退避后同一进度不再重复输出）；synckey 常态日志三行合并为一行人话（INFO），连续失败 2/3 次升 WARNING，熔断文案保留排查信息；不含 synckey/fix_no_synckey 实现术语。
```

「优化」最上方插入：

```markdown
- **Secret 精简改名（无旧名兼容）**: `WEREAD_CURL_BASH`→`WEREAD_WEB_CURL`、`WEREAD_LOGIN_CURL`→`WEREAD_APP_CURL`；`WEREAD_ANDROID_TOKEN`/`WEREAD_IOS_TOKEN` 彻底删除（兑换 token 完全由 /login 重放生成，平台自识别）；刷新失败且无 token 时跳过兑换并以失败状态推送（exit_code=1），不再用必然过期的种子 token 空跑一次。
```

4. **全文焕新旧名**：AGENTS.md 内所有 `WEREAD_CURL_BASH`、`WEREAD_LOGIN_CURL`（含历史残留 `WEREAD_LOGIN_CURL_BASH`）、`WEREAD_ANDROID_TOKEN`、`WEREAD_IOS_TOKEN` 字样全部改写为当前名称与当前表述 —— 包括 Changelog 节的历史条目（条目语义与日期定位保留，涉及的 Secret 名按 `WEREAD_WEB_CURL`/`WEREAD_APP_CURL` 改写；提到"手动 token/抓包 Token"的表述改为"由 WEREAD_APP_CURL 自动生成"），确保 Step 5 的 grep 零命中。

- [ ] **Step 4: changelog.md 更新**

在文件顶部插入（保持三分类、无日期头）：

```markdown
### 添加

- **配置检查按钮**: Actions 手动触发「WeReadIt 配置检查」，几秒出报告直推手机，配完立刻知道对错

### 修复

- **阅读日志**: 进度不再重复打印，synckey 修复提示合并为一行人话，连续失败会升级告警

### 优化

- **配置精简**: Secret 只需两个（WEREAD_WEB_CURL + WEREAD_APP_CURL），手动 token 彻底取消，老用户按迁移说明删旧建新即可
```

（插入到既有内容之前，旧条目保留。）

- [ ] **Step 5: 全库旧名痕迹终扫 + 全量验证 + Commit**

```bash
cd "S:\Github Repositories\WeReadIt" && grep -rn "WEREAD_CURL_BASH\|WEREAD_LOGIN_CURL\|WEREAD_ANDROID_TOKEN\|WEREAD_IOS_TOKEN" src/ tests/ README.md AGENTS.md changelog.md .github/workflows/ ; python -m pytest tests/ -q && python -m ruff check src/ tests/
```

预期：grep 无输出；测试与 ruff 全过。然后：

```bash
cd "S:\Github Repositories\WeReadIt" && git add README.md AGENTS.md changelog.md .github/workflows/deploy.yml && git commit -m "[docs] Secret 改名精简与配置检查按钮：README 三步走、迁移说明、deploy.yml 焕新"
```

---

## Self-Review 记录

- **Spec 覆盖**：§1 配置模型（Task 1）、§2 平台自识别（Task 1）、§3 配置检查（Task 2）、§4 日志回显（Task 3）、§5 文档范围（Task 4）、无痕迹扫描（Task 1 Step 5 + Task 4 Step 5 双重）。
- **类型一致性**：`Config` 新字段与 property（Task 1 定义）在 Task 2（config_check 消费 web_curl/weread_app_curl/push_method/read_num/exchange_award）一致；`RefreshResult` 字段（既有）在 Task 1/2 测试使用一致。
- **占位符**：无 TBD/TODO；所有代码步骤含完整代码与命令。
- **已知裁定**：刷新失败且无 token 时 exit_code=1 + is_success=False（spec 未明确，本计划裁定，已在 Task 1 标注）；熔断文案保留"synckey"（keepalive 回归断言硬约束）。
