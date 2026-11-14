"""最大化态进全屏：双缓存预置的契约（2026-11-14）。

真机实测坐实的根因：Maximized→Normal 时窗口会被摆**两次**——
  Win32 `WINDOWPLACEMENT.rcNormalPosition`（SW_RESTORE 用）与 WinForms 私有的
  `Form::restoreBounds` / `restoredWindowBounds`（WinForms 随后又摆一次）。
两处都记着进全屏前的小窗口，只改任意一处都会露出 `1350x975` 的中间帧。
两处同时预置成全屏 → 几何 `2582x1550 → 2560x1600 → 2582x1626 → 2560x1600`，
全程铺满；退出时两处对称写回 → 用户点「还原」仍回 `1350x975`。

**退出也有同一类中间帧**（2026-11-14 补）：旧顺序是「先把两处缓存写回小矩形
→ 再 SW_MAXIMIZE」，状态切换期间窗口照样被摆回 `1350x975`（真机 7.5ms 出现、
持续约 38ms）。改成「先 SW_MAXIMIZE → 等 WS_MAXIMIZE 落定 → 才写回两处缓存」，
几何只剩 `2560x1600 → 2582x1550`。见 `.cache/probe_fs_exit_sequence.py`。

这里守自动化能守的部分：进出时序与作用域、字段探测、保存/写入/回滚对称、
状态轮询、失败整体降级。观感归人工回归（DEV_GUIDE）。
"""

from __future__ import annotations

import ctypes as _real_ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asase_reader import gui  # noqa: E402

src = (ROOT / "src" / "asase_reader" / "gui.py").read_text(encoding="utf-8")
fails: list[str] = []


def ok(cond: bool, label: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        fails.append(label)


def _plain(text: str) -> str:
    return "\n".join(l for l in text.splitlines()
                     if not l.strip().startswith("#"))


def _fn_src(name: str) -> str:
    i = src.index(name)
    j = src.find("\ndef ", i + 1)
    return src[i:j if j > 0 else len(src)]


def _toggle_code() -> str:
    i = src.index("    def toggle_fullscreen(self)")
    j = src.find("\n    def ", i + 1)
    body = src[i:j if j > 0 else len(src)]
    a = body.find('"""')
    b = body.find('"""', a + 3) if a >= 0 else -1
    if a >= 0 and b > 0:
        body = body[:a] + body[b + 3:]
    return _plain(body)


print("--- 静态：两处缓存都动、只在最大化态、退出先切状态后写回 ---")
t = _toggle_code()
# 锚点缺失时保持 FAIL 而不是 NameError（缺调用本身已经是一条红）
i_save = i_pre = i_restore = i_max = i_wait = i_wb = -1
try:
    i_save = t.index("self._fs_placement = _get_window_placement(hwnd)")
    i_pre = t.index("_preset_fullscreen_bounds(")
    i_restore = t.index("_restore_from_maximized(hwnd)")
    i_max = t.index("_show_window(hwnd, 3)")
    i_wait = t.index("_wait_maximized(hwnd)")
    i_wb = t.index("_restore_fullscreen_bounds(")
    ok(i_save < i_pre < i_restore,
       "保存原始 placement → 双缓存预置 → SW_RESTORE（反了就保存了被污染的值）")
    ok(i_max < i_wait < i_wb,
       "退出先 SW_MAXIMIZE → 等状态落定 → 才写回两处缓存（反了就退回退出闪烁）")
except ValueError as e:
    ok(False, f"toggle_fullscreen 里缺关键调用：{e}")

ok(t.count("_restore_fullscreen_bounds(") == 1,
   "一次退出只写回一次，统一走 helper")
try:
    i_fs2 = t.rindex("if self._fs_state == 2:")
    i_elif = t.index("elif self._fs_rect:")
    ok(i_fs2 < i_wb < i_elif,
       "写回只在最大化态分支，非最大化态不受影响")
except ValueError as e:
    ok(False, f"找不到最大化态分支边界：{e}")
ok("_wf_write(native, self._fs_wf_bounds)" not in t
   and "_set_window_placement(hwnd, self._fs_placement)" not in t,
   "退出分支不绕开 helper 直接写回（旧顺序被钉死）")
ok("if _wait_maximized" not in t and "if not _wait_maximized" not in t,
   "轮询超时也照常写回（缓存不能长期留着整屏矩形）")


def _fn_body(name: str) -> str:
    """剥掉 docstring 与注释行，只留可执行代码（防断言被文档文字满足）。"""
    body = _fn_src(name)
    a = body.find('"""')
    b = body.find('"""', a + 3) if a >= 0 else -1
    if a >= 0 and b > 0:
        body = body[:a] + body[b + 3:]
    return _plain(body)


wait_src = _fn_body("def _wait_maximized")
ok("0x01000000" in wait_src and "GetWindowLongPtrW" in wait_src,
   "等的是 WS_MAXIMIZE 样式位（与 _restore_from_maximized 同源）")
ok("time.monotonic()" in wait_src and "timeout" in wait_src
   and "time.sleep" in wait_src, "轮询有超时兜底且不空转")
ok("c_void_p" in wait_src, "句柄包 c_void_p（这套 ctypes 下裸 int 会炸）")

rb_src = _fn_body("def _restore_fullscreen_bounds")
ok("_wf_write(native, saved)" in rb_src
   and "_preset_restore_rect(" in rb_src,
   "写回 helper 两处都动（少一处就污染还原位置）")
ok("wp_orig.rcNormalPosition" in rb_src and "r.r - r.l" in rb_src
   and "r.b - r.t" in rb_src,
   "Win32 写回原始还原矩形，且按 (l,t,w,h) 换算")
ok("if saved" in rb_src and "wp_orig is not None" in rb_src,
   "无缓存 / 无 placement 时安全跳过")

ok(t.count("_preset_fullscreen_bounds(") == 1,
   "一次进入只预置一次，退出分支不预置")
ok("self._fs_state == 2 and bounds" in t,
   "预置只在最大化态 + 拿得到屏幕矩形时做（别的态会污染还原位置）")
ok("if self._fs_state != 0:" in t,
   "SW_RESTORE 仍对所有非普通态生效（预置失败要能降级回原路径）")
ok(t.count("bounds = _screen_bounds_phys(hwnd)") == 1,
   "预置与铺满共用同一份屏幕矩形")
ok(t.count("self._fs_wf_bounds = []") >= 2,
   "进入时重置 + 退出时清空，失败路径不会留陈旧值")

wf_fields = _plain(_fn_src("def _wf_fields"))
const_def = "\n".join(l for l in src.splitlines()
                      if l.startswith("_WF_BOUND_FIELDS"))
ok("_WF_BOUND_FIELDS" in wf_fields
   and '"restoreBounds"' in const_def
   and '"restoredWindowBounds"' in const_def,
   "探测字段覆盖 WinForms 两个还原矩形字段（常量 + 引用）")
ok("FieldType.FullName" in wf_fields and "BaseType" in wf_fields,
   "按类型过滤 + 沿 BaseType 往上找（换 .NET 版本探测不到就降级）")
combo = _plain(_fn_src("def _preset_fullscreen_bounds"))
ok("_preset_restore_rect" in combo and "_wf_write" in combo,
   "组合 helper 同时改 Win32 placement 与 WinForms 缓存")
ok("_set_window_placement(hwnd, wp_orig)" in combo
   and "_wf_write(native, saved)" in combo,
   "部分失败要把两处都回滚（不许只改一半）")

print("\n--- 行为：字段探测 / 保存写入 / 失败降级 ---")


class FakeField:
    def __init__(self, name, ftype="System.Drawing.Rectangle", value="OLD"):
        self.Name = name
        self.FieldType = SimpleNamespace(FullName=ftype)
        self._value = value

    def GetValue(self, _obj):
        return self._value

    def SetValue(self, _obj, v):
        self._value = v


class FakeType:
    def __init__(self, fields, base=None):
        self._fields, self._base = fields, base

    @property
    def BaseType(self):
        return self._base

    def GetFields(self, _flags):
        return self._fields


class FakeNative:
    def __init__(self, t):
        self._t = t

    def GetType(self):
        return self._t


f1 = FakeField("restoreBounds", value="R1")
f2 = FakeField("restoredWindowBounds", value="R2")
base = FakeType([f1, f2, FakeField("otherBounds"),
                 FakeField("restoreBounds", ftype="System.String")])
native = FakeNative(FakeType([], base=base))
found = gui._wf_fields(native)
ok([n for n, _f in found] == ["restoreBounds", "restoredWindowBounds"],
   f"只认两个指定名字且类型必须是 Rectangle（实际 {[n for n, _ in found]}）")

dup = FakeField("restoreBounds", value="DUP")
cap = gui._wf_capture(FakeNative(FakeType([f1, dup])))
ok(len(cap) == 1 and cap[0][1] == "R1",
   "同名字段只留第一个（capture 去重）")

f3 = FakeField("restoreBounds", value="ORIGINAL")
saved = [(f3, f3.GetValue(None))]
ok(gui._wf_write(None, [(f3, "FULL")]) == 1
   and f3.GetValue(None) == "FULL", "写入返回成功数且值已变")
ok(gui._wf_write(None, saved) == 1
   and f3.GetValue(None) == "ORIGINAL", "写回原值对称恢复")

rect = gui._wf_rect((10, 20, 800, 600))
ok(rect is not None and (rect.X, rect.Y, rect.Width, rect.Height)
   == (10, 20, 800, 600),
   "_wf_rect 用 (x,y,w,h) 构造出正确的 System.Drawing.Rectangle")


class FakeU32:
    def __init__(self, get_ok=True, set_ok=True, show=3):
        self.get_ok, self.set_ok, self.show, self.set_calls = (
            get_ok, set_ok, show, [])

    def GetWindowPlacement(self, _hwnd, p):
        if not self.get_ok:
            return 0
        wp = p._obj
        wp.length = _real_ctypes.sizeof(gui._WINDOWPLACEMENT)
        wp.showCmd = self.show
        wp.rcNormalPosition = gui._RECT(100, 100, 500, 400)
        return 1

    def SetWindowPlacement(self, _hwnd, p):
        if not self.set_ok:
            return 0
        wp = p._obj
        r = wp.rcNormalPosition
        self.set_calls.append((wp.showCmd, (r.l, r.t, r.r, r.b)))
        return 1


class FakeCtypes:
    def __init__(self, u32):
        self.windll = SimpleNamespace(user32=u32)

    def __getattr__(self, name):
        return getattr(_real_ctypes, name)


_orig_ctypes = gui.ctypes
try:
    u32 = FakeU32()
    gui.ctypes = FakeCtypes(u32)
    ok(gui._preset_restore_rect(0, (0, 0, 2560, 1600)) is False,
       "句柄为 0：返回 False")
    ok(gui._preset_restore_rect(4242, None) is False,
       "没有屏幕矩形：返回 False")
    ok(gui._preset_restore_rect(4242, (0, 0, 2560, 1600)) is True,
       "Win32 预置成功返回 True")
    ok(u32.set_calls == [(3, (0, 0, 2560, 1600))],
       f"rcNormalPosition 写成整屏且 showCmd 原样保留（实际 {u32.set_calls}）")
    u32.set_calls.clear()
    gui._preset_restore_rect(4242, (100, 50, 800, 600))
    ok(u32.set_calls == [(3, (100, 50, 900, 650))],
       "带偏移的屏幕也算对右下角")
    gui.ctypes = FakeCtypes(FakeU32(get_ok=False))
    ok(gui._preset_restore_rect(4242, (0, 0, 2560, 1600)) is False,
       "GetWindowPlacement 失败：返回 False")
    gui.ctypes = FakeCtypes(FakeU32(set_ok=False))
    ok(gui._preset_restore_rect(4242, (0, 0, 2560, 1600)) is False,
       "SetWindowPlacement 失败：返回 False")
finally:
    gui.ctypes = _orig_ctypes

print("\n--- 组合 helper：两处都写、任一失败整体回滚 ---")
_orig = {n: getattr(gui, n) for n in (
    "_wf_capture", "_wf_rect", "_preset_restore_rect",
    "_wf_write", "_set_window_placement")}
try:
    calls = []
    a, b = FakeField("restoreBounds", value="OLD1"), FakeField(
        "restoredWindowBounds", value="OLD2")
    saved_items = [(a, "OLD1"), (b, "OLD2")]
    gui._wf_capture = lambda _n: saved_items
    gui._wf_rect = lambda r: ("FULL", tuple(r))
    gui._preset_restore_rect = lambda h, r: (calls.append(("win32", r)) or True)
    gui._set_window_placement = lambda h, wp: (
        calls.append(("rollback", wp)) or True)

    def good_write(_n, items):
        calls.append(("wf", list(items)))
        for f, v in items:
            f.SetValue(None, v)
        return len(items)

    gui._wf_write = good_write
    ok_bounds, got = gui._preset_fullscreen_bounds(
        4242, "native", (0, 0, 2560, 1600), "WP_ORIG")
    ok(ok_bounds is True and got is saved_items,
       "两处都成功：返回 (True, 保存的原值)")
    ok(a.GetValue(None) == ("FULL", (0, 0, 2560, 1600))
       and b.GetValue(None) == ("FULL", (0, 0, 2560, 1600)),
       "两个 WinForms 字段都被写成全屏矩形")
    ok(("win32", (0, 0, 2560, 1600)) in calls,
       "Win32 placement 也预置了（缺一处就会有小窗帧）")

    calls.clear()
    gui._wf_capture = lambda _n: []
    ok(gui._preset_fullscreen_bounds(4242, "n", (0, 0, 2560, 1600), "WP")
       == (False, []) and not calls,
       "字段探测不到：整体跳过，连 Win32 都不碰")

    calls.clear()
    gui._wf_capture = lambda _n: saved_items
    gui._wf_rect = lambda _r: None
    ok(gui._preset_fullscreen_bounds(4242, "n", (0, 0, 2560, 1600), "WP")
       == (False, []) and not calls,
       "Rectangle 构造失败：整体跳过")

    calls.clear()
    gui._wf_rect = lambda r: ("FULL", tuple(r))
    gui._preset_restore_rect = lambda h, r: False
    ok(gui._preset_fullscreen_bounds(4242, "n", (0, 0, 2560, 1600), "WP")
       == (False, []) and not any(c[0] == "wf" for c in calls),
       "Win32 预置失败：不写 WinForms 字段")

    calls.clear()
    gui._preset_restore_rect = lambda h, r: (
        calls.append(("win32", r)) or True)
    gui._wf_write = lambda _n, items: len(items) - 1   # 部分写入失败
    ok(gui._preset_fullscreen_bounds(4242, "n", (0, 0, 2560, 1600),
                                    "WP_ORIG") == (False, []),
       "WinForms 部分写入失败：返回 False（走原路径降级）")
    ok(("rollback", "WP_ORIG") in calls,
       "部分失败时把 Win32 placement 回滚到原始值")
finally:
    for n, v in _orig.items():
        setattr(gui, n, v)

print("\n--- 行为：退出状态轮询与两处写回 ---")


class FakeU32Wait:
    def __init__(self, styles):
        self.styles, self.calls = list(styles), 0

    def GetWindowLongPtrW(self, _hwnd, _idx):
        self.calls += 1
        return self.styles[min(self.calls - 1, len(self.styles) - 1)]


class BadU32:
    def GetWindowLongPtrW(self, *_a):
        raise RuntimeError("boom")


_orig_ctypes2 = gui.ctypes
try:
    gui.ctypes = FakeCtypes(FakeU32Wait([0, 0, 0x01000000]))
    ok(gui._wait_maximized(4242, timeout=0.5) is True,
       "WS_MAXIMIZE 置位后返回 True")
    gui.ctypes = FakeCtypes(FakeU32Wait([0]))
    ok(gui._wait_maximized(4242, timeout=0.01) is False,
       "一直没置位：超时返回 False（调用方仍会写回）")
    ok(gui._wait_maximized(0, timeout=0.01) is False, "句柄为 0：False")
    gui.ctypes = FakeCtypes(BadU32())
    ok(gui._wait_maximized(4242, timeout=0.01) is False,
       "Win32 抛错：False（不拖垮退出）")
finally:
    gui.ctypes = _orig_ctypes2

_orig2 = {n: getattr(gui, n) for n in
          ("_wf_write", "_preset_restore_rect")}
try:
    calls = []
    fa = FakeField("restoreBounds", value="OLD_A")
    fb = FakeField("restoredWindowBounds", value="OLD_B")
    saved2 = [(fa, "OLD_A"), (fb, "OLD_B")]

    def good_wf(_n, items):
        calls.append(("wf", list(items)))
        return len(items)

    gui._wf_write = good_wf
    gui._preset_restore_rect = lambda h, r: (
        calls.append(("win32", r)) or True)
    wp = SimpleNamespace(rcNormalPosition=SimpleNamespace(
        l=100, t=200, r=450, b=800))
    ok(gui._restore_fullscreen_bounds(4242, "native", saved2, wp) is True,
       "两处都写回成功：返回 True")
    ok(calls == [("wf", saved2), ("win32", (100, 200, 350, 600))],
       f"WinForms 写原值 + Win32 写回 (x,y,w,h)（实际 {calls}）")

    calls.clear()
    ok(gui._restore_fullscreen_bounds(4242, "n", [], None) is True
       and calls == [], "无缓存 / 无 placement：不调用任何写入")

    calls.clear()
    gui._wf_write = lambda _n, items: 0
    ok(gui._restore_fullscreen_bounds(4242, "n", saved2, wp) is False,
       "WinForms 写入失败：返回 False")

    calls.clear()
    gui._wf_write = good_wf
    gui._preset_restore_rect = lambda h, r: (
        calls.append(("win32", r)) or False)
    ok(gui._restore_fullscreen_bounds(4242, "n", saved2, wp) is False,
       "Win32 写回失败：返回 False")
finally:
    for n, v in _orig2.items():
        setattr(gui, n, v)

print()
if fails:
    print(f"FAILED {len(fails)}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
