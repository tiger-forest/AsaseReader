"""应用图标（v1.30.0）：exe 图标 + 窗口/任务栏图标 + 页面 favicon。

背景：assets/ 里躺着一份 7 尺寸集成包 icon.ico（16/24/32/48/64/128/256，条目是
PNG 压缩的 Vista 格式）和一批分立尺寸。接进来要走三条路，缺一条就只有半截：
  1. exe 文件图标 —— PyInstaller spec 的 `icon=`（资源管理器 / 快捷方式看到它）；
  2. 窗口 + 任务栏 —— `webview.start(icon=...)`。不传的话 pywebview 会退回
     `ExtractIconW(sys.executable)`，源码运行时那是 **python.exe 的图标**：
     打包后碰巧对、源码跑就错，属于「只在一种跑法下正确」的坑；
  3. 页面 favicon —— 静态服务的 /favicon.ico + gui.html 的 <link rel="icon">。

**最关键的一条**：第 2 条走的是 `System.Drawing.Icon`，而它读不动 PNG 压缩的 ICO
条目 —— 单尺寸的抛 `Icon.BmpFrame()` 越界，多尺寸的不抛但**画出来是彩色噪点**
（2026-11-02 实测：拿 DrawIcon 画出来对比过）。所以窗口/任务栏/favicon 一律用
`icon_win.ico`：由那批 PNG 转成 **32bpp BMP 条目**的同尺寸副本
（`tools/make_icon_win.py`）。exe 图标仍用交付的 `icon.ico`
（PyInstaller 嵌入时会自己转成 BMP，实测没问题）。
这条约束由「每个条目必须是 32bpp BMP」的断言守着 —— 换回 PNG 条目必须红。

能自动验的都钉在这里：
  - 两个图标文件都在，且结构合法（ICO 头 / 条目落在文件内 / 尺寸齐全）；
  - 窗口用的那份是 32bpp BMP（PNG 条目 = 任务栏噪点）；
  - `_icon_path()` 在源码与 frozen 两种跑法下都指到真实文件，找不到返回 None
    （图标是门面，不能反过来挡住启动）；
  - spec 声明了 icon= 且把图标随包带上（运行时还要按路径读它）；
  - 真的把 icon 传给了 webview.start，且 TypedError 旁路还在（老 pywebview）；
  - favicon 路由：配了就 200 + x-icon + 字节一致，没配就 404，/outputs/ 零回归。

每条断言都过了变异验证：把实现改坏，这里必须红。
"""

from __future__ import annotations

import struct
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asase_reader import gui  # noqa: E402
from asase_reader.staticserver import StaticServer, _EXTRA_TYPES  # noqa: E402

ICO = ROOT / "src" / "asase_reader" / "assets" / "icon.ico"          # 交付母版（exe 用）
WIN = ROOT / "src" / "asase_reader" / "assets" / "icon_win.ico"      # 32bpp BMP 副本（窗口用）
spec_src = (ROOT / "AsaseReader.spec").read_text(encoding="utf-8")
gui_src = (ROOT / "src" / "asase_reader" / "gui.py").read_text(encoding="utf-8")
html_src = (ROOT / "src" / "asase_reader" / "assets" / "gui.html").read_text(
    encoding="utf-8")

fails: list[str] = []


def ok(cond, msg):
    if cond:
        print(f"PASS  {msg}")
    else:
        fails.append(msg)
        print(f"FAIL  {msg}")


def parse_ico(path: Path):
    """解析 ICO：返回 (count, [(w, h, payload_kind, in_bounds)])。"""
    b = path.read_bytes()
    _res, typ, count = struct.unpack("<HHH", b[:6])
    entries = []
    for i in range(count):
        off = 6 + i * 16
        w, h, _cc, _r, _pl, bc, nbytes, ioff = struct.unpack(
            "<BBBBHHII", b[off:off + 16])
        payload = b[ioff:ioff + nbytes]
        kind = "PNG" if payload[:8] == b"\x89PNG\r\n\x1a\n" else (
            "BMP" if nbytes > 0 else "empty")
        entries.append((w or 256, h or 256, bc, kind, ioff + nbytes <= len(b)))
    return typ, count, entries


print("--- 图标文件本身：是合法的多尺寸 ICO ---")
ok(ICO.is_file(), f"交付的集成包存在：{ICO.name}")
typ, count, entries = parse_ico(ICO)
ok(typ == 1, f"ICO type == 1（图标），实际 {typ}")
ok(count >= 1, f"至少 1 个尺寸条目，实际 {count}")
ok(all(e[4] for e in entries),
   "每个条目的 imageOffset+bytesInRes 都落在文件内（越界就是坏文件，"
   "Windows 会直接拒绝加载）")
ok(all(e[3] in ("PNG", "BMP") for e in entries),
   f"每个条目都是 PNG 或 BMP 负载（实际 {[e[3] for e in entries]}）")
sizes = {e[0] for e in entries}
ok(16 in sizes and 256 in sizes,
   f"同时含 16（任务栏小图标）与 256（缩略图大图），实际 {sorted(sizes)}")
ok(len(entries) >= 4,
   f"条目够多才能各尺寸都清晰，实际 {len(entries)} 个 —— 只有一个 16x16 的话"
   f"资源管理器大图标视图会糊")

print("\n--- 窗口用的那份必须是 32bpp BMP 条目（PNG 条目 = 任务栏噪点）---")
ok(WIN.is_file(), f"存在：{WIN.name}")
_t, _c, wentries = parse_ico(WIN)
ok(_t == 1 and _c >= 1, f"是合法 ICO（type={_t} 条目={_c}）")
ok(all(e[3] == "BMP" for e in wentries),
   f"全部是 BMP 条目（实际 {[e[3] for e in wentries]}）—— "
   f"PNG 压缩的条目 System.Drawing.Icon 读不动：单尺寸抛 Icon.BmpFrame() 越界，"
   f"多尺寸不抛但**画出来是彩色噪点**")
ok(all(e[2] == 32 for e in wentries),
   f"全部 32bpp（实际 {[e[2] for e in wentries]}）—— "
   f"用 Icon.FromHandle(GetHicon()) 存出来会变成 4bpp（16 色），画质被压坏")
wsizes = {e[0] for e in wentries}
ok(16 in wsizes and 256 in wsizes,
   f"与母版一样含 16 与 256（实际 {sorted(wsizes)}）—— 少一个尺寸，"
   f"任务栏或 Alt-Tab 就会拿放大图硬缩")
ok(all(e[4] for e in wentries), "每个条目都落在文件内")

print("\n--- 分立尺寸的图标也在（备用，不参与运行）---")
for name in ("icon_16.ico", "icon_32.ico", "icon_48.ico", "icon_256.ico",
             "icon.png", "icon_1024.png"):
    ok((ROOT / "src" / "asase_reader" / "assets" / name).is_file(),
       f"{name} 在包内")

print("\n--- _icon_path()：两种跑法都指到真实文件 ---")
p = gui._icon_path()
ok(p is not None and Path(p).is_file(), f"源码运行能找到图标（实际 {p}）")
ok(p is not None and Path(p).name == "icon_win.ico",
   f"用的是 32bpp BMP 副本（实际 {p}）—— 直接用交付的 icon.ico（PNG 条目）"
   f"任务栏会画成噪点")

# 打包后：datas 把图标放到 asase_reader/assets，_MEIPASS 那一份应当优先
tmp = Path(tempfile.mkdtemp(prefix="asase_icon_"))
fake_mei = tmp / "_MEIPASS" / "asase_reader" / "assets"
fake_mei.mkdir(parents=True)
for n in ("icon.ico", "icon_win.ico"):
    (fake_mei / n).write_bytes((ROOT / "src" / "asase_reader" / "assets" / n)
                               .read_bytes())
old_frozen, old_mei = getattr(sys, "frozen", False), getattr(sys, "_MEIPASS", None)
try:
    sys.frozen = True  # type: ignore[attr-defined]
    sys._MEIPASS = str(tmp / "_MEIPASS")  # type: ignore[attr-defined]
    p2 = gui._icon_path()
finally:
    if old_frozen:
        sys.frozen = old_frozen  # type: ignore[attr-defined]
    else:
        del sys.frozen
    if old_mei is not None:
        sys._MEIPASS = old_mei  # type: ignore[attr-defined]
    elif hasattr(sys, "_MEIPASS"):
        del sys._MEIPASS
ok(p2 is not None and Path(p2) == fake_mei / "icon_win.ico",
   f"frozen 时优先取解包目录那一份（实际 {p2}）—— 取不到就只剩 exe 资源图标，"
   f"窗口图标会退回 python/exe 的默认值")

# 找不到必须返回 None，而不是抛异常把启动搞挂
old_name = gui.ICON_FILE
try:
    gui.ICON_FILE = "__no_such_icon__.ico"
    ok(gui._icon_path() is None,
       "图标不存在时返回 None（绝不抛异常）—— 图标是门面，不能反过来挡住启动")
finally:
    gui.ICON_FILE = old_name

print("\n--- 打包配置：exe 图标 + 随包带图标文件 ---")
ok(bool([ln for ln in spec_src.splitlines() if ln.strip().startswith("icon=ICON")]),
   "spec 的 EXE 参数里有 icon=ICON，且**不是被注释掉的**"
   "（只在正文里搜 'icon=ICON' 会被注释里的同一串骗过去）")
ok("ICON = 'src/asase_reader/assets/icon.ico'" in spec_src,
   "ICON 常量指向交付的集成包（exe 图标）—— 指到分立尺寸的话只有一个尺寸可用")
ok("(ICON, 'asase_reader/assets')" in spec_src
   and "(ICON_WIN, 'asase_reader/assets')" in spec_src,
   "两个图标都进了 datas：窗口图标是**运行时按路径读**的，不随包就只能在源码跑法下生效")
ok('"icon": str(icon_file)' in gui_src,
   "launch_gui 真的把 icon 传给了 webview.start")
ok("if icon_file is not None else {}" in gui_src,
   "图标缺失时就不带这个参数（而不是传个不存在的路径让 pywebview 忽略）")
ok("except TypeError" in gui_src,
   "老 pywebview 没有这些参数时还有旁路（不能因为多了个图标参数就整体起不来）")
ok("icon_file=icon_file" in gui_src,
   "launch_gui 把图标交给了静态服务 —— 不交的话 favicon 路由永远 404")

print("\n--- 静态服务：/favicon.ico ---")


def get(base: str, rel: str):
    try:
        with urllib.request.urlopen(base + rel) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


srv = StaticServer(root=tmp, gui_html=ROOT / "src" / "asase_reader" / "assets" / "gui.html",
                   icon_file=WIN)
try:
    base = srv.start()
    code, head, body = get(base, "/favicon.ico")
    ok(code == 200, f"/favicon.ico 返回 200（实际 {code}）")
    ok(head.get("Content-Type") in ("image/x-icon", "image/vnd.microsoft.icon"),
       f"MIME 是图标类型（实际 {head.get('Content-Type')}）")
    # 行为断言验不出「兜底」：本机 mimetypes 恰好认得 .ico，换台机器就不认了。
    # 所以兜底声明本身也要钉住（删掉 _EXTRA_TYPES 里的 .ico 必须红）。
    ok(_EXTRA_TYPES.get(".ico") == "image/x-icon",
       "_EXTRA_TYPES 里兜底了 .ico —— mimetypes 在有的机器上认不出这个后缀，"
       "只在自己机器上验 MIME 是假绿灯")
    ok(body == WIN.read_bytes(), "内容与窗口用的那份图标完全一致")
finally:
    srv.stop()

srv2 = StaticServer(root=tmp,
                    gui_html=ROOT / "src" / "asase_reader" / "assets" / "gui.html")
try:
    base2 = srv2.start()
    code2, _, _ = get(base2, "/favicon.ico")
    ok(code2 == 404,
       f"没配图标时 /favicon.ico 是 404（实际 {code2}）—— 不能因为缺图标就 500/500+ 报错")
    # 零回归：/outputs/ 与根路径不受 favicon 路由影响
    (tmp / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    c_img, h_img, _ = get(base2, "/outputs/a.jpg")
    ok(c_img == 200 and h_img.get("Content-Type", "").startswith("image/"),
       f"/outputs/ 下的图片照旧 200（实际 {c_img}）—— favicon 路由不能抢它的路径")
    c_root, _, _ = get(base2, "/")
    ok(c_root == 200, f"首页照旧 200（实际 {c_root}）")
finally:
    srv2.stop()

print("\n--- 页面声明 favicon ---")
ok('rel="icon"' in html_src, "gui.html 声明了 <link rel=\"icon\">")
ok('href="/favicon.ico"' in html_src, "指向 /favicon.ico（与后端路由对得上）")
_i_link = html_src.find('rel="icon"')
_i_title = html_src.find("<title>")
ok(_i_link > _i_title, "link 在 <title> 之后（head 里）")

print()
if fails:
    print(f"FAILED {len(fails)} 条：")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
