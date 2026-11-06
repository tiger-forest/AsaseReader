"""HTTP 抓取层：单一 AsyncClient、低并发、限速、重试与风控退避。

风控要点：
- 完整的浏览器式请求头，连接复用；
- 所有请求都经过 RateLimiter，绝不裸奔；
- 412/429 视为风控信号，交给 limiter 惩罚退避，连续触发终止任务。
"""

from __future__ import annotations

import asyncio

import httpx

from .errors import NetworkError, NotFoundError, RiskControlError
from .ratelimit import RateLimiter

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.bilibili.com/",
}


class Fetcher:
    def __init__(
        self,
        page_limiter: RateLimiter,
        image_limiter: RateLimiter,
        concurrency: int = 2,
        timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        self.page_limiter = page_limiter
        self.image_limiter = image_limiter
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            headers=_BASE_HEADERS,
            timeout=timeout,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=concurrency, max_keepalive_connections=concurrency
            ),
        )
        self._sem = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def fetch_page(self, url: str, cookie: str = "") -> str:
        """抓取 opus 页面 HTML。

        cookie 只在这一处使用：充电专属内容需要登录态才拿得到完整图片清单。
        图片下载一律走 fetch_image（不带任何凭据）。
        """
        r = await self._request(url, self.page_limiter, cookie=cookie)
        return r.text

    async def fetch_image(self, url: str) -> bytes:
        """下载一张图片的二进制内容。永不携带登录凭据。"""
        r = await self._request(url, self.image_limiter)
        return r.content

    async def _request(
        self, url: str, limiter: RateLimiter, cookie: str = ""
    ) -> httpx.Response:
        async with self._sem:
            last_error: Exception | None = None
            for attempt in range(1, self.max_retries + 1):
                await limiter.acquire()
                try:
                    # 凭据只随本次请求发出，不进 client 的 cookie jar：
                    # httpx 默认会持久化 Set-Cookie 并在后续请求自动回传，
                    # 那会把登录态泄漏到图片请求上，也会让浏览器那边的 Cookie 被顶掉。
                    resp = await self._client.get(
                        url, headers={"Cookie": cookie} if cookie else None
                    )
                except httpx.HTTPError as e:
                    last_error = e
                    if attempt == self.max_retries:
                        raise NetworkError(f"网络错误（已重试 {attempt} 次）: {url} -> {e}") from e
                    await asyncio.sleep(2**attempt)
                    continue
                finally:
                    self._client.cookies.clear()

                if resp.status_code in (412, 429):
                    backoff = limiter.penalize_risk()
                    if attempt == self.max_retries:
                        raise RiskControlError(
                            f"请求被拦截（HTTP {resp.status_code}），退避 {backoff}s 后仍未恢复: {url}"
                        )
                    await asyncio.sleep(backoff)
                    continue

                if resp.status_code == 404:
                    raise NotFoundError(f"资源不存在（404）: {url}")

                if 500 <= resp.status_code < 600:
                    last_error = NetworkError(f"服务端错误 HTTP {resp.status_code}: {url}")
                    if attempt == self.max_retries:
                        raise last_error
                    await asyncio.sleep(2**attempt)
                    continue

                if resp.status_code >= 400:
                    raise NetworkError(f"意外状态码 HTTP {resp.status_code}: {url}")

                limiter.note_success()
                return resp

            raise NetworkError(f"请求失败: {url} ({last_error})")
