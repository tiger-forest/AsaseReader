"""托盘与关闭行为（v2.2.0）：能自动验的那部分。

原生托盘图标、右键菜单、双击唤起、退出后端口释放，jsdom 和静态断言都测不出来，
只能进人工回归清单。这里钉死的是「决策逻辑 + 接线位置」：

  - 默认「直接关闭」，坏配置不许把用户突然变成进托盘；
  - 关闭拦截的唯一入口是 events.closing（不是只在前端 × 按钮判断）；
  - 「退出」必须先放行，否则 destroy() 会被自己拦死；
  - 托盘控件只能建在 before_show（同步 UI 线程），不能挪到 shown；
  - 单实例判定在创建 Backend / 启动固定端口服务之前；
  - 关机放行与唤醒消息都挂在已有的 WndProc 钩子里。

每条断言都要过变异验证：把实现改坏，这里必须红（见 .cache/mutate_tray.py）。
静态断言一律**锚定具体函数体切片**，不做全文件搜字符串 —— 别处出现同名
字符串会让改坏的实现蒙混过关（2026-11-06 已吃过两次亏）。
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
ok("tray_icon.set_visible(True)" in closing_body, "隐藏时显示托盘图标")
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
ok("tray_icon.set_visible(False)" in show_body, "回来后托盘图标收起")
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

print("\n--- 托盘菜单只有两项 ---")
ok('WinForms.ToolStripMenuItem("显示主界面")' in tray_src
   and 'WinForms.ToolStripMenuItem("退出")' in tray_src, "两项菜单文案正确")
ok(tray_src.count("menu.Items.Add(") == 2,
   "菜单项恰好 Add 两次 —— 用户明确只做显示主界面和退出")
ok("icon.Visible = False" in tray_src, "启动时图标不可见，默认「直接关闭」不冒托盘")
ok("DoubleClick" in tray_src, "双击图标等价于「显示主界面」")

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
