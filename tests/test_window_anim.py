"""原生无边框 + DWM 过渡（v1.28.0）：能自动验的那部分。

方案（替换掉 v1.23~v1.27 五个版本的手工补间）：
  - 窗口创建时保留标准 WS_OVERLAPPEDWINDOW 样式（**不传 frameless**），
    WM_NCCALCSIZE 钩子把整个窗口算成客户区 → 看着无边框；
  - 最大化 / 还原的过渡交给系统 DWM（实测 8 个中间帧，与标准窗口一致）；
  - 设置里关掉动画时，只在**那一次**调用里临时禁用 DWM 过渡。

真机「顺不顺」要人眼/录像判断（.cache/native_anim_results），但下面这些能钉死：
  - 手工补间整套不回潮（帧推送 / 补间线程 / MaximizedBounds 魔法全删了）；
  - pywebview 的 frameless=True 不回来 —— 它会把窗口样式剥掉，DWM 就不给过渡
    （这是整个方案的前提，也真机实测过：frameless 0~1 个中间帧）；
  - WM_NCCALCSIZE 钩子：挂上 / 幂等 / 返回 0（整个窗口是客户区）；
  - maximize_window 的方向以真实 WindowState 为权威（连点、Win+↑ 都不会判错）；
  - animate=False / restore_at：先禁 DWM 过渡 → 动窗口 → finally 恢复；
  - toggle_fullscreen 不走 pywebview 的实现（FormBorderStyle 翻转会 RecreateHandle，
    把钩子和样式全洗掉）。

每条断言都过了变异验证：把实现改坏，这里必须红。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asase_reader import gui  # noqa: E402

src = (ROOT / "src" / "asase_reader" / "gui.py").read_text(encoding="utf-8")
html_src = (ROOT / "src" / "asase_reader" / "assets" / "gui.html").read_text(
    encoding="utf-8")


def _code(text):
    """剥掉注释行：注释里会提到要禁止的符号（比如「不再用 frameless=True」），
    不剥会误伤。docstring 同理由 _fn_code 处理。"""
    return "\n".join(l for l in text.splitlines()
                     if not l.strip().startswith("#"))


def _fn_body(name):
    """取**某一个**函数/方法的源码到下一个同级定义为止。

    必须按缩进层级取边界：模块级函数（行首 def）里常嵌套内部函数
    （`_install_native_borderless` 里的 `_proc`、`launch_gui` 里的
    `_init_native_window`），拿「下一个 4 空格缩进 def」当边界会把外层
    函数体拦腰截断，后面的断言全都在查一段不存在的代码。
    """
    i = src.index(name)
    line_start = src.rfind("\n", 0, i) + 1
    prefix = src[line_start:i]
    indent = len(prefix) - len(prefix.lstrip())
    if indent == 0:                       # 模块级函数
        j = len(src)
        for marker in ("\ndef ", "\nif __name__"):
            k = src.find(marker, i + 1)
            if k > 0:
                j = min(j, k)
    else:                                 # 类方法
        j = src.find("\n" + " " * indent + "def ", i + 1)
        if j < 0:
            j = len(src)
    return src[i:j]


def _fn_code(name):
    """`_fn_body` 再剥掉开头那段 docstring（说明里常点名禁止出现的符号）。"""
    text = _fn_body(name)
    for q in ('"""', "'''"):
        a = text.find(q)
        if a >= 0:
            b = text.find(q, a + 3)
            if b > 0:
                return _code(text[:a] + text[b + 3:])
    return _code(text)


fails: list[str] = []


def ok(cond, msg):
    if cond:
        print(f"PASS  {msg}")
    else:
        fails.append(msg)
        print(f"FAIL  {msg}")


print("--- 手工补间整套不回潮（v1.23~v1.27 的死路，别再绕回去）---")
for sym, why in [
    ("ANIM_MS", "动画时长常量：动画已交给 DWM，前后端都不该再有"),
    ("_animate_window_rect", "补间线程：窗口几何逐帧改是 v1.23 卡闪的根因"),
    ("winAnimFrameGeom", "帧推送：手工补间的配套设施"),
    ("_push_anim_frame", "帧推送入队"),
    ("_set_maximized_bounds", "让 maximize() 落在原地的魔法（补间专用）"),
    ("_placement_normal_rect", "补间会骗过系统的还原位置记忆，才需要手工写回"),
    ("ease_out_cubic", "手工缓动"),
    ("lerp_rect", "手工插值"),
]:
    ok(sym not in src, f"gui.py 里没有 {sym} —— {why}")

ok("frameless=True" not in _code(_fn_body("def launch_gui")),
   "create_window 不传 frameless=True —— pywebview 的 frameless 会剥掉窗口样式，"
   "DWM 就不给最大化/还原过渡（真机实测 frameless 0~1 个中间帧 vs 标准 8 个）")

print("\n--- 前端同步删净（旧补间的另一半）---")
for sym in ["winAnimScale", "endWinAnimScale", "winAnimFrameGeom",
            "WIN_ANIM_MS", "win-anim"]:
    ok(sym not in html_src,
       f"gui.html 里没有 {sym} —— 前端不再锁布局/缩放画面/收帧推送")
ok('api().maximize_window(uiWinAnim())' in html_src,
   "前端把动画开关原样传给后端（动画开不开由后端临时控制 DWM 决定）")

print("\n--- 别给 SetWindowPos 声明 argtypes（会打断 pywebview 的拖窗口）---")
# ctypes.windll.user32 是全进程共享的单例，声明 argtypes 会连 pywebview 一起生效。
# 它的 move() 就是拖窗口（pywebview-drag-region）走的那条，以 SWP_NOSIZE 模式调用、
# 宽高直接传 None → 声明成 c_int 就抛 ArgumentError，拖动静默失败。
ok("SetWindowPos.argtypes" not in _code(src),
   "gui.py 里没有 SetWindowPos.argtypes —— 声明它会让 pywebview 的 move() 抛 "
   "ArgumentError（宽高传的是 None），拖窗口就静默失效了")

print("\n--- SetWindowPos 只许走 _set_window_pos（裸调传 -1/-2 会被静默拒绝）---")
# 反面坑（v1.29.0 真机断全屏，探针 .cache/probe_fullscreen2.py 实测）：没有
# argtypes 时裸 int 的 -1/-2（HWND_TOPMOST/NOTOPMOST，64 位指针哨兵）只按
# 32 位传递，不会被符号扩展成 0xFFFFFFFFFFFFFFFF → SetWindowPos 当无效句柄
# 静默拒绝（返回 0）。所以句柄参数必须在 _set_window_pos 里包 c_void_p。
_swp_callsites = _code(src).count("SetWindowPos(")
ok(_swp_callsites == 1,
   f"gui.py 非注释代码里 SetWindowPos( 只出现 1 次（实际 {_swp_callsites} 次）—— "
   f"必须全部走 _set_window_pos()，裸调传 -1/-2 会被当无效句柄静默拒绝")
ok("c_void_p" in _fn_code("def _set_window_pos"),
   "_set_window_pos 内部把句柄参数包 c_void_p —— 不包的话 -1 只剩 32 位 0xFFFFFFFF")
ok("return bool(rc)" in _fn_code("def _set_window_pos"),
   "_set_window_pos 返回 SetWindowPos 的真实成败 —— 不查返回值失败就藏住了")

print("\n--- 不写 native.WindowState（pythonnet 3.x 裸 int 赋枚举属性会抛）---")
# v1.28.0~v1.30.1 的坑：`native.WindowState = 0/2` 在 pythonnet 3.x 下直接抛
# "int can not be converted to Enum implicitly"，最大化态进全屏被 except 吞成
# ok:False、窗口原地不动（探针 .cache/probe_fullscreen3.py 实锤）。
# 状态切换一律走 ShowWindow（SW_RESTORE / SW_MAXIMIZE），读属性没事。
ok("WindowState =" not in _code(src),
   "gui.py 非注释代码里没有『WindowState =』赋值 —— pythonnet 3.x 裸 int 赋给"
   ".NET 枚举属性直接抛，最大化态进/出全屏就是这么断的；状态切换走 _show_window")

print("\n--- WM_NCCALCSIZE 钩子：整个窗口算成客户区 ---")
# 静态：钩子命中 NCCALCSIZE 时必须 return 0（= 不保留任何非客户区）。
# 返回别的值（或走 DefWindowProc）标题栏/边框就会画出来。
_nc = _code(_fn_body("def _install_native_borderless"))
ok("return 0" in _nc,
   "钩子命中 WM_NCCALCSIZE(wparam) 时返回 0 —— 非零会保留非客户区，边框就冒出来了")
ok("before_show" in _code(_fn_body("def launch_gui")),
   "钩子在 before_show（首帧绘制前）装上 —— 晚了标题栏会闪一帧")


class _FakeU32:
    """假 user32：只记调用。SetWindowLongPtrW 返回一个非零「旧 wndproc」。"""

    def __init__(self):
        self.calls = []

    def SetWindowLongPtrW(self, hwnd, what, cb):
        self.calls.append(("set", hwnd, what))
        return 0xAAAA          # 非 0 = 旧的窗口过程句柄（0 会被当失败）

    def CallWindowProcW(self, old, h, msg, wp, lp):
        self.calls.append(("call", msg, old))
        return 7

    def DefWindowProcW(self, h, msg, wp, lp):
        self.calls.append(("def", msg))
        return 8

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        # hwnd / after 在生产代码里已被 _set_window_pos 包成 c_void_p，断言前要拆
        hv = getattr(hwnd, "value", hwnd)
        self.calls.append(("swp", hv, int(flags)))
        return 1


class _Handle:
    def __init__(self, v):
        self._v = v

    def ToInt64(self):
        return self._v


class _FakeNative:
    def __init__(self, hwnd=4242, state=0):
        self.Handle = _Handle(hwnd)
        self.WindowState = state


class _FakeWin:
    def __init__(self, native):
        self.native = native
        self.calls = []

    def maximize(self):
        self.calls.append("maximize")

    def restore(self):
        self.calls.append("restore")

    def move(self, x, y):
        self.calls.append(("move", x, y))


print("\n--- _install_native_borderless：挂上 / 幂等 / 命中与放行 ---")
_orig_ctypes = gui.ctypes
_orig_hook = gui._nccalcsize_hook
_u32 = _FakeU32()
try:
    # cast / c_void_p 转发给真 ctypes（回调对象是真 WINFUNCTYPE 造的，得真 cast）
    gui.ctypes = SimpleNamespace(
        windll=SimpleNamespace(user32=_u32),
        cast=_orig_ctypes.cast, c_void_p=_orig_ctypes.c_void_p)
    gui._nccalcsize_hook = None

    w1 = _FakeWin(_FakeNative(hwnd=4242))
    ok(gui._install_native_borderless(w1) is True,
       "能拿到句柄时挂钩成功")
    ok(gui._nccalcsize_hook is not None
       and gui._nccalcsize_hook[0] == 4242,
       "全局钩子记下 (hwnd, callback, old) —— callback 必须一直被引用，"
       "否则 GC 后下一条窗口消息就崩")

    _u32.calls.clear()
    ok(gui._install_native_borderless(w1) is True, "同一句柄重复调用：幂等返回")
    ok(not _u32.calls,
       "幂等时不重挂（SetWindowLongPtrW 没被再调）—— 重挂会把自己挂成旧链")

    w2 = _FakeWin(_FakeNative(hwnd=8888))
    _u32.calls.clear()
    ok(gui._install_native_borderless(w2) is True, "换了句柄要重挂")
    ok(("set", 8888, -4) in _u32.calls, "重挂走的是 GWL_WNDPROC(-4)")

    # 回调行为：命中 NCCALCSIZE(wparam≠0) 返回 0；其他消息转给旧过程
    _cb = gui._nccalcsize_hook[1]
    _old = gui._nccalcsize_hook[2]
    _u32.calls.clear()
    r = _cb(8888, 0x0083, 1, 1234)           # WM_NCCALCSIZE, wParam=TRUE
    ok(r == 0 and not _u32.calls,
       "命中 WM_NCCALCSIZE(wparam) → 返回 0 且不转发（整个窗口是客户区）")
    r = _cb(8888, 0x0083, 0, 1234)           # wParam=FALSE（系统只在算尺寸时传 TRUE）
    ok(("call", 0x0083, _old) in _u32.calls,
       "wparam=0 的 NCCALCSIZE 转发给旧窗口过程（保持原行为）")
    _u32.calls.clear()
    _cb(8888, 0x0005, 0, 0)                  # WM_SIZE 之类的普通消息
    ok(("call", 0x0005, _old) in _u32.calls,
       "普通消息原样转发给旧窗口过程（钩子只管 NCCALCSIZE）")

    ok(gui._install_native_borderless(_FakeWin(None)) is False,
       "拿不到句柄时安全返回 False，不抛")

    # 首帧白条：非客户区尺寸是窗口创建时缓存的，只换窗口过程不会重算 ——
    # 必须补一条 SWP_FRAMECHANGED 强制系统立刻重算，否则首帧还按「有标题栏」画。
    w3 = _FakeWin(_FakeNative(hwnd=9999))
    _u32.calls.clear()
    ok(gui._install_native_borderless(w3) is True, "换句柄安装成功")
    swp = [c for c in _u32.calls if c[0] == "swp"]
    ok(len(swp) == 1 and swp[0][1] == 9999 and (swp[0][2] & 0x20) != 0,
       f"挂完钩子补发 SWP_FRAMECHANGED(0x20)（实际 {swp}）—— 不补的话首帧会露出"
       f"那条系统白条（缓存的标题栏），直到第一次最大化才刷新")
    ok(len(swp) == 1 and (swp[0][2] & 0x01) and (swp[0][2] & 0x02),
       f"补发要带 NOSIZE|NOMOVE（实际 flags=0x{swp[0][2]:x}）—— "
       f"漏了会把窗口挪到 0,0 并缩成 0×0")
    _u32.calls.clear()
    gui._install_native_borderless(w3)
    ok(not [c for c in _u32.calls if c[0] == "swp"],
       "幂等路径不重复补发（已经挂过了，重发只是白折腾一次）")
finally:
    gui.ctypes = _orig_ctypes
    gui._nccalcsize_hook = _orig_hook


# ---- maximize_window 的行为 ----

def _api_with(win, maximized=False):
    api = gui.GuiApi.__new__(gui.GuiApi)
    api._b = SimpleNamespace(window=win)
    api._maximized = maximized
    api._keep_taskbar = True
    api._fullscreen = False
    api._on_top = False
    api._fs_rect = None
    api._fs_state = 0
    api.set_taskbar_mode = lambda *a, **k: None    # 不碰 WinForms
    return api


class _Recorder:
    """同时记录 _install_native_borderless 与 _set_dwm_transitions 的调用。"""

    def __init__(self):
        self.installs = 0
        self.transitions = []      # [(hwnd, enabled), ...]

    def install(self, win):
        self.installs += 1
        return True

    def transitions_fn(self, hwnd, enabled):
        self.transitions.append((hwnd, enabled))


_orig_install = gui._install_native_borderless
_orig_transitions = gui._set_dwm_transitions
_orig_dwm_attr = gui._dwm_attr


print("\n--- maximize_window：方向以真实 WindowState 为权威 ---")
# 【守 v1.27.2 之前的竞态】旧实现里还原补间期间原生 WindowState 仍是 Maximized，
# 第二次点击又被判成还原（用户想重新最大化）。新实现每次先读真实 WindowState，
# 且同步 Invoke 返回时状态已落定 —— 这里钉死「WindowState 说了算」。
r1 = _Recorder()
gui._install_native_borderless, gui._set_dwm_transitions = r1.install, r1.transitions_fn
try:
    # WindowState=2（哪怕记账说没最大化）→ 必须走还原
    w_a = _FakeWin(_FakeNative(hwnd=11, state=2))
    ra = _api_with(w_a, maximized=False).maximize_window(True)
    ok(w_a.calls == ["restore"],
       f"WindowState=2 时必须调 restore（实际 {w_a.calls}）—— 记账说了不算")
    ok(ra.get("maximized") is False and ra.get("ok") is True, "返回值如实报告已还原")

    # WindowState=0（哪怕记账说已最大化）→ 必须走最大化
    w_b = _FakeWin(_FakeNative(hwnd=12, state=0))
    rb = _api_with(w_b, maximized=True).maximize_window(True)
    ok(w_b.calls == ["maximize"],
       f"WindowState=0 时必须调 maximize（实际 {w_b.calls}）—— 用户用 Win+↑/"
       f"系统快捷键最大化过时，靠记账会判反")
    ok(rb.get("maximized") is True, "返回值如实报告已最大化")

    # WindowState 拿不到（非 WinForms 环境）→ 退回归账
    w_c = _FakeWin(None)
    _api_with(w_c, maximized=True).maximize_window(True)
    ok(w_c.calls == ["restore"],
       f"WindowState 读不到时退回自己的记账（实际 {w_c.calls}）")
finally:
    gui._install_native_borderless = _orig_install
    gui._set_dwm_transitions = _orig_transitions

print("\n--- animate=True：完全不动 DWM 过渡（系统动画照常播）---")
r2 = _Recorder()
gui._install_native_borderless, gui._set_dwm_transitions = r2.install, r2.transitions_fn
try:
    _api_with(_FakeWin(_FakeNative(hwnd=21, state=0))).maximize_window(True)
    _api_with(_FakeWin(_FakeNative(hwnd=21, state=2))).maximize_window(True)
    ok(r2.transitions == [],
       f"动画开着时不碰 DWM 过渡开关（实际 {r2.transitions}）—— "
       f"系统默认就是开的，多设一次反而可能把别的窗口动画状态搅乱")
finally:
    gui._install_native_borderless = _orig_install
    gui._set_dwm_transitions = _orig_transitions

print("\n--- animate=False：那一次禁过渡 → 动窗口 → finally 恢复 ---")
# 关掉动画的用户要的是「一步到位」。禁用必须发生在 maximize/restore **之前**
# （晚了 DWM 已经开始播），并且**必须**在 finally 里开回来 —— 漏了的话，
# 以后这个窗口的最小化/还原动画也全没了。
r3 = _Recorder()
gui._install_native_borderless, gui._set_dwm_transitions = r3.install, r3.transitions_fn
try:
    w_d = _FakeWin(_FakeNative(hwnd=31, state=0))
    _api_with(w_d).maximize_window(False)
    ok(r3.transitions and r3.transitions[0] == (31, False),
       f"动窗口之前先禁过渡（实际首个调用 {r3.transitions[:1]}）")
    ok(r3.transitions[-1] == (31, True),
       f"finally 里把过渡开回来（实际末次调用 {r3.transitions[-1:]}）—— "
       f"漏了以后最小化动画也没了")
    ok(len(r3.transitions) == 2 and w_d.calls == ["maximize"],
       f"整个过程只禁一次开一次（实际 {r3.transitions} / {w_d.calls}）")

    # 字符串形态的前端参数也要被解析
    w_e = _FakeWin(_FakeNative(hwnd=32, state=0))
    _api_with(w_e).maximize_window("false")
    ok(w_e.calls == ["maximize"] and (32, False) in r3.transitions,
       'animate 传 "false" 字符串也要当 False 处理（pywebview 不保证类型）')

    # 动窗口抛异常时也必须恢复过渡（finally 的意义）
    class _Boom(_FakeWin):
        def maximize(self):
            raise RuntimeError("boom")

    r3.transitions.clear()
    r4 = _api_with(_Boom(_FakeNative(hwnd=33, state=0))).maximize_window(False)
    ok(r4.get("ok") is False and r3.transitions[-1] == (33, True),
       f"maximize 抛异常也要在 finally 恢复过渡（实际 {r3.transitions}）且安全返回")
finally:
    gui._install_native_borderless = _orig_install
    gui._set_dwm_transitions = _orig_transitions

print("\n--- restore_at：拖顶栏脱离最大化，同样先禁过渡再还原定位 ---")
r5 = _Recorder()
gui._install_native_borderless, gui._set_dwm_transitions = r5.install, r5.transitions_fn
try:
    w_f = _FakeWin(_FakeNative(hwnd=41, state=2))
    rr = _api_with(w_f).restore_at(300, 200)
    ok(w_f.calls[0] == "restore" and w_f.calls[1] == ("move", 300, 200),
       f"restore_at 要 restore + move 到目标点（实际 {w_f.calls}）")
    ok(r5.transitions[0] == (41, False) and r5.transitions[-1] == (41, True),
       f"拖拽要立刻到位：先禁过渡、结束再开回来（实际 {r5.transitions}）")
    ok(rr.get("maximized") is False, "返回值报告已还原")
finally:
    gui._install_native_borderless = _orig_install
    gui._set_dwm_transitions = _orig_transitions

print("\n--- 静态钉住顺序与写法（时序在真机才看得见后果）---")
_mw = _fn_code("def maximize_window")
# animate=False 时禁用必须发生在窗口操作之前（实现放在 try 前面：
# _set_dwm_transitions 自带兜底不会抛，就算挂钩那步抛了也还没禁过，无泄漏）
_i_dis = _mw.find("_set_dwm_transitions(hwnd, False)")
_i_max = _mw.find("w.maximize()")
_i_res = _mw.find("w.restore()")
ok(_i_dis >= 0 and _i_dis < min(x for x in (_i_max, _i_res) if x >= 0),
   "禁过渡在 maximize/restore 之前 —— 晚了 DWM 已经开始播这一帧")
ok("_set_dwm_transitions(hwnd, True)" in _mw and "finally:" in _mw,
   "恢复过渡在 finally 里 —— 任何异常路径都不能把系统动画永久关掉")

print("\n--- toggle_fullscreen：不走 pywebview 的 FormBorderStyle 翻转 ---")
# pywebview 的 toggle_fullscreen() 靠翻 FormBorderStyle 实现，会触发 RecreateHandle
# —— 钩子、窗口样式、WebView2 句柄全被洗掉。Windows 主路径必须纯 SetWindowPos。
r6 = _Recorder()
gui._set_dwm_transitions = r6.transitions_fn
gui._dwm_attr = lambda hwnd, attr, value: True
_orig_rect = gui._window_rect_phys
_orig_bounds = gui._screen_bounds_phys


def _swp_handle(v):
    """拆 c_void_p（生产代码在 _set_window_pos 里包的），并把 64 位哨兵折回 -1/-2。"""
    v = getattr(v, "value", v)
    if v is None:
        return 0
    v = int(v)
    if v >= 1 << 63:
        v -= 1 << 64
    return v


class _SWPU32:
    def __init__(self):
        self.pos = []
        self.show = []       # ShowWindow 的 cmd 序列
        self.rc = 1          # 改成 0 模拟 SetWindowPos 失败（真机上就这么静默失败的）
        self.placements_get = 0
        self.placements_set = []   # SetWindowPlacement 记 (showCmd, rcNormal 宽)

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        self.pos.append((_swp_handle(after), int(x), int(y), int(w), int(h), int(flags)))
        return self.rc

    def ShowWindow(self, hwnd, cmd):
        self.show.append(int(cmd))
        return 1

    def GetWindowLongPtrW(self, hwnd, what):
        return 0            # 无 WS_MAXIMIZE 位：_restore_from_maximized 的轮询立即通过

    def GetWindowPlacement(self, hwnd, wp_ptr):
        # byref(wp) 的 CArgObject 拆回原结构体，填一个 800x600 的还原位置
        self.placements_get += 1
        wp = wp_ptr._obj
        wp.showCmd = 3
        wp.rcNormalPosition.l = 100
        wp.rcNormalPosition.t = 100
        wp.rcNormalPosition.r = 900
        wp.rcNormalPosition.b = 700
        return 1

    def SetWindowPlacement(self, hwnd, wp_ptr):
        wp = wp_ptr._obj
        self.placements_set.append((int(wp.showCmd), int(wp.rcNormalPosition.r - wp.rcNormalPosition.l)))
        return 1


_swpu32 = _SWPU32()
try:
    gui._window_rect_phys = lambda hwnd: (10, 20, 1065, 763)
    gui._screen_bounds_phys = lambda hwnd: (0, 0, 1707, 1067)
    gui.ctypes = SimpleNamespace(
        windll=SimpleNamespace(user32=_swpu32),
        c_void_p=_orig_ctypes.c_void_p,
        byref=_orig_ctypes.byref, sizeof=_orig_ctypes.sizeof)

    w_g = _FakeWin(_FakeNative(hwnd=51, state=0))
    api_g = _api_with(w_g)
    rf = api_g.toggle_fullscreen()
    ok("toggle_fullscreen" not in [c for c in w_g.calls if isinstance(c, str)],
       f"不调 pywebview 的 w.toggle_fullscreen()（实际 {w_g.calls}）—— "
       f"FormBorderStyle 翻转会 RecreateHandle，把 NCCALCSIZE 钩子洗掉")
    ok(rf.get("fullscreen") is True and api_g._fullscreen is True, "进入全屏状态翻转")
    ok(any(p[0] == -1 for p in _swpu32.pos),
       f"进入全屏用 SetWindowPos 提到 HWND_TOPMOST(-1)（实际 {_swpu32.pos}）")
    ok(r6.transitions and r6.transitions[0] == (51, False)
       and r6.transitions[-1] == (51, True),
       f"全屏期间禁过渡、结束恢复（实际 {r6.transitions}）")

    ro = api_g.toggle_fullscreen()
    ok(ro.get("fullscreen") is False, "退出全屏状态翻转")
    ok(any(p[0] == -2 for p in _swpu32.pos),
       "退出全屏用 HWND_NOTOPMOST(-2) 放回去 —— 不放的话窗口永远在最前")
    # 进来时 WindowState=0（普通态）→ 退出应回到记下的矩形
    back = [p for p in _swpu32.pos if p[0] == -2]
    ok(back and back[0][1:5] == (10, 20, 1065, 763),
       f"退出回到进来时记下的矩形（实际 {back[0][1:5] if back else None}）")

    print("\n--- SetWindowPos 失败：如实 ok:False、绝不翻状态 ---")
    # v1.29.0 的真机坑：裸 int -1/-2 被 SetWindowPos 静默拒绝（返回 0），
    # 旧代码不查返回值照样 ok:True → 前端停在伪全屏再也切不回来。
    _swpu32.rc = 0
    w_bad = _FakeWin(_FakeNative(hwnd=52, state=0))
    api_bad = _api_with(w_bad)
    rf_bad = api_bad.toggle_fullscreen()
    ok(rf_bad.get("ok") is False and api_bad._fullscreen is False
       and api_bad._on_top is False,
       f"铺满失败：ok:False 且状态不翻（实际 {rf_bad}）—— 失败还记成全屏，"
       f"前端就卡在伪全屏（v1.29.0 用户看到的就是这个）")
    ok("铺满" in (rf_bad.get("reason") or ""),
       f"reason 说明是铺满这一步失败（实际 {rf_bad.get('reason')}）")
    # 退出失败：窗口还铺着满屏，状态必须保持全屏，不许翻
    api_bad._fullscreen = True
    api_bad._on_top = True
    api_bad._fs_rect = (10, 20, 1065, 763)
    ro_bad = api_bad.toggle_fullscreen()
    ok(ro_bad.get("ok") is False and api_bad._fullscreen is True
       and api_bad._on_top is True,
       f"恢复失败：ok:False 且保持全屏状态（实际 {ro_bad}）—— 窗口还铺着，"
       f"翻了状态前后端就再也对不上了")
    _swpu32.rc = 1

    print("\n--- 最大化态进全屏：SW_RESTORE / SW_MAXIMIZE，不写 native.WindowState ---")
    # v1.28.0 起的坑：pythonnet 3.x 裸 int 赋给枚举属性直接抛
    # （"int can not be converted to Enum implicitly"，探针 probe_fullscreen3 实锤），
    # 从最大化进全屏必炸 → ok:False、窗口原地不动。必须走 ShowWindow。
    _swpu32.show.clear()
    w_max = _FakeWin(_FakeNative(hwnd=53, state=2))
    api_max = _api_with(w_max)
    rmx = api_max.toggle_fullscreen()
    ok(rmx.get("ok") is True and api_max._fullscreen is True,
       f"最大化态进全屏成功（实际 {rmx}）")
    ok(9 in _swpu32.show,
       f"先 SW_RESTORE(9) 落回普通态（实际 {_swpu32.show}）—— 最大化状态不接受"
       f"几何变更，不先恢复的话铺满会被忽略")
    ok(w_max.native.WindowState == 2,
       "native.WindowState 没被写（读到的仍是进全屏前的 2）—— 写它就会炸")
    ok(any(p[0] == -1 for p in _swpu32.pos),
       "铺满照常走 SetWindowPos(HWND_TOPMOST)")
    _swpu32.show.clear()
    _swpu32.placements_set.clear()
    rmx2 = api_max.toggle_fullscreen()
    ok(rmx2.get("ok") is True and api_max._fullscreen is False,
       f"从全屏退回最大化态成功（实际 {rmx2}）")
    ok(3 in _swpu32.show,
       f"退出时 SW_MAXIMIZE(3) 回最大化（实际 {_swpu32.show}）")
    ok(len(_swpu32.placements_set) >= 1 and _swpu32.placements_set[0][1] == 800,
       f"退出全屏时通过 SetWindowPlacement 写回 rcNormalPosition（实际 "
       f"{_swpu32.placements_set}）—— 进全屏铺满会污染还原位置，不写回的话"
       f"之后点「还原」回不去原来的窗口大小（探针 .cache/probe_restore_bounds.py 坐实）")
    ok(api_max._maximized is True,
       f"退出回最大化要同步 _maximized 记账（实际 {api_max._maximized}）")
    ok(api_max._fs_placement is None and api_max._fs_rect is None,
       "退出成功后清理 _fs_placement / _fs_rect —— 陈旧值喂给下次失败路径就乱了")
finally:
    gui._set_dwm_transitions = _orig_transitions
    gui._dwm_attr = _orig_dwm_attr
    gui._window_rect_phys = _orig_rect
    gui._screen_bounds_phys = _orig_bounds
    gui.ctypes = _orig_ctypes

print("\n--- 无窗口时不能抛异常 ---")
api = gui.GuiApi.__new__(gui.GuiApi)
api._b = SimpleNamespace(window=None)
try:
    r = api.maximize_window(True)
    ok(r.get("ok") is False, f"没有窗口时安全返回 ok:False（实际 {r}）")
    r = api.toggle_fullscreen()
    ok(r.get("ok") is False, f"没有窗口时全屏切换也安全返回（实际 {r}）")
except Exception as e:
    ok(False, f"没有窗口时抛了异常：{e}")

print()
if fails:
    print(f"FAILED {len(fails)} 条：")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
