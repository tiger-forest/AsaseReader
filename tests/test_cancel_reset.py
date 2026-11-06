"""取消令牌的复位 —— 回归测试。

曾经的真实 bug：cancel() 只置位、从不复位，导致**取消过一次之后，
之后每一个任务都在第一个 checkpoint 被立刻判成已取消**，
表现为"怎么重试、怎么新加，一下载就自动取消"。

本测试钉死三件事：
1. 取消照样能停掉正在跑的任务（原有行为不能破坏）
2. 取消**消费完**之后令牌必须复位（重试 / 新增 / worker 空闲三条路径）
3. 还有任务在跑时不能抢先复位（否则用户点的取消就失效了）

不发任何网络请求，页面与图片都用打桩。

跑法（也由 tests/selftest.py 统一调用）：
    python tests/test_cancel_reset.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import os  # noqa: E402
from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
import asase_reader.backend as backend_mod  # noqa: E402
from asase_reader.backend import Backend, Job  # noqa: E402
from asase_reader.control import Control  # noqa: E402
from asase_reader.fetch import Fetcher  # noqa: E402
from asase_reader.ratelimit import RateLimiter  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
NORMAL_HTML = (FIX / "opus_1261150454223470609.html").read_text(encoding="utf-8")
OPUS_ID = "1261150454223470609"  # 与夹具里的 opus_id 一致

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class StubFetcher(Fetcher):
    """打桩页面与图片，不发网络请求。"""

    def __init__(self, page_limiter, image_limiter, **kw) -> None:
        super().__init__(RateLimiter(0, 0), RateLimiter(0, 0), **kw)
        self.img_calls: list[str] = []

    async def fetch_page(self, url: str, cookie: str = "") -> str:
        return NORMAL_HTML

    async def fetch_image(self, url: str) -> bytes:
        self.img_calls.append(url)
        return PNG


def new_backend(tmp: Path) -> Backend:
    ensure_tmp_env()
    shelf = tmp / "shelf"
    state = tmp / "state"
    shelf.mkdir(parents=True)
    state.mkdir()
    assert_tmp_guard(shelf, "shelf")
    assert_tmp_guard(state, "state")
    assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
    b = Backend(state_dir=state)
    b.config["shelf_dir"] = str(shelf)
    b.config["download_dir"] = ""
    b.config["setup_done"] = True
    for k in ("delay_min", "delay_max", "image_delay_min", "image_delay_max"):
        b.config[k] = 0
    return b


async def _wait_idle(b: Backend, timeout: float) -> bool:
    """等到队列里没有 queued / running 的任务。"""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if not any(j.status in ("queued", "running") for j in b.jobs):
            return True
        await asyncio.sleep(0.05)
    return False


async def drive_worker(b: Backend, timeout: float = 8.0) -> bool:
    """真跑一段 worker 循环，直到队列跑空或超时。"""
    b._stop = False
    task = asyncio.create_task(b._worker())
    try:
        ok = await asyncio.wait_for(_wait_idle(b, timeout - 0.5), timeout)
    finally:
        b._stop = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return ok


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="asase_cancel_"))
    orig_fetcher = backend_mod.Fetcher
    backend_mod.Fetcher = StubFetcher
    try:
        print("== 1. Control 令牌本身：取消 → 复位")
        ctrl = Control()
        check(not ctrl.cancelled, "初始未取消")
        ctrl.cancel()
        check(ctrl.cancelled, "cancel() 后置位")
        ctrl.reset()
        check(not ctrl.cancelled, "reset() 后解除")
        ctrl.cancel()
        ctrl.pause()
        ctrl.cancel()
        check(not ctrl.paused, "取消会顺带解除暂停（不卡在暂停里）")

        print("== 2. 端到端：取消 → 重试 → 真的能跑完（★ 本 bug 的复现点）")
        b = new_backend(tmp / "t2")
        b.add_urls(OPUS_ID)
        job = b.jobs[0]
        check(job.status == "queued", "任务已入队")

        b.control.cancel()  # 模拟用户点取消（worker 还没跑）
        asyncio.run(drive_worker(b))
        check(job.status == "cancelled", f"取消后任务被停（实际 {job.status}）")
        check(
            not b.control.cancelled,
            "worker 跑空后，空闲分支兜底把令牌复位了",
        )

        # 重新置位，模拟"取消过一次"的残留状态，专门验证 retry 这条复位路径
        b.control.cancel()
        check(b.control.cancelled, "再次置位，模拟残留的取消状态")

        b.retry_job(OPUS_ID)  # 用户点重试
        check(job.status == "queued", "重试把任务放回队列")
        check(
            not b.control.cancelled,
            "★ 重试后令牌已复位（否则会在第一个 checkpoint 又被取消）",
        )
        ok = asyncio.run(drive_worker(b))
        check(ok, "worker 在超时前跑完了队列")
        check(
            job.status == "done",
            f"★ 重试的任务真的下载完成了（实际 {job.status} / {job.error}）",
        )

        print("== 3. 端到端：取消 → 新增任务 → 也能跑完")
        b3 = new_backend(tmp / "t3")
        b3.add_urls(OPUS_ID)
        b3.control.cancel()
        asyncio.run(drive_worker(b3))
        check(b3.jobs[0].status == "cancelled", "先被取消")
        # 取消发生在第一个 checkpoint，一张图都没下、没写 meta.json，
        # 所以同一个 ID 可以重新下（不会撞"已在书架"跳过）
        b3.jobs.clear()
        b3.control.cancel()  # 再次置位，模拟残留状态
        b3.add_urls(OPUS_ID)
        check(
            not b3.control.cancelled,
            "★ 新增任务后令牌已复位",
        )
        ok3 = asyncio.run(drive_worker(b3))
        check(ok3, "worker 跑完了")
        check(
            b3.jobs[-1].status == "done",
            f"★ 取消之后新加的任务能正常下载（实际 {b3.jobs[-1].status} / {b3.jobs[-1].error}）",
        )

        print("== 4. 有任务在跑时不抢先复位（不能让用户点的取消失效）")
        b4 = new_backend(tmp / "t4")
        running = Job(opus_id=OPUS_ID)
        running.status = "running"
        b4.jobs = [running]
        b4.control.cancel()
        b4._clear_cancel_if_idle()
        check(
            b4.control.cancelled,
            "有任务正在跑 → 令牌保持取消，正在跑的那个照样会被停",
        )

        print("== 5. worker 空闲时兜底复位")
        b5 = new_backend(tmp / "t5")
        b5.jobs = []
        b5.control.cancel()
        b5._clear_cancel_if_idle()
        check(not b5.control.cancelled, "队列里没在跑的 → 复位")

        print("== 6. 粘 Cookie 触发的重新排队也会复位")
        b6 = new_backend(tmp / "t6")
        j6 = Job(opus_id=OPUS_ID)
        j6.status = "failed"
        j6.need_cookie = True
        b6.jobs = [j6]
        b6.control.cancel()
        b6.submit_cookie("SESSDATA=x; bili_jct=y; DedeUserID=1")
        check(j6.status == "queued", "等待登录态的任务被重新排队")
        check(not b6.control.cancelled, "★ 重新排队后令牌已复位")
    finally:
        backend_mod.Fetcher = orig_fetcher
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"失败 {len(fails)} 项：")
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("取消令牌复位：全部通过")


if __name__ == "__main__":
    main()
