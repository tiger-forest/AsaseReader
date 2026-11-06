"""重构回归测试：v1→v2 迁移、本地导入、暂存目录导入、残留清理。

只在系统临时目录里操作，不动真实的 outputs。

注意：**必须用 tempfile.mkdtemp() 而不是 tests/ 下的固定目录。**
以前写死 `tests/tmp_shelf`，一旦某轮测试中途崩溃留下残留，
后续每一次运行都会读到脏数据而失败 —— 表现是"什么改动都被这项测试抓到"的
假阳性，非常误导。固定目录还会累积到几十个文件后难以清理。
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
from asase_reader.download import opus_is_complete  # noqa: E402
from asase_reader.library import Library, LibraryError, opus_chapter_id  # noqa: E402
from asase_reader.store import natural_key, sniff_image_size  # noqa: E402

# 每次运行一个全新的临时目录：上一轮的残留绝不能影响这一轮
TMP = Path(tempfile.mkdtemp(prefix="asase_library_test_"))
ensure_tmp_env()
assert_tmp_guard(TMP, "TMP")
assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


OPUS = "1261150454223470609"


V1_FIXTURE = ROOT / "tests" / "fixtures" / "shelf_v1"


def reset():
    """从 tests/fixtures/shelf_v1 拷一份 v1 书架来跑迁移。

    夹具是**真实的旧版数据**（旧布局目录 + v1 的 library.json，图片已压缩到几十字节
    以便入库），不依赖当前 outputs 的状态——它自己早就是 v2 了，所以测试随时可重跑。
    """
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    shelf = TMP / "shelf"
    shutil.copytree(V1_FIXTURE, shelf)
    return shelf


print("== 1. v1 -> v2 迁移（就地认领 + 目录规范化）")
shelf = reset()
old_dir = shelf / "长夜月的长夜" / f"{OPUS}_测试用专栏标题"
check(old_dir.is_dir(), "迁移前旧布局目录存在")
lib = Library(shelf / "library.json", shelf)
cid = opus_chapter_id("1261150454223470609")
check(not old_dir.exists(), "旧目录已搬走")
new_dir = shelf / "chapters" / cid
check(new_dir.is_dir(), f"新目录存在 chapters/{cid}")
check((new_dir / "meta.json").is_file(), "meta.json 跟着搬过来了")
check(len(lib.chapters) == 1, "章节池里有 1 条")
check(lib.chapters[0]["id"] == cid, f"章节 id = {cid}")
check(lib.chapters[0]["dir"] == f"chapters/{cid}", "dir 指向新位置")
check(lib.chapters[0]["author"] == "长夜月的长夜", "作者从 meta 里读出来了")
check(lib.works[0]["chapters"] == [cid], "作品的 chapters 变成 id 列表")
check((shelf / "library.json.bak").is_file(), "迁移前留了 .bak 备份")
check(not (shelf / "长夜月的长夜").exists(), "搬空后的作者目录被顺手清掉")
check((shelf / "_covers").is_dir(), "_covers 原地保留")
src = json.loads((shelf / "library.json").read_text(encoding="utf-8"))
check(src.get("version") == 2, "library.json 标记为 v2")
check(lib.work_cover_url(lib.works[0]) is not None, "作品封面仍解析得出（_covers 路径没失效）")

print("== 2. 未入库 = 池里但没被作品引用")
lib.works[0]["chapters"] = []
lib.save()
lib2 = Library(shelf / "library.json", shelf)
orphans = lib2.unassigned()
check(len(orphans) == 1 and orphans[0]["id"] == cid, "未入库区能列出该章节")
check(len(lib2.chapter_images(cid)) >= 1, "阅读器能取到图片")

print("== 3. 本地图片导入（自然排序 + 同构 meta）")
imgs = TMP / "imgs"
imgs.mkdir()
for name in ["2.jpg", "10.jpg", "1.jpg"]:
    (imgs / name).write_bytes(b"\xff\xd8\xff" + b"\x00" * 40)
# 造一张真 PNG 用来验证宽高嗅探
png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (800).to_bytes(4, "big") + (1600).to_bytes(4, "big") + b"\x00" * 8
(imgs / "3.png").write_bytes(png)
files = sorted(imgs.iterdir(), key=lambda p: natural_key(p.name))
check([p.name for p in files] == ["1.jpg", "2.jpg", "3.png", "10.jpg"],
      "自然排序：2 在 10 前面")
ch = lib2.import_local_images("导入的章节", "某作者", [str(p) for p in files],
                              source_dir=str(imgs))
check(ch["source"] == "local" and ch["id"].startswith("c"), f"本地章节 id = {ch['id']}")
meta = json.loads((shelf / ch["dir"] / "meta.json").read_text(encoding="utf-8"))
check(meta["source"] == "local", "meta 标注 source=local")
check([i["file"] for i in meta["images"]] == ["images/001.jpg", "images/002.jpg",
                                              "images/003.png", "images/004.jpg"],
      "图片按自然序重命名")
check(meta["images"][2]["width"] == 800 and meta["images"][2]["height"] == 1600,
      "PNG 宽高嗅探正确（长图判断要用）")
check(all((imgs / f).exists() for f in ["1.jpg", "2.jpg", "3.png", "10.jpg"]),
      "导入是复制，用户原图没被动过")
check(len(lib2.unassigned()) == 2, "导入后未入库区有 2 条")

print("== 4. 归入作品 / 移除 / 彻底删除")
w = lib2.create_work("测试作品", "某作者")
lib2.add_chapters(w["id"], [cid, ch["id"]])
check(len(lib2.get_work(w["id"])["chapters"]) == 2, "两个来源的章节都能加入同一作品")
check(len(lib2.unassigned()) == 0, "归完后未入库区清空")
lib2.remove_chapter(w["id"], cid)
check(len(lib2.unassigned()) == 1, "移除后回到未入库区")
lib2.delete_chapter(ch["id"])
check(not (shelf / ch["dir"]).exists(), "彻底删除会删掉磁盘目录")
check(len(lib2.chapters) == 1, "章节池里也清掉了")

print("== 5. 残留清理（没有 meta.json 的目录）")
junk = shelf / "chapters" / "opus_999"
junk.mkdir()
(junk / "images").mkdir()
(junk / "images" / "001.jpg").write_bytes(b"x")
inc = lib2.find_incomplete()
check([i["name"] for i in inc] == ["opus_999"], "能列出残目录")
check(lib2.purge_incomplete(["opus_999"]) == 1 and not junk.exists(), "清理后目录删除")
# 有 meta.json 的目录不能被误删
check(lib2.purge_incomplete([cid]) == 0 and (shelf / "chapters" / cid).is_dir(),
      "有 meta.json 的章节不会被误清理")

print("== 6. 扫描自愈（磁盘上有、库里没有就补进池）")
before = len(lib2.chapters)
lib3 = Library(shelf / "library.json", shelf)
lib3.chapters = []
lib3.save()
lib4 = Library(shelf / "library.json", shelf)
check(len(lib4.chapters) == before, "重新载入时把磁盘上的章节补回池里")

print("== 7. 暂存目录导入（双目录模式）")
staging = TMP / "staging"
# 造一个"书架上还没有"的已下载章节放进暂存目录
staged = staging / "某人" / "999999_另一篇"
shutil.copytree(shelf / "chapters" / cid, staged)
meta_p = staged / "meta.json"
m = json.loads(meta_p.read_text(encoding="utf-8"))
lib6 = Library(shelf / "library.json", shelf)
# 先保持原 opus_id：这一篇书架上已经有了
check(len(lib6.scan_pending_imports(staging)) == 0,
      "书架上已有的章节不会被重复列出")
# 再换成一个书架上没有的 id
m["opus_id"] = "999999"
meta_p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
pending = lib6.scan_pending_imports(staging)
check(len(pending) == 1 and pending[0]["opus_id"] == "999999",
      "暂存目录里未入库的章节能被列出")
imported = lib6.import_opus_dir(Path(pending[0]["path"]), mode="move")
check((shelf / imported["dir"]).is_dir(), "搬进书架成功")
check(imported["dir"] == "chapters/opus_999999", f"落到 chapters/opus_999999（实际 {imported['dir']}）")
check(not staged.exists(), "move 模式下暂存目录里的原件已移走")
check(len(lib6.scan_pending_imports(staging)) == 0, "导入后暂存目录清空")

print("== 8. 目录配置兼容旧 out 键")
from asase_reader.backend import Backend  # noqa: E402

cfgdir = TMP / "state"
cfgdir.mkdir()
(cfgdir / "config.json").write_text(
    json.dumps({"out": "outputs", "concurrency": 3}), encoding="utf-8")
b = Backend(state_dir=cfgdir)
check(b.config["shelf_dir"] == "outputs", "旧 out 键被认领成 shelf_dir")
check(b.download_root() is None, "没有 download_dir 时是单目录模式")
check(b.shelf_root().name == "outputs", f"shelf_root = {b.shelf_root()}")
b.config["shelf_dir"] = str(shelf)
b.config["download_dir"] = str(staging)
b.config["setup_done"] = True
assert_tmp_guard(shelf, "shelf")
r = b.update_config({"shelf_dir": str(shelf), "download_dir": str(staging), "setup_done": True})
check(r["ok"], f"目录保存成功 {r.get('error','')}")
check(b.download_root() == staging, "download_root 指向暂存目录")
b.config["download_dir"] = str(shelf / "chapters")
bad = b.update_config({"download_dir": str(shelf / "chapters")})
check(bad["ok"] is False and "书架目录" in bad["error"], f"包含关系被拦下：{bad['error']}")

print("== 9. 单目录模式落点")
b2 = Backend(state_dir=cfgdir)
b2.config["shelf_dir"] = str(shelf)
b2.config["download_dir"] = ""
b2.config["setup_done"] = True
landing = b2._landing_dir("1261150454223470609")
check(landing == shelf / "chapters" / cid, f"单目录模式直接落 chapters/{cid}")

print("== 10. Backend 入库流程（单目录模式）")
import asyncio  # noqa: E402
from asase_reader.backend import Job  # noqa: E402
from asase_reader.models import ImageItem, Opus  # noqa: E402
from asase_reader.store import write_meta  # noqa: E402

b3 = Backend(state_dir=cfgdir)
b3.config["shelf_dir"] = str(shelf)
b3.config["download_dir"] = ""
b3.config["setup_done"] = True
opus = Opus(opus_id="777", source_url="https://x/777", title="第七七篇",
            author_name="某人", author_mid=1,
            images=[ImageItem(index=1, url="https://i0.hdslb.com/a.jpg", file="images/001.jpg",
                              width=800, height=1200)])
landing = b3._landing_dir("777", opus)
check(landing == shelf / "chapters" / "opus_777", "落点 = chapters/opus_777")
landing.mkdir(parents=True)
(landing / "images").mkdir()
(landing / "images" / "001.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 20)
write_meta(landing, {
    "source": "opus", "opus_id": "777", "title": "第七七篇",
    "author": {"mid": 1, "name": "某人"},
    "cover": None,
    "images": [{"index": 1, "file": "images/001.jpg", "url": "https://i0.hdslb.com/a.jpg",
                "width": 800, "height": 1200, "size": 23}],
})
check(opus_is_complete(landing, opus, True), "opus_is_complete 认出已下完（原地判断，不依赖下载目录）")
job = Job(opus_id="777")
job.dir = str(b3._register(job, landing))
check(b3.library.has_opus("777"), "_register 后章节进了池")
check(job.stage == "imported", f"任务阶段 = {job.stage}")
st = b3.library_state()
check(any(c["id"] == "opus_777" for c in st["orphans"]), "library_state 的未入库区能看到它")
w2 = b3.create_work("作品二", "某人")
b3.add_chapters(w2["id"], ["opus_777"])
det = b3.work_detail(w2["id"])
check(det["chapters"][0]["chapter_id"] == "opus_777", "work_detail 返回 chapter_id")
check(len(b3.chapter_images("opus_777")["images"]) == 1, "阅读器能取到图")

print("== 11. 编辑会话（begin_edit / cancel_edit 的封面快照）")
# 回归：begin_edit 曾因 backend 里残留的旧属性名 library.outputs_root 直接抛
# AttributeError，导致「新建作品」「编辑作品」全部报错。
b3.begin_edit(w2["id"])
check(True, "begin_edit 不再抛 AttributeError")
b3.update_work(w2["id"], {"name": "作品二改名"})
check(b3.edit_dirty()["dirty"], "改动被编辑会话识别")
b3.cancel_edit()
check(b3.library.get_work(w2["id"])["name"] == "作品二", "cancel_edit 回滚了改名")
b3.commit_edit()

print("== 11b. Backend.lock 必须可重入")
# 回归：import_files / delete_chapter / purge_incomplete 都是在持锁状态下调
# self.log()，而 log() 自己也要拿这把锁。用普通 Lock 会直接死锁（界面卡死）。
b3.lock.acquire()
got = b3.lock.acquire(timeout=2)
b3.lock.release()
if got:
    b3.lock.release()
check(got, "Backend.lock 可重入（持锁时调 log() 不会死锁）")

print("== 12. Backend 上没有指向不存在 Library 属性的引用")
import inspect  # noqa: E402
import re  # noqa: E402

src_all = inspect.getsource(sys.modules["asase_reader.backend"])
used = set(re.findall(r"self\.library\.(\w+)", src_all))
missing = sorted(a for a in used if not hasattr(b3.library, a))
check(not missing, f"backend 引用的 Library 属性都存在（缺失：{missing}）")

print("== 13. 作品时间戳 / 标签（v1.5 埋的字段）与 list_works 排序")
# 旧库（v1 迁移来的）作品：三个时间戳空串、tags 空列表 —— 最近更新排序时沉底
w_old = lib2.works[0]
check(w_old.get("created_at") == "" and w_old.get("updated_at") == ""
      and w_old.get("last_read_at") == "", "旧库作品时间戳缺省为空串")
check(w_old.get("tags") == [], "旧库作品 tags 缺省为空列表")

# 新建作品：created_at/updated_at 有值，last_read_at/tags 为空
w13 = lib2.create_work("时间戳作品", "作者甲")
check(bool(w13["created_at"]) and bool(w13["updated_at"]), "新建作品带 created_at/updated_at")
check(w13["last_read_at"] == "" and w13["tags"] == [], "last_read_at / tags 初始为空")
wid13 = w13["id"]

# 时间戳是秒级精度，同一秒内两次操作会撞串 —— 测试用「先打一个已知旧值，
# 操作后断言不等于它」的方式判定刷新，不靠 sleep。
MARK = "2000-01-01T00:00:00+00:00"


def mark(wid):
    lib2.get_work(wid)["updated_at"] = MARK
    lib2.save()


# 造一个未入库章节用于加入/移除
imgs13 = TMP / "imgs13"
imgs13.mkdir()
(imgs13 / "1.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 40)
ch13 = lib2.import_local_images("第十三章", "作者甲", [str(imgs13 / "1.jpg")])

mark(wid13)
lib2.add_chapters(wid13, [ch13["id"]])
check(lib2.get_work(wid13)["updated_at"] != MARK, "add_chapters 刷新 updated_at")

mark(wid13)
lib2.move_chapter(wid13, ch13["id"], 0)  # 只有一个章节，等于没动 —— 但函数被调用了
check(lib2.get_work(wid13)["updated_at"] == MARK, "move_chapter 刻意不刷新 updated_at")

mark(wid13)
lib2.update_work(wid13, {"tags": [" 连载 ", "横版", "连载"]})
w13r = lib2.get_work(wid13)
# v1.8.0 起打标签算「整理」，与 move_chapter 同理不刷新——否则给旧作品补几个标签
# 就会把它顶到书架最前
check(w13r["updated_at"] == MARK, "update_work(tags) 刻意不刷新 updated_at（整理不算更新）")
check(w13r["tags"] == ["连载", "横版"], "tags 写入并去空白（不去重，交给 UI）")

# 改名字 / 作者是改内容，仍然要刷新
mark(wid13)
lib2.update_work(wid13, {"author": "新作者"})
check(lib2.get_work(wid13)["updated_at"] != MARK, "update_work(author) 刷新 updated_at")

mark(wid13)
lib2.remove_chapter(wid13, ch13["id"])
check(lib2.get_work(wid13)["updated_at"] != MARK, "remove_chapter 刷新 updated_at")

# list_works：不扫盘、最近更新倒序、空时间戳沉底且保持书库原序
# 排序结论不许依赖真实时钟：时间戳只到秒，测试跑得快的时候其余作品会和 wid13
# 撞在同一秒，稳定排序就并列、谁在前全看创建顺序。把其余作品的 updated_at 统一
# 压成一个固定的旧值（w_old 保留空串以验证沉底），wid13 保留刚刷新过的 now。
OLD = "2001-01-01T00:00:00+00:00"
for w in lib2.works:
    if w["id"] not in (wid13, w_old["id"]):
        w["updated_at"] = OLD
lib2.save()

b13 = Backend(state_dir=TMP / "state13")
b13.config["shelf_dir"] = str(shelf)
b13.config["download_dir"] = ""
b13.config["setup_done"] = True
lst = b13.list_works()["works"]
check([w["id"] for w in lst][0] == wid13, "刚更新过的作品排最前（其余压成旧值，不靠时钟）")
ids13 = [w["id"] for w in lst]
check(ids13[-1] == w_old["id"], "空 updated_at 的旧作品沉底")
check(all(w["tags"] is not None and "updated_at" in w for w in lst),
      "list_works 返回 tags 与 updated_at 字段")

# 排序键是 max(updated_at, last_read_at)：刚读过的（内容没变）也要顶到前面
NOW = "2026-10-22T12:00:00+00:00"
NEWER = "2026-10-22T13:00:00+00:00"
wa = lib2.create_work("甲：只更新过", "作者乙")
wb = lib2.create_work("乙：只刚读过", "作者乙")
for w in lib2.works:
    if w["id"] in (wa["id"], wb["id"]):
        w["updated_at"] = OLD
        w["last_read_at"] = ""
wa["updated_at"] = NOW        # 甲：内容新，没读过
wb["last_read_at"] = NEWER    # 乙：内容旧，但读的时间比甲更新的时间还晚
lib2.save()

# 换一个 Backend 实例：b13 已经把库读进内存了，之后在 lib2 上新建的作品它看不到
b14 = Backend(state_dir=TMP / "state14")
b14.config["shelf_dir"] = str(shelf)
b14.config["download_dir"] = ""
b14.config["setup_done"] = True
ids2 = [w["id"] for w in b14.list_works()["works"]]
check(ids2.index(wb["id"]) < ids2.index(wa["id"]),
      "排序取 max(更新, 阅读)：刚读过的排在只更新过的前面")

# 作品页与编辑页共用 work_detail，标签要走这个接口过去（v1.8.0）
b15 = Backend(state_dir=TMP / "state15")
b15.config["shelf_dir"] = str(shelf)
b15.config["download_dir"] = ""
b15.config["setup_done"] = True
b15.update_work(wid13, {"tags": ["科幻", " 完结 "]})
d15 = b15.work_detail(wid13)
check(d15.get("tags") == ["科幻", "完结"], "work_detail 返回规整后的 tags")
check("author" in d15 and "chapters" in d15, "work_detail 仍带 author / chapters")
check(ids2[-1] == w_old["id"], "两个时间都空的旧作品照样沉底")

# mark_read 只写 last_read_at：阅读不是修改作品内容，不能顶掉「最近更新」的语义
lib2.mark_read(wb["id"])
wbm = lib2.get_work(wb["id"])
check(wbm["last_read_at"] != "" and wbm["last_read_at"] != NEWER, "mark_read 写入 last_read_at")
check(wbm["updated_at"] == OLD, "mark_read **不**刷新 updated_at（阅读不算改作品）")
check(b13.mark_read("压根不存在的作品") == {"ok": False},
      "mark_read 对不存在的作品返回 ok:False，不抛异常挡住阅读")

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
