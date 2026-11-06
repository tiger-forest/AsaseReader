"""GUI 入口：pywebview 窗口 + 后端接线。"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

import webview

from . import __version__
from .backend import Backend
from .staticserver import StaticServer

# pywebview 只接受 "描述 (*.ext;*.ext)" 形式的字符串序列，不能传元组
FILE_TYPES = {
    "image": ("图片文件 (*.jpg;*.jpeg;*.png;*.webp;*.gif;*.bmp)",),
    "text": ("文本文件 (*.txt;*.md;*.csv;*.url)", "所有文件 (*.*)"),
    # 与 videograb.VIDEO_EXTS 保持一致：Chromium 能播的那几种（mkv 不在内）
    "video": ("视频文件 (*.mp4;*.m4v;*.webm;*.mov)",),
    # ffmpeg 是外部程序，不打包进 exe，所以给用户一个手选路径的口子
    "exe": ("可执行文件 (*.exe)", "所有文件 (*.*)"),
}


# ---- 应用图标 ----
#
# 两个文件各有分工，**别合并成一个**：
# - `icon.ico`：交付的 7 尺寸集成包，条目是 PNG 压缩的 Vista 格式。
#   PyInstaller 读得动（它嵌入 exe 时会转成 BMP 条目），所以 **exe 文件图标用它**。
# - `icon_win.ico`：由上面那批 PNG 转成 **32bpp BMP 条目**的同尺寸副本
#   （`tools/make_icon_win.py`）。**窗口 / 任务栏 / favicon 用它** ——
#   那条路走 `System.Drawing.Icon`，它读不动 PNG 压缩的条目：单尺寸的会抛
#   `Icon.BmpFrame()` 越界，多尺寸的不抛但画出来是**彩色噪点**（2026-11-02 实测）。
ICON_FILE = "icon_win.ico"


def _icon_path() -> Path | None:
    """应用图标的绝对路径；找不到返回 None（图标是门面，不能反过来挡住启动）。

    源码运行：本文件旁的 assets/。
    打包后：__file__ 落在 PyInstaller 解包目录里，同样指向 asase_reader/assets/；
    万一 __file__ 不可靠，再按 sys._MEIPASS 兜一次（datas 的落地位置）。
    """
    cands = [Path(__file__).resolve().parent / "assets" / ICON_FILE]
    meipass = getattr(sys, "_MEIPASS", "") if getattr(sys, "frozen", False) else ""
    if meipass:
        cands.insert(0, Path(meipass) / "asase_reader" / "assets" / ICON_FILE)
    for p in cands:
        if p.is_file():
            return p
    return None


# ---- Win32：原生无边框（标准样式 + WM_NCCALCSIZE 钩子）与 DWM 过渡开关 ----
#
# 为什么不再是 pywebview 的 frameless=True（v1.28.0 改的）：
# frameless 会把窗口样式剥成光秃秃的 WS_OVERLAPPED，DWM 因此**不给**最大化/还原
# 过渡（实测：标准窗口 8 个中间帧，frameless 0~1 个，见 .cache/native_anim_results）。
# 所以改成：创建时保留标准 WS_OVERLAPPEDWINDOW 样式（不传 frameless），
# 再用 WM_NCCALCSIZE 钩子把整个窗口都算成客户区 —— 看着仍是无边框，
# 但最大化/还原/最小化全部走系统原生动画，又稳又不闪。
#
# 试过的死路（2026-11-01 实测，细节见当日日志）：
# - frameless + 只补 WS_THICKFRAME：照样没有过渡（缺的是 WS_CAPTION 那一组）；
# - frameless + SetWindowLong 后装全套样式：过渡有了，但 WinForms 的尺寸记账被
#   绕开，客户区塌成 4px 直到下一次重算 —— 样式必须在**创建时**就正确；
# - 自己逐帧补间（v1.23~v1.27 五个版本）：窗口几何与 Chromium 画面分属两个进程，
#   永远做不到同帧原子呈现，怎么调都差口气。

_GUI_PORT = 28765       # 界面服务的固定端口（端口是 localStorage「源」的一部分，见 launch_gui）

_GWL_WNDPROC = -4
_WM_NCCALCSIZE = 0x0083
_DWMWA_TRANSITIONS_FORCEDISABLED = 3   # DwmSetWindowAttribute：强制关过渡
_DWMWA_CORNER_PREFERENCE = 33          # 全屏时去圆角（沿用 pywebview 的做法）
_DWMWA_BORDER_COLOR = 34               # 全屏时去边框色（沿用 pywebview 的做法）

if os.name == "nt":
    _u32 = ctypes.windll.user32
    _P64 = ctypes.sizeof(ctypes.c_void_p) == 8
    _WPARAM_T = ctypes.c_uint64 if _P64 else ctypes.c_uint32
    _LPARAM_T = ctypes.c_int64 if _P64 else ctypes.c_int32
    _LRESULT_T = ctypes.c_int64 if _P64 else ctypes.c_int32
    _u32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _u32.GetWindowLongPtrW.restype = _LRESULT_T
    _u32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _u32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _u32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_uint, _WPARAM_T, _LPARAM_T]
    _u32.CallWindowProcW.restype = _LRESULT_T
    _u32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    _WPARAM_T, _LPARAM_T]
    _u32.DefWindowProcW.restype = _LRESULT_T
    # 【绝不要给 SetWindowPos 声明 argtypes】ctypes.windll.user32 是全进程共享的
    # 单例，声明会连 pywebview 一起生效。它的 move()（拖窗口的 pywebview-drag-region
    # 走的就是这条）以 SWP_NOSIZE 模式调用，宽高直接传 None：
    #     SetWindowPos(hwnd, None, x, y, None, None, SWP_NOSIZE|...)
    # 声明了 c_int 就会抛
    #     ArgumentError: argument 5: TypeError: 'NoneType' object cannot be
    #     interpreted as an integer
    # 拖动因此静默失败。不声明时 ctypes 用默认转换（整数→C int、None→NULL），
    # 两边都能过 —— 我们自己的调用参数本来就是整数，不受影响。
    # 【但注意反面】没有 argtypes 时，裸 int 的 `-1`/`-2`（HWND_TOPMOST 等
    # 64 位哨兵值）不会被符号扩展成指针尺寸 → 静默失败。所以本项目里
    # SetWindowPos 只许走 `_set_window_pos()`（句柄参数包 c_void_p + 查返回值）。
    # 这两条都由 tests/test_window_anim.py 的静态断言钉着。
    _WNDPROC_T = ctypes.WINFUNCTYPE(_LRESULT_T, ctypes.c_void_p, ctypes.c_uint,
                                    _WPARAM_T, _LPARAM_T)


def _hwnd(window) -> int:
    """顶层窗口句柄（int）；拿不到返回 0。"""
    try:
        handle = getattr(getattr(window, "native", None), "Handle", None)
        if handle is None:
            return 0
        hwnd = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
        return hwnd if hwnd > 0 else 0
    except Exception:
        return 0


class _RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                ("r", ctypes.c_long), ("b", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("showCmd", ctypes.c_uint),
        ("ptMinPosition", _POINT),
        ("ptMaxPosition", _POINT),
        ("rcNormalPosition", _RECT),
    ]


def _get_window_placement(hwnd: int):
    """GetWindowPlacement：拿 `WINDOWPLACEMENT`（含**还原位置** rcNormalPosition）。

    进全屏前存一份：进全屏会先 SW_RESTORE 落回普通态、再 SetWindowPos 铺满，
    后者发生在 Normal 态，系统会把 rcNormalPosition 直接改写成铺满尺寸
    （真机探针 `.cache/probe_restore_bounds.py` 坐实：400x300 → 1707x1067）。
    不存的话，之后从最大化「还原」就回不去原来的窗口大小了。失败返回 None。
    """
    if not hwnd:
        return None
    try:
        wp = _WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(
                ctypes.c_void_p(int(hwnd)), ctypes.byref(wp)):
            return None
        return wp
    except Exception:
        return None


def _set_window_placement(hwnd: int, wp) -> bool:
    """SetWindowPlacement：把存下的 WINDOWPLACEMENT 写回（救回 rcNormalPosition）。

    退出全屏回最大化之前必须先写回，否则 SW_MAXIMIZE 之后就以被污染的
    铺满尺寸当还原位置了。`wp` 为 None（进全屏时就没拿到）则原样放弃。
    """
    if not hwnd or wp is None:
        return False
    try:
        return bool(ctypes.windll.user32.SetWindowPlacement(
            ctypes.c_void_p(int(hwnd)), ctypes.byref(wp)))
    except Exception:
        return False


def _window_rect_phys(hwnd: int):
    """GetWindowRect：窗口外接矩形 `(x, y, w, h)`，物理像素；失败返回 None。"""
    if not hwnd:
        return None
    try:
        r = _RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (int(r.l), int(r.t), int(r.r - r.l), int(r.b - r.t))
    except Exception:
        return None


def _screen_bounds_phys(hwnd: int):
    """窗口**所在那块屏幕**的整屏矩形，物理像素；失败返回 None。"""
    try:
        import clr
        clr.AddReference("System.Windows.Forms")
        import System
        import System.Windows.Forms as WinForms

        b = WinForms.Screen.FromHandle(System.IntPtr(hwnd)).Bounds
        return (int(b.X), int(b.Y), int(b.Width), int(b.Height))
    except Exception:
        return None


def _dwm_attr(hwnd: int, attr: int, value: int) -> bool:
    """DwmSetWindowAttribute 的安全包装；失败（老系统 / 无 DWM）返回 False。"""
    if not hwnd:
        return False
    try:
        v = ctypes.c_uint32(value & 0xFFFFFFFF)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        return int(hr) == 0
    except Exception:
        return False


def _set_dwm_transitions(hwnd: int, enabled: bool) -> None:
    """开 / 关这个窗口的 DWM 过渡（最大化 / 还原 / 最小化动画）。

    只在「必须立刻到位」的操作前后临时关：设置页把动画关掉时的那一次最大化 /
    还原、拖顶栏脱离最大化、进出全屏。平时不碰，系统默认就是开的。
    """
    if hwnd:
        _dwm_attr(hwnd, _DWMWA_TRANSITIONS_FORCEDISABLED, 0 if enabled else 1)


def _set_window_pos(hwnd, insert_after, x, y, w, h, flags) -> bool:
    """`SetWindowPos` 在本项目里的**唯一入口**（别处不许直接调）。

    为什么不能裸调、更不能传裸 int 句柄（v1.29.0 真机断过全屏，探针
    `.cache/probe_fullscreen2.py` 实测 A/B 对照）：
    - 模块顶部注释说明了为什么**不能给 SetWindowPos 声明 argtypes**
      （pywebview 的 move() 宽高传 None，声明 c_int 会把它弄炸）；
    - 但没有 argtypes 时，裸 Python int 的 `-1` / `-2`（HWND_TOPMOST /
      NOTOPMOST）经 ctypes 默认转换只按 32 位传递 —— `0xFFFFFFFF` 不会被
      符号扩展成 64 位的 `0xFFFFFFFFFFFFFFFF`，SetWindowPos 把它当成无效句柄
      **静默拒绝（返回 0，LastError=1400）**。v1.29.0 删 argtypes 修拖动时，
      全屏的铺满/恢复两刀就是这样一起断的。
    所以句柄参数一律在这里包 `c_void_p`，**返回值必须查**（0 = 失败）——
    那次就是「失败了照样 ok:True」才把静默失败藏了一个版本。
    """
    if not hwnd:
        return False
    try:
        after = (ctypes.c_void_p(insert_after)
                 if insert_after is not None else None)
        rc = ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)), after,
            int(x), int(y), int(w), int(h), int(flags))
        return bool(rc)
    except Exception:
        return False


def _show_window(hwnd, cmd) -> bool:
    """`ShowWindow` 的安全包装（SW_MAXIMIZE=3 / SW_RESTORE=9 等）。"""
    if not hwnd:
        return False
    try:
        return bool(ctypes.windll.user32.ShowWindow(
            ctypes.c_void_p(int(hwnd)), int(cmd)))
    except Exception:
        return False


def _restore_from_maximized(hwnd) -> bool:
    """把最大化/最小化的窗口落回普通态（SW_RESTORE），并等状态真的落定。

    为什么不写 WinForms 的 `native.WindowState` 属性：pythonnet 3.x 起，
    **裸 int 赋给 .NET 枚举属性直接抛**（"int can not be converted to Enum
    implicitly"）—— v1.28.0 起「最大化状态进全屏」就是这一下炸的（探针
    `.cache/probe_fullscreen3.py` 实锤：ok:False、窗口原地不动）。走
    pywebview 的 `w.restore()` 虽然安全，但要绕一圈 UI 线程封送；
    SW_RESTORE 是纯窗口管理器操作，哪个线程发都一样。
    落定要等：恢复不是瞬间生效的，紧跟其后的 SetWindowPos 铺满若撞在
    最大化状态上可能被忽略 —— 轮询 WS_MAXIMIZE 样式位直到清掉（超时兜底）。
    """
    if not _show_window(hwnd, 9):          # SW_RESTORE
        return False
    try:
        u32 = ctypes.windll.user32
        WS_MAXIMIZE = 0x01000000
        deadline = time.monotonic() + 0.2
        while True:
            style = int(u32.GetWindowLongPtrW(ctypes.c_void_p(int(hwnd)), -16))
            if not style & WS_MAXIMIZE:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
    except Exception:
        return False


# (hwnd, callback, old_proc)。callback 必须一直被引用着，否则 GC 后窗口过程指向
# 被回收的内存，下一条窗口消息就崩了。
_nccalcsize_hook = None


def _install_native_borderless(window) -> bool:
    """给窗口装 WM_NCCALCSIZE 钩子：整个窗口都算客户区，原生标题栏/边框不再出现。

    必须在首次显示**之前**装好（before_show 事件，见 launch_gui）：第一帧绘制时
    钩子已生效，标题栏从头到尾不会闪出来。
    幂等：同一句柄挂过一次就直接返回；句柄变了（理论上不该发生 —— 已经没有会
    触发 RecreateHandle 的路径了）就重挂。
    """
    global _nccalcsize_hook
    if os.name != "nt":
        return False
    hwnd = _hwnd(window)
    if not hwnd:
        return False
    if _nccalcsize_hook and _nccalcsize_hook[0] == hwnd:
        return True

    u32 = ctypes.windll.user32

    def _proc(h, msg, wparam, lparam):
        try:
            if msg == _WM_NCCALCSIZE and wparam:
                return 0      # 整个窗口都是客户区 → 不绘制任何非客户区
            return u32.CallWindowProcW(_proc.old, h, msg, wparam, lparam)
        except Exception:
            return u32.DefWindowProcW(h, msg, wparam, lparam)

    cb = _WNDPROC_T(_proc)
    prev = u32.SetWindowLongPtrW(hwnd, _GWL_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))
    if not prev:
        return False
    _proc.old = prev
    _nccalcsize_hook = (hwnd, cb, prev)

    # 挂上钩子还不够：非客户区的尺寸是**窗口创建时**就缓存好的，改了窗口过程不会
    # 自动重算 —— 首帧仍按「有标题栏」画，直到第一次最大化/最小化才刷新，
    # 看上去就是「刚打开时上方有一条系统白条，交互一下才消失」。
    # SWP_FRAMECHANGED 强制系统立刻重算非客户区（不改位置/尺寸/层序，也不激活）。
    try:
        # NOMOVE|NOSIZE|NOZORDER|NOACTIVATE|FRAMECHANGED
        _set_window_pos(hwnd, None, 0, 0, 0, 0,
                        0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
    except Exception:
        pass      # 发不出去最多是白条还在，不能让窗口起不来
    return True


def _set_always_on_top(window, on: bool) -> bool:
    """Windows 上把窗口提到 / 放回 topmost。

    pywebview 的 toggle_fullscreen() 已经把窗口铺满整块屏幕（SetWindowPos 到
    Screen.Bounds），**但它给的 z-order 是 HWND_TOP** —— 任务栏自己就是 topmost 窗口，
    所以全屏之后任务栏照样浮在最上面。提到 HWND_TOPMOST 才真的盖得住。

    位置与尺寸 pywebview 已经设好了，这里只动 z-order。
    拿不到句柄（非 Windows、或换了个后端）就返回 False —— 那时退回 pywebview 原本的
    行为，只是任务栏还露着，功能不受影响。
    """
    if os.name != "nt":
        return False
    try:
        handle = getattr(getattr(window, "native", None), "Handle", None)
        if handle is None:
            return False
        hwnd = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
        if not hwnd:
            return False
        SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
        return _set_window_pos(
            hwnd, -1 if on else -2, 0, 0, 0, 0,   # HWND_TOPMOST / HWND_NOTOPMOST
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        return False


def _set_window_rect_native(window, x: int, y: int, width: int, height: int) -> bool:
    """一次 SetWindowPos 同时改位置与尺寸，消掉「位置已变、尺寸还没变」的中间态。

    pywebview 的 `move()` 只改位置（SWP_NOSIZE）、`resize()` 才补尺寸，分两次调用中间
    会有一个真实可见的帧：拖左边缘时右边缘先缩一下再弹回来，实测最大 29px ——
    拖的时候看到的就是抽搐。合成一次调用就没这个中间态了。

    必须带 SWP_NOZORDER：不带的话 z-order 会被重置，全屏期间特意提到 HWND_TOPMOST
    的窗口会被打回普通层（pywebview 自己的 resize() 就没带这个标志）。

    拿不到句柄（非 Windows）返回 False，调用方会退回 pywebview 的两次调用。
    """
    if os.name != "nt":
        return False
    try:
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is None:
            return False
        hwnd = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
        if not hwnd:
            return False
        try:
            scale = float(getattr(native, "_scale", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        SWP_NOZORDER = 0x0004
        return _set_window_pos(
            hwnd, None,
            int(x * scale), int(y * scale), int(width * scale), int(height * scale),
            SWP_NOZORDER,
        )
    except Exception:
        return False


def _window_scale(window) -> float:
    """pywebview 的 DPI 缩放系数；拿不到就当 1.0（未缩放）。"""
    try:
        return float(getattr(getattr(window, "native", None), "_scale", 1.0) or 1.0)
    except Exception:
        return 1.0


def _pick_paths(kind: str, window, multiple: bool = False) -> list[str]:
    """弹出原生打开文件对话框，返回选中路径列表；取消返回空列表。

    当前 pywebview 的 JS 端已经没有 window.pywebview.create_file_dialog，
    必须在 Python 侧调用 window.create_file_dialog 再把结果交回前端。
    multiple=True 时用于一次选一组图片（导入章节）。
    """
    kinds = FILE_TYPES.get(kind, FILE_TYPES["text"])
    if window is not None:
        try:
            res = window.create_file_dialog(
                webview.FileDialog.OPEN, "", bool(multiple), "", kinds
            )
            if res:
                return [str(x) for x in res]
            return []
        except Exception:  # 原生对话框不可用时回退到 tkinter
            pass
    one = _pick_with_tkinter(kinds, folder=False, multiple=multiple)
    return [one] if one and not multiple else (list(one) if one else [])


def _pick_folder(window) -> str | None:
    if window is not None:
        try:
            res = window.create_file_dialog(webview.FileDialog.FOLDER, "", False, "", ())
            if res:
                return str(res[0])
            return None
        except Exception:
            pass
    return _pick_with_tkinter((), folder=True)


def _tk_filetypes(filetypes: tuple[str, ...]) -> list[tuple[str, str]]:
    """把 pywebview 的 "描述 (*.a;*.b)" 形式转成 tkinter 的 (描述, "*.a *.b")。"""
    out = []
    for spec in filetypes:
        if "(" in spec and spec.endswith(")"):
            desc, exts = spec[:-1].split("(", 1)
            out.append((desc.strip(), exts.strip().replace(";", " ")))
    return out or [("所有文件", "*.*")]


def _pick_with_tkinter(filetypes: tuple[str, ...], folder: bool,
                       multiple: bool = False):
    """兜底方案：tkinter 的原生对话框，独立于 WebView 的 UI 线程。

    返回 str / list[str] / None（本机 venv 没有 tkinter，实际走不到这里）。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None

    tktypes = _tk_filetypes(filetypes)
    result: list = [None]
    done = threading.Event()

    def run() -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if folder:
                result[0] = filedialog.askdirectory()
            elif multiple:
                result[0] = list(filedialog.askopenfilenames(filetypes=tktypes))
            else:
                result[0] = filedialog.askopenfilename(filetypes=tktypes)
            root.destroy()
        except Exception:
            result[0] = None
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    done.wait(120)
    return result[0] or None


class GuiApi:
    """暴露给前端的 API 面（只挑选 Backend 中允许 JS 调用的方法）。

    注意：这里的**每一个公开属性**都会被 pywebview 反射进 JS（见 webview.util.
    inject_pywebview 的 get_functions）：它会对每个非下划线属性 getattr，若拿到的
    是"非 callable 但带 __module__ 的对象"就递归遍历。所以窗口引用必须叫 _window：
    - 叫 window 时返回的是 webview.Window 实例，递归会把整个 Window（含 gui 里的
      WinForms/COM 对象）全部 getattr 一遍；其中 width/height/x/y 每个都会
      events.shown.wait(15)，启动时前端长时间卡在"已注入 pywebview 对象、
      但 api 函数还没创建"的中间态（表现为书架空白、点回书架报
      "library_state is not a function"）；
    - 顺带还会把 window.close / window.evaluate_js 泄漏给页面。
    """

    def __init__(self, backend: Backend) -> None:
        self._b = backend
        # pywebview 的 Window.fullscreen **只是创建时的参数**，toggle_fullscreen()
        # 并不会更新它，所以这里自己记账（供前端查询真实状态）。
        self._fullscreen = False
        # 全屏期间是否真的把窗口提到了 topmost（拿不到句柄的机器上始终是 False）
        self._on_top = False
        # 窗口最大化状态 / 最大化时是否避开任务栏（无边框窗口自绘控制栏用）
        self._maximized = False
        self._keep_taskbar = True
        # 进全屏时记下的窗口矩形（物理像素）、WindowState 与 WINDOWPLACEMENT，退出时还原
        self._fs_rect = None
        self._fs_state = 0
        self._fs_placement = None

    @property
    def _window(self):
        """原生对话框需要窗口句柄；下划线开头 → 不会被反射给 JS。"""
        return self._b.window

    def add_urls(self, text: str) -> dict:
        return {"added": self._b.add_urls(text or "")}

    def get_state(self) -> dict:
        return self._b.get_state()

    def remove_job(self, opus_id: str) -> dict:
        self._b.remove_job(opus_id)
        return {"ok": True}

    def retry_job(self, opus_id: str) -> dict:
        self._b.retry_job(opus_id)
        return {"ok": True}

    def clear_finished(self) -> dict:
        self._b.clear_finished()
        return {"ok": True}

    def pause(self) -> dict:
        self._b.pause()
        return {"ok": True}

    def resume(self) -> dict:
        self._b.resume()
        return {"ok": True}

    def cancel_all(self) -> dict:
        self._b.cancel_all()
        return {"ok": True}

    def update_config(self, cfg: dict) -> dict:
        return self._b.update_config(cfg or {})

    def preview_dir(self, path: str) -> dict:
        """首启模态 / 设置页目录输入的预检通道（方案 §4.3）：跳过探针规则，
        不落盘不 mkdir 不提交，给前端「确认前黄字」（空间 / 安装目录）取数。"""
        return self._b.preview_dir(path or "")

    # ---- 界面设置的持久化副本 ----
    # 前端把这些偏好写在 localStorage 里，但 localStorage 归浏览器管：端口一变
    # （源 = 协议+主机+端口）或 WebView2 用了临时配置目录，下次启动就全没了。
    # 后端这份 ui_settings.json 是**兜底副本**，每次改动同步写、启动时回写前端。
    # 它只存显示偏好，与 config.json（下载行为）互不相干。

    def get_ui_settings(self) -> dict:
        return self._b.get_ui_settings()

    def save_ui_settings(self, settings: dict) -> dict:
        return self._b.save_ui_settings(settings or {})

    # ---- 登录凭据（充电专属内容） ----
    # Cookie 只在后端内存里，这些方法都不返回凭据原文。

    def submit_cookie(self, cookie: str) -> dict:
        return self._b.submit_cookie(cookie or "")

    def clear_cookie(self) -> dict:
        return self._b.clear_cookie()

    def add_paid_up(self, mid: str, name: str = "") -> dict:
        return self._b.add_paid_up(mid, name)

    def remove_paid_up(self, mid: str) -> dict:
        return self._b.remove_paid_up(mid)

    def read_url_file(self, path: str) -> dict:
        return {"text": self._b.read_url_file(path or "")}

    def open_dir(self, path: str) -> dict:
        self._b.open_dir(path or "")
        return {"ok": True}

    # ---- 窗口控制（无边框窗口自绘控制栏） ----
    # 无边框之后没有原生标题栏，最小化 / 最大化 / 关闭改由前端自绘按钮调用这里。
    # 测试环境 backend.window 是 None，每个方法都要安全返回而不是抛异常。

    def is_maximized(self) -> dict:
        """查询窗口当前是否真的处于最大化。"""
        w = getattr(self._b, "window", None)
        if w is None:
            return {"maximized": False}
        native = getattr(w, "native", None)
        if native is not None:
            try:
                return {"maximized": int(native.WindowState) == 2}   # Maximized
            except Exception:
                pass
        return {"maximized": bool(self._maximized)}

    def set_taskbar_mode(self, keep_taskbar: bool = True) -> dict:
        """最大化时是否避开系统任务栏（默认 True = 避开；False = 盖住整屏）。

        靠 WinForms 的 MaximizedBounds：设成屏幕工作区就避开任务栏，设空就铺满。
        """
        self._keep_taskbar = bool(keep_taskbar)
        w = getattr(self._b, "window", None)
        native = getattr(w, "native", None)
        if native is not None:
            try:
                import clr
                clr.AddReference("System.Windows.Forms")
                clr.AddReference("System.Drawing")
                import System.Windows.Forms as WinForms
                import System.Drawing as Drawing

                screen = WinForms.Screen.FromHandle(native.Handle)
                native.MaximizedBounds = (
                    screen.WorkingArea if self._keep_taskbar else Drawing.Rectangle.Empty
                )
            except Exception:
                pass
        return {"ok": True, "keep_taskbar": self._keep_taskbar}

    def restore_at(self, x: int, y: int) -> dict:
        """从最大化还原并定位到指定坐标（拖顶栏脱离最大化时用）。

        拖拽要「立刻到位」：先临时关掉 DWM 过渡，还原 + 定位完再开回来 —
        拖着手还播一段动画的话，窗口会甩出去又弹回来。
        """
        w = getattr(self._b, "window", None)
        if w is None:
            return {"ok": False, "reason": "no-window"}
        hwnd = _hwnd(w)
        if hwnd:
            _set_dwm_transitions(hwnd, False)
        try:
            w.restore()
            self._maximized = False
            try:
                w.move(int(x), int(y))
            except Exception:
                pass
            return {"ok": True, "maximized": False}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        finally:
            if hwnd:
                _set_dwm_transitions(hwnd, True)

    def move_window(self, x: int, y: int) -> dict:
        """移动窗口到指定坐标。"""
        w = getattr(self._b, "window", None)
        if w is None:
            return {"ok": False, "reason": "no-window"}
        try:
            w.move(int(x), int(y))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def minimize_window(self) -> dict:
        """最小化窗口。"""
        w = getattr(self._b, "window", None)
        if w is None:
            return {"ok": False, "reason": "no-window"}
        try:
            w.minimize()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def maximize_window(self, animate: bool = True) -> dict:
        """切换最大化 / 还原。

        v1.28.0 起不再自己补间：窗口带着标准 WS_OVERLAPPEDWINDOW 样式（边框由
        WM_NCCALCSIZE 钩子隐藏），DWM 自带原生过渡，实测帧序列与标准窗口一致
        （`.cache/native_anim_results`）。`animate=False`（设置里关掉动画）时，
        只在这**一次**调用里临时把 DWM 过渡禁用，窗口一步到位，用完立刻开回来。

        每次先读真实的 WindowState 再决定走向 —— 用户可能用系统快捷键（Win+↑）
        最大化过，只靠自己的记账会对不上。`maximize()` / `restore()` 内部是同步
        Invoke，返回时状态已经落定，返回值就是权威状态，前端直接信。
        """
        w = getattr(self._b, "window", None)
        if w is None:
            return {"ok": False, "reason": "no-window"}
        if isinstance(animate, str):
            animate = animate.strip().lower() not in ("false", "0", "no")
        animate = bool(animate)
        hwnd = _hwnd(w)
        if hwnd:
            _install_native_borderless(w)      # 幂等兜底：句柄若被重建过就补挂
            if not animate:
                _set_dwm_transitions(hwnd, False)
        try:
            native = getattr(w, "native", None)
            ws = getattr(native, "WindowState", None) if native is not None else None
            if ws is not None:
                self._maximized = int(ws) == 2

            if self._maximized:                       # ---- 还原 ----
                w.restore()
                self._maximized = False
                return {"ok": True, "maximized": False}

            # ---- 最大化：按当前设置落一次 MaximizedBounds（避开/盖住任务栏） ----
            self.set_taskbar_mode(getattr(self, "_keep_taskbar", True))
            w.maximize()
            self._maximized = True
            return {"ok": True, "maximized": True}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        finally:
            if hwnd and not animate:
                _set_dwm_transitions(hwnd, True)

    def set_window_rect(self, x: int, y: int, width: int, height: int) -> dict:
        """拖边缘 / 角落改窗口位置与大小（无边框窗口没有原生边框，前端自己接管）。

        为什么不是原生 WM_NCHITTEST：鼠标停在窗口边缘时最上层其实是 WebView2 的渲染
        窗口（属于浏览器进程），它的 `DefWindowProc` 只会回 `HTCLIENT`，而
        `SetWindowLongPtr` 改不了别的进程的窗口过程 —— 父 Form 根本收不到这次问询。
        所以判定与拖拽都放在页面里做，这里只负责落地。

        坐标与尺寸是**逻辑像素**（与 `window.move/resize` 一致，也和前端的
        `screenX` / CSS 像素对齐）。最小尺寸在这里兜住 —— 前端不夹，
        免得两边各写一个 820×600 然后慢慢对不上。
        """
        win = getattr(self._b, "window", None)
        if win is None:
            return {"ok": False, "reason": "no-window"}
        try:
            x, y = int(x), int(y)
            w, h = int(width), int(height)
            min_size = getattr(win, "min_size", None) or (0, 0)
            try:
                lo_w, lo_h = int(min_size[0]), int(min_size[1])
            except Exception:
                lo_w, lo_h = 0, 0
            w, h = max(lo_w, w), max(lo_h, h)
            if not _set_window_rect_native(win, x, y, w, h):
                win.move(x, y)          # 拿不到句柄时退回两次调用（代价是有中间态，
                win.resize(w, h)        # 但只在非 Windows 上发生，Windows 上拿不到句柄的情况极少）
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def close_window(self) -> dict:
        """关闭窗口（等价于原生标题栏的关闭）。"""
        w = getattr(self._b, "window", None)
        if w is None:
            return {"ok": False, "reason": "no-window"}
        try:
            w.destroy()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    # ---- 阅读器全屏 ----

    def toggle_fullscreen(self) -> dict:
        """切换原生全屏。

        v1.28.0 起不走 pywebview 的 toggle_fullscreen()：它靠来回翻 FormBorderStyle
        实现，而窗口现在带着标准样式（边框靠 WM_NCCALCSIZE 钩子隐藏）—— 翻
        FormBorderStyle 会触发 RecreateHandle，钩子和样式全被洗掉，WebView2 也得
        跟着重建句柄。改成纯 SetWindowPos：进去 = 铺满整屏 + topmost，
        出来 = 回到进来时记下的矩形与最大化状态。全程关 DWM 过渡，全屏要立刻到位。

        拿不到窗口（典型＝跑测试）或底层报错时返回 `ok: False`，前端据此降级成
        CSS 伪全屏（只是把导航栏和阅读器条收起来），而不是点了没反应。
        拿不到句柄（非 Windows）时退回 pywebview 的实现。

        `SetWindowPos` 只走 `_set_window_pos()`（裸 int 的 -1/-2 句柄哨兵会被
        静默拒绝，见该函数的 docstring）；铺满 / 恢复失败时**如实返回 ok: False
        且不翻状态** —— 失败还记成全屏的话，前端停在伪全屏就再也切不回来了。
        """
        w = getattr(self._b, "window", None)
        if w is None:
            return {"ok": False, "fullscreen": False, "reason": "no-window"}
        hwnd = _hwnd(w)
        if not hwnd:
            try:
                w.toggle_fullscreen()
                self._fullscreen = not self._fullscreen
                if self._fullscreen:
                    self._on_top = _set_always_on_top(w, True)
                elif self._on_top:
                    _set_always_on_top(w, False)   # 退出必须放回去，否则永远在最前
                    self._on_top = False
                return {"ok": True, "fullscreen": self._fullscreen}
            except Exception as e:  # 原生全屏在某台机器上失灵时，别拖垮阅读器
                return {"ok": False, "fullscreen": self._fullscreen, "reason": str(e)}
        try:
            SWP_SHOWWINDOW = 0x0040
            SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
            native = getattr(w, "native", None)
            _set_dwm_transitions(hwnd, False)
            try:
                if not self._fullscreen:
                    # 记下进来前的矩形与状态（含还原位置 WINDOWPLACEMENT），退出时按它们还原
                    self._fs_placement = _get_window_placement(hwnd)
                    self._fs_rect = _window_rect_phys(hwnd)
                    try:
                        self._fs_state = int(getattr(native, "WindowState", 0) or 0)
                    except Exception:
                        self._fs_state = 0
                    if self._fs_state != 0:
                        # 先落回普通态，几何由下面铺满。不写 native.WindowState：
                        # pythonnet 3.x 裸 int 赋枚举属性会抛（v1.28.0 起最大化态
                        # 进全屏就是这一下炸的），走 SW_RESTORE 见该函数的说明。
                        _restore_from_maximized(hwnd)
                    bounds = _screen_bounds_phys(hwnd)
                    if bounds:
                        # HWND_TOPMOST(-1)：铺满 + 压过任务栏。铺满失败要如实说 —
                        # 失败还记成全屏的话，前端会停在伪全屏且再也切不回来
                        if not _set_window_pos(hwnd, -1, bounds[0], bounds[1],
                                               bounds[2], bounds[3], SWP_SHOWWINDOW):
                            return {"ok": False, "fullscreen": False,
                                    "reason": "SetWindowPos 铺满失败（窗口未动）"}
                    else:  # 屏幕信息拿不到就只提 topmost，别瞎铺（失败也只是任务栏露着）
                        _set_window_pos(hwnd, -1, 0, 0, 0, 0,
                                        SWP_NOMOVE | SWP_NOSIZE)
                    _dwm_attr(hwnd, _DWMWA_CORNER_PREFERENCE, 1)      # 全屏不圆角
                    _dwm_attr(hwnd, _DWMWA_BORDER_COLOR, 0xFFFFFFFE)  # 全屏无边框色
                    self._fullscreen = True
                    self._on_top = True
                else:
                    _dwm_attr(hwnd, _DWMWA_CORNER_PREFERENCE, 0)
                    _dwm_attr(hwnd, _DWMWA_BORDER_COLOR, 0xFFFFFFFF)
                    if self._fs_state == 2:
                        # 回最大化前：先落一次 MaximizedBounds（同 maximize_window
                        # 的做法），再把存下的 WINDOWPLACEMENT 写回 —— 不写的话
                        # rcNormalPosition 已被铺满尺寸污染（进全屏时 Normal 态下
                        # SetWindowPos 干的），之后点「还原」回不去原来的大小。
                        self.set_taskbar_mode(getattr(self, "_keep_taskbar", True))
                        _set_window_placement(hwnd, self._fs_placement)
                        _show_window(hwnd, 3)     # SW_MAXIMIZE：进来时是最大化 → 回最大化
                        pos_ok = _set_window_pos(
                            hwnd, -2, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                        self._maximized = True
                    elif self._fs_rect:
                        pos_ok = _set_window_pos(
                            hwnd, -2, self._fs_rect[0], self._fs_rect[1],
                            self._fs_rect[2], self._fs_rect[3], SWP_SHOWWINDOW)
                        self._maximized = False
                    else:
                        pos_ok = _set_window_pos(
                            hwnd, -2, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
                        self._maximized = False
                    if not pos_ok:
                        # 窗口还铺着满屏，状态不能翻 —— 如实上报，前端继续按全屏处理
                        return {"ok": False, "fullscreen": True,
                                "reason": "SetWindowPos 恢复窗口失败（仍全屏）"}
                    self._fullscreen = False
                    self._on_top = False
                    # 进出一次的暂存全部作废，防止下次失败路径用到陈旧值
                    self._fs_placement = None
                    self._fs_rect = None
            finally:
                _set_dwm_transitions(hwnd, True)
            return {"ok": True, "fullscreen": self._fullscreen}
        except Exception as e:
            return {"ok": False, "fullscreen": self._fullscreen, "reason": str(e)}

    # ---- 作品库 ----

    def library_state(self) -> dict:
        return self._b.library_state()

    def list_works(self) -> dict:
        return self._b.list_works()

    def work_detail(self, wid: str) -> dict:
        return self._b.work_detail(wid)

    def mark_read(self, wid: str) -> dict:
        return self._b.mark_read(wid)

    def chapter_images(self, chapter_id: str) -> dict:
        return self._b.chapter_images(chapter_id)

    def create_work(self, name: str, author: str) -> dict:
        return self._b.create_work(name or "", author or "")

    def update_work(self, wid: str, patch: dict) -> dict:
        return self._b.update_work(wid, patch or {})

    def delete_work(self, wid: str) -> dict:
        return self._b.delete_work(wid)

    def add_chapters(self, wid: str, chapter_ids: list) -> dict:
        return self._b.add_chapters(wid, chapter_ids or [])

    def remove_chapter(self, wid: str, chapter_id: str) -> dict:
        return self._b.remove_chapter(wid, chapter_id)

    def move_chapter(self, wid: str, chapter_id: str, delta: int) -> dict:
        return self._b.move_chapter(wid, chapter_id, delta)

    def update_chapter(self, wid: str, chapter_id: str, patch: dict) -> dict:
        return self._b.update_chapter(wid, chapter_id, patch or {})

    def set_work_cover_file(self, wid: str, src: str) -> dict:
        return self._b.set_work_cover_file(wid, src or "")

    def set_chapter_cover_file(self, wid: str, chapter_id: str, src: str) -> dict:
        return self._b.set_chapter_cover_file(wid, chapter_id, src or "")

    def save_chapter_cover_data(self, wid: str, chapter_id: str, data_url: str) -> dict:
        return self._b.save_chapter_cover_data(wid, chapter_id, data_url or "")

    def clear_work_cover(self, wid: str) -> dict:
        return self._b.clear_work_cover(wid)

    def clear_chapter_cover(self, wid: str, chapter_id: str) -> dict:
        return self._b.clear_chapter_cover(wid, chapter_id)

    # ---- 章节导入与清理 ----

    def import_files(self, title: str, author: str, paths: list) -> dict:
        """一组图片 -> 一个章节。"""
        return self._b.import_files(title or "", author or "", list(paths or []))

    def import_folder(self, title: str, author: str, path: str) -> dict:
        return self._b.import_folder(title or "", author or "", path or "")

    def import_pending(self, opus_ids: list) -> dict:
        """把暂存目录里已完成的章节搬进书架。"""
        return self._b.import_pending(list(opus_ids or []))

    def rename_chapter(self, chapter_id: str, title: str, author: str) -> dict:
        return self._b.rename_chapter(chapter_id, title or "", author or "")

    def delete_chapter_images(self, chapter_id: str, files: list) -> dict:
        """删除章节内的部分图片（真删文件 + 剩余重新编号），不可逆。"""
        return self._b.delete_chapter_images(chapter_id, list(files or []))

    def delete_chapter(self, chapter_id: str) -> dict:
        return self._b.delete_chapter(chapter_id)

    def list_incomplete(self) -> dict:
        return self._b.list_incomplete()

    def purge_incomplete(self, names: list) -> dict:
        return self._b.purge_incomplete(list(names or []))

    # ---- 导出（复制到普通文件夹，书架原件保留） ----

    def export_start(self, kind: str, target_id: str, dest: str,
                     naming: str = "numbered") -> dict:
        """kind: chapter | work | all；target_id 对 all 无意义。"""
        return self._b.export_start(
            kind or "", target_id or "", dest or "", naming or "numbered"
        )

    def export_status(self) -> dict:
        return self._b.export_status()

    def export_cancel(self) -> dict:
        return self._b.export_cancel()

    # ---- 视频转图集（v1.15） ----
    # 状态机：start（预估）→ status 轮询到 phase=confirm → confirm（抽帧入库）→ status。

    def video_import_start(self, paths: list, mode: str = "frames") -> dict:
        return self._b.video_import_start(list(paths or []), str(mode or "frames"))

    def video_import_confirm(self) -> dict:
        return self._b.video_import_confirm()

    def video_import_status(self) -> dict:
        return self._b.video_import_status()

    def video_import_cancel(self) -> dict:
        return self._b.video_import_cancel()

    def video_tool_state(self) -> dict:
        return self._b.video_tool_state()

    # ---- 编辑会话（进入/离开编辑页） ----

    def begin_edit(self, wid: str) -> dict:
        return self._b.begin_edit(wid or "")

    def commit_edit(self) -> dict:
        return self._b.commit_edit()

    def cancel_edit(self) -> dict:
        return self._b.cancel_edit()

    def edit_dirty(self) -> dict:
        return self._b.edit_dirty()

    # ---- 原生对话框 ----

    def choose_file(self, kind: str = "image") -> dict:
        """打开单文件选择框（kind: image | text），返回 {"path": ...}。"""
        paths = _pick_paths(kind or "image", self._window, multiple=False)
        return {"path": paths[0] if paths else ""}

    def choose_files(self, kind: str = "image") -> dict:
        """打开多选文件框（一次选一组图片），返回 {"paths": [...]}。"""
        return {"paths": _pick_paths(kind or "image", self._window, multiple=True)}

    def choose_folder(self) -> dict:
        """打开目录选择框，返回 {"path": ...}。"""
        return {"path": _pick_folder(self._window) or ""}


def launch_gui(state_dir: str | None = None) -> None:
    """启动 GUI。state_dir 缺省 = DATA_DIR（方案 §3 双锚点：frozen 时
    %LOCALAPPDATA%\\AsaseReader、源码时仓库根 .asase）；显式传入的相对路径
    仍锚定项目根目录，exe 启动时不受 cwd 影响。"""
    from .backend import PROJECT_ROOT, get_data_dir  # exe 场景下为 exe 所在目录

    if state_dir is None:
        sd = get_data_dir()
    else:
        sd = Path(state_dir)
        if not sd.is_absolute():
            sd = PROJECT_ROOT / sd
    backend = Backend(state_dir=sd)
    backend.start()
    html_path = Path(__file__).parent / "assets" / "gui.html"
    icon_file = _icon_path()
    # 图片服务只挂书架目录：书架是唯一的内容库
    server = StaticServer(root=backend.shelf_root(), gui_html=html_path,
                          icon_file=icon_file)
    # 固定端口：localStorage 按「源」隔离，而源里**含端口**。端口每次随机 = 每次
    # 都是新源，上一次存的主题/卡片大小/阅读模式全读不回来。固定住就不会漂；
    # 真被别的程序占了，StaticServer 自己回退到随机端口（那时还有后端那份
    # ui_settings.json 兜底，见 GuiApi.save_ui_settings）。
    base = server.start(port=_GUI_PORT)
    backend.static_server = server
    window = webview.create_window(
        title=f"浅濑阅读器 Asase Reader v{__version__}",
        url=f"{base}/",
        js_api=GuiApi(backend),
        width=1080,
        height=800,
        min_size=(820, 600),
        # 无边框（v1.28.0 起）= 标准窗口样式 + WM_NCCALCSIZE 钩子隐藏非客户区，
        # 不再用 pywebview 的 frameless=True —— 它会把窗口样式剥掉，DWM 就不给
        # 最大化/还原过渡了（实测 .cache/native_anim_results：frameless 0~1 个
        # 中间帧，本方案 8 个，与标准窗口一致）。
        # 拖窗口仍靠前端的 pywebview-drag-region；拖边缘改大小仍是前端接管的
        # set_window_rect（原生 WM_NCHITTEST 在 WebView2 上收不到，见 gui.html）。
    )
    backend.window = window

    def _init_native_window():
        """首次显示前装好边框钩子，并把最大化边界限定到工作区。

        before_show 在句柄已建、窗口还没画第一帧时触发：钩子这时装上，
        标题栏从头到尾不会出现。MaximizedBounds 不设的话，边框被隐藏的窗口
        最大化会盖住任务栏，所以显式设成 WorkingArea（设置页可改）。
        拿不到 native 就跳过，代价只是最大化时盖住任务栏，功能不受影响。
        """
        _install_native_borderless(window)
        try:
            import clr
            clr.AddReference("System.Windows.Forms")
            import System.Windows.Forms as WinForms

            native = getattr(window, "native", None)
            if native is None:
                return
            native.MaximizedBounds = WinForms.Screen.FromHandle(native.Handle).WorkingArea
        except Exception:
            pass

    window.events.before_show += _init_native_window
    try:
        # private_mode 默认 True：WebView2 把整个用户数据目录放进系统临时目录，
        # 进程一退就删 —— localStorage 跟着一起没。关掉它并指定固定目录，
        # 界面设置才真的留得住（同上面的固定端口，两个都得改，缺一个都不行）。
        try:
            storage_path = backend.state_dir / "webview_data"
            webview.start(
                private_mode=False,
                storage_path=str(storage_path),
                # 官方 docstring 写「仅 GTK/QT」，但 winforms 分支同样读它
                # （platforms/winforms.py：BrowserView 构造时 Icon(_state['icon'])）。
                # 不传的话它退回 ExtractIconW(sys.executable, 0) —— 源码运行时
                # 那是 python.exe 的图标，只有打包成 exe 才碰巧是对的。
                **({"icon": str(icon_file)} if icon_file is not None else {}),
            )
        except TypeError:      # 老版本 pywebview 没有这几个参数
            webview.start()
    finally:
        backend.stop()
        server.stop()


if __name__ == "__main__":
    launch_gui()
