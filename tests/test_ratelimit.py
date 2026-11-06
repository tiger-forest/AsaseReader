"""风控红线专项测试：412 退避、每日上限、跨天重置、参数校验。

为什么单独写这一个文件：
变异测试证明，把 `penalize_risk()` 的退避改成 0、或者让每日上限失效，
现有的全部自动化测试**依然全绿** —— 也就是说，项目最看重的风控红线
（账号安全优先于下载速度）此前是零覆盖的。这个文件专门把它们钉死。

不发任何网络请求。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asase_reader.errors import DailyLimitReached, RiskControlError  # noqa: E402
from asase_reader.ratelimit import RateLimiter  # noqa: E402

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="asase_rl_test_"))


# ---------------------------------------------------------------- 1. 参数校验

print("== 1. 构造参数校验（防止配错成无间隔直连）")
try:
    RateLimiter(min_interval=-1, max_interval=5)
    check(False, "负的间隔应被拒绝")
except ValueError:
    check(True, "负的间隔被拒绝")

try:
    RateLimiter(min_interval=5, max_interval=2)
    check(False, "max < min 应被拒绝")
except ValueError:
    check(True, "max < min 被拒绝")

# ------------------------------------------------------------ 2. 412 风控退避

print("\n== 2. 412/429 触发时的指数退避（30s -> 120s -> 600s，第四次终止）")
rl = RateLimiter(0, 0)
expected = [30.0, 120.0, 600.0]
got = []
for _ in range(3):
    got.append(rl.penalize_risk())
check(got == expected,
      f"前三次退避 = {got}（应为 {expected}）——改回 0 就是不再退避，必须失败")
check(rl.risk_hits == 3, "risk_hits 累计到 3")

try:
    rl.penalize_risk()
    check(False, "连续第四次应抛 RiskControlError 终止任务")
except RiskControlError:
    check(True, "连续第四次抛 RiskControlError（触发风控就停，不硬刚）")

print("\n== 3. 成功后风控计数归零")
rl2 = RateLimiter(0, 0)
rl2.penalize_risk()
rl2.penalize_risk()
check(rl2.risk_hits == 2, "累计 2 次")
rl2.note_success()
check(rl2.risk_hits == 0, "成功后归零（下次触发重新从 30s 起算）")

# -------------------------------------------------------------- 4. 每日上限

print("\n== 4. 每日上限（每日请求数是硬性闸门）")


async def drain(limiter: RateLimiter, n: int) -> int:
    """连续 acquire n 次，返回在被拦下之前成功拿到的次数。"""
    ok = 0
    for _ in range(n):
        try:
            await limiter.acquire()
        except DailyLimitReached:
            return ok
        ok += 1
    return ok


state = tmpdir() / "daily.json"
rl3 = RateLimiter(0, 0, daily_limit=5, state_path=state)
n_ok = asyncio.run(drain(rl3, 10))
check(n_ok == 5, f"上限 5 时只能拿到 5 次（实际 {n_ok}）——改成不判断就会拿到 10 次")

try:
    asyncio.run(rl3.acquire())
    check(False, "超限后再 acquire 应抛 DailyLimitReached")
except DailyLimitReached:
    check(True, "超限后抛 DailyLimitReached")

check(rl3.daily_count == 5, f"计数停在 5（实际 {rl3.daily_count}）")

print("\n== 5. 每日计数会落盘（重启程序不重置）")
check(state.is_file(), "计数写进了 state 文件")
saved = json.loads(state.read_text(encoding="utf-8"))
check(saved.get("count") == 5, f"落盘的 count = {saved.get('count')}")

rl4 = RateLimiter(0, 0, daily_limit=5, state_path=state)
check(rl4.daily_count == 5, "新实例读回同一天的计数（防重启绕过上限）")

print("\n== 6. 跨天自动重置")
data = json.loads(state.read_text(encoding="utf-8"))
data["date"] = "2000-01-01"  # 昨天的记录
state.write_text(json.dumps(data), encoding="utf-8")
rl5 = RateLimiter(0, 0, daily_limit=5, state_path=state)
check(rl5.daily_count == 0, "换了日期后计数归零（明天能继续下）")

print("\n== 7. 没有上限时不应拦人")
rl6 = RateLimiter(0, 0)
n_ok = asyncio.run(drain(rl6, 50))
check(n_ok == 50, "未设 daily_limit 时不拦截")

print()
print("FAILED:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
