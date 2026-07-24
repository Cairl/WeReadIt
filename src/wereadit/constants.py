"""项目常量：URL、加密盐、默认值、平台标识、奖励等级。

所有硬编码值集中在此，便于一处修改全局生效。
"""

from __future__ import annotations

# 阅读/登录接口
READ_URL = "https://weread.qq.com/web/book/read"
RENEW_URL = "https://weread.qq.com/web/login/renewal"
FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"

# 兑换接口
EXCHANGE_URL = "https://i.weread.qq.com/weekly/exchange"

# App 端 /login 接口超时（用于刷新 skey/accessToken，实现兑换 Token 自动续期）
LOGIN_TIMEOUT = 15
# App 端 /login 重试参数（刷新 token，网络异常时退避重试）
LOGIN_MAX_ATTEMPTS = 3
LOGIN_RETRY_INTERVAL = 5

# 【保活策略 - SIGN_KEY 不能改】
# 用于 sg 字段签名: sg = sha256(ts + rn + SIGN_KEY)
# 逆向自微信读书 web 端 JS,服务器据此验证请求合法性
# 详见 wxread_keepalive_analysis.md 第 6.1 节
SIGN_KEY = "3c5c8717f3daf09iop3423zafeqoi"

# 阅读循环默认参数
DEFAULT_READ_NUM = 120
# 【保活策略 - 30 秒是经验值,不能调快】
# 模拟真实阅读"翻一页停 30 秒"的节奏,调快会触发风控
# 详见 wxread_keepalive_analysis.md 第 2.1 节
READ_INTERVAL_SECONDS = 30
SECONDS_PER_READ = 30  # 每次阅读计 30 秒

# 熔断阈值（防止死循环卡死 GitHub Actions）
# 连续 N 次无 synckey → 抛 ReadFailedError
# 连续 N 次 cookie 过期 → 抛 CookieExpiredError
MAX_NO_SYNCKEY = 3
MAX_COOKIE_FAIL = 3
# 熔断前的短暂退避（秒），避免连发请求触发风控
CIRCUIT_BREAKER_BACKOFF = 5

# refresh_cookie 多轮重试参数
# 第 1 轮失败后等 REFRESH_COOKIE_BASE_WAIT 秒重试第 2 轮，第 2 轮失败后等 2×BASE_WAIT 秒重试第 3 轮
# 总耗时 = 3 种 payload × 1s + 30s + 3 种 payload × 1s + 60s + 3 种 payload × 1s ≈ 96s
REFRESH_COOKIE_MAX_ROUNDS = 3
REFRESH_COOKIE_BASE_WAIT = 30

# 【保活策略 - 不能简化为 1 种】
# 应对 renewal 接口版本变化,3 种 payload 提高续期成功率
# 详见 wxread_keepalive_analysis.md 第 4.2 节
COOKIE_DATA_VARIANTS = [
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": False},
    {"rq": "%2Fweb%2Fbook%2Fread", "ql": True},
    {"rq": "%2Fweb%2Fbook%2Fread"},
]

# 【保活策略 - bookId 不能改】
# 不在 books.json 列表里,是写死的特殊 ID,用于触发服务器重建阅读上下文
# 长期删除会导致"刷了但时长不增加"
# 详见 wxread_keepalive_analysis.md 第 5.3 节
FIX_SYNCKEY_BOOK_IDS = ["3300060341"]

# HTTP 请求默认超时（秒）
DEFAULT_TIMEOUT = 10
# 【保活策略 - 各接口超时分级】
# read 主业务可能慢响应,放宽到 15s;renewal/fix 应快,保持 10s
READ_TIMEOUT = 15
RENEW_TIMEOUT = 10
FIX_SYNCKEY_TIMEOUT = 10
EXCHANGE_TIMEOUT = 15
PUSH_TIMEOUT = 30

# 推送重试参数
PUSH_MAX_ATTEMPTS = 3
PUSH_RETRY_MIN_WAIT = 180
PUSH_RETRY_MAX_WAIT = 360

# 兑换重试参数
EXCHANGE_MAX_RETRY = 3
EXCHANGE_RETRY_INTERVAL = 5

# 兑换 Token 补刷阈值（秒）：阅读前刷新后，若兑换前 token 年龄超过该值则补刷一次
# （App token 有效期约 2 小时，90 分钟留 30 分钟余量）
TOKEN_MAX_AGE_SECONDS = 90 * 60

# 平台标识
PLATFORM_ANDROID = "android"
PLATFORM_IOS = "ios"

# Android 平台常量
ANDROID_UA = (
    "WeRead/8.2.6 WRBrand/other Dalvik/2.1.0 "
    "(Linux; U; Android 14; 25102RKBEC Build/UQ1A.240205.07021608)"
)
ANDROID_BASEAPI = "34"
ANDROID_APPVER = "8.2.6.10163989"
ANDROID_OSVER = "14"
ANDROID_CHANNEL_ID = "0"
ANDROID_BASEVER = "8.2.6.10163989"
ANDROID_PF = "wechat_wx-2001-android-100-weread"

# iOS 平台常量
IOS_UA = "WeRead/10.2.0 (iPhone; iOS 26.5.2; Scale/3.00)"
IOS_BASEVER = "10.2.0.85"
IOS_V = "10.2.0.85"
IOS_CHANNEL_ID = "AppStore"
IOS_PF = "weread_wx-2001-iap-2001-iphone"

# 奖励等级 ID 顺序（对应 EXCHANGE_AWARD 策略字符串的 8 个位置）
# 顺序：5 个时长奖励（4,5,1,2,3）+ 3 个天数奖励（11,12,13）
AWARD_LEVEL_IDS = [4, 5, 1, 2, 3, 11, 12, 13]

# 兑换选择类型
CHOICE_NONE = 0
CHOICE_CARD = 1
CHOICE_COIN = 2

# accessToken/skey 过期错误码
ERRCODE_TOKEN_EXPIRED = -2012

# 默认兑换策略
DEFAULT_EXCHANGE_AWARD = "2,2,2,2,2,2,2,2"

# 【保活策略 - data 固定字段不能改成动态】
# 作者实测得出:appId/ci/co/sm/pr/ps/pc 必须保持初始值
# 改动会导致"阅读时间不增加"(README issue 已证实)
# 详见 wxread_keepalive_analysis.md 第 3.1 节
# 每次循环只更新: b/c(随机) + ct/rt/ts/rn/sg/s(动态)
DEFAULT_READ_DATA = {
    "appId": "wb182564874603h266381671",
    "b": "ce032b305a9bc1ce0b0dd2a",
    "c": "7f632b502707f6ffaa6bf2e",
    "ci": 27,
    "co": 389,
    "sm": "19聚会《三体》网友的聚会地点是一处僻静",
    "pr": 74,
    "rt": 15,
    "ts": 1744264311434,
    "rn": 466,
    "sg": "2b2ec618394b99deea35104168b86381da9f8946d4bc234e062fa320155409fb",
    "ct": 1744264311,
    "ps": "4ee326507a65a465g015fae",
    "pc": "aab32e207a65a466g010615",
    "s": "36cc0815",
}

# 【保活策略 - headers 必须保留 baggage Sentry 头】
# 这是 Sentry SDK 自动注入的浏览器指纹,真实浏览器才有
# 删除会被风控识别为非浏览器请求
# 详见 wxread_keepalive_analysis.md 第 6.3 节
DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,ko;q=0.5",
    "baggage": (
        "sentry-environment=production,sentry-release=dev-1730698697208,"
        "sentry-public_key=ed67ed71f7804a038e898ba54bd66e44,"
        "sentry-trace_id=1ff5a0725f8841088b42f97109c45862"
    ),
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
}

# 默认 cookies 模板（本地部署时由用户替换）
DEFAULT_COOKIES = {
    "RK": "oxEY1bTnXf",
    "ptcz": "53e3b35a9486dd63c4d06430b05aa169402117fc407dc5cc9329b41e59f62e2b",
    "pac_uid": "0_e63870bcecc18",
    "iip": "0",
    "_qimei_uuid42": "183070d3135100ee797b08bc922054dc3062834291",
    "wr_avatar": (
        "https%3A%2F%2Fthirdwx.qlogo.cn%2Fmmopen%2Fvi_32%2F"
        "eEOpSbFh2Mb1bUxMW9Y3FRPfXwWvOLaNlsjWIkcKeeNg6vlVS5kOVuhNKGQ1M8zaggLqMPmpE5qIUdqEXlQgYg%2F132"
    ),
    "wr_gender": "0",
}
