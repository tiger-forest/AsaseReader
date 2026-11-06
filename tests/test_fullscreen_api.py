"""全屏 API 契约测试（Python 侧）。

jsdom 测不了真实的窗口全屏，但它测不到**这里**的东西也一样重要：
`GuiApi.toggle_fullscreen()` 是浏览器那条链路的兜底，它的契约必须在 Python 侧钉死——
尤其是「拿不到窗口 / 底层报错时要返回 ok:False，而不是抛异常或假装成功」。

（这一项是变异测试补出来的：把无窗口的兜底改成返回 ok:True，前端就会以为自己进了全屏，
而 GuiApi 冒烟测试只验证"调用不抛异常"，照样全绿。）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader.backend import Backend  # noqa: E402
from asase_reader.gui import GuiApi  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="asase_fullscreen_api_"))
ensure_tmp_env()
assert_tmp_guard(TMP, "state_dir")

fails: list[str] = []


def ok(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        fails.append(label)


class FakeWindow:
    """打桩窗口：记录调用次数，可按需抛异常。"""

    def __init__(self, boom: bool = False) -> None:
        self.n = 0
        self.boom = boom

    def toggle_fullscreen(self) -> None:
        self.n += 1
        if self.boom:
            raise RuntimeError("原生全屏在这台机器上失灵了")


print("--- 无窗口：不许抛异常，要如实说没成功 ---")
b = Backend(state_dir=TMP / "state")
api = GuiApi(b)
ok(getattr(b, "window", None) is None, "测试环境下 backend.window 是 None")
r = api.toggle_fullscreen()
ok(isinstance(r, dict) and r.get("ok") is False, f"无窗口返回 ok:False（实际 {r}）")
ok(r.get("fullscreen") is False, "无窗口时状态仍是未全屏")

print("--- 有窗口：状态自己记账（pywebview 不会更新 Window.fullscreen）---")
w = FakeWindow()
b.window = w
r1 = api.toggle_fullscreen()
ok(r1.get("ok") is True and r1.get("fullscreen") is True, f"第一次进入（实际 {r1}）")
ok(w.n == 1, f"底层窗口被切换了一次（实际 {w.n} 次）")
r2 = api.toggle_fullscreen()
ok(r2.get("fullscreen") is False, f"第二次是退出（实际 {r2}）")
ok(w.n == 2, "累计两次")
r3 = api.toggle_fullscreen()
ok(r3.get("fullscreen") is True, "第三次又进入")
ok(w.n == 3, "状态与调用次数始终同步")

print("--- 底层报错：降级而不是让阅读器崩掉 ---")
w2 = FakeWindow(boom=True)
b.window = w2
before = dict(vars(api))
r = api.toggle_fullscreen()
ok(isinstance(r, dict) and r.get("ok") is False, f"抛异常时返回 ok:False（实际 {r}）")
ok("失灵" in (r.get("reason") or ""), "reason 里带上原因，方便排错")
ok(api._fullscreen == before.get("_fullscreen"), "异常不改变已记账的状态")

print("--- 窗口置 topmost：盖住任务栏的那一刀 ---")
import os  # noqa: E402

from asase_reader.gui import _set_always_on_top  # noqa: E402

ok(_set_always_on_top(None, True) is False, "没有窗口对象：返回 False（不抛异常）")
ok(_set_always_on_top(object(), True) is False, "窗口没有 native.Handle：返回 False")


class FakeHandle:
    def ToInt64(self):
        return 0      # 0 = 拿不到真句柄


class FakeNative:
    Handle = FakeHandle()


class FakeWinWithHandle:
    native = FakeNative()


ok(_set_always_on_top(FakeWinWithHandle(), True) is False, "句柄是 0：返回 False")

if os.name == "nt":
    # v1.28.0 起 Windows 有句柄时走 SetWindowPos 主路径（不再碰 pywebview 的
    # toggle_fullscreen）。这里**故意用无效句柄**：真实 SetWindowPos 对它返回 0
    # （失败）—— 正好拿来钉「失败必须如实上报、绝不翻状态」这条契约：
    # v1.29.0 就是因为不查返回值，裸 int 的 -1/-2 被静默拒绝后照样 ok:True，
    # 全屏在真机上断了整个版本（探针 .cache/probe_fullscreen2.py 实锤）。
    # （成功路径的参数级断言由 test_window_anim.py 的假 user32 负责。）
    hwnd = 0x0BADF00D

    class RealNative:
        Handle = type("H", (), {"ToInt64": staticmethod(lambda: hwnd)})()

    class RealWin(FakeWindow):
        native = RealNative()

    b.window = RealWin()
    api._fullscreen = False
    api._on_top = False
    r = api.toggle_fullscreen()
    ok(r.get("ok") is False and api._fullscreen is False
       and api._on_top is False,
       f"无效句柄铺满失败：ok:False 且状态不翻（实际 {r}）—— "
       f"失败还 ok:True 就是 v1.29.0 那个静默断全屏")
    ok(bool(r.get("reason")), f"失败要在 reason 里说明原因（实际 {r}）")
else:
    print("SKIP  非 Windows：置 topmost 恒为 False，只测了上面几条兜底")

print()
shutil.rmtree(TMP, ignore_errors=True)
if fails:
    print(f"FAILED {len(fails)}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
