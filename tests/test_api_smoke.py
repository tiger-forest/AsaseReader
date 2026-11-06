"""对 GuiApi 的每个公开方法做一次真实调用，抓重构后残留的属性名/签名错误。

原则：只调"不需要网络、不需要交互"的方法；需要窗口的（choose_file/choose_folder）
跳过。用临时书架目录，绝不碰真实 outputs。
"""

import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader.gui import GuiApi  # noqa: E402
from asase_reader.store import write_meta  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="asase_smoke_"))
ensure_tmp_env()
assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
assert_tmp_guard(TMP, "TMP")
shelf = TMP / "shelf"
state = TMP / "state"
state.mkdir()
shelf.mkdir()

# 造一个可用的章节
cid = "opus_smoke1"
d = shelf / "chapters" / cid
(d / "images").mkdir(parents=True)
(d / "images" / "001.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 20)
(d / "images" / "002.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 20)
write_meta(d, {
    "source": "opus", "opus_id": "smoke1", "title": "冒烟篇",
    "author": {"mid": 1, "name": "某作者"},
    "cover": {"file": "cover.jpg", "url": "u0", "width": 800, "height": 600},
    "images": [
        {"index": 1, "file": "images/001.jpg", "url": "u1", "width": 800, "height": 1200},
        {"index": 2, "file": "images/002.jpg", "url": "u2", "width": 800, "height": 1200},
    ],
})
(d / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)

from asase_reader.backend import Backend  # noqa: E402

b = Backend(state_dir=state)
b.config["shelf_dir"] = str(shelf)
b.config["download_dir"] = ""
b.config["setup_done"] = True
assert_tmp_guard(b.shelf_root(), "shelf_root")
api = GuiApi(b)  # _window 是只读 property，为 None 时自动走 tkinter 兜底

# 需要真实存在的 wid / chapter_id
w = api.create_work("冒烟作品", "某作者")
wid = w["id"]
api.add_chapters(wid, [cid])

ARGS = {
    "get_state": (),
    "remove_job": ("123",),
    "retry_job": ("123",),
    "clear_done": (),
    "pause": (),
    "resume": (),
    "cancel_all": (),
    "update_config": ({"concurrency": 2},),
    # 首启模态 / 设置页的目录预检（2.0.0）：跳过探针、不落盘
    "preview_dir": (str(TMP / "preview_target"),),
    # 界面设置的后端副本（v1.29.0）：写到临时 state 目录，不碰真实数据
    "get_ui_settings": (),
    "save_ui_settings": ({"uiTheme": "apple"},),
    "library_state": (),
    "list_works": (),
    "work_detail": (wid,),
    "mark_read": (wid,),
    "chapter_images": (cid,),
    "create_work": ("新建作品", "作者"),
    "update_work": (wid, {"name": "改名"}),
    "add_chapters": (wid, [cid]),
    "move_chapter": (wid, cid, -1),
    "update_chapter": (wid, cid, {"note": "x"}),
    "rename_chapter": (cid, "新标题", "新作者"),
    "set_work_cover_file": (wid, str(d / "cover.jpg")),
    "set_chapter_cover_file": (wid, cid, str(d / "cover.jpg")),
    # 从画面数据存封面（视频章节截取当前帧）：内容是什么字节无所谓，这里只验接口通
    "save_chapter_cover_data": (wid, cid, "data:image/jpeg;base64,AAEC"),
    "clear_work_cover": (wid,),
    "clear_chapter_cover": (wid, cid),
    "begin_edit": (wid,),
    "edit_dirty": (),
    "cancel_edit": (),
    "commit_edit": (),
    "import_files": ("导入测试", "某作者", [str(d / "images" / "001.jpg")]),
    "import_folder": ("文件夹导入", "某作者", str(d / "images")),
    "import_pending": ([],),
    "list_incomplete": (),
    "purge_incomplete": ([],),
    # 导出：写到临时目录，不动真实数据（后台线程跑，跑完再清理临时目录）
    "export_start": ("work", wid, str(TMP / "export_out"), "numbered"),
    "export_status": (),
    "export_cancel": (),
    # 登录凭据：整串都是假的，且只进内存不落盘。
    # add_paid_up 必须在 remove_paid_up 之前（按字母序跑，天然满足）
    "submit_cookie": ("SESSDATA=fake_for_smoke; bili_jct=fake; DedeUserID=1",),
    "clear_cookie": (),
    "add_paid_up": ("123456", "测试UP"),
    "remove_paid_up": ("123456",),
    # 视频转图集：都走"参数不对就直接返回 ok:False"的分支，不真跑 ffmpeg
    "video_import_start": ([str(d / "cover.jpg")],),   # 不是视频后缀 → 被过滤掉
    "video_import_confirm": (),                        # 当前没有待确认的转换
    "video_import_status": (),
    "video_import_cancel": (),
    "video_tool_state": (),
    # 无 arguments；测试环境 backend.window 是 None，应安全返回 ok:False 而不是抛异常
    "toggle_fullscreen": (),
    # 无边框窗口自绘控制栏（v1.22）：测试环境没有窗口，都要安全返回而不是抛异常
    "minimize_window": (),
    "maximize_window": (True,),
    "close_window": (),
    "is_maximized": (),
    "set_taskbar_mode": (True,),
    "restore_at": (100, 100),
    "move_window": (100, 100),
    "set_window_rect": (100, 100, 900, 700),
}

# 会破坏后续用例引用的对象，单独放到最后跑。
# delete_chapter_images 排最前：它删的是 images/002.jpg，而别的用例要用 001.jpg。
DESTRUCTIVE = {
    "delete_chapter_images": (cid, ["images/002.jpg"]),
    "remove_chapter": (wid, cid),
    "delete_chapter": (cid,),
    "delete_work": (wid,),
}

fails = []
members = dict(inspect.getmembers(api, predicate=inspect.ismethod))
order = sorted(n for n in members if not n.startswith("_"))


def run(name: str) -> None:
    fn = members[name]
    args = ARGS.get(name, DESTRUCTIVE.get(name))
    if args is None:
        print(f"  ??   {name} 无预设参数")
        fails.append(f"{name}: 未覆盖")
        return
    try:
        fn(*args)
        print(f"  ok   {name}")
    except Exception as e:
        tb = traceback.format_exc().strip().splitlines()[-1]
        print(f"  FAIL {name}: {type(e).__name__}: {e}")
        print(f"       {tb}")
        fails.append(f"{name}: {type(e).__name__}: {e}")


for name in order:
    if name in ("choose_file", "choose_folder", "choose_files", "read_url_file",
                "clear_finished", "add_url", "add_urls", "open_dir", "open_url"):
        print(f"  skip {name}（需要窗口 / 会发起真实网络请求 / 会打开外部程序）")
        continue
    if name in DESTRUCTIVE:
        continue
    run(name)

print("  -- 破坏性操作（最后跑） --")
for name in DESTRUCTIVE:
    run(name)

# 等导出线程落地再清理，否则 rmtree 会撞上还在写的文件
for _ in range(100):
    if not api.export_status()["running"]:
        break
    time.sleep(0.05)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nFAILED: {len(fails)}")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
