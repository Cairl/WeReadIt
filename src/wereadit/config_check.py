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
    # vid 是账号身份 ID（PII），报告会 logger.info 到 Actions 日志（公开仓库任何人可见），脱敏前 4 位
    vid = cfg.cookies["wr_vid"]
    vid_preview = f"{vid[:4]}****" if len(vid) > 4 else "****"
    return True, f"[正常] WEREAD_WEB_CURL：解析成功，vid={vid_preview}"


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


def main() -> int:
    """配置检查入口。返回 0（全部正常）或 1（任一异常）。"""
    setup_logging()
    cfg = load_config()

    results = [
        _check_web_curl(cfg),
        _check_app_curl(cfg),
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
