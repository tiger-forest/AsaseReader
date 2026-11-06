"""离线下载全链路测试：真实 HTML 解析 + 打桩图片，不发任何网络请求。

这是唯一能覆盖「解析 → 限速 → 落盘 → meta.json → 入库 → 章节池」整条链路的测试，
而它完全不碰 B 站：页面用抓下来的 HTML 夹具，图片用打桩返回的合法字节。
因此可以随时随地在任何对话里跑，不用担心风控。

跑法（也由 tests/selftest.py 统一调用）：
    python tests/test_download_offline.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import os  # noqa: E402
from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader.backend import Backend  # noqa: E402
from asase_reader.control import Control  # noqa: E402
from asase_reader.download import download_opus, opus_is_complete  # noqa: E402
from asase_reader.fetch import Fetcher  # noqa: E402
from asase_reader.library import Library  # noqa: E402
from asase_reader.parse import parse_opus_html  # noqa: E402
from asase_reader.ratelimit import RateLimiter  # noqa: E402
from asase_reader.store import chapter_dir  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "opus_1261150454223470609.html"

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


# 一张最小的合法 JPEG（有 SOI/EOI，够 sniff_ext 认出来）
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class StubFetcher(Fetcher):
    """只替换图片抓取：返回打桩字节，其余（限速、重试、风控）都用真家伙。"""

    def __init__(self, *a, **kw) -> None:
        # 限速区间设成 0，测试不用真等几秒
        super().__init__(RateLimiter(0, 0), RateLimiter(0, 0), *a, **kw)
        self.calls: list[str] = []

    async def fetch_image(self, url: str) -> bytes:
        self.calls.append(url)
        return PNG if url.lower().endswith(".png") else JPEG

    async def fetch_page(self, url: str) -> str:
        return FIXTURE.read_text(encoding="utf-8")


def main() -> None:
    ensure_tmp_env()
    tmp = Path(tempfile.mkdtemp(prefix="asase_dl_"))
    shelf = tmp / "shelf"
    state = tmp / "state"
    shelf.mkdir()
    state.mkdir()
    assert_tmp_guard(shelf, "shelf")
    assert_tmp_guard(state, "state")
    assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")

    print("== 1. 用真实 HTML 夹具解析")
    html = FIXTURE.read_text(encoding="utf-8")
    opus = parse_opus_html(html, "https://www.bilibili.com/opus/1261150454223470609")
    check(opus.opus_id == "1261150454223470609", f"解析出 opus_id（实际 {opus.opus_id}）")
    check(bool(opus.title), f"解析出标题：{opus.title!r}")
    check(len(opus.images) > 0, f"解析出 {len(opus.images)} 张正文图")
    if not opus.images:
        print("夹具里没有图片，后续断言无意义，中止")
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)

    print("== 2. 下载到书架章节目录（单目录模式落点）")
    fetcher = StubFetcher()
    target = chapter_dir(shelf, f"opus_{opus.opus_id}")

    async def run() -> tuple[Path, object]:
        async with fetcher:
            return await download_opus(
                fetcher, opus, target, include_cover=True, log=lambda *a, **k: None
            )

    dir_path, stats = asyncio.run(run())
    check(dir_path == target, f"落点 = {dir_path.relative_to(shelf).as_posix()}")
    check(stats.downloaded + stats.skipped == len(opus.images) + (
        1 if opus.cover else 0), f"全部图片都落盘了（{stats.downloaded} 新下 / {stats.skipped} 跳过）")
    check(stats.failed == 0, "没有失败的图")
    check(opus_is_complete(target, opus, True), "opus_is_complete 认为已下完")

    print("== 3. meta.json 结构")
    meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    check(meta["source"] == "opus", "meta.source = opus")
    check(meta["opus_id"] == opus.opus_id, "meta.opus_id 正确")
    check(bool(meta["title"]) and bool(meta["author"]["name"]), "meta 里有标题和作者（章节自洽）")
    check(len(meta["images"]) == len(opus.images), "meta.images 数量对得上")
    first = meta["images"][0]
    check(bool(first.get("file")) and bool(first.get("url")), "每条图有 file 与 url")
    check((target / first["file"]).is_file(), "meta 里记的文件真实存在")

    print("== 4. 文件编号与去重")
    files = [m["file"] for m in meta["images"]]
    check(files == sorted(files), "文件名按阅读顺序递增编号")
    check(all((target / f).is_file() for f in files), "所有图片文件都在磁盘上")
    urls = [m["url"] for m in meta["images"]]
    if len(set(urls)) < len(urls):
        check(len(set(files)) == len(set(urls)), "重复 URL 复用同一个文件（不重复下载）")

    print("== 5. 入库：章节进池、可被作品引用")
    b = Backend(state_dir=state)
    b.config["shelf_dir"] = str(shelf)
    b.config["download_dir"] = ""
    b.config["setup_done"] = True
    assert_tmp_guard(b.shelf_root(), "shelf_root")
    landing = b._landing_dir(opus.opus_id, opus)
    check(landing == target, "后端算出的落点与刚才下载的位置一致")
    cid = b._register(type("J", (), {"opus_id": opus.opus_id, "dir": "", "stage": "queued"})(), target)
    lib = Library(shelf / "library.json", shelf)
    check(lib.has_opus(opus.opus_id), "章节进了章节池")
    st = b.library_state()
    check(any(c["id"] == f"opus_{opus.opus_id}" for c in st["orphans"]), "出现在未入库区")
    w = b.create_work("离线测试作品", meta["author"]["name"])
    b.add_chapters(w["id"], [f"opus_{opus.opus_id}"])
    det = b.work_detail(w["id"])
    check(det["chapters"] and det["chapters"][0]["chapter_id"] == f"opus_{opus.opus_id}",
          "能归进作品并被 work_detail 读到")
    check(len(b.chapter_images(f"opus_{opus.opus_id}")["images"]) == len(opus.images),
          "阅读器能取到全部图片")

    print("== 6. 中途取消（Control 协作式中断）")
    target2 = chapter_dir(tmp / "shelf2", f"opus_{opus.opus_id}_cancel")
    ctrl = Control()
    ctrl.cancel()
    fetcher2 = StubFetcher()

    async def run_cancel():
        async with fetcher2:
            try:
                await download_opus(fetcher2, opus, target2, control=ctrl)
            except Exception as e:
                return type(e).__name__
        return "no-exception"

    got = asyncio.run(run_cancel())
    check(got == "OperationCancelled", f"取消后抛出 OperationCancelled（实际 {got}）")
    check(not (target2 / "meta.json").exists(), "取消的章节没有 meta.json → 扫描时不可见")

    print("== 7. 已完整时跳过（不重复下载）")
    fetcher3 = StubFetcher()

    async def run_again():
        async with fetcher3:
            _, s = await download_opus(fetcher3, opus, target, include_cover=True)
            return s

    s3 = asyncio.run(run_again())
    check(s3.downloaded == 0 and s3.skipped == len(opus.images) + 1,
          f"第二次全部跳过（downloaded={s3.downloaded}, skipped={s3.skipped}）")
    check(fetcher3.calls == [], "没有再发起任何图片请求（断点续传生效）")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("FAILED:", len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
