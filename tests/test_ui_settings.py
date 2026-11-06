"""界面设置的持久化（v1.29.0）：改完退出再进，偏好要还在。

原来的毛病：界面设置只写 localStorage，而它归浏览器管 —— 两处都会让它读不回来：
  1. 静态服务用随机端口，而 localStorage 按「源」隔离，**源里含端口** → 每次
     启动都是新源，上一次存的全读不到；
  2. `webview.start()` 默认 private_mode=True，WebView2 把用户数据目录放进系统
     临时目录，进程一退就删。
所以补了双保险：端口固定（占住了才回退随机）+ 关掉无痕并指定固定数据目录，
再加一份后端副本 `ui_settings.json`，启动时回写前端 —— 就算前两条都漂了，
偏好也不会丢。

「启动后主题还是默认」只能人工看（jsdom 里没有 WebView2 的真实存储），
但下面这些能钉死：
  - 后端副本：存 / 读 / 合并 / 文件坏了回默认 / 不进 config.json；
  - 固定端口：给了就用、被占就回退（起不来比端口漂更糟）；
  - 前端写入一律走 setUiPref（localStorage + 后端同步），不许有漏网的；
  - boot() 先把后端副本写回 localStorage，再初始化界面（顺序反了等于白存）。

每条断言都过了变异验证：把实现改坏，这里必须红。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader import gui  # noqa: E402
from asase_reader.backend import Backend  # noqa: E402
from asase_reader.staticserver import StaticServer  # noqa: E402

html_src = (ROOT / "src" / "asase_reader" / "assets" / "gui.html").read_text(
    encoding="utf-8")
gui_src = (ROOT / "src" / "asase_reader" / "gui.py").read_text(encoding="utf-8")

TMP = Path(tempfile.mkdtemp(prefix="asase_uisettings_"))
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


print("--- 后端副本：存 / 读 / 合并 ---")
(TMP / "state").mkdir(parents=True, exist_ok=True)
(TMP / "state" / "config.json").write_text(json.dumps({"shelf_dir": str(TMP / "shelf")}), encoding="utf-8")
b = Backend(state_dir=TMP / "state")
assert_tmp_guard(b.shelf_root(), "shelf")
cfg_before = json.dumps(b.config, sort_keys=True)

ok(b.get_ui_settings() == {}, "没有文件时返回空字典（全部走默认值）")

r = b.save_ui_settings({"uiTheme": "notion"})
ok(r.get("ok") is True, f"保存成功（实际 {r}）")
ok(b.get_ui_settings() == {"uiTheme": "notion"}, "存进去能原样读回来")

b.save_ui_settings({"uiMode": "dark"})
got = b.get_ui_settings()
ok(got == {"uiTheme": "notion", "uiMode": "dark"},
   f"第二次保存是**合并**不是覆盖（实际 {got}）—— 覆盖的话改一次主题就把别的偏好清了")

p = TMP / "state" / "ui_settings.json"
ok(p.exists(), "真的落盘了（不是只在内存里）")
ok(json.loads(p.read_text(encoding="utf-8")) == got, "文件内容与读到的一致")

# 值一律存成字符串：localStorage 的口径就是字符串，存成别的类型回写时会变味
b.save_ui_settings({"uiCardSize": 280})
ok(b.get_ui_settings()["uiCardSize"] == "280",
   "值一律转成字符串存（localStorage 的口径）—— 回写时不会出现 '280' vs 280 的错位")

print("\n--- 坏了也不能让程序起不来 ---")
(TMP / "state" / "ui_settings.json").write_text("{ 这不是 json", encoding="utf-8")
ok(b.get_ui_settings() == {}, "文件是坏 json 时返回空字典（回默认值，不抛异常）")
ok(b.save_ui_settings({"uiTheme": "apple"}).get("ok") is True,
   "坏文件之后仍能保存（覆盖掉坏的，自愈）")
(TMP / "state" / "ui_settings.json").write_text('["不是对象"]', encoding="utf-8")
ok(b.get_ui_settings() == {}, "文件是 json 但不是对象时同样回默认")
ok(b.save_ui_settings(None).get("ok") is True, "传 None 不当异常（当没传）")

print("\n--- 只管显示偏好，不碰 config.json ---")
ok(json.dumps(b.config, sort_keys=True) == cfg_before,
   "读写界面设置没有改动 config.json —— 下载行为与显示偏好互不干扰")
ok("config.json" not in str(TMP / "state" / "ui_settings.json"), "副本是独立文件")

print("\n--- get_state 带上副本，前端启动时才有得写回 ---")
ok("ui_settings" in b.get_state(), "get_state 返回 ui_settings")
ok(b.get_state()["ui_settings"] == b.get_ui_settings(),
   "get_state 里的副本与 get_ui_settings 一致")

print("\n--- GuiApi 两个方法只是转发 ---")
(TMP / "state2").mkdir(parents=True, exist_ok=True)
(TMP / "state2" / "config.json").write_text(json.dumps({"shelf_dir": str(TMP / "shelf2")}), encoding="utf-8")
b2 = Backend(state_dir=TMP / "state2")
api = gui.GuiApi(b2)
ok(api.get_ui_settings() == {}, "GuiApi.get_ui_settings 转发后端")
ok(api.save_ui_settings({"uiTheme": "apple"}).get("ok") is True,
   "GuiApi.save_ui_settings 转发后端")
ok(api.get_ui_settings() == {"uiTheme": "apple"}, "转发的是同一份数据（不是各存一份）")

print("\n--- 静态服务：固定端口，被占才回退 ---")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


port = _free_port()
srv = StaticServer(root=TMP, gui_html=TMP / "gui.html")
try:
    base = srv.start(port=port)
    ok(srv.port == port, f"给了端口就用它（实际 {srv.port} / 期望 {port}）")
    ok(base == f"http://127.0.0.1:{port}", f"base URL 与端口一致（实际 {base}）")
finally:
    srv.stop()

# 占住一个端口再要它：必须回退，绝不能因为端口冲突就起不来
occupied = socket.socket()
occupied.bind(("127.0.0.1", 0))
occupied.listen(1)
busy = occupied.getsockname()[1]
srv2 = StaticServer(root=TMP, gui_html=TMP / "gui.html")
try:
    srv2.start(port=busy)
    ok(srv2.port != busy and srv2.port > 0,
       f"端口被占时回退到随机端口而不是崩（实际 {srv2.port}）—— 起不来比端口漂更糟；"
       f"漂了还有 ui_settings.json 兜底")
finally:
    srv2.stop()
    occupied.close()

srv3 = StaticServer(root=TMP, gui_html=TMP / "gui.html")
try:
    srv3.start()
    ok(srv3.port > 0, f"不给端口时照旧随机（实际 {srv3.port}）—— 兼容旧调用")
finally:
    srv3.stop()

ok(f"start(port={port})" in gui_src or "server.start(port=" in gui_src,
   "launch_gui 启动服务时传了固定端口 —— 不传的话每次端口都变，localStorage 每次换源")
ok("private_mode=False" in gui_src,
   "webview.start 关掉了 private_mode —— 默认 True 会把 WebView2 数据目录放进"
   "临时目录，进程一退 localStorage 就被删")

print("\n--- 前端：界面偏好一律走 setUiPref（localStorage + 后端各一份）---")
ok("function setUiPref(" in html_src, "定义了 setUiPref")
for key in ["uiThumbMode", "uiTheme", "uiMode", "uiCardHover", "uiCardShadow",
            "uiMaxTaskbar", "uiWinAnim", "uiCoverMode", "uiCardSize",
            "readerMode", "uiThumbLast"]:
    ok(f'setUiPref("{key}"' in html_src,
       f"{key} 走 setUiPref —— 直接写 localStorage 的话后端副本就没有它")
    ok(f'localStorage.setItem("{key}"' not in html_src,
       f"{key} 没有绕过 setUiPref 直接写 localStorage（那正是设置存不住的老路子）")

print("\n--- 启动时先把副本写回 localStorage，再初始化界面 ---")
_boot = html_src[html_src.index("async function boot()"):]
ok("s.ui_settings" in _boot, "boot() 读了后端那份副本")
_i_write = _boot.find("localStorage.setItem(k,")
_i_init = _boot.find("initUiSettings()")
ok(_i_write >= 0 and _i_init >= 0 and _i_write < _i_init,
   f"写回发生在 initUiSettings() 之前（写回 {_i_write} / 初始化 {_i_init}）—— "
   f"反了的话这次启动读到的还是浏览器里的旧值，副本白存")

print()
if fails:
    print(f"FAILED {len(fails)} 条：")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
