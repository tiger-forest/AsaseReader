"""系统托盘与单实例（v2.2.0）。

设计要点（详见 .workbuddy/memory/plan-tray-close-behavior.md）：

- **零新依赖**：pywebview 在 Windows 上跑 WinForms 消息循环，项目已装 pythonnet，
  直接用 `System.Windows.Forms.NotifyIcon` 即可，不需要 pystray / Pillow / pywin32。
  托盘控件与主窗口共用同一个 UI 线程，不引入第二个消息循环。
- **拦截点唯一**：关闭走 `window.events.closing`（同步、UI 线程），
  前端 × 按钮与 Alt+F4 / 任务栏「关闭」自动汇合，不在前端另做判断。
- **退出必须放行**：`window.destroy()` 内部就是 `Form.Close()`，同样会触发
  `closing`；托盘「退出」要先置 `exiting`，否则会被自己拦死。
- **单实例**：命名 Mutex + `RegisterWindowMessage` 广播。第二个实例不启动后端、
  不抢固定端口 28765，只把已有窗口叫回来后直接返回。

纯逻辑（parse_close_to_tray / decide_close / is_primary_instance）刻意不依赖
Win32，方便在没有 GUI 的环境里做单测与变异验证。
"""

from __future__ import annotations

import ctypes
import os
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


class TrayIcon:
    """WinForms 托盘图标，菜单只有「显示主界面」「退出」两项。

    ``create()`` 必须挂在 ``window.events.before_show``（Event(self, True)，
    同步跑在 UI 线程）；**不要挂 events.shown** —— 它是 Event(self)，回调在
    异步线程，跨线程创建 WinForms 控件会出事。
    NotifyIcon / ContextMenuStrip / 事件处理器都要长期持有引用，否则被 GC
    回收后图标会凭空消失（与 gui._nccalcsize_hook 同一类坑）。
    """

    def __init__(self, icon_path, on_show, on_exit):
        self._icon_path = icon_path
        self._on_show = on_show
        self._on_exit = on_exit
        self._icon = None
        self._menu = None
        self._items = []
        self._handlers = []

    def _handler(self, fn):
        def _call(sender, args):
            try:
                fn()
            except Exception:
                pass        # 托盘回调里抛异常会带崩消息循环，一律吞掉
        self._handlers.append(_call)
        return _call

    def create(self) -> bool:
        if os.name != "nt" or self._icon is not None:
            return False
        try:
            import clr
            clr.AddReference("System.Windows.Forms")
            clr.AddReference("System.Drawing")
            import System.Drawing as Drawing
            import System.Windows.Forms as WinForms

            icon = WinForms.NotifyIcon()
            menu = WinForms.ContextMenuStrip()
            show_item = WinForms.ToolStripMenuItem("显示主界面")
            exit_item = WinForms.ToolStripMenuItem("退出")
            show_item.Click += self._handler(self._on_show)
            exit_item.Click += self._handler(self._on_exit)
            menu.Items.Add(show_item)
            menu.Items.Add(exit_item)
            icon.DoubleClick += self._handler(self._on_show)
            icon.ContextMenuStrip = menu
            if self._icon_path and Path(self._icon_path).is_file():
                icon.Icon = Drawing.Icon(str(self._icon_path))
            icon.Text = "浅濑阅读器 Asase Reader"
            icon.Visible = False          # 默认「直接关闭」时永远不显示

            self._icon = icon
            self._menu = menu
            self._items = [show_item, exit_item]
            return True
        except Exception:
            self._icon = None
            return False

    def set_visible(self, visible: bool) -> bool:
        if self._icon is None:
            return False
        try:
            self._icon.Visible = bool(visible)
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.Visible = False
                icon.Dispose()
            except Exception:
                pass
        self._menu = None
        self._items = []
        self._handlers = []
