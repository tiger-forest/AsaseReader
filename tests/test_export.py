"""导出功能：目录结构、命名模式、冲突跳过、未入库单章、work.json 合并。

导出是"复制"语义，所以测试要盯住两件事：①目标目录长得对不对；
②书架里的原件一格都没动（数量、内容都不变）。
只在系统临时目录里操作，不动真实的 outputs。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402

from asase_reader.export import (  # noqa: E402
    ExportCancelled,
    NAMING_TITLE,
    chapter_folder_name,
    plan_all,
    plan_chapter,
    plan_work,
    run_plan,
)
from asase_reader.library import Library  # noqa: E402
from asase_reader.store import write_meta  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="asase_export_test_"))
ensure_tmp_env()
assert_tmp_guard(TMP, "TMP")
assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
shelf = TMP / "shelf"
dest = TMP / "out"

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


JPG = b"\xff\xd8\xff" + b"\x00" * 40


def mkchapter(cid, n_images, cover=True, title="", author="某作者"):
    d = shelf / "chapters" / cid
    (d / "images").mkdir(parents=True, exist_ok=True)
    imgs = []
    for i in range(1, n_images + 1):
        name = f"{i:03d}.jpg"
        (d / "images" / name).write_bytes(JPG)
        imgs.append({"index": i, "file": f"images/{name}", "url": None,
                     "width": 800, "height": 1200, "size": len(JPG)})
    if cover:
        (d / "cover.jpg").write_bytes(JPG)
    write_meta(d, {
        "source": "opus", "opus_id": cid.replace("opus_", ""), "title": title or cid,
        "author": {"mid": 1, "name": author},
        "cover": ({"file": "cover.jpg", "url": "u", "width": 800, "height": 600}
                  if cover else None),
        "images": imgs,
    })
    return d


print("== 1. 整部导出的目录结构")
mkchapter("opus_1", 2, cover=True, title="第一话 开端")
mkchapter("opus_2", 1, cover=False, title="第二话 后续")
lib = Library(shelf / "library.json", shelf)
w = lib.create_work("作品甲", "作者甲")
lib.add_chapters(w["id"], ["opus_1", "opus_2"])
wid = w["id"]

items = plan_work(lib, wid, dest)
res = run_plan(items)
work_dir = dest / "作品甲"
ch1 = work_dir / "第01话 第一话 开端"
check(ch1.is_dir(), "章节文件夹：作品甲/第01话 第一话 开端")
check((ch1 / "001.jpg").is_file() and (ch1 / "002.jpg").is_file(), "正文图按顺序落进章节文件夹")
check((ch1 / "00_cover.jpg").is_file(), "章节头图命名成 00_cover.jpg")
check((work_dir / "cover.jpg").is_file(), "作品文件夹里有 cover.jpg")
check((work_dir / "work.json").is_file(), "作品文件夹里有 work.json")
check(not (work_dir / "第02话 第二话 后续" / "00_cover.jpg").exists(),
      "没有头图的章节不会凭空造一个封面")
check(res["copied"] == len(items), f"清单全部执行（{res['copied']}/{len(items)}）")

print("== 2. work.json 内容")
data = json.loads((work_dir / "work.json").read_text(encoding="utf-8"))
check(data.get("format") == "asase-work/1", "带格式标记 asase-work/1")
check(data["work"] == {"name": "作品甲", "author": "作者甲"}, "作品名与作者写进去了")
check(len(data["chapters"]) == 2, "两章都记录了")
c1 = data["chapters"][0]
check(c1["index"] == 1 and c1["title"] == "第一话 开端", "章节序号与标题")
check(c1["images"] == ["001.jpg", "002.jpg"], "图片文件名列表")
check(c1["cover"] == "00_cover.jpg", "封面字段名")
check(c1["opus_id"] == "1", "opus_id 保留（将来能导回书架）")

print("== 3. 命名模式：只用章节名")
dest2 = TMP / "out_title"
items = plan_work(lib, wid, dest2, NAMING_TITLE)
run_plan(items)
check((dest2 / "作品甲" / "第一话 开端").is_dir(), "title 模式下文件夹就是章节名")
check(not (dest2 / "作品甲" / "第01话 第一话 开端").exists(), "title 模式下不加序号前缀")

print("== 4. 重复导出：已存在的跳过，不覆盖")
before = (work_dir / "第01话 第一话 开端" / "001.jpg").read_bytes()
items = plan_work(lib, wid, dest)
res2 = run_plan(items)
# 清单 = 头图 + 3 张正文图 + 作品封面 + work.json；第二次只有 work.json 会被重写
check(res2["copied"] == 1, "第二次只有 work.json 被重写（图片全部跳过）")
check(res2["skipped"] == 5, f"5 个已存在的图片被跳过（实际 {res2['skipped']}）")
check((work_dir / "第01话 第一话 开端" / "001.jpg").read_bytes() == before, "原文件没被覆盖")

print("== 5. 单章导出：已入库带作品层，未入库是顶层文件夹")
mkchapter("opus_3", 1, cover=False, title="散落的一话")
lib2 = Library(shelf / "library.json", shelf)
dest3 = TMP / "out_one"
items = plan_chapter(lib2, "opus_1", dest3)
run_plan(items)
one = json.loads((dest3 / "作品甲" / "work.json").read_text(encoding="utf-8"))
check(len(one["chapters"]) == 1 and one["chapters"][0]["index"] == 1,
      "单章导出也写 work.json，只含这一章")
check((dest3 / "作品甲" / "第01话 第一话 开端" / "001.jpg").is_file(), "单章带作品层")

dest4 = TMP / "out_orphan"
items = plan_chapter(lib2, "opus_3", dest4)
run_plan(items)
check((dest4 / "散落的一话" / "001.jpg").is_file(), "未入库章节导出成顶层「章节标题/」")
check(not (dest4 / "work.json").exists(), "未入库章节不写 work.json")

print("== 6. 分批导出会合并进同一个作品文件夹（work.json 累加）")
dest5 = TMP / "out_merge"
run_plan(plan_chapter(lib2, "opus_1", dest5))
run_plan(plan_chapter(lib2, "opus_2", dest5))
merged = json.loads((dest5 / "作品甲" / "work.json").read_text(encoding="utf-8"))
check(len(merged["chapters"]) == 2, "两次单章导出后 work.json 里两章都在")
check([c["index"] for c in merged["chapters"]] == [1, 2], "合并后按序号排序")

print("== 7. 整个书库：作品各一个文件夹 + 未入库章节各自一个")
dest6 = TMP / "out_all"
items = plan_all(lib2, dest6)
run_plan(items)
check((dest6 / "作品甲" / "work.json").is_file(), "作品导出成文件夹")
check((dest6 / "散落的一话" / "001.jpg").is_file(), "未入库章节一并导出")

print("== 8. 取消：中途停下不再继续")
dest7 = TMP / "out_cancel"
items = plan_all(lib2, dest7)
canceled = False
try:
    run_plan(items, should_cancel=lambda: True)
except ExportCancelled:
    canceled = True
check(canceled, "取消时抛 ExportCancelled")
check(not any(dest7.rglob("*.jpg")), "取消后一个文件都没落地")

print("== 9. 书架原件完好（导出 = 复制）")
src1 = shelf / "chapters" / "opus_1" / "images" / "001.jpg"
check(src1.is_file() and src1.read_bytes() == JPG, "书架里的原件还在且内容不变")
check(len(list(shelf.glob("chapters/*"))) == 3, "书架还是 3 个章节目录")

print("== 10. 章节文件夹命名的边界")
check(chapter_folder_name(1, "第一话 开端") == "第01话 第一话 开端", "序号补零 + 章节名")
check(chapter_folder_name(1, "第1话 开端") == "第1话 开端", "标题自带「第N话」时不重复加前缀")
check(chapter_folder_name(3, "") == "第03话", "没有标题时只剩序号")
check(chapter_folder_name(2, "a/b:c") == "第02话 a_b_c", "非法字符被替换（不会写出坏路径）")
check(chapter_folder_name(1, "标题", NAMING_TITLE) == "标题", "title 模式只用章节名")

print("== 11. 导出目录不能放进书架（会被认成未完成的下载残留）")
from asase_reader.backend import Backend  # noqa: E402

state = TMP / "state"
state.mkdir(exist_ok=True)
b = Backend(state_dir=state)
b.config["shelf_dir"] = str(shelf)
b.config["download_dir"] = ""
b.config["setup_done"] = True
assert_tmp_guard(b.shelf_root(), "shelf_root")
r = b.export_start("all", "", str(shelf / "导出"), "numbered")
check(r.get("ok") is False and "书架目录里面" in (r.get("error") or ""),
      f"导进书架被拒绝：{r.get('error')}")
r = b.export_start("all", "", str(TMP / "legal_out"), "numbered")
check(r.get("ok") is True, "书架外的目录可以导出")
import time  # noqa: E402

for _ in range(100):
    if not b.export_status()["running"]:
        break
    time.sleep(0.05)
st = b.export_status()
check(st["finished"] and st["copied"] > 0, f"后台线程跑完了（copied={st['copied']}）")
check((TMP / "legal_out" / "作品甲").is_dir(), "后台导出真的写出了目录")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\nFAILED: {len(fails)}")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
