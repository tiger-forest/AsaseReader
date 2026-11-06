"""限速与风控退避。

核心原则：
- 请求间隔加随机抖动，避免固定间隔的机器特征；
- 触发 412/429 时指数退避（30s -> 2min -> 10min），连续三次直接终止任务；
- 页面请求有每日上限，图片 CDN 请求宽松一些但同样有间隔。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import date
from pathlib import Path

from .errors import DailyLimitReached, RiskControlError

_RISK_BACKOFF = [30.0, 120.0, 600.0]


class RateLimiter:
    """异步限速器：随机间隔 + 可选每日上限 + 412/429 惩罚退避。"""

    def __init__(
        self,
        min_interval: float = 2.0,
        max_interval: float = 5.0,
        daily_limit: int | None = None,
        state_path: Path | None = None,
    ) -> None:
        if min_interval < 0 or max_interval < min_interval:
            raise ValueError("非法的限速区间参数")
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.daily_limit = daily_limit
        self.state_path = state_path
        self._next_allowed = 0.0
        self._penalty_until = 0.0
        self._risk_hits = 0
        self._today = date.today().isoformat()
        self._daily_count = self._load_today_count()

    # ---- 每日计数持久化 ----

    def _load_today_count(self) -> int:
        if not self.state_path or not self.state_path.exists():
            return 0
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data.get("date") == self._today:
                return int(data.get("count", 0))
        except Exception:
            pass
        return 0

    def _save_today_count(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"date": self._today, "count": self._daily_count}),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception:
            pass  # 计数持久化失败不阻断任务

    # ---- 对外接口 ----

    async def acquire(self) -> None:
        """在发起一次请求前调用：睡眠到允许时刻，并推进下一次的允许时间。"""
        if self.daily_limit is not None:
            if self._daily_count >= self.daily_limit:
                raise DailyLimitReached(
                    f"已达每日上限 {self.daily_limit} 次请求，明天再跑吧（不要改小改大继续刚）"
                )
            self._daily_count += 1
            self._save_today_count()

        now = time.monotonic()
        wait_until = max(self._next_allowed, self._penalty_until)
        delay = wait_until - now
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_allowed = time.monotonic() + random.uniform(
            self.min_interval, self.max_interval
        )

    def penalize_risk(self) -> float:
        """遇到 412/429 时调用。返回本次应退避的秒数；超过 3 次抛 RiskControlError。"""
        self._risk_hits += 1
        if self._risk_hits > len(_RISK_BACKOFF):
            raise RiskControlError(
                "连续多次触发风控拦截（412/429），已自动停止。请今天先别再跑，"
                "正常用浏览器逛逛 B 站即可，明天再试。"
            )
        backoff = _RISK_BACKOFF[self._risk_hits - 1]
        self._penalty_until = time.monotonic() + backoff
        return backoff

    def note_success(self) -> None:
        self._risk_hits = 0

    @property
    def risk_hits(self) -> int:
        return self._risk_hits

    @property
    def daily_count(self) -> int:
        return self._daily_count
