"""系统托盘与单实例（v2.2.0；托盘图标常驻 + 自绘圆角菜单，2026-11-15）。

设计要点（详见 .workbuddy/memory/plan-tray-close-behavior.md）：

- **零新依赖**：pywebview 在 Windows 上跑 WinForms 消息循环，项目已装 pythonnet，
  直接用 `System.Windows.Forms.NotifyIcon` 即可，不需要 pystray / Pillow / pywin32。
  托盘控件与主窗口共用同一个 UI 线程，不引入第二个消息循环。
- **图标常驻**：程序一运行托盘图标就在，与「关闭时进入托盘」开关无关
  （2026-11-15 用户拍板）。只有真正退出（dispose）时才收起。
- **自绘菜单**：不再用系统菜单控件（它改不动圆角/内边距/配色）。改用无边框、
  置顶、不进任务栏的 WinForms Form + GDI+ 画整张卡片。代价是定位、失焦关闭、
  Esc、右键触发都要自己做，且这些**只能真机人工验**。
- **拦截点唯一**：关闭走 `window.events.closing`（同步、UI 线程），
  前端 × 按钮与 Alt+F4 / 任务栏「关闭」自动汇合，不在前端另做判断。
- **退出必须放行**：`window.destroy()` 内部就是 `Form.Close()`，同样会触发
  `closing`；托盘「退出」要先置 `exiting`，否则会被自己拦死。
- **单实例**：命名 Mutex + `RegisterWindowMessage` 广播。第二个实例不启动后端、
  不抢固定端口 28765，只把已有窗口叫回来后直接返回。

纯逻辑（parse_close_to_tray / decide_close / is_primary_instance /
menu_geometry / hit_test / clamp_position / choose_palette）刻意不依赖 Win32，
方便在没有 GUI 的环境里做单测与变异验证。
"""
from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path

CLOSE_TO_TRAY_KEY = "closeToTray"

ERROR_ALREADY_EXISTS = 183
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

_MUTEX_NAME = "Local\\AsaseReader.SingleInstance.v1"
_WAKE_MSG_NAME = "AsaseReader.WakeWindow.v1"
_HWND_BROADCAST = 0xFFFF

# 系统关机 / 注销也会来 FormClosing。pywebview 的 closing.set() 不传 CloseReason，
# 所以由 WndProc 收到 WM_QUERYENDSESSION / WM_ENDSESSION 时置位，让拦截放行。
_session_ending = False

_k32 = None
_u32 = None
_instance_handle = None       # 内核句柄常驻，进程退出时自动释放
_wake_msg_id_cache = 0
_GUI = {}                     # 长期持有 .NET 命名空间引用，防止 GC 后菜单失灵


def parse_close_to_tray(settings) -> bool:
    """把后端副本里的显示偏好转成布尔。

    缺省 / 文件坏了 / 值非法一律回 **False（直接关闭）** —— 这是拍板的默认值，
    绝不能因为一份旧配置让用户突然找不到程序。
    """
    if not isinstance(settings, dict):
        return False
    raw = settings.get(CLOSE_TO_TRAY_KEY, "0")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def mark_session_ending() -> None:
    global _session_ending
    _session_ending = True


def reset_session_ending() -> None:
    """单测之间复位；生产代码不需要调。"""
    global _session_ending
    _session_ending = False


def decide_close(close_to_tray: bool, exiting: bool = False,
                 session_ending: bool | None = None) -> str:
    """关闭拦截的唯一决策：返回 ``hide``（取消关闭、进托盘）或 ``allow``。

    刻意抽成不碰窗口的纯函数，才能对「默认值 / 退出放行 / 关机放行」做变异验证。
    """
    if session_ending is None:
        session_ending = _session_ending
    if exiting or session_ending:
        return "allow"
    return "hide" if close_to_tray else "allow"


def is_primary_instance(last_error: int) -> bool:
    """CreateMutexW 之后 GetLastError()==183 表示已有实例在跑。"""
    return int(last_error) != ERROR_ALREADY_EXISTS


# ---------------------------------------------------------------------------
# 自绘菜单：纯几何 / 配色（不依赖 WinForms，可单测 + 变异）
# ---------------------------------------------------------------------------

MENU_ITEMS = ("显示主界面", "退出")

# 96 DPI 下的基准几何（像素）。绘制与命中测试**共用同一份 geometry** ——
# 两处各写一套数字迟早会错位（hover 高亮和点击落点必须严丝合缝）。
_BASE_GEOM = {
    "width": 208,
    "radius": 10,
    "pad_v": 6,
    "pad_h": 4,
    "item_h": 36,
    "sep_h": 1,
    "icon_x": 14,
    "icon_size": 16,
    "text_x": 42,
    "font_px": 13,
    "hover_radius": 6,
    "sep_margin": 8,
}

# 与软件的明暗外观（uiMode）对齐；rgb 三元组，避免引入颜色解析。
_PALETTES = {
    "light": {
        "bg": (255, 255, 255), "border": (225, 227, 230),
        "text": (32, 33, 36), "icon": (95, 99, 104),
        "hover": (241, 243, 244), "separator": (232, 234, 237),
    },
    "dark": {
        "bg": (43, 45, 49), "border": (63, 65, 71),
        "text": (232, 234, 237), "icon": (181, 183, 189),
        "hover": (58, 61, 67), "separator": (67, 70, 77),
    },
}


def parse_ui_mode(settings) -> str:
    """从后端 ui_settings 副本读明暗；坏值一律回浅色。"""
    if isinstance(settings, dict):
        v = str(settings.get("uiMode", "light") or "light").strip().lower()
        if v == "dark":
            return "dark"
    return "light"


def choose_palette(mode: str) -> dict:
    """返回配色副本（调用方可以自由改，不许污染常量表）。"""
    return dict(_PALETTES.get(mode, _PALETTES["light"]))


def menu_geometry(scale: float = 1.0) -> dict:
    """按 DPI 比例算出整张卡片的几何。"""
    try:
        s = max(0.5, min(float(scale), 4.0))
    except (TypeError, ValueError):
        s = 1.0
    g = {k: int(round(v * s)) for k, v in _BASE_GEOM.items()}
    g["sep_h"] = max(1, g["sep_h"])
    g["height"] = g["pad_v"] * 2 + g["item_h"] * 2 + g["sep_h"]
    g["scale"] = s
    return g


def item_y(g: dict, index: int) -> int:
    """第 index 项的上边界；两项之间夹着一条分隔线。"""
    if index == 0:
        return g["pad_v"]
    return g["pad_v"] + g["item_h"] + g["sep_h"]


def hit_test(x, y, g: dict) -> int:
    """把鼠标坐标映射成菜单项下标；落在留白/分隔线/卡片外一律 -1。"""
    if x is None or y is None:
        return -1
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return -1
    if x < 0 or y < 0 or x >= g["width"] or y >= g["height"]:
        return -1
    for i in (0, 1):
        top = item_y(g, i)
        if top <= y < top + g["item_h"]:
            return i
    return -1


def clamp_position(cx, cy, wl, wt, wr, wb, w, h) -> tuple[int, int]:
    """菜单右下角贴光标，并夹在光标所在屏的工作区内。

    托盘通常在右下角，菜单必须向左上展开；不夹取的话会掉到任务栏下面或出屏。
    """
    inset = 2
    x = int(cx) - w
    y = int(cy) - h
    x = max(int(wl) + inset, min(x, int(wr) - w - inset))
    y = max(int(wt) + inset, min(y, int(wb) - h - inset))
    return (x, y)


def _win32():
    global _k32, _u32
    if os.name != "nt":
        return None, None
    if _k32 is None:
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _k32.CreateMutexW.restype = ctypes.c_void_p
        _u32 = ctypes.WinDLL("user32", use_last_error=True)
        _u32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
        _u32.RegisterWindowMessageW.restype = ctypes.c_uint
        _u32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_void_p, ctypes.c_void_p]
        _u32.PostMessageW.restype = ctypes.c_int
    return _k32, _u32


def wake_message_id() -> int:
    """注册一次自定义消息；两个进程用同一个名字才能互相认出来。"""
    global _wake_msg_id_cache
    if _wake_msg_id_cache:
        return _wake_msg_id_cache
    _, u = _win32()
    if u is None:
        return 0
    try:
        _wake_msg_id_cache = int(u.RegisterWindowMessageW(_WAKE_MSG_NAME) or 0)
    except Exception:
        return 0
    return _wake_msg_id_cache


def acquire_single_instance() -> tuple[bool, int]:
    """返回 ``(是否主实例, 唤醒消息 id)``。任何异常都退回「照常启动」。"""
    msg = wake_message_id()
    k, _ = _win32()
    if k is None:
        return True, msg
    global _instance_handle
    try:
        # bInitialOwner=False：只看「名字是否已存在」，不抢所有权。
        _instance_handle = k.CreateMutexW(None, 0, _MUTEX_NAME)
        err = ctypes.get_last_error()
    except Exception:
        return True, msg
    return is_primary_instance(err), msg


def wake_existing_window(msg_id: int | None = None) -> bool:
    """广播唤醒消息；已有窗口的 WndProc 收到后 show() 回来。

    用 HWND_BROADCAST 而不是传路径/端口：不落盘、不留状态文件、崩溃后无残留，
    隐藏窗口同样是顶层窗口，能收到广播。
    """
    mid = int(msg_id or wake_message_id() or 0)
    _, u = _win32()
    if u is None or not mid:
        return False
    try:
        return bool(u.PostMessageW(ctypes.c_void_p(_HWND_BROADCAST), mid,
                                   ctypes.c_void_p(0), ctypes.c_void_p(0)))
    except Exception:
        return False


def _gui():
    """加载并长期持有 WinForms / GDI+ 命名空间。"""
    if "W" not in _GUI:
        import clr
        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        import System.Drawing as Drawing
        import System.Windows.Forms as WinForms
        import System.Drawing.Drawing2D as Drawing2D
        import System.Drawing.Text as DrawingText
        _GUI["D"] = Drawing
        _GUI["W"] = WinForms
        _GUI["D2"] = Drawing2D
        _GUI["DT"] = DrawingText
    return _GUI["D"], _GUI["W"], _GUI["D2"], _GUI["DT"]


def _color(D, rgb):
    return D.Color.FromArgb(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _rounded_path(D2, x, y, w, h, r):
    """圆角矩形 GraphicsPath；调用方负责 Dispose。"""
    d = max(2, int(r) * 2)
    p = D2.GraphicsPath()
    p.AddArc(float(x), float(y), float(d), float(d), 180.0, 90.0)
    p.AddArc(float(x + w - d), float(y), float(d), float(d), 270.0, 90.0)
    p.AddArc(float(x + w - d), float(y + h - d), float(d), float(d), 0.0, 90.0)
    p.AddArc(float(x), float(y + h - d), float(d), float(d), 90.0, 90.0)
    p.CloseAllFigures()
    return p


def _draw_icon(gfx, index, x, y, size, pen):
    """两个 16px 线条图标：窗口 / 退出。只用矩形与直线，避免 DrawArc 的重载风险。"""
    s = float(size)
    fx, fy = float(x), float(y)
    if index == 0:
        gfx.DrawRectangle(pen, fx, fy + s * 0.14, s * 0.9, s * 0.72)
        gfx.DrawLine(pen, fx, fy + s * 0.34, fx + s * 0.9, fy + s * 0.34)
        gfx.DrawLine(pen, fx + s * 0.14, fy + s * 0.24,
                     fx + s * 0.28, fy + s * 0.24)
    else:
        gfx.DrawRectangle(pen, fx + 1.0, fy + 1.0, s * 0.72, s - 2.0)
        gfx.DrawLine(pen, fx + s * 0.48, fy + s * 0.5, fx + s, fy + s * 0.5)
        gfx.DrawLine(pen, fx + s * 0.78, fy + s * 0.28, fx + s, fy + s * 0.5)
        gfx.DrawLine(pen, fx + s * 0.78, fy + s * 0.72, fx + s, fy + s * 0.5)


class TrayMenu:
    """自绘圆角卡片菜单（方案 B）。

    不使用系统菜单控件：它画的是系统样式（直角、蓝色方块高亮），圆角 / 内边距 /
    hover / 配色都改不动。这里用一个无边框、置顶、不进任务栏的 WinForms Form，
    GDI+ 自绘整张卡片，视觉与阅读器的明暗外观对齐。

    ``create()`` 必须和 NotifyIcon 一样在 ``before_show``（同步 UI 线程）调用。
    所有事件处理器、Form、Region、Font 都要长期持有引用，否则被 GC 后菜单失灵
    （与 gui._nccalcsize_hook 同一类坑）。
    """

    def __init__(self, on_show, on_exit, theme_provider=None):
        self._on_show = on_show
        self._on_exit = on_exit
        self._theme_provider = theme_provider
        self._form = None
        self._region = None
        self._handlers = []
        self._palette = choose_palette("light")
        self._geom = menu_geometry(1.0)
        self._hover = -1
        self._shown_at = 0.0
        self._busy = False       # 防 hide → Deactivate → hide 的递归

    def _handler(self, fn):
        def _call(sender, args):
            try:
                fn(args)
            except Exception:
                pass        # 菜单回调里抛异常会带崩消息循环，一律吞掉
        self._handlers.append(_call)
        return _call

    def create(self) -> bool:
        D, W, D2, _ = _gui()
        form = W.Form()
        form.FormBorderStyle = getattr(W.FormBorderStyle, "None")
        form.ShowInTaskbar = False
        form.StartPosition = W.FormStartPosition.Manual
        form.TopMost = True
        form.KeyPreview = True
        form.AutoScaleMode = getattr(W.AutoScaleMode, "None")
        # ShowInTaskbar=False 自带 WS_EX_TOOLWINDOW，菜单不会进 Alt+Tab / 任务栏。
        # 访问 Handle 会立刻建原生窗口，之后才能按真实 DPI 定尺寸。
        _ = form.Handle.ToInt64()
        try:
            gfx = form.CreateGraphics()
            dpi = float(gfx.DpiX)
            gfx.Dispose()
        except Exception:
            dpi = 96.0
        self._geom = menu_geometry(dpi / 96.0)
        g = self._geom
        form.Size = D.Size(g["width"], g["height"])
        form.BackColor = _color(D, self._palette["bg"])
        path = _rounded_path(D2, 0, 0, g["width"], g["height"], g["radius"])
        self._region = D.Region(path)
        path.Dispose()
        form.Region = self._region

        form.Paint += self._handler(lambda a: self._paint(a.Graphics))
        form.MouseMove += self._handler(lambda a: self._on_move(a.X, a.Y))
        form.MouseDown += self._handler(
            lambda a: self._on_down(a.X, a.Y,
                                    a.Button == W.MouseButtons.Left))
        form.MouseLeave += self._handler(lambda a: self._set_hover(-1))
        form.Deactivate += self._handler(lambda a: self._on_deactivate())
        form.KeyDown += self._handler(
            lambda a: self._on_key(a.KeyCode == W.Keys.Escape))
        self._form = form
        return True

    def _refresh_theme(self) -> None:
        mode = "light"
        if self._theme_provider is not None:
            try:
                mode = parse_ui_mode(self._theme_provider())
            except Exception:
                mode = "light"
        palette = choose_palette(mode)
        if palette != self._palette:
            self._palette = palette
            if self._form is not None:
                D, _, _, _ = _gui()
                self._form.BackColor = _color(D, palette["bg"])
                self._form.Invalidate()

    def _paint(self, gfx) -> None:
        """GDI+ 画整张卡片：底色 → 细边框 → hover → 分隔线 → 图标 + 文字。"""
        D, _, D2, DT = _gui()
        g, p = self._geom, self._palette
        w, h = g["width"], g["height"]
        gfx.Clear(_color(D, p["bg"]))
        gfx.SmoothingMode = D2.SmoothingMode.AntiAlias
        try:
            gfx.TextRenderingHint = DT.TextRenderingHint.ClearTypeGridFit
        except Exception:
            pass

        border = D.Pen(_color(D, p["border"]), 1.0)
        bp = _rounded_path(D2, 0.5, 0.5, w - 1, h - 1, g["radius"])
        gfx.DrawPath(border, bp)
        bp.Dispose()
        border.Dispose()

        if self._hover in (0, 1):
            top = item_y(g, self._hover)
            hb = D.SolidBrush(_color(D, p["hover"]))
            hp = _rounded_path(D2, g["pad_h"], top,
                               w - 2 * g["pad_h"], g["item_h"],
                               g["hover_radius"])
            gfx.FillPath(hb, hp)
            hp.Dispose()
            hb.Dispose()

        sep = D.Pen(_color(D, p["separator"]), 1.0)
        sy = float(item_y(g, 1) - max(1, g["sep_h"] / 2))
        gfx.DrawLine(sep, float(g["sep_margin"]), sy,
                     float(w - g["sep_margin"]), sy)
        sep.Dispose()

        pen = D.Pen(_color(D, p["icon"]), 1.2)
        brush = D.SolidBrush(_color(D, p["text"]))
        font = D.Font("Microsoft YaHei", float(g["font_px"]),
                      D.FontStyle.Regular, D.GraphicsUnit.Pixel)
        for i, label in enumerate(MENU_ITEMS):
            top = item_y(g, i)
            iy = top + (g["item_h"] - g["icon_size"]) // 2
            _draw_icon(gfx, i, g["icon_x"], iy, g["icon_size"], pen)
            ty = top + (g["item_h"] - g["font_px"]) / 2.0 - 1.0
            gfx.DrawString(label, font, brush, D.PointF(float(g["text_x"]),
                                                         float(ty)))
        font.Dispose()
        brush.Dispose()
        pen.Dispose()

    def _set_hover(self, index: int) -> None:
        if index != self._hover:
            self._hover = index
            if self._form is not None:
                self._form.Invalidate()

    def _on_move(self, x, y) -> None:
        self._set_hover(hit_test(x, y, self._geom))

    def _on_down(self, x, y, is_left) -> None:
        if not is_left:
            return
        index = hit_test(x, y, self._geom)
        if index < 0:
            return
        self.hide()
        cb = self._on_show if index == 0 else self._on_exit
        try:
            cb()
        except Exception:
            pass

    def _on_key(self, is_escape) -> None:
        if is_escape:
            self.hide()

    def _on_deactivate(self) -> None:
        # Show 之后 Activate 之前会短暂来一次 Deactivate；刚弹出的 200ms 内忽略，
        # 否则菜单会「一闪就没」。真点别处时早过了这个窗口。
        if time.time() - self._shown_at < 0.2:
            return
        self.hide()

    def show(self) -> bool:
        f = self._form
        if f is None:
            return False
        try:
            D, W, _, _ = _gui()
            self._refresh_theme()
            pos = W.Cursor.Position
            wa = W.Screen.FromPoint(pos).WorkingArea
            g = self._geom
            x, y = clamp_position(pos.X, pos.Y, wa.Left, wa.Top,
                                  wa.Right, wa.Bottom, g["width"], g["height"])
            self._hover = -1
            f.Location = D.Point(int(x), int(y))
            self._shown_at = time.time()
            f.Show()
            f.BringToFront()
            f.Activate()
            f.Invalidate()
            return True
        except Exception:
            return False

    def hide(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            self._hover = -1
            if self._form is not None:
                self._form.Hide()
        except Exception:
            pass
        finally:
            self._busy = False

    def dispose(self) -> None:
        f, region = self._form, self._region
        self._form = None
        self._region = None
        self._handlers = []
        if f is not None:
            try:
                f.Hide()
                f.Dispose()
            except Exception:
                pass
        if region is not None:
            try:
                region.Dispose()
            except Exception:
                pass


class TrayIcon:
    """WinForms 托盘图标 + 自绘圆角菜单（菜单只有「显示主界面」「退出」两项）。

    ``create()`` 必须挂在 ``window.events.before_show``（Event(self, True)，
    同步跑在 UI 线程）；**不要挂 events.shown** —— 它是 Event(self)，回调在
    异步线程，跨线程创建 WinForms 控件会出事。
    NotifyIcon / Form / 事件处理器都要长期持有引用，否则被 GC 回收后图标消失
    （与 gui._nccalcsize_hook 同一类坑，见 gui.py:301-303）。
    """

    def __init__(self, icon_path, on_show, on_exit, theme_provider=None):
        self._icon_path = icon_path
        self._on_show = on_show
        self._on_exit = on_exit
        self._theme_provider = theme_provider
        self._icon = None
        self._menu = None
        self._handlers = []

    def _handler(self, fn):
        def _call(sender, args):
            try:
                fn(args)
            except Exception:
                pass        # 托盘回调里抛异常会带崩消息循环，一律吞掉
        self._handlers.append(_call)
        return _call

    def _show_window(self):
        self.hide_menu()
        try:
            self._on_show()
        except Exception:
            pass

    def create(self) -> bool:
        if os.name != "nt" or self._icon is not None:
            return False
        try:
            D, W, _, _ = _gui()
            icon = W.NotifyIcon()
            if self._icon_path and Path(self._icon_path).is_file():
                icon.Icon = D.Icon(str(self._icon_path))
            icon.Text = "浅濑阅读器 Asase Reader"
            icon.Visible = True       # 常驻：程序一运行就在，与 closeToTray 开关无关
            self._icon = icon
            # 菜单创建失败时降级成「右键 = 显示主界面」，绝不退回系统菜单样式。
            try:
                menu = TrayMenu(self._show_window, self._on_exit,
                                self._theme_provider)
                menu.create()
                self._menu = menu
            except Exception:
                self._menu = None
            icon.MouseDown += self._handler(
                lambda a: self._on_icon_down(a.Button == W.MouseButtons.Right))
            icon.DoubleClick += self._handler(lambda a: self._show_window())
            return True
        except Exception:
            self._icon = None
            self._menu = None
            return False

    def _on_icon_down(self, is_right) -> None:
        if not is_right:
            return
        if self._menu is None:
            self._show_window()
        else:
            self._menu.show()

    def hide_menu(self) -> None:
        if self._menu is not None:
            self._menu.hide()

    def dispose(self) -> None:
        if self._menu is not None:
            try:
                self._menu.dispose()
            except Exception:
                pass
            self._menu = None
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.Visible = False
                icon.Dispose()
            except Exception:
                pass
        self._handlers = []
