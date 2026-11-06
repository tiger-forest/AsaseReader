class BiliOpusError(Exception):
    """所有本项目异常的基类。"""


class ParseError(BiliOpusError):
    """页面结构无法解析（可能是风控拦截页、需要登录、专栏已删除）。"""


class NetworkError(BiliOpusError):
    """网络层错误或服务端 5xx，重试后仍失败。"""


class NotFoundError(BiliOpusError):
    """资源不存在（404）。"""


class RiskControlError(BiliOpusError):
    """触发 B 站风控（412 / 429），多次退避后仍被拦截。

    抛出后应停止整个任务，不要继续硬刚。
    """


class DailyLimitReached(BiliOpusError):
    """已触达每日请求上限，主动停止以免风险累积。"""
