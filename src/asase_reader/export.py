"""把书架里的内容导出成普通文件夹。

产物结构（导出是**复制**，书架里的原件一律保留）：

    导出目录/
      作品名/
        work.json                 作品与章节信息（我们自己的格式）
        cover.jpg                 作品封面
        第01话 章节名/
          00_cover.jpg            章节头图
          001.jpg  002.jpg …      正文图（沿用书架里的文件名，不重命名）

设计要点：
- **只做复制**：导出 = 一份副本，磁盘占用翻倍，原件不受影响；
- **不转码、不改图片文件名**：meta 里的顺序就是阅读顺序，重命名只会添乱；
- **目标已存在则跳过**：绝不覆盖用户在导出目录里已有的文件；
- 规划（plan_*）与执行（run_plan）分开：规划是纯函数，可以直接断言目录结构，
  执行只管按清单搬运，便于测试也便于中途取消。
- 未入库的章节（不属于任何作品）导出成**顶层的「章节标题/」文件夹**，不写 work.json。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .library import Library, LibraryError
from .store import safe_component

WORK_JSON = "work.json"
CHAPTER_COVER_STEM = "00_cover"      # 章节头图：00_ 前缀，排在所有正文图前面
WORK_COVER_NAME = "cover"            # 作品封面（扩展名沿用原文件）
FORMAT_TAG = "asase-work/1"

# 章节文件夹的两种命名方式（导出的用户在对话框里选）
NAMING_NUMBERED = "numbered"   # 第01话 章节名
NAMING_TITLE = "title"         # 只用章节名
NAMING_MODES = (NAMING_NUMBERED, NAMING_TITLE)

_RE_CH_PREFIX = re.compile(r"^第\s*\d+\s*[话章回节]")


class ExportCancelled(Exception):
    """用户在导出过程中点了取消。"""


@dataclass
class ExportItem:
    """一条待执行动作：要么是复制文件，要么是写一段文本（work.json）。"""

    dst: Path
    src: Path | None = None
    text: str | None = None


def chapter_folder_name(index: int, title: str, mode: str = NAMING_NUMBERED) -> str:
    """章节文件夹名。

    - numbered：``第01话 章节名``（序号补零，排序永远正确）；
    - title：只用章节名。
    标题本身已经带「第N话」时不再重复加前缀。
    """
    t = (title or "").strip()
    if mode == NAMING_TITLE:
        return safe_component(t or (f"第{index}话" if index else "未命名章节"), max_len=80)
    # 未入库的章节没有序号（index=0）：直接用章节标题，不要凭空加个"章节"前缀
    if not index:
        return safe_component(t or "未命名章节", max_len=80)
    if t and _RE_CH_PREFIX.match(t):
        return safe_component(t, max_len=80)
    label = f"第{index:02d}话"
    return safe_component(f"{label} {t}".strip() if t else label, max_len=80)


def _meta_cover_file(meta: dict) -> str | None:
    """meta 里的头图相对路径；历史数据可能是字符串或 dict，一律宽容处理。"""
    raw = meta.get("cover")
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, dict):
        return raw.get("file") or None
    return None


def _chapter_cover_src(lib: Library, ch: dict, meta: dict) -> Path | None:
    """章节导出的封面来源：B 站头图优先，没有头图时用章节的自定义封面。"""
    f = _meta_cover_file(meta)
    if f:
        p = lib.shelf_root / ch["dir"] / f
        if p.is_file():
            return p
    custom = ch.get("cover")
    if custom:
        p = lib.shelf_root / custom
        if p.is_file():
            return p
    return None


def _chapter_images(lib: Library, ch: dict, meta: dict) -> list[tuple[Path, str]]:
    """（源文件, 导出后的文件名）——顺序与 meta 一致，跳过下载失败/缺文件的条目。"""
    base = lib.shelf_root / ch["dir"]
    out: list[tuple[Path, str]] = []
    for img in meta.get("images") or []:
        f = img.get("file")
        if not f or img.get("error"):
            continue
        src = base / f
        if not src.is_file():
            continue
        out.append((src, Path(f).name or f))
    return out


def _work_cover_src(lib: Library, work: dict, chapters: list[dict]) -> Path | None:
    """作品封面：自定义封面 → 第一个有封面的章节 → 第一章的第一张图。"""
    custom = work.get("cover")
    if custom:
        p = lib.shelf_root / custom
        if p.is_file():
            return p
    for ch in chapters:
        src = _chapter_cover_src(lib, ch, lib._read_meta(ch["dir"]))
        if src:
            return src
    for ch in chapters:
        imgs = _chapter_images(lib, ch, lib._read_meta(ch["dir"]))
        if imgs:
            return imgs[0][0]
    return None


def _plan_chapter_dir(lib: Library, ch: dict, parent: Path, index: int,
                      mode: str) -> tuple[list[ExportItem], dict]:
    """一个章节 -> 一个文件夹，返回（动作清单, 写进 work.json 的摘要）。"""
    meta = lib._read_meta(ch["dir"])
    folder = parent / chapter_folder_name(index, ch.get("title") or "", mode)
    items: list[ExportItem] = []

    cover_src = _chapter_cover_src(lib, ch, meta)
    cover_name = None
    if cover_src is not None:
        cover_name = f"{CHAPTER_COVER_STEM}{cover_src.suffix.lower()}"
        items.append(ExportItem(dst=folder / cover_name, src=cover_src))

    names: list[str] = []
    for src, name in _chapter_images(lib, ch, meta):
        names.append(name)
        items.append(ExportItem(dst=folder / name, src=src))

    # 视频章节（v1.19）：导出的是视频文件本身，不是图片流。
    # 不带它就是导出一个空文件夹 —— 备份会缺一章，而这一章恰恰是剧情的一部分。
    video_name = None
    if ch.get("source") == "video":
        vf = str((meta.get("video") or {}).get("file") or "").strip()
        if vf:
            vsrc = lib.shelf_root / ch["dir"] / vf
            if vsrc.is_file():
                # 保留原扩展名：容器与后缀不符会播不了
                video_name = "video" + vsrc.suffix.lower()
                items.append(ExportItem(dst=folder / video_name, src=vsrc))

    summary = {
        "index": index,
        "title": ch.get("title") or "",
        "note": ch.get("note") or "",
        "source": ch.get("source"),
        "opus_id": ch.get("opus_id"),
        "folder": folder.name,
        "images": names,
        "cover": cover_name,
        "video": video_name,       # 非视频章节是 None：导出元数据里也看得出这一章是什么
    }
    return items, summary


def _work_json_text(work: dict, summaries: list[dict], dest: Path) -> str:
    existing = dest / WORK_JSON
    merged = list(summaries)
    if existing.is_file():
        try:
            old = json.loads(existing.read_text(encoding="utf-8"))
        except Exception:
            old = None
        if isinstance(old, dict) and isinstance(old.get("chapters"), list):
            # 分批导出会写进同一个作品文件夹：按（序号/opus_id）去重后合并，
            # 保证先导出第 1 话、再导出第 2 话时 work.json 里两话都在
            def key(c: dict) -> tuple:
                return (str(c.get("opus_id") or ""), int(c.get("index") or 0))

            by_key = {key(c): c for c in old["chapters"] if isinstance(c, dict)}
            by_key.update({key(c): c for c in merged})
            merged = sorted(by_key.values(), key=lambda c: int(c.get("index") or 0))
    data = {
        "format": FORMAT_TAG,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": f"浅濑阅读器 Asase Reader v{__version__}",
        "work": {"name": work.get("name") or "", "author": work.get("author") or ""},
        "chapters": merged,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _plan_work(lib: Library, work: dict, dest_root: Path, mode: str,
               only: str | None = None) -> list[ExportItem]:
    """一部作品 -> 作品文件夹（含 work.json 与作品封面）。

    only 指定章节 id 时只导出这一章（单章导出用），结构与整部导出完全一致，
    所以分批导出会自然合并到同一个作品文件夹里。
    """
    work_dir = dest_root / safe_component(work.get("name") or "未命名作品", max_len=80)
    chapters = []
    for i, cid in enumerate(work.get("chapters") or [], 1):
        if only and cid != only:
            continue
        ch = lib.chapter_by_id(cid)
        if ch:
            chapters.append((i, ch))
    if not chapters:
        return []

    items: list[ExportItem] = []
    summaries: list[dict] = []
    for index, ch in chapters:
        one, summary = _plan_chapter_dir(lib, ch, work_dir, index, mode)
        items.extend(one)
        summaries.append(summary)

    cover_src = _work_cover_src(lib, work, [c for _, c in chapters])
    if cover_src is not None:
        items.append(
            ExportItem(
                dst=work_dir / f"{WORK_COVER_NAME}{cover_src.suffix.lower()}",
                src=cover_src,
            )
        )
    items.append(
        ExportItem(dst=work_dir / WORK_JSON, text=_work_json_text(work, summaries, work_dir))
    )
    return items


def _plan_orphan(lib: Library, ch: dict, dest_root: Path, mode: str) -> list[ExportItem]:
    """未入库章节：导出成顶层的「章节标题/」文件夹，不写 work.json。"""
    items, _ = _plan_chapter_dir(lib, ch, dest_root, 0, mode)
    return items


def plan_chapter(lib: Library, chapter_id: str, dest_root: Path,
                 mode: str = NAMING_NUMBERED) -> list[ExportItem]:
    """单章导出：属于某个作品时带作品层，否则是顶层文件夹。"""
    ch = lib.chapter_by_id(str(chapter_id))
    if not ch:
        raise LibraryError(f"章节不存在：{chapter_id}")
    for w in lib.works:
        if ch["id"] in (w.get("chapters") or []):
            return _plan_work(lib, w, dest_root, mode, only=ch["id"])
    return _plan_orphan(lib, ch, dest_root, mode)


def plan_work(lib: Library, work_id: str, dest_root: Path,
              mode: str = NAMING_NUMBERED) -> list[ExportItem]:
    return _plan_work(lib, lib.get_work(str(work_id)), dest_root, mode)


def plan_all(lib: Library, dest_root: Path, mode: str = NAMING_NUMBERED) -> list[ExportItem]:
    """整个书库：每部作品一个文件夹，未入库章节各自一个顶层文件夹。"""
    items: list[ExportItem] = []
    used: set[str] = set()
    for w in lib.works:
        used.update(str(c) for c in (w.get("chapters") or []))
        items.extend(_plan_work(lib, w, dest_root, mode))
    for ch in lib.chapters:
        if ch["id"] in used:
            continue
        items.extend(_plan_orphan(lib, ch, dest_root, mode))
    return items


def run_plan(items: list[ExportItem],
             on_progress=None,
             should_cancel=None) -> dict:
    """按清单搬运。目标已存在则**跳过**（不覆盖），work.json 例外（ Merge 后重写）。"""
    copied = skipped = failed = 0
    total = len(items)
    for i, it in enumerate(items, 1):
        if should_cancel and should_cancel():
            raise ExportCancelled()
        try:
            it.dst.parent.mkdir(parents=True, exist_ok=True)
            if it.text is not None:
                it.dst.write_text(it.text, encoding="utf-8")
                copied += 1
            elif it.src is None:
                failed += 1
            elif it.dst.exists():
                skipped += 1
            else:
                shutil.copy2(it.src, it.dst)
                copied += 1
        except OSError:
            failed += 1
        if on_progress:
            on_progress(i, total, it.dst.name)
    return {"copied": copied, "skipped": skipped, "failed": failed, "total": total}
