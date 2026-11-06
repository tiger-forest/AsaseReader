"""路径处理 / 静态服务 / 封面 / URL 解析的无头验证。

覆盖的都是「自动化容易漏、但错了很致命」的地方：
- 书架目录可以是任意绝对路径（含空格、中文、反斜杠），且能持久化；
- 切换书架目录后图片服务与作品库要跟着走；
- 静态服务的路径穿越防护必须一直有效（这是本地服务，被穿透等于任意读盘）；
- 自定义封面从任意位置复制进 _covers，非法文件要报错而不是静默失败；
- read_url_file 的编码兜底、URL 提取与去重。

跑法（也由 tests/selftest.py 统一调用）：
    python tests/test_paths_and_server.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import os  # noqa: E402
from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
import asase_reader.library as libmod  # noqa: E402
from asase_reader.backend import PROJECT_ROOT, Backend, default_shelf_dir  # noqa: E402
from asase_reader.gui import FILE_TYPES, GuiApi, _tk_filetypes  # noqa: E402
from asase_reader.staticserver import StaticServer  # noqa: E402
from webview.util import parse_file_type  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> None:
    ensure_tmp_env()
    assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
    tmp = Path(tempfile.mkdtemp(prefix="asase_paths_"))
    assert_tmp_guard(tmp, "tmp")
    cfg_path = tmp / "config.json"
    b = Backend(state_dir=tmp / "state", config_path=cfg_path)

    print("== 1. 默认书架目录 = 「文档」Known Folder 下的默认书库（只读回退）")
    check(b.shelf_root() == default_shelf_dir(), f"默认 = {b.shelf_root()}")
    check(Path(b.shelf_dir_display()).is_absolute(), "对外展示的是绝对路径")

    print("== 2. 任意绝对路径（空格 / 中文 / 反斜杠）")
    far = tmp / "我的 漫画库" / "Comics"
    b.update_config({"shelf_dir": str(far), "setup_done": True})
    assert_tmp_guard(far, "far")
    check(b.shelf_root() == far, f"含空格与中文的路径可用：{b.shelf_root()}")
    check(far.is_dir(), "换目录时自动创建")
    saved = Backend(state_dir=tmp / "state", config_path=cfg_path)
    check(saved.shelf_root() == far, "重启后仍记住（已持久化）")

    far2 = tmp / "D风格"
    b.update_config({"shelf_dir": str(far2).replace("/", "\\")})
    check(b.shelf_root() == far2, "反斜杠路径也能正确解析")

    print("== 3. 静态服务随书架目录切换 + 穿越防护")
    old_dir = tmp / "旧库"
    old_dir.mkdir()
    (old_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\x00OLD")
    far.mkdir(parents=True, exist_ok=True)
    ch_dir = far / "chapters" / "opus_123"
    (ch_dir / "images").mkdir(parents=True)
    (ch_dir / "meta.json").write_text(
        '{"source":"opus","opus_id":"123","title":"第一章","author":{"name":"某作者"},'
        '"cover":{"file":"cover.jpg"},"images":[{"file":"images/001.jpg","width":800,'
        '"height":1200},{"file":"images/002.jpg","width":800,"height":1200}]}',
        encoding="utf-8",
    )
    (ch_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\x00NEWCOVER")
    (ch_dir / "images" / "001.jpg").write_bytes(b"\xff\xd8\xff\x00IMG1")
    (ch_dir / "images" / "002.jpg").write_bytes(b"\xff\xd8\xff\x00IMG2")

    gui_html = ROOT / "src" / "asase_reader" / "assets" / "gui.html"
    server = StaticServer(root=old_dir, gui_html=gui_html)
    base = server.start()
    b.static_server = server
    try:
        check(urllib.request.urlopen(base + "/outputs/cover.jpg").read()
              == b"\xff\xd8\xff\x00OLD", "切换前服务指向旧目录")
        b.update_config({"shelf_dir": str(far)})
        check(server.root == far, "切换书架目录后服务 root 跟着变")
        got = urllib.request.urlopen(
            base + quote("/outputs/chapters/opus_123/images/001.jpg")).read()
        check(got == b"\xff\xd8\xff\x00IMG1", "能取到新目录下的图片")

        for bad in ("/outputs/..%2Fpyproject.toml", "/outputs/%2e%2e/pyproject.toml"):
            blocked = False
            try:
                urllib.request.urlopen(base + bad)
            except urllib.error.HTTPError as e:
                blocked = e.code == 404
            except Exception:
                blocked = True
            check(blocked, f"路径穿越被拦下：{bad}")
    finally:
        server.stop()

    print("== 4. 作品库随目录重指向 + 扫描")
    check(b.library.shelf_root == far, "library 指向新书架")
    check(b.library.path == far / "library.json", "library.json 位置正确")
    st = b.library_state()
    check(any(c["id"] == "opus_123" for c in st["orphans"]), "扫到未入库的 opus_123")
    w = b.create_work("测试作品", "某作者")
    b.add_chapters(w["id"], ["opus_123"])
    check((far / "library.json").exists(), "写入 library.json")
    check(len(b.chapter_images("opus_123")["images"]) == 2, "阅读器能取到 2 张图")

    print("== 5. 自定义封面（源文件在任意位置）")
    src_img = tmp / "外部封面.png"
    src_img.write_bytes(b"\x89PNG\r\n\x1a\n")
    r = b.set_work_cover_file(w["id"], str(src_img))
    check(r["cover_url"].startswith("/outputs/_covers/work_"), f"封面 URL：{r.get('cover_url')}")
    cov_rel = b.library.get_work(w["id"])["cover"]
    check((far / cov_rel).is_file(), "封面文件已复制进书架")
    s2 = StaticServer(root=far, gui_html=gui_html)
    b2 = s2.start()
    try:
        check(urllib.request.urlopen(b2 + r["cover_url"]).read()[:4] == b"\x89PNG",
              "封面可通过本地服务访问")
    finally:
        s2.stop()

    r2 = b.set_chapter_cover_file(w["id"], "opus_123", str(src_img))
    det = b.work_detail(w["id"])
    check(det["chapters"][0]["cover_url"].startswith("/outputs/_covers/ch_"),
          f"章节封面：{det['chapters'][0].get('cover_url')}")

    (tmp / "bad.txt").write_text("x")
    for bad_src in (str(tmp / "不存在.jpg"), str(tmp / "bad.txt")):
        rejected = False
        try:
            b.set_work_cover_file(w["id"], bad_src)
        except libmod.LibraryError:
            rejected = True
        check(rejected, f"拒绝非法封面：{bad_src}")

    print("== 5b. 封面改完要能**立刻**看到（URL 版本号 + 禁强缓存）")
    # 从章节内容里选封面：前端手里只有「相对书架目录」的路径
    rel_ok = True
    try:
        r3 = b.set_chapter_cover_file(w["id"], "opus_123", "chapters/opus_123/images/002.jpg")
    except Exception as e:                       # 不接受相对路径 = 这条路整个断了
        rel_ok = False
        r3 = {"cover_url": ""}
        print(f"       {type(e).__name__}: {e}")
    check(rel_ok, f"相对路径也能设封面：{r3.get('cover_url')}")
    check("?v=" in r3["cover_url"], f"封面 URL 带版本号：{r3.get('cover_url')}")
    rel3 = b.library.get_chapter("opus_123")["cover"]
    check(rel3.startswith("_covers/"), f"索引记的是 _covers 下的相对路径：{rel3}")
    time.sleep(0.02)      # 版本号取到毫秒，隔开才验得出「覆盖后 URL 变了」
    (far / rel3).write_bytes(b"\xff\xd8\xff\x00CHANGED")
    check(b.library.chapter_cover_url(b.library.get_chapter("opus_123")) != r3["cover_url"],
          "覆盖封面后 URL 变了（URL 一变，缓存自然失效）")
    rejected = False
    try:
        b.set_chapter_cover_file(w["id"], "opus_123", "../库外/evil.jpg")
    except libmod.LibraryError:
        rejected = True
    check(rejected, "拒绝逃出书架目录的相对路径（这是本地服务，穿透等于任意读盘）")

    print("== 5c. 从画面数据存封面（视频章节截当前帧）")
    u4 = b.save_chapter_cover_data(w["id"], "opus_123", "data:image/jpeg;base64,AAEC")["cover_url"]
    check(u4.startswith("/outputs/_covers/ch_") and "?v=" in u4, f"画面数据存成封面：{u4}")
    check((far / "_covers/ch_opus_123.jpg").read_bytes() == b"\x00\x01\x02",
          "字节原样落盘（前端给什么就存什么，不经转码）")
    for bad in ("", "data:image/jpeg;base64,@@@@"):
        rejected = False
        try:
            b.save_chapter_cover_data(w["id"], "opus_123", bad)
        except libmod.LibraryError:
            rejected = True
        check(rejected, f"拒绝非法的画面数据：{bad!r}")

    s3 = StaticServer(root=far, gui_html=gui_html)
    b3 = s3.start()
    try:
        cov_path = "/outputs/" + b.library.get_chapter("opus_123")["cover"]
        cc = (urllib.request.urlopen(b3 + quote(cov_path)).headers.get("Cache-Control") or "").lower()
        check("no-store" in cc, f"封面禁用了强缓存（Cache-Control = {cc!r}）")
        cc2 = (urllib.request.urlopen(
            b3 + quote("/outputs/chapters/opus_123/images/001.jpg")
        ).headers.get("Cache-Control") or "").lower()
        check("max-age=3600" in cc2, f"正文图片缓存照旧（Cache-Control = {cc2!r}）")
    finally:
        s3.stop()

    print("== 6. read_url_file（utf-8 / gbk / 去重）")
    t1 = tmp / "urls.txt"
    t1.write_text("https://www.bilibili.com/opus/111\n# 注释\nopus/222\n", encoding="utf-8")
    check("opus/111" in b.read_url_file(str(t1)), "utf-8 可读且忽略注释")
    t2 = tmp / "urls_gbk.txt"
    t2.write_bytes("https://www.bilibili.com/opus/333\n".encode("gbk"))
    check("opus/333" in b.read_url_file(str(t2)), "gbk 兜底可读")
    check(b.read_url_file(str(tmp / "nope.txt")) == "", "不存在的文件返回空串")

    AID, BID = "1261150454223470609", "1261150454223470610"
    t3 = tmp / "urls_ok.txt"
    t3.write_text(
        f"https://www.bilibili.com/opus/{AID}\n# 注释行\nopus/{BID}\n"
        f"https://www.bilibili.com/opus/{AID}\n",
        encoding="utf-8",
    )
    added = b.add_urls(b.read_url_file(str(t3)))
    check(added == [AID, BID], f"解析出 ID 且去重（实际 {added}）")

    print("== 7. 文件对话框参数与返回值包装")
    for _kind, specs in FILE_TYPES.items():
        for s in specs:
            parse_file_type(s)
    check(True, "所有文件类型过滤串都能被 pywebview 解析")
    check(_tk_filetypes(FILE_TYPES["image"])
          == [("图片文件", "*.jpg *.jpeg *.png *.webp *.gif *.bmp")], "tk 兜底格式串正确")

    class FakeWindow:
        def __init__(self, result):
            self.result = result
            self.calls = []

        def create_file_dialog(self, dialog_type, directory, allow_multiple,
                               save_filename, file_types):
            self.calls.append((int(dialog_type), allow_multiple, file_types))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    api = GuiApi(b)
    api._b.window = FakeWindow(("C:/x/pic.png",))
    check(api.choose_file("image") == {"path": "C:/x/pic.png"}, "选文件返回路径")
    check(api._b.window.calls[0][0] == 10, "用的是 FileDialog.OPEN")
    api._b.window = FakeWindow(None)
    check(api.choose_file("image") == {"path": ""}, "取消返回空串")
    api._b.window = FakeWindow(("D:/Comics",))
    check(api.choose_folder() == {"path": "D:/Comics"}, "选目录返回路径")
    check(api._b.window.calls[0][0] == 20, "用的是 FileDialog.FOLDER")
    api._b.window = FakeWindow(("a.png", "b.png"))
    check(api.choose_files("image") == {"paths": ["a.png", "b.png"]}, "多选返回数组")
    check(api._b.window.calls[0][1] is True, "多选时 allow_multiple=True")
    api._b.window = FakeWindow(RuntimeError("boom"))
    check(api.choose_file("image") == {"path": ""}, "异常时安全降级为空串")

    print("== 8. get_state 带出实际目录")
    stt = b.get_state()
    check(stt["shelf_dir"] == str(far), f"shelf_dir = {stt.get('shelf_dir')}")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("FAILED:", len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
