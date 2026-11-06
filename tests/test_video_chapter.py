"""视频章节：导入 / 元数据 / media 接口 / 封面降级。

与「视频转图集」（test_videograb）是并列的两条路：那条把视频抽成图片章节，
这条**原样保留一个视频文件**，在阅读器里直接播。所以这里守的是另一批不变量：

- 文件是**复制**进书架的（原文件不动），且**保留原扩展名**
  （把 webm 存成 .mp4 会让 Chromium 因为容器不符拒播）
- meta.json 不存 duration/width/height（那三个要 ffprobe，而播放时 <video> 自己知道）
- 章节池里 `source == "video"`，与 opus / local 平级；`media_type` 不进池
- `chapter_media` 对视频章节返回 url/format，对图片章节返回 None；
  文件被外部删掉时**仍返回**（让前端走 error 占位卡，而不是退回图片分支）
- 没有 ffmpeg 也要能导入（封面降级为占位），不能卡死核心链路
- 有 ffmpeg 时真实截一帧当封面

跑法（也由 tests/selftest.py 统一调用）：
    python tests/test_video_chapter.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402

import asase_reader.library as libmod  # noqa: E402
from asase_reader import videograb  # noqa: E402
from asase_reader.backend import Backend  # noqa: E402
from asase_reader.export import NAMING_NUMBERED, plan_work, run_plan  # noqa: E402
from asase_reader.store import write_meta  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> None:
    print("== 1. build_thumb_cmd 命令拼装")
    cmd = videograb.build_thumb_cmd("ffmpeg", "in.mp4", "out/cover.jpg")
    check("-frames:v" in cmd and "1" in cmd, "只取一帧（-frames:v 1）")
    check(cmd[-1] == "out/cover.jpg", "输出路径在最后")
    check(not any(x == "-vsync" for x in cmd),
          "不能用 -vsync（ffmpeg 9 已删除，写了直接失败）")
    check("-y" in cmd, "带 -y（覆盖已有封面）")

    ensure_tmp_env()
    tmp = Path(tempfile.mkdtemp(prefix="asase_vidch_"))
    shelf_tmp = tmp / "shelf"
    shelf_tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "config.json").write_text(json.dumps({"shelf_dir": str(shelf_tmp), "setup_done": True}), encoding="utf-8")
    b = Backend(state_dir=tmp / "state", config_path=tmp / "config.json")
    shelf = b.shelf_root()
    assert_tmp_guard(shelf, "shelf")
    assert_tmp_guard(b.state_dir, "state_dir")
    lib = b.library

    # 假视频文件：导入本身不需要它是真视频（只有截帧才需要）
    src = tmp / "短片.webm"
    src.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 2048)
    orig_bytes = src.read_bytes()

    print("== 2. 导入为视频章节（无 ffmpeg 也要能导）")
    ch = lib.import_video_file(src, title="第05话 动态特典", author="某作者")
    check(ch["source"] == "video", f"chapters 池 source = video（实际 {ch['source']}）")
    check("media_type" not in ch, "media_type 不进 chapters 池（会被 _norm_chapter 丢掉）")
    check(ch["title"] == "第05话 动态特典", "标题落库")
    check(orig_bytes == src.read_bytes(), "原文件没被动过（是复制不是移动）")

    d = shelf / ch["dir"]
    check((d / "video.webm").is_file(), "书架里是 video.webm（保留原扩展名）")
    check((d / "video.webm").read_bytes() == orig_bytes, "复制内容与源文件一致")

    meta = lib.read_chapter_meta(ch["id"])
    check(meta.get("source") == "video", "meta.source = video")
    check((meta.get("video") or {}).get("file") == "video.webm", "meta.video.file 正确")
    check((meta.get("video") or {}).get("size") == 2052, "meta.video.size 正确")
    check(str(src) == (meta.get("video") or {}).get("orig_path"), "记下原始路径（只留痕）")
    for gone in ("duration", "width", "height"):
        check(gone not in (meta.get("video") or {}),
              f"meta.video 不存 {gone}（要 ffprobe 才拿得到，播放时不需要）")
    check(meta.get("images") == [], "meta.images 是空的（视频章节没有图片流）")
    check(meta.get("cover") is None, "无 ffmpeg 时封面为空（降级为占位）")

    print("== 3. chapter_media 接口")
    m = lib.chapter_media(ch["id"])
    check(m is not None and m["url"].endswith("video.webm"), f"url = {m and m['url']}")
    check(m is not None and m["url"].startswith("/outputs/"), "url 走静态服务前缀")
    check(m is not None and m["format"] == "webm", f"format = {m and m['format']}")
    check(m is not None and m["size"] == 2052, "size 带上")

    r = b.chapter_images(ch["id"])
    check(r["images"] == [], "chapter_images 图片列表为空")
    check(r["media"] is not None, "chapter_images 带上 media 字段（前端一个 if 分流）")

    print("== 4. 非视频文件要被拒")
    bad = tmp / "cover.png"
    bad.write_bytes(b"\x89PNG\r\n1a\n")
    rejected = False
    try:
        lib.import_video_file(bad)
    except libmod.LibraryError:
        rejected = True
    check(rejected, "非视频后缀抛 LibraryError")
    rejected = False
    try:
        lib.import_video_file(tmp / "不存在.mp4")
    except libmod.LibraryError:
        rejected = True
    check(rejected, "文件不存在抛 LibraryError")

    print("== 5. 图片章节的 media 一律 None")
    img_dir = tmp / "图"
    img_dir.mkdir()
    (img_dir / "a.jpg").write_bytes(b"\xff\xd8\xff\x00A")
    pch = lib.import_local_images("图片章节", "", [str(img_dir / "a.jpg")])
    check(pch["source"] == "local", "图片章节 source = local")
    check(lib.chapter_media(pch["id"]) is None, "图片章节 chapter_media 返回 None")
    check(b.chapter_images(pch["id"])["media"] is None, "图片章节的 media 字段是 None")
    check(len(b.chapter_images(pch["id"])["images"]) == 1, "图片章节的图片照旧返回")

    # 变异验证抓出来的：光断言「图片章节 media 是 None」钉不住判据 —— meta 里本来就没有
    # video 块，所以就算不看 source 也返回 None。必须人为塞一个 video 块进去，
    # 才能证明判据是**池里的 source**，而不是「meta 里有没有 video 块」。
    dirty = json.loads(json.dumps(lib.read_chapter_meta(pch["id"])))
    dirty["video"] = {"file": "video.mp4", "size": 1}
    write_meta(shelf / pch["dir"], dirty)
    check(lib.chapter_media(pch["id"]) is None,
          "图片章节 meta 里混进 video 块也返回 None（判据是 source）")

    print("== 6. 文件被外部删掉时仍返回 media（前端走 error 占位卡）")
    (d / "video.webm").unlink()
    m2 = lib.chapter_media(ch["id"])
    check(m2 is not None and m2["url"].endswith("video.webm"),
          "文件没了也照常返回（退回图片分支才是错的）")

    print("== 7. video_import_start(mode='video')")
    src2 = tmp / "另一段.mov"
    src2.write_bytes(b"\x00" * 512)
    res = b.video_import_start([str(src2)], "video")
    check(res.get("ok") is True, f"导入成功（{res}）")
    check(res.get("made") == 1, "入库 1 个章节")
    check(res.get("mode") == "video", "返回里标了 mode")
    st = b.library_state()
    orphan_ids = [c["id"] for c in st["orphans"]]
    check(res["chapter_ids"][0] in orphan_ids,
          "落在「未入库的章节」（与图片章节同一套语义）")
    check(ch["id"] in orphan_ids, "先前导入的那个也还在未入库区")

    nofile = b.video_import_start([str(tmp / "没有.mp4")], "video")
    check(nofile.get("ok") is False, "全是无效文件时返回 ok=False")
    none_video = b.video_import_start([str(bad)], "video")
    check(none_video.get("ok") is False, "非视频后缀直接拒绝")

    print("== 8. 有 ffmpeg 时真实截首帧当封面")
    ffmpeg = videograb.resolve_ffmpeg()
    if not ffmpeg:
        print("  SKIP  这台机器没有 ffmpeg，跳过真实截帧（前面的断言已通过）")
    else:
        clip = tmp / "真片.mp4"
        gen = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=160x120:d=1:r=10",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
            capture_output=True, text=True, errors="replace")
        if gen.returncode != 0 or not clip.is_file():
            check(False, f"合成测试视频失败：{(gen.stderr or '')[-300:]}")
        else:
            vch = lib.import_video_file(clip, title="带封面的", ffmpeg=ffmpeg)
            vd = shelf / vch["dir"]
            check((vd / "cover.jpg").is_file(), "截出 cover.jpg")
            check((vd / "cover.jpg").stat().st_size > 0, "封面不是空文件")
            check(lib.chapter_cover_url(vch) is not None,
                  "chapter_cover_url 能取到视频章节的封面")
            check((lib.read_chapter_meta(vch["id"]).get("cover") or {}).get("file")
                  == "cover.jpg", "meta.cover 指向 cover.jpg")
            check((vd / "video.mp4").is_file(), "mp4 存成 video.mp4")

    print("== 9. 导出：视频文件与封面一起走，work.json 标明这一章是视频")
    # 前面第 6 步把 ch 的视频文件删了（测「文件丢失」分支），这里重新导一个完整的
    src3 = tmp / "要导出的.mp4"
    src3.write_bytes(b"\x00" * 1024)
    vch3 = lib.import_video_file(src3, title="第05话 动态特典")
    dest = tmp / "导出"
    w = lib.create_work("带视频的作品", "某作者")
    lib.add_chapters(w["id"], [vch3["id"]])
    items = plan_work(lib, w["id"], dest, NAMING_NUMBERED)
    run_plan(items)
    ch_dirs = [d for d in dest.iterdir() if d.is_dir()]
    check(len(ch_dirs) == 1, f"导出出一个作品文件夹（实际 {len(ch_dirs)}）")
    subs = [d for d in ch_dirs[0].iterdir() if d.is_dir()] if ch_dirs else []
    check(len(subs) == 1, "作品里有一个章节文件夹")
    if subs:
        names = sorted(p.name for p in subs[0].iterdir())
        check(any(n.startswith("video.") for n in names),
              f"章节文件夹里有视频文件（实际 {names}）")
        # src3 是个假 mp4，截帧必然失败 → 封面降级为「没有」，所以导不出 00_cover。
        # 这条断言是真的：真视频（有 ffmpeg 时）会导出 00_cover，见第 8 步的 cover.jpg。
        check(not any(n.startswith("00_cover") for n in names),
              f"截不出帧的视频不导出 00_cover（降级为没封面），实际 {names}")
    wj = ch_dirs[0] / "work.json" if ch_dirs else None
    check(wj is not None and wj.is_file(), "写出了 work.json")
    if wj and wj.is_file():
        data = json.loads(wj.read_text(encoding="utf-8"))
        cs = data.get("chapters") or []
        check(len(cs) == 1 and cs[0].get("source") == "video", "work.json 标了 source=video")
        check(len(cs) == 1 and (cs[0].get("video") or "").startswith("video."),
              f"work.json 的章节摘要带 video 字段（实际 {cs[0].get('video') if cs else None}）")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("FAILED:", len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
