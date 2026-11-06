"""章节内删除图片（v1.16）。

删的是**真文件**、还会把剩下的重新编号成连续编号，所以这个测试盯的是三件事：

1. 磁盘与 meta 必须始终对得上（删掉的没了、剩下的名字真的是 001、002……）；
2. 校验必须在动磁盘之前做完 —— 删光 / 不属于本章 / 路径越界三种非法请求，
   一个文件都不能少，更不能留下一半删完的章节；
3. 删除后立刻读（缓存、封面、列表张数）必须已经是新状态。

只在系统临时目录里操作。**必须用 tempfile.mkdtemp()**，理由见 test_library.py 的开头。
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asase_reader.library import Library  # noqa: E402
from asase_reader.store import write_meta  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="asase_chimg_test_"))
SHELF = TMP / "shelf"
SHELF.mkdir(parents=True)

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


JPG = b"\xff\xd8\xff" + b"\x00" * 24
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


CUR_SHELF = SHELF   # fresh() 会换掉它：每个用例用自己的书架（换目录 = 换书库）


def mk_chapter(cid, names, cover=None, shelf=None):
    """造一个章节：names 是 images/ 下的文件名，cover 传 None 就是没有头图。"""
    d = (shelf or CUR_SHELF) / "chapters" / cid
    (d / "images").mkdir(parents=True)
    for n in names:
        (d / "images" / n).write_bytes(PNG if n.endswith(".png") else JPG)
    meta = {
        "source": "local",
        "opus_id": None,
        "title": cid,
        "author": {"mid": None, "name": "某作者"},
        "cover": ({"file": cover, "url": None, "width": 800, "height": 600}
                  if cover else None),
        "images": [
            {"index": i + 1, "file": f"images/{n}", "url": None,
             "width": 800, "height": 600, "size": 27}
            for i, n in enumerate(names)
        ],
    }
    write_meta(d, meta)
    return d


def fresh():
    """开一个全新的 Library（换目录 = 换书库，所以每个用例用自己的 shelf）。"""
    global CUR_SHELF
    root = Path(tempfile.mkdtemp(prefix="asase_chimg_case_"))
    shelf = root / "shelf"
    shelf.mkdir(parents=True)
    CUR_SHELF = shelf
    return Library(shelf / "library.json", shelf), shelf


def files_of(d):
    return sorted(p.name for p in (d / "images").iterdir())


def do_delete(lib, cid, files):
    """包一层：抛异常也记成一条失败，而不是让整个文件崩掉。

    不这么做的话，「改坏了的实现」会把测试进程直接带崩 —— 退出码虽然非 0，
    但一条 FAIL 断言都没有（testing.md 雷 #2：崩溃不是抓到），而且后面的用例
    会被连坐、什么都看不出来。
    """
    try:
        return lib.delete_chapter_images(cid, list(files)), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def meta_of(d):
    return json.loads((d / "meta.json").read_text(encoding="utf-8"))


print("== 1. 删中间的一张：剩下的重新编号，顺序不变")
lib, shelf = fresh()
d = mk_chapter("c1", ["001.jpg", "002.jpg", "003.jpg"])
lib.register_chapter("c1", "local")
before = [p.read_bytes() for p in sorted((d / "images").iterdir())]
r, err = do_delete(lib, "c1", ["images/002.jpg"])
check(err is None, f"删除没有抛异常：{err}")
check(bool(r) and r["removed"] == 1 and r["remaining"] == 2, f"返回张数：{r}")
check(files_of(d) == ["001.jpg", "002.jpg"], f"磁盘上剩 001/002 连续：{files_of(d)}")
m = meta_of(d)
check([i["file"] for i in m["images"]] == ["images/001.jpg", "images/002.jpg"],
      f"meta 里的 file 跟着改了：{[i['file'] for i in m['images']]}")
check([i["index"] for i in m["images"]] == [1, 2], "meta 里的 index 也重新排了")
# 内容必须是「原来第 1 张 + 原来第 3 张」，不能是「第 1 张 + 第 2 张」被覆盖
after = [p.read_bytes() for p in sorted((d / "images").iterdir())]
check(after == [before[0], before[2]], "剩下的内容就是原来第 1、3 张（没被互相覆盖）")

print("== 2. 删第一张：整体前移")
lib, shelf = fresh()
d = mk_chapter("c2", ["001.jpg", "002.jpg", "003.jpg", "004.jpg"])
lib.register_chapter("c2", "local")
keep = [p.read_bytes() for p in sorted((d / "images").iterdir())][1:]
r, err = do_delete(lib, "c2", ["images/001.jpg"])
check(err is None, f"删除没有抛异常：{err}")
check(files_of(d) == ["001.jpg", "002.jpg", "003.jpg"], f"编号前移：{files_of(d)}")
check([p.read_bytes() for p in sorted((d / "images").iterdir())] == keep,
      "前移后内容顺序不变")

print("== 3. 扩展名各自保留（jpg / png 混排）")
lib, shelf = fresh()
d = mk_chapter("c3", ["001.jpg", "002.png", "003.jpg"])
lib.register_chapter("c3", "local")
r, err = do_delete(lib, "c3", ["images/002.png"])
check(err is None, f"删除没有抛异常：{err}")
check(files_of(d) == ["001.jpg", "002.jpg"], f"剩下的按自己的扩展名编号：{files_of(d)}")

print("== 4. 一次删多张（不连续）")
lib, shelf = fresh()
d = mk_chapter("c4", [f"{i:03d}.jpg" for i in range(1, 6)])
lib.register_chapter("c4", "local")
r, err = do_delete(lib, "c4", ["images/002.jpg", "images/004.jpg"])
check(err is None, f"删除没有抛异常：{err}")
check(bool(r) and r["removed"] == 2 and r["remaining"] == 3, f"一次删 2 张：{r}")
check(files_of(d) == ["001.jpg", "002.jpg", "003.jpg"], f"剩 3 张连续：{files_of(d)}")

print("== 5. 删光要被拒，而且一个文件都不能少")
lib, shelf = fresh()
d = mk_chapter("c5", ["001.jpg", "002.jpg"])
lib.register_chapter("c5", "local")
r, err = do_delete(lib, "c5", ["images/001.jpg", "images/002.jpg"])
check(r is None and "至少" in (err or ""), f"删光被拒：{err}")
check(files_of(d) == ["001.jpg", "002.jpg"], "被拒后文件一个没少（校验在动磁盘之前）")

print("== 6. 不属于本章的 file —— 一个都不删")
lib, shelf = fresh()
d = mk_chapter("c6", ["001.jpg", "002.jpg"])
other = mk_chapter("c6b", ["001.jpg"])
lib.register_chapter("c6", "local")
lib.register_chapter("c6b", "local")
for bad, label in [("images/999.jpg", "不存在的编号"),
                   ("images/../c6b/images/001.jpg", "别的章节的图片")]:
    r, err = do_delete(lib, "c6", [bad])
    check(r is None and err is not None, f"{label} 被拒：{err}")
check(files_of(d) == ["001.jpg", "002.jpg"], "非法请求后本章文件一个没少")
check(files_of(other) == ["001.jpg"], "别的章节也没被牵连")

print("== 6b. meta 被改脏、file 越出章节目录 —— 纵深防御真的会拦")
lib, shelf = fresh()
d = mk_chapter("c6c", ["001.jpg", "002.jpg"])
lib.register_chapter("c6c", "local")
# 手改 meta 模拟脏数据：这条 file 在 meta 里（所以能通过"属于本章"那道检查），
# 但解析后会跑到章节目录外面去。**这道校验只有这种数据才走得到**，
# 所以必须专门造一个用例，否则它就是一段没人测过的死代码。
m = meta_of(d)
m["images"].append({"index": 3, "file": "images/../../outside.jpg", "url": None})
(d / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
lib._meta_cache.clear()
r, err = do_delete(lib, "c6c", ["images/../../outside.jpg"])
check(r is None and err is not None, f"越界的 file 被拦下：{err}")
check(files_of(d) == ["001.jpg", "002.jpg"], "越界请求没动本章任何文件")
check(not (shelf / "chapters" / "outside.jpg").exists(), "章节目录外面也没被碰到")

print("== 7. 空请求")
lib, shelf = fresh()
d = mk_chapter("c7", ["001.jpg"])
lib.register_chapter("c7", "local")
r, err = do_delete(lib, "c7", [])
check(r is None and err is not None, f"空列表被拒：{err}")

print("== 8. 删完立刻读：缓存失效、列表张数、封面回退")
lib, shelf = fresh()
d = mk_chapter("c8", ["001.jpg", "002.jpg", "003.jpg"])   # 没有头图 → 封面取正文第一张
lib.register_chapter("c8", "local")
check(lib.chapter_cover_url(lib.get_chapter("c8")) == "/outputs/chapters/c8/images/001.jpg",
      "删之前封面 = 正文第一张")
ims = lib.chapter_images("c8")
check(len(ims) == 3 and ims[0]["file"] == "images/001.jpg", "chapter_images 带 file 字段")
r, err = do_delete(lib, "c8", ["images/001.jpg"])
check(err is None, f"删除没有抛异常：{err}")
ims = lib.chapter_images("c8")
check(len(ims) == 2 and ims[0]["file"] == "images/001.jpg",
      f"删完立刻读就是新状态（缓存已失效）：{[i['file'] for i in ims]}")
scan = [c for c in lib.scan_chapters() if c["id"] == "c8"][0]
check(scan["ok_count"] == 2 and scan["image_count"] == 2, f"列表张数同步：{scan['ok_count']}")
check(lib.chapter_cover_url(lib.get_chapter("c8")) == "/outputs/chapters/c8/images/001.jpg",
      "封面自动落到新的第一张")

print("== 9. 磁盘上少了文件也不能崩（meta 说有、实际没有）")
lib, shelf = fresh()
d = mk_chapter("c9", ["001.jpg", "002.jpg", "003.jpg"])
lib.register_chapter("c9", "local")
(d / "images" / "002.jpg").unlink()
r, err = do_delete(lib, "c9", ["images/001.jpg"])
check(err is None, f"缺文件时不该抛异常：{err}")
check(bool(r) and r["remaining"] == 2, f"缺文件也能完成：{r}")
check([i["file"] for i in meta_of(d)["images"]] == ["images/001.jpg", "images/002.jpg"],
      "meta 仍按剩下的张数重排")

print("== 10. 头图（cover.jpg）不受影响")
lib, shelf = fresh()
d = mk_chapter("c10", ["001.jpg", "002.jpg"], cover="cover.jpg")
(d / "cover.jpg").write_bytes(JPG)
lib.register_chapter("c10", "local")
r, err = do_delete(lib, "c10", ["images/001.jpg"])
check(err is None, f"删除没有抛异常：{err}")
check((d / "cover.jpg").is_file(), "头图还在（它不在 images/ 里，不参与重排）")
check(meta_of(d)["cover"]["file"] == "cover.jpg", "meta 里的头图指向没被改")

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    print(f"FAILED: {len(fails)}")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("OK 章节内删除图片全部通过")
sys.exit(0)
