"""托盘与关闭行为（v2.2.0 建，2026-11-15 扩）：能自动验的那部分。

原生托盘图标、右键菜单观感、双击唤起、退出后端口释放，jsdom 和静态断言都测不出来，
只能进人工回归清单。这里钉死的是「决策逻辑 + 自绘菜单的纯几何/配色 + 接线位置」：

  - 默认「直接关闭」，坏配置不许把用户突然变成进托盘；
  - 关闭拦截的唯一入口是 events.closing（不是只在前端 × 按钮判断）；
  - 「退出」必须先放行，否则 destroy() 会被自己拦死；
  - **托盘图标常驻**：程序一运行就 Visible=True，关闭/唤起都不许再切可见性；
  - **自绘菜单**：不用系统菜单控件，圆角 Region / DPI / 命中 / 夹取 / 明暗配色；
  - 托盘控件只能建在 before_show（同步 UI 线程），不能挪到 shown；
  - 单实例判定在创建 Backend / 启动固定端口服务之前；
  - 关机放行与唤醒消息都挂在已有的 WndProc 钩子里。

每条断言都要过变异验证：把实现改坏，这里必须红（见 .cache/mutate_tray.py 与
.cache/mutate_tray_menu.py）。静态断言一律**锚定具体函数体切片**，不做全文件搜
字符串 —— 别处出现同名字符串会让改坏的实现蒙混过关（2026-11-06 已吃过两次亏）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader import gui, tray  # noqa: E402
from asase_reader.backend import Backend  # noqa: E402

gui_src = (ROOT / "src" / "asase_reader" / "gui.py").read_text(encoding="utf-8")
tray_src = (ROOT / "src" / "asase_reader" / "tray.py").read_text(encoding="utf-8")
html_src = (ROOT / "src" / "asase_reader" / "assets" / "gui.html").read_text(
    encoding="utf-8")

TMP = Path(tempfile.mkdtemp(prefix="asase_tray_"))
ensure_tmp_env()
assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
assert_tmp_guard(TMP, "TMP")

fails: list[str] = []


def ok(cond, msg):
    if cond:
        print(f"PASS  {msg}")
    else:
        fails.append(msg)
        print(f"FAIL  {msg}")


def section(src: str, start: str, end: str) -> str:
    """取两个锚点之间的源码切片。

    锚点被改坏时返回空串，让下面的断言给出**明确 FAIL** —— 不能让测试自己
    崩在 ValueError 上冒充「变红」（测试崩溃 ≠ 断言失败，2026-11-06 抓到过两次）。
    """
    try:
        i = src.index(start)
        j = src.index(end, i)
        return src[i:j]
    except ValueError:
        return ""


def code_line(body: str, prefix: str) -> bool:
    """切片里存在一行「去缩进后以 prefix 开头」的**真实代码**。

    不能只用 `prefix in body`：把实现注释掉、或者写成 `pass  # prefix`，
    子串还在，静态断言就会被骗过去（2026-11-15 变异 M07/M12 实测抓到）。
    """
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(prefix) and not s.startswith("#"):
            return True
    return False


print("--- 默认值：直接关闭 ---")
ok(tray.parse_close_to_tray({}) is False, "配置里没有这个键 → 直接关闭")
ok(tray.parse_close_to_tray(None) is False, "配置不是字典 → 直接关闭")
ok(tray.parse_close_to_tray(["坏文件"]) is False, "配置是列表 → 直接关闭")
ok(tray.parse_close_to_tray({"closeToTray": "1"}) is True, "显式 \"1\" → 进托盘")
ok(tray.parse_close_to_tray({"closeToTray": "0"}) is False, "显式 \"0\" → 直接关闭")
ok(tray.parse_close_to_tray({"closeToTray": "随便什么"}) is False,
   "非法值 → 直接关闭（不能因为一份旧配置改变关闭行为）")
ok(tray.parse_close_to_tray({"closeToTray": " TRUE "}) is True,
   "大小写 / 空格容错，与 localStorage 字符串口径一致")

print("\n--- 关闭决策：纯函数 ---")
ok(tray.decide_close(False) == "allow", "默认不进托盘 → 放行")
ok(tray.decide_close(True) == "hide", "开了进托盘 → 取消关闭并隐藏")
ok(tray.decide_close(True, exiting=True) == "allow",
   "托盘菜单「退出」必须放行，否则 destroy() 触发 closing 会被自己拦死")
ok(tray.decide_close(True, session_ending=True) == "allow",
   "系统关机 / 注销必须放行，不能拖住关机")
tray.mark_session_ending()
ok(tray.decide_close(True) == "allow", "WndProc 置位的全局标志同样生效")
tray.reset_session_ending()
ok(tray.decide_close(True) == "hide", "标志复位后回到正常的进托盘")

print("\n--- 单实例判定 ---")
ok(tray.is_primary_instance(0) is True, "GetLastError=0 → 主实例")
ok(tray.is_primary_instance(tray.ERROR_ALREADY_EXISTS) is False,
   "ERROR_ALREADY_EXISTS(183) → 已有实例，第二个不启动")
ok(tray.is_primary_instance(5) is True, "别的错误码不误判成已有实例")

print("\n--- 自绘菜单：明暗配色 ---")
ok(tray.parse_ui_mode({}) == "light", "uiMode 缺键 → 浅色")
ok(tray.parse_ui_mode({"uiMode": "dark"}) == "dark", "uiMode=dark → 深色")
ok(tray.parse_ui_mode({"uiMode": " DARK "}) == "dark", "大小写 / 空格容错")
ok(tray.parse_ui_mode({"uiMode": "随便"}) == "light", "非法值 → 浅色")
ok(tray.parse_ui_mode(None) == "light", "配置不是字典 → 浅色")
light = tray.choose_palette("light")
dark = tray.choose_palette("dark")
ok(light["bg"] == (255, 255, 255) and dark["bg"] == (43, 45, 49),
   "浅色白底 / 深色底，两套配色确实不同")
ok(tray.choose_palette("不存在")["bg"] == (255, 255, 255),
   "未知模式回退浅色，不抛异常")
light["bg"] = (0, 0, 0)
ok(tray.choose_palette("light")["bg"] == (255, 255, 255),
   "choose_palette 返回副本，改它不会污染常量表")
ok(len(light) == 6 and "hover" in light and "separator" in light
   and "border" in light and "icon" in light and "text" in light,
   "配色六项齐全：底 / 边框 / 文字 / 图标 / hover / 分隔线")

print("\n--- 自绘菜单：几何 / DPI / 命中 / 定位 ---")
g1 = tray.menu_geometry(1.0)
ok(g1["width"] == 208, "96 DPI 基准宽度 208")
ok(g1["height"] == g1["pad_v"] * 2 + g1["item_h"] * 2 + g1["sep_h"],
   "高度 = 上下留白 + 两项 + 分隔线（绘制与命中同源）")
ok(tray.item_y(g1, 0) == g1["pad_v"], "第 0 项从上留白开始")
ok(tray.item_y(g1, 1) == g1["pad_v"] + g1["item_h"] + g1["sep_h"],
   "第 1 项在分隔线之后")
g2 = tray.menu_geometry(2.0)
ok(g2["width"] == 416 and g2["height"] == g1["height"] * 2,
   "200% DPI 时宽高同比放大")
ok(tray.menu_geometry("坏值")["width"] == 208, "非法 scale 回退 1.0")
ok(tray.menu_geometry(100)["scale"] == 4.0, "scale 上限夹在 4.0")
ok(tray.menu_geometry(-5)["scale"] == 0.5, "scale 下限夹在 0.5")

ok(tray.hit_test(g1["text_x"], tray.item_y(g1, 0) + 10, g1) == 0,
   "点第 0 行 → 下标 0")
ok(tray.hit_test(g1["text_x"], tray.item_y(g1, 1) + 10, g1) == 1,
   "点第 1 行 → 下标 1")
ok(tray.hit_test(g1["text_x"], 0, g1) == -1, "点上留白 → -1")
ok(tray.hit_test(g1["text_x"], g1["height"] - 1, g1) == -1, "点下留白 → -1")
ok(tray.hit_test(g1["text_x"], tray.item_y(g1, 1) - 1, g1) == -1,
   "点分隔线 → -1（不能误触发任意一项）")
ok(tray.hit_test(-1, 20, g1) == -1 and tray.hit_test(g1["width"], 20, g1) == -1,
   "x 越界 → -1")
ok(tray.hit_test(None, 20, g1) == -1 and tray.hit_test(20, None, g1) == -1,
   "坐标缺失 → -1，不抛异常")
ok(tray.hit_test("bad", "bad", g1) == -1, "非数字坐标 → -1")

w, h = g1["width"], g1["height"]
x, y = tray.clamp_position(1700, 1000, 0, 0, 1707, 1019, w, h)
ok(x == 1700 - w and y == 1000 - h, "右下角光标 → 菜单向左上展开")
ok(x >= 0 and y >= 0 and x + w <= 1707 and y + h <= 1019,
   "夹取后整张卡片都在工作区内")
x2, y2 = tray.clamp_position(10, 10, 0, 0, 1707, 1019, w, h)
ok(x2 >= 2 and y2 >= 2, "光标贴左上角时不会把菜单顶出屏幕上/左边")
x3, y3 = tray.clamp_position(-1500, -100, -1920, -200, 0, 880, w, h)
ok(x3 >= -1918 and y3 >= -198, "副屏（负坐标工作区）也能正确夹取")

print("\n--- 自绘菜单：行为（不依赖真实 GUI） ---")
calls = []
m = tray.TrayMenu(lambda: calls.append("show"), lambda: calls.append("exit"))
ok(m.show() is False, "没 create 时 show() 安全返回 False")
m._on_down(g1["text_x"], tray.item_y(g1, 0) + 10, True)
m._on_down(g1["text_x"], tray.item_y(g1, 1) + 10, True)
ok(calls == ["show", "exit"], "两项点击分别走「显示主界面」和「退出」")
m._on_down(g1["text_x"], tray.item_y(g1, 0) + 10, False)
ok(calls == ["show", "exit"], "右键按在菜单上不触发项")
m._on_down(g1["text_x"], 0, True)
ok(calls == ["show", "exit"], "点留白不触发项")
m._set_hover(1)
ok(m._hover == 1, "hover 状态能被 MouseMove 记账")
m._on_key(True)
ok(m._hover == -1, "Esc 关闭时清掉 hover 记账")
m._on_key(False)
ok(m._hover == -1, "别的按键不误关菜单")
m2 = tray.TrayMenu(lambda: None, lambda: None,
                   theme_provider=lambda: {"uiMode": "dark"})
m2._refresh_theme()
ok(m2._palette["bg"] == (43, 45, 49), "主题 provider 返回 dark 时切深色")
m3 = tray.TrayMenu(lambda: None, lambda: None,
                   theme_provider=lambda: (_ for _ in ()).throw(RuntimeError()))
m3._refresh_theme()
ok(m3._palette["bg"] == (255, 255, 255), "主题 provider 抛错时回退浅色，不崩")
ok(tray.MENU_ITEMS == ("显示主界面", "退出") and len(tray.MENU_ITEMS) == 2,
   "菜单恰好两项，文案与拍板一致")

icalls = []
ti = tray.TrayIcon(None, lambda: icalls.append("show"),
                   lambda: icalls.append("exit"))
ti._on_icon_down(False)
ok(icalls == [], "左键单击托盘不弹菜单")
ti._on_icon_down(True)
ok(icalls == ["show"], "菜单创建失败时右键降级为「显示主界面」")
ti.hide_menu()
ti.dispose()
ok(ti._icon is None and ti._menu is None, "dispose 后不留图标与菜单引用")

print("\n--- 静态契约：托盘图标常驻 ---")
icon_create = section(tray_src,
                      '        if os.name != "nt" or self._icon is not None:',
                      "    def _on_icon_down(self, is_right)")
ok("icon.Visible = True" in icon_create,
   "TrayIcon.create 里图标 Visible=True —— 程序一运行就常驻")
ok("icon.Visible = False" not in icon_create,
   "create 里没有把图标藏起来（旧行为是关窗后才冒出来）")
ok("set_visible" not in gui_src,
   "gui.py 不再有任何图标可见性切换 —— 常驻与 closeToTray 开关完全解耦")
ok("ContextMenuStrip" not in tray_src and "ToolStripMenuItem" not in tray_src
   and "menu.Items.Add(" not in tray_src,
   "彻底移除系统菜单控件（它画的是直角 + 蓝色方块高亮）")
ok("self._handlers.append(_call)" in tray_src,
   "事件处理器仍被长期持有，GC 不会让菜单失灵")
_dispose_anchor = ("    def dispose(self) -> None:\n"
                   "        if self._menu is not None:")
dispose_body = (tray_src[tray_src.index(_dispose_anchor):]
                if _dispose_anchor in tray_src else "")
ok(code_line(dispose_body, "self._menu.dispose()")
   and code_line(dispose_body, "icon.Dispose()"),
   "退出时菜单 Form 与托盘图标一起释放，不留幽灵图标")

print("\n--- 静态契约：自绘圆角菜单 ---")
menu_create = section(tray_src, "        D, W, D2, _ = _gui()",
                      "    def _refresh_theme(self)")
ok('getattr(W.FormBorderStyle, "None")' in menu_create,
   "Form 无边框（.NET 枚举 None 是 Python 关键字，必须 getattr）")
ok("form.ShowInTaskbar = False" in menu_create, "菜单不进任务栏 / Alt+Tab")
ok("form.TopMost = True" in menu_create, "菜单置顶，盖得住最大化的主窗口")
ok("form.KeyPreview = True" in menu_create, "KeyPreview 开启才能接 Esc")
ok(code_line(menu_create, "form.Region = self._region")
   and "_rounded_path(D2" in menu_create, "圆角 Region 真的挂在 Form 上")
ok("form.Paint += " in menu_create and "form.MouseMove += " in menu_create
   and "form.MouseDown += " in menu_create, "绘制 / hover / 点击三类事件都接好")
ok("form.Deactivate += " in menu_create, "失焦关闭（点别处收起菜单）")
ok("W.Keys.Escape" in menu_create and "W.MouseButtons.Left" in menu_create,
   "Esc 与左键判定都锚在 WinForms 枚举上")
ok("dpi / 96.0" in menu_create and "menu_geometry(" in menu_create,
   "按真实 DPI 算几何，高 DPI 下不会缩成一小块")

paint_body = section(tray_src, "    def _paint(self, gfx)",
                     "    def _set_hover(self, index: int)")
ok("_draw_icon(gfx, i," in paint_body and "MENU_ITEMS" in paint_body
   and "DrawString" in paint_body, "每项都画图标 + 文字")
ok("self._hover in (0, 1)" in paint_body and "self._geom" in paint_body,
   "hover 高亮用与命中测试同一份 geometry")
ok("separator" in paint_body and "DrawLine" in paint_body, "两项之间有细分隔线")
ok("SmoothingMode" in paint_body, "开抗锯齿，圆角与线条不毛糙")

menu_show = section(tray_src, "    def show(self) -> bool:",
                    "    def hide(self) -> None:")
ok("clamp_position(" in menu_show and "W.Screen.FromPoint(pos).WorkingArea" in menu_show,
   "定位按光标所在屏的工作区夹取，不会掉到任务栏下面")
ok("f.Show()" in menu_show and "f.Activate()" in menu_show,
   "显示并激活（激活后 Deactivate 才能收起）")
ok(code_line(menu_show, "self._refresh_theme()"),
   "每次弹出读一次主题，设置改动即时生效")
ok(code_line(menu_show, "self._shown_at = time.time()")
   and code_line(section(tray_src, "    def _on_deactivate(self)",
                         "    def show(self) -> bool:"),
                 "if time.time() - self._shown_at < 0.2:"),
   "有弹出保护，Show→Activate 之间的瞬时 Deactivate 不会让菜单一闪就没")

icon_down = section(tray_src, "    def _on_icon_down(self, is_right)",
                    "    def hide_menu(self)")
ok("W.MouseButtons.Right" in icon_create and "icon.MouseDown += " in icon_create,
   "右键由 NotifyIcon.MouseDown 自己触发（没有系统菜单控件兜底）")
ok("self._menu.show()" in icon_down, "右键弹出自绘菜单")
ok("DoubleClick" in icon_create, "双击图标仍等价于「显示主界面」")
ok("theme_provider=api.get_ui_settings" in gui_src,
   "配色 provider 接在 GuiApi 的公开方法上，不碰私有属性")

print("\n--- GuiApi 内存镜像：设置改完立刻生效 ---")
state = TMP / "state"
state.mkdir(parents=True, exist_ok=True)
(state / "config.json").write_text(
    json.dumps({"shelf_dir": str(TMP / "shelf")}), encoding="utf-8")


def new_api():
    b = Backend(state_dir=state)
    assert_tmp_guard(b.shelf_root(), "shelf")
    return gui.GuiApi(b)


api = new_api()
ok(api._close_to_tray is False, "启动时镜像默认 False")
api.save_ui_settings({"closeToTray": "1"})
ok(api._close_to_tray is True,
   "save_ui_settings 同步更新内存镜像 —— 只读文件的话刚勾上就点 × 会读到旧值")
api.save_ui_settings({"closeToTray": "0"})
ok(api._close_to_tray is False, "改回直接关闭也立即生效")
ok(new_api()._close_to_tray is False, "落盘的是 \"0\"，重启镜像仍是 False")
Backend(state_dir=state).save_ui_settings({"closeToTray": "1"})
ok(new_api()._close_to_tray is True, "启动时从后端副本 ui_settings.json 初始化镜像")

print("\n--- 接线位置：关闭拦截是唯一入口 ---")
closing_body = section(gui_src, "    def _on_closing():",
                       "    window.events.closing += _on_closing")
ok("window.events.closing += _on_closing" in gui_src,
   "拦截挂在 events.closing —— 前端 × 按钮与 Alt+F4 / 任务栏关闭都走 FormClosing")
ok("window.events.shown += _on_closing" not in gui_src,
   "没有错挂到 shown（异步线程，且根本不是关闭事件）")
ok("tray.decide_close(api._close_to_tray, api._exiting)" in closing_body,
   "决策走纯函数，并把 exiting 传进去")
ok("window.hide()" in closing_body, "进托盘是隐藏窗口，不是最小化")
ok("tray_icon.hide_menu()" in closing_body, "关闭时若菜单开着，顺手收起")
ok("return False" in closing_body, "返回 False 才会让 pywebview 取消关闭")
ok("closeWindow()" in html_src and "api().close_window()" in html_src,
   "前端 × 按钮仍走原来的 close_window，没有另做一套判断（否则两条路径行为分叉）")

print("\n--- 接线位置：退出 / 唤起 / 托盘创建 ---")
exit_body = section(gui_src, "    def _exit_from_tray():",
                    "    tray_icon = tray.TrayIcon")
ok("api._exiting = True" in exit_body, "退出前先置放行标志")
ok("window.destroy()" in exit_body, "退出真的销毁窗口（不是再 hide 一次）")
ok("tray_icon.dispose()" in exit_body, "退出前释放托盘图标，不留幽灵图标")
show_body = section(gui_src, "    def _show_from_tray():", "    def _exit_from_tray():")
ok("window.show()" in show_body, "显示主界面确实把窗口 show 回来")
ok("tray_icon.hide_menu()" in show_body, "唤起时收起菜单，但不收起常驻图标")
ok("_install_native_borderless(window)" in show_body,
   "唤起后幂等补挂边框钩子")
init_body = section(gui_src, "    def _init_native_window():",
                    "    window.events.before_show += _init_native_window")
ok("tray_icon.create()" in init_body,
   "托盘控件在 before_show 创建（同步 UI 线程）")
ok("window.events.before_show += _init_native_window" in gui_src
   and "window.events.shown += _init_native_window" not in gui_src,
   "没有把托盘创建挪到 shown —— Event(self) 的回调在异步线程")
_fin_anchor = "    finally:\n        try:\n            tray_icon.dispose()"
fin = gui_src[gui_src.index(_fin_anchor):] if _fin_anchor in gui_src else ""
ok("tray_icon.dispose()" in fin[:200] and "server.stop()" in fin[:300],
   "正常退出也会释放图标，且 backend.stop / server.stop 仍在 finally 里")

print("\n--- 接线位置：单实例在抢端口之前 ---")
launch = section(gui_src, "def launch_gui(", "\n\nif __name__ ==")
ok("primary, wake_id = tray.acquire_single_instance()" in launch,
   "launch_gui 里做了单实例判定")
ok("acquire_single_instance()" in launch and "server.start(port=" in launch
   and launch.index("acquire_single_instance()") < launch.index("server.start(port="),
   "判定在创建 Backend / 启动固定端口服务之前，第二个实例不会去抢 28765")
ok("tray.wake_existing_window(wake_id)" in launch
   and "if not primary:" in launch, "非主实例只广播唤醒消息，然后返回")
ok("api = GuiApi(backend)" in launch and "js_api=api," in launch,
   "GuiApi 实例被 launch_gui 的闭包直接持有（closing 才能读内存镜像）")

print("\n--- 接线位置：WndProc 的关机放行与唤醒 ---")
proc_body = section(gui_src, "    def _proc(h, msg, wparam, lparam):",
                    "    cb = _WNDPROC_T(_proc)")
ok("tray.WM_QUERYENDSESSION" in proc_body and "tray.mark_session_ending()" in proc_body,
   "关机 / 注销消息置位放行标志（pywebview 的 closing 不传 CloseReason）")
ok("_wake_msg_id" in proc_body and "_wake_callback" in proc_body
   and "cb()" in proc_body,
   "唤醒广播在已有 WndProc 钩子里处理，没有另开一条窗口过程")
ok("msg == _WM_NCCALCSIZE" in proc_body,
   "加分支没把原来的 WM_NCCALCSIZE 处理挤掉（无边框窗口的根基）")

print("\n--- 前端设置项 ---")
ok('id="cfg_ui_close_to_tray"' in html_src, "设置页有开关")
ok('setUiPref("closeToTray"' in html_src, "改动走 setUiPref（localStorage + 后端副本）")
ok('localStorage.setItem("closeToTray"' not in html_src,
   "没有绕过 setUiPref 直接写 localStorage")
ok('localStorage.getItem("closeToTray") === "1"' in html_src,
   "applyUiSettings 会回填开关状态")
ok('id="cfg_ui_close_to_tray"></label>' in html_src
   or 'id="cfg_ui_close_to_tray">' in html_src,
   "开关没有 checked 属性 → 默认不勾选")

print()
if fails:
    print(f"FAILED {len(fails)} 条：")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
