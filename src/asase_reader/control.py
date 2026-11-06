"""暂停 / 取消令牌（GUI 与 CLI 共用）。

设计为协作式：下载器在"每张图片之间"和"每篇之间"检查令牌，
不会粗暴掐断进行中的请求，保证文件不写一半。
"""

from __future__ import annotations

import asyncio


class OperationCancelled(Exception):
    """任务被用户取消（非错误，不应显示为红色异常）。"""


class Control:
    def __init__(self) -> None:
        self._paused = asyncio.Event()
        self._cancelled = asyncio.Event()

    # ---- 由任意线程调用的开关 ----

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.clear()  # 取消同时解除暂停，避免卡在暂停里

    def reset(self) -> None:
        """让取消令牌复位。

        取消是**一次性**的：它只该作用于"当前这一批"任务。跑完这一批之后
        若不复位，令牌会永远停在已取消状态，之后每个新任务在第一个
        checkpoint 就被判取消——表现为"取消过一次，之后怎么下都自动取消"。
        """
        self._cancelled.clear()

    # ---- 由下载协程调用 ----

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    async def checkpoint(self) -> None:
        """在安全的位置调用：取消则抛异常，暂停则等待，直到恢复或取消。"""
        if self._cancelled.is_set():
            raise OperationCancelled()
        while self._paused.is_set() and not self._cancelled.is_set():
            await asyncio.sleep(0.2)
        if self._cancelled.is_set():
            raise OperationCancelled()
