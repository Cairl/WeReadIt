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
pytest tests/                     # 全部单元测试
pytest tests/ -v                  # 详细输出
pytest tests/test_config.py -v    # 单文件
ruff check src/ tests/            # 代码检查
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
├── config.py       # Config frozen dataclass + property 自动检测
├── constants.py    # URL、加密盐、默认值、平台常量
├── core/
│   ├── reader.py   # 阅读循环 + Cookie 刷新
│   └── exchanger.py # 奖励兑换
├── infra/
│   ├── http.py     # HttpClient（requests.Session 封装）
│   └── curl_parser.py # cURL 命令解析
├── push/           # 推送渠道（策略模式 + @register 注册表）
│   ├── base.py
│   ├── registry.py
│   └── {pushplus,wxpusher,telegram,serverchan}.py
├── utils/          # crypto, logging
└── schemas/        # books.json（书籍/章节列表）
```

新增推送渠道：在 `push/` 下新建文件，用 `@register("xxx")` 装饰 `Pusher` 子类即可，无需改动主流程。

## Key Design Decisions

- **Config 是 frozen dataclass**：加载后不可变，推送渠道和兑换平台通过 `@property` 自动检测已配置的 token，无需显式指定 method/platform 字段。
- **推送注册表**：`push/registry.py` 用装饰器注册代替 if-elif 链。
- **Cookie 刷新**：失效后自动调 `login/renewal` 接口刷新，最多尝试 3 种 payload 变体。
- **兑换重试**：指数退避，最多 3 次，token 过期会抛出 `CookieExpiredError` 并推送通知。

## CI/CD

GitHub Actions 定时触发（`deploy.yml`），北京时间每天 00:00 运行。环境变量通过 Secrets/Variables 注入，详见 README。

## Pull Request Guidelines

- 标题格式：`[模块] 简要描述`，如 `[push] 新增钉钉推送渠道`
- 提交前确保 `pytest tests/` 和 `ruff check src/ tests/` 通过
