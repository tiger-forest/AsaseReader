"""作品库：library.json（v2）的读写、章节池与导入。

设计约定：
- 书架是唯一的内容库：章节存在 ``{shelf_dir}/chapters/<chapter_id>/``，索引存在
  ``{shelf_dir}/library.json``；
- **章节是顶层池**，作品只持有 chapter_id 的有序列表；没有被任何作品引用的章节
  就是「未入库」，不需要额外的状态字段；
- 章节目录名与作者/标题/所属作品无关，所以重新归类、改作品名都不会牵动磁盘路径；
- 两种来源（B 站下载 / 本地图片导入）写出**同构**的 meta.json，下游的阅读、封面、
  编辑逻辑因此不需要区分来源。

v1 兼容：旧结构是 ``works[].chapters[]`` 内嵌、以 opus_id 为主键、章节目录形如
``{作者}/{opus_id}_{标题}/``。首次载入时会自动升级（先备份 library.json.bak）并把
旧目录搬进 chapters/<id>/，整个过程幂等，中断后下次启动会接着做完。
"""

from __future__ import annotations

import base64
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .store import (
    build_local_meta,
    build_video_meta,
    chapter_dir,
    natural_key,
    sniff_image_size,
    write_meta,
)
from .videograb import VideoGrabError, extract_thumb, is_video

LIB_VERSION = 2

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


class LibraryError(Exception):
    pass


def _url(rel: Path | str) -> str:
    """shelf 下的相对路径 -> 可供前端 <img> 使用的 URL。"""
    parts = Path(rel).as_posix().split("/")
    return "/outputs/" + "/".join(quote(p) for p in parts)


def opus_chapter_id(opus_id: str) -> str:
    """B 站章节的 id：确定性，重扫/重导都不会产生新条目。"""
    return "opus_" + str(opus_id)


def _new_local_id() -> str:
    return "c" + uuid.uuid4().hex[:8]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_tags(tags) -> list[str]:
    """标签规整：去空白、去空串、按出现顺序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        t = str(t).strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


class Library:
    def __init__(self, library_path: Path, shelf_root: Path, readonly: bool = False) -> None:
        self.path = Path(library_path)
        self.shelf_root = Path(shelf_root)
        self.version = LIB_VERSION
        # readonly=True = 首启未确认时的只读闸门（方案 §3 / Claude 二轮 P-4）：
        # load() 只做纯读解析（v1 结构照常解析进内存），跳过全部会碰磁盘的
        # 修复动作；save() 直接 return。Library 不引用 Backend，闸门靠参数传入。
        self.readonly = bool(readonly)
        self.chapters: list[dict] = []
        self.works: list[dict] = []
        self._meta_cache: dict[tuple[str, float], dict] = {}
        self.load()

    # ---------- 持久化 ----------

    def load(self) -> None:
        raw: dict = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}

        # NOTE-010（v1.31.0 存量 bug，随 2.0.0 顺手修）：library.json 被手改成
        # 根数组时，下面的 raw.get() 会抛 AttributeError → 程序起不来。
        # 防御：根不是 dict 就当空库（重造不出数据，但绝不让程序起不来）。
        if not isinstance(raw, dict):
            raw = {}

        if isinstance(raw.get("chapters"), list):
            chapters = [c for c in raw["chapters"] if isinstance(c, dict)]
            works = [w for w in (raw.get("works") or []) if isinstance(w, dict)]
        else:
            # v1：章节内嵌在作品里，以 opus_id 为主键（纯读解析进内存，
            # readonly 时 _from_v1 内部跳过 _backup，见其注记）
            chapters, works = self._from_v1(
                [w for w in (raw.get("works") or []) if isinstance(w, dict)]
            )

        self.chapters = [self._norm_chapter(c) for c in chapters]
        self.works = [self._norm_work(w) for w in works]

        if self.readonly:
            # 未确认（setup_done=False）时跳过全部碰磁盘的修复动作（§3 闸门，
            # 2026-11-04 沙箱实施坐实为四处）：① _migrate_old_dirs 会 glob 并
            # **移动**章节目录；② _heal_scan 补条目后要 save；③ _from_v1 的
            # _backup 写 library.json.bak（含 v1 格式时）；④ _drop_dangling 会
            # **清空内存库**——未确认时书架常指向空/新目录（default_shelf_dir
            # 回退值），全部条目会被判 dangling，当次会话书架整个消失。
            return
        changed = self._migrate_old_dirs()   # 旧 {作者}/{标题}/ 布局 -> chapters/<id>/
        changed |= self._heal_scan()         # 磁盘上有、库里没有的章节补进池
        changed |= self._drop_dangling()     # 库里有、磁盘上已不在的章节
        if changed:
            self.save()

    def save(self) -> None:
        if self.readonly:
            return  # 未确认期间的一切写库操作静默短路（由确认后的缓存重建兜住）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"version": LIB_VERSION, "chapters": self.chapters, "works": self.works},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def _backup(self) -> None:
        """升级前留一份原件（library.json.bak），出问题可以直接换回来。"""
        try:
            if self.path.exists():
                # 注意不能用 with_suffix：那会把 library.json 变成 library.bak
                shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bak"))
        except Exception:
            pass

    @staticmethod
    def _norm_chapter(c: dict) -> dict:
        cid = str(c.get("id") or "").strip()
        opus_id = str(c.get("opus_id") or "").strip() or None
        if not cid:
            cid = opus_chapter_id(opus_id) if opus_id else _new_local_id()
        source = c.get("source") or ("opus" if opus_id else "local")
        out = {
            "id": cid,
            "source": source,
            "opus_id": opus_id,
            "title": c.get("title") or "",
            "author": c.get("author") or "",
            "dir": c.get("dir") or f"chapters/{cid}",
            "cover": c.get("cover"),
            "note": c.get("note") or "",
            "added_at": c.get("added_at") or _now(),
        }
        return out

    @staticmethod
    def _norm_work(w: dict) -> dict:
        out = {
            "id": w.get("id") or ("w" + uuid.uuid4().hex[:8]),
            "name": w.get("name") or "未命名作品",
            "author": w.get("author") or "",
            "cover": w.get("cover"),
            "chapters": [],
            # 三个时间戳（ISO 字符串）：旧库一律缺省空串 —— 空串排序最小，
            # 「最近更新」倒序时自然沉底，且稳定排序下保持原书库顺序。
            # last_read_at 预留给将来「最近阅读」排序（届时在 openReader 里刷新）；
            # tags 预留给标签筛选（v1.5 只埋字段，不做 UI）。
            "created_at": w.get("created_at") or "",
            "updated_at": w.get("updated_at") or "",
            "last_read_at": w.get("last_read_at") or "",
            "tags": _norm_tags(w.get("tags")),
        }
        for c in w.get("chapters") or []:
            if isinstance(c, str):
                out["chapters"].append(c)
            # v1 残留的内嵌字典在 _from_v1 里已处理，这里不再出现
        return out

    def _from_v1(self, works: list[dict]) -> tuple[list[dict], list[dict]]:
        """v1 -> v2：把内嵌章节提升进顶层池，作品改存 id 列表。

        readonly 时不写 _backup（升级留档在确认后的下一个会话自然补跑，
        那时 library.json 已是 v1 原样、会再走一次这条路径）。
        """
        if not self.readonly:
            self._backup()
        chapters: list[dict] = []
        new_works: list[dict] = []
        for w in works:
            ids: list[str] = []
            for c in w.get("chapters") or []:
                if not isinstance(c, dict):
                    continue
                opus_id = str(c.get("opus_id") or "").strip() or None
                cid = opus_chapter_id(opus_id) if opus_id else _new_local_id()
                rel = str(c.get("path") or "")
                author = ""
                title = c.get("title") or ""
                if rel:
                    meta = self._read_meta(rel)
                    author = (meta.get("author") or {}).get("name") or ""
                    if not title:
                        title = meta.get("title") or ""
                chapters.append(
                    self._norm_chapter(
                        {
                            "id": cid,
                            "source": "opus" if opus_id else "local",
                            "opus_id": opus_id,
                            "title": title,
                            "author": author,
                            "dir": rel or f"chapters/{cid}",
                            "cover": c.get("cover"),
                            "note": c.get("note") or "",
                        }
                    )
                )
                ids.append(cid)
            nw = dict(w)
            nw["chapters"] = ids
            new_works.append(nw)
        return chapters, new_works

    def _migrate_old_dirs(self) -> bool:
        """把旧布局 ``{作者}/{opus_id}_{标题}/`` 搬进 ``chapters/<id>/``（同盘 rename）。"""
        changed = False
        try:
            metas = sorted(self.shelf_root.glob("*/*/meta.json"))
        except Exception:
            return False
        for meta_file in metas:
            d = meta_file.parent
            try:
                rel = d.relative_to(self.shelf_root)
            except Exception:
                continue
            if rel.parts[0] == "chapters":
                continue  # 已经是新布局
            meta = self._read_meta(rel.as_posix())
            opus_id = str(meta.get("opus_id") or "").strip() or None
            cid = opus_chapter_id(opus_id) if opus_id else _new_local_id()
            target = chapter_dir(self.shelf_root, cid)
            if target.exists():
                new_rel = target.relative_to(self.shelf_root).as_posix()
            else:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(d), str(target))
                    new_rel = target.relative_to(self.shelf_root).as_posix()
                    changed = True
                    # 搬走后作者目录通常就空了，顺手删掉（非空时 rmdir 会失败，忽略即可）
                    try:
                        d.parent.rmdir()
                    except OSError:
                        pass
                except Exception:
                    continue
            # 库里已有同 id 的条目就把 dir 指到新位置，没有就补一条
            ch = self.chapter_by_id(cid)
            if ch:
                if ch["dir"] != new_rel:
                    ch["dir"] = new_rel
                    changed = True
            else:
                self.chapters.append(
                    self._norm_chapter(
                        {
                            "id": cid,
                            "opus_id": opus_id,
                            "title": meta.get("title") or "",
                            "author": (meta.get("author") or {}).get("name") or "",
                            "dir": new_rel,
                        }
                    )
                )
                changed = True
            self._meta_cache.pop(rel.as_posix(), None)
        return changed

    def _heal_scan(self) -> bool:
        """磁盘上有 meta.json 但库里没有的章节，补进池（任何中断都能自愈）。"""
        changed = False
        have = {c["dir"] for c in self.chapters}
        try:
            metas = sorted((self.shelf_root / "chapters").glob("*/meta.json"))
        except Exception:
            return False
        for meta_file in metas:
            d = meta_file.parent
            try:
                rel = d.relative_to(self.shelf_root).as_posix()
            except Exception:
                continue
            if rel in have:
                continue
            meta = self._read_meta(rel)
            cid = d.name
            opus_id = str(meta.get("opus_id") or "").strip() or None
            self.chapters.append(
                self._norm_chapter(
                    {
                        "id": cid,
                        "source": "opus" if opus_id else "local",
                        "opus_id": opus_id,
                        "title": meta.get("title") or cid,
                        "author": (meta.get("author") or {}).get("name") or "",
                        "dir": rel,
                    }
                )
            )
            have.add(rel)
            changed = True
        return changed

    def _drop_dangling(self) -> bool:
        """库里有、但磁盘上章节目录已经不在的条目，清掉以免留下一堆打不开的卡片。"""
        alive = []
        changed = False
        for c in self.chapters:
            if (self.shelf_root / c["dir"] / "meta.json").is_file():
                alive.append(c)
            else:
                changed = True
        # 去重：同一个 id 只保留第一条
        seen: set[str] = set()
        uniq = []
        for c in alive:
            if c["id"] in seen:
                changed = True
                continue
            seen.add(c["id"])
            uniq.append(c)
        self.chapters = uniq
        valid = set(seen)
        for w in self.works:
            ids = [i for i in w["chapters"] if i in valid]
            if len(ids) != len(w["chapters"]):
                changed = True
            w["chapters"] = ids
        return changed

    # ---------- 基础查询 ----------

    def chapter_by_id(self, cid: str) -> dict | None:
        for c in self.chapters:
            if c["id"] == cid:
                return c
        return None

    def get_chapter(self, cid: str) -> dict:
        ch = self.chapter_by_id(cid)
        if not ch:
            raise LibraryError(f"章节不存在：{cid}")
        return ch

    def has_opus(self, opus_id: str) -> bool:
        return any(c["opus_id"] == str(opus_id) for c in self.chapters)

    def used_chapter_ids(self) -> set[str]:
        used: set[str] = set()
        for w in self.works:
            used.update(w["chapters"])
        return used

    def _read_meta(self, rel: str) -> dict:
        p = self.shelf_root / rel / "meta.json"
        try:
            key = (rel, p.stat().st_mtime)
            hit = self._meta_cache.get(key)
            if hit is not None:
                return hit
            meta = json.loads(p.read_text(encoding="utf-8"))
            self._meta_cache[key] = meta
            return meta
        except Exception:
            return {}

    def read_chapter_meta(self, cid: str) -> dict:
        return self._read_meta(self.get_chapter(cid)["dir"])

    def _chapter_abs(self, ch: dict) -> Path:
        return self.shelf_root / ch["dir"]

    # ---------- 扫描 ----------

    def scan_chapters(self) -> list[dict]:
        """章节池里全部章节的展示信息（含是否已被某作品收录）。"""
        used = self.used_chapter_ids()
        out: list[dict] = []
        for c in self.chapters:
            meta = self._read_meta(c["dir"])
            images = meta.get("images") or []
            ok = sum(1 for i in images if not i.get("error"))
            out.append(
                {
                    "id": c["id"],
                    "chapter_id": c["id"],
                    "opus_id": c["opus_id"] or "",
                    "source": c["source"],
                    "title": c["title"] or meta.get("title") or c["id"],
                    "author": c["author"] or (meta.get("author") or {}).get("name") or "",
                    "path": c["dir"],
                    "image_count": len(images),
                    "ok_count": ok,
                    "note": c["note"],
                    "assigned": c["id"] in used,
                    "added_at": c["added_at"],
                    "cover_url": self.chapter_cover_url(c),
                }
            )
        # 新的排前面：刚下载/刚导入的章节最可能需要马上归类
        out.sort(key=lambda d: d.get("added_at") or "", reverse=True)
        return out

    def unassigned(self) -> list[dict]:
        return [d for d in self.scan_chapters() if not d["assigned"]]

    def find_incomplete(self) -> list[dict]:
        """chapters/ 下没有 meta.json 的残目录（下载中断留下的）。"""
        out: list[dict] = []
        root = self.shelf_root / "chapters"
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir()):
            if d.is_dir() and not (d / "meta.json").is_file():
                try:
                    n = len([p for p in d.iterdir() if p.is_file()])
                except Exception:
                    n = 0
                out.append(
                    {
                        "id": d.name,
                        "name": d.name,
                        "files": n,
                        "path": d.relative_to(self.shelf_root).as_posix(),
                    }
                )
        return out

    def purge_incomplete(self, names: list[str]) -> int:
        root = self.shelf_root / "chapters"
        n = 0
        for name in names or []:
            d = root / str(name)
            try:
                if d.is_dir() and root in d.parents and not (d / "meta.json").is_file():
                    shutil.rmtree(d)
                    n += 1
            except Exception:
                pass
        return n

    # ---------- 封面解析 ----------

    @staticmethod
    def _first_image_file(meta: dict) -> str | None:
        for img in meta.get("images") or []:
            if img.get("file") and not img.get("error"):
                return img["file"]
        return None

    def _cover_url(self, rel: str) -> str:
        """自定义封面的 URL：尾部带文件修改时间（毫秒）当版本号。

        封面会被反复覆盖（换一张、从视频里截一帧），**路径始终不变** ——
        URL 不变的话 WebView2 就照着缓存直接出图，改完要重启程序才看得到。
        带上 mtime 后每次覆盖都是新 URL，缓存自然失效。
        用毫秒而不是秒：同一秒内连着换两次封面，秒级版本号是不变的。
        """
        u = _url(rel)
        try:
            v = int((self.shelf_root / rel).stat().st_mtime * 1000)
        except Exception:
            return u
        return f"{u}?v={v}" if v else u

    def chapter_cover_url(self, chapter: dict) -> str | None:
        """封面优先级：自定义封面 → 头图（cover.*）→ 正文第一张图。"""
        if chapter.get("cover"):
            return self._cover_url(chapter["cover"])
        meta = self._read_meta(chapter["dir"])
        base = Path(chapter["dir"])
        # meta 里的 cover 正常是 {"file": ...} 或 None；历史/手改的 meta 可能是
        # 别的形状，这里一律宽容处理，不能让整个书架列表因为一条脏数据打不开。
        raw = meta.get("cover")
        if isinstance(raw, str):  # 直接就是相对路径
            return _url(base / raw)
        f = raw.get("file") if isinstance(raw, dict) else None
        if f:
            return _url(base / f)
        f = self._first_image_file(meta)
        if f:
            return _url(base / f)
        return None

    def work_cover_url(self, work: dict) -> str | None:
        """作品封面：自定义封面 → 第一个有封面的章节。"""
        if work.get("cover"):
            return self._cover_url(work["cover"])
        for cid in work.get("chapters", []):
            ch = self.chapter_by_id(cid)
            if not ch:
                continue
            u = self.chapter_cover_url(ch)
            if u:
                return u
        return None

    def chapter_images(self, cid: str) -> list[dict]:
        ch = self.get_chapter(cid)
        meta = self._read_meta(ch["dir"])
        images: list[dict] = []
        for img in meta.get("images") or []:
            if img.get("file") and not img.get("error"):
                images.append(
                    {
                        # file 是删除时的定位依据：下标会在重排后漂移，相对路径不会
                        "file": img["file"],
                        "index": len(images) + 1,
                        "url": _url(Path(ch["dir"]) / img["file"]),
                        "width": img.get("width"),
                        "height": img.get("height"),
                    }
                )
        return images

    def chapter_media(self, cid: str) -> dict | None:
        """视频章节的媒体信息；图片章节一律 None。

        文件被外部删掉时也**照常返回**（url 会 404），让前端 <video> 走 error 分支
        显示「文件丢失」占位卡 —— 这里返回 None 会让前端退回图片分支，那才是错的。
        """
        ch = self.get_chapter(cid)
        if ch.get("source") != "video":
            return None
        meta = self._read_meta(ch["dir"])
        v = meta.get("video") or {}
        f = str(v.get("file") or "").strip()
        if not f:
            return None
        return {
            "file": f,
            "url": _url(Path(ch["dir"]) / f),
            "format": Path(f).suffix.lstrip(".").lower(),
            "size": v.get("size"),
        }

    # ---------- 作品操作 ----------

    def get_work(self, wid: str) -> dict:
        for w in self.works:
            if w["id"] == wid:
                return w
        raise LibraryError("作品不存在")

    def create_work(self, name: str, author: str) -> dict:
        now = _now()
        work = {
            "id": "w" + uuid.uuid4().hex[:8],
            "name": (name or "").strip() or "未命名作品",
            "author": (author or "").strip(),
            "cover": None,
            "chapters": [],
            "created_at": now,
            "updated_at": now,
            "last_read_at": "",
            "tags": [],
        }
        self.works.append(work)
        self.save()
        return work

    def update_work(self, wid: str, patch: dict) -> dict:
        w = self.get_work(wid)
        touched = False
        if "name" in patch:
            w["name"] = (patch["name"] or "").strip() or w["name"]
            touched = True
        if "author" in patch:
            w["author"] = (patch["author"] or "").strip()
            touched = True
        # 打标签是「整理」，不是改内容 —— 与 move_chapter 同理，不刷新 updated_at，
        # 否则给旧作品补几个标签就会把它顶到书架最前。
        if "tags" in patch:
            w["tags"] = _norm_tags(patch.get("tags"))
        if patch.get("clear_cover") and w.get("cover"):
            old = w["cover"]
            w["cover"] = None
            self._drop_cover_if_unused(old)
            touched = True
        if touched:
            w["updated_at"] = _now()
        self.save()
        return w

    def mark_read(self, wid: str) -> dict:
        """记一次「最近阅读」：**只写 last_read_at，不碰 updated_at**。

        阅读不算修改作品内容，不该让它顶掉「最近更新」的语义（整理章序不刷新
        updated_at 是同一个道理）。作品照样会靠前——排序取
        max(updated_at, last_read_at)，读过本身就是一次新动作。
        """
        w = self.get_work(wid)
        w["last_read_at"] = _now()
        self.save()
        return w

    def delete_work(self, wid: str) -> dict:
        w = self.get_work(wid)
        cover = w.get("cover")
        self.works.remove(w)
        self.save()
        if cover:
            self._drop_cover_if_unused(cover)
        return w

    # ---------- 章节操作 ----------

    def add_chapters(self, wid: str, chapter_ids: list[str]) -> dict:
        w = self.get_work(wid)
        have = set(w["chapters"])
        added = False
        for cid in chapter_ids or []:
            cid = str(cid)
            if cid in have or not self.chapter_by_id(cid):
                continue
            w["chapters"].append(cid)
            have.add(cid)
            added = True
        if added:
            # 有新章节入账 = 作品被更新（下载/导入入库也走这里，
            # 刚下完的作品会自然浮到「最近更新」最前面）
            w["updated_at"] = _now()
        self.save()
        return w

    def remove_chapter(self, wid: str, cid: str) -> dict:
        w = self.get_work(wid)
        before = len(w["chapters"])
        w["chapters"] = [i for i in w["chapters"] if i != cid]
        if len(w["chapters"]) != before:
            w["updated_at"] = _now()
        ch = self.chapter_by_id(cid)
        if ch and ch.get("cover") and not self._chapter_in_any_work(cid):
            self._drop_cover_if_unused(ch["cover"])
        self.save()
        return w

    def move_chapter(self, wid: str, cid: str, delta: int) -> dict:
        # 只调作品内部章序，刻意**不**刷新 updated_at：
        # 否则整理页拖一下顺序作品就顶到「最近更新」最前，很烦。
        w = self.get_work(wid)
        idx = next((i for i, x in enumerate(w["chapters"]) if x == cid), None)
        if idx is None:
            return w
        new = idx + int(delta)
        if 0 <= new < len(w["chapters"]):
            w["chapters"].insert(new, w["chapters"].pop(idx))
        self.save()
        return w

    def update_chapter(self, wid: str, cid: str, patch: dict) -> dict:
        w = self.get_work(wid)
        if cid not in w["chapters"]:
            raise LibraryError("该作品里没有这个章节")
        ch = self.get_chapter(cid)
        if "title" in patch:
            ch["title"] = (patch["title"] or "").strip() or ch["title"]
        if "note" in patch:
            ch["note"] = patch["note"] or ""
        if "author" in patch:
            ch["author"] = (patch["author"] or "").strip()
        if patch.get("clear_cover") and ch.get("cover"):
            old = ch["cover"]
            ch["cover"] = None
            self._drop_cover_if_unused(old)
        self.save()
        return w

    def rename_chapter(self, cid: str, title: str, author: str | None = None) -> dict:
        """未入库章节也能改名（不要求它属于某个作品）。"""
        ch = self.get_chapter(cid)
        ch["title"] = (title or "").strip() or ch["title"]
        if author is not None:
            ch["author"] = author.strip()
        self.save()
        return ch

    def delete_chapter(self, cid: str) -> dict:
        """把章节从书库彻底移除（含磁盘文件），并从所有作品里摘掉引用。"""
        ch = self.get_chapter(cid)
        cover = ch.get("cover")
        self.chapters = [c for c in self.chapters if c["id"] != cid]
        for w in self.works:
            if cid in w["chapters"]:
                w["chapters"] = [i for i in w["chapters"] if i != cid]
                w["updated_at"] = _now()
        self.save()
        if cover:
            self._drop_cover_if_unused(cover)
        try:
            d = self._chapter_abs(ch)
            root = self.shelf_root.resolve()
            if root in d.resolve().parents:
                shutil.rmtree(d)
        except Exception:
            pass
        return ch

    def delete_chapter_images(self, cid: str, files: list[str]) -> dict:
        """删除章节内的部分图片（含磁盘文件），剩下的重新编号成 001、002……

        **不可逆**：文件真的从磁盘上删掉，不做回收站也不做撤销。所以三步校验
        （图片确实属于本章 / 解析后不越出章节目录 / 不能删光）全部在动磁盘之前做完，
        任一条不满足就一个文件都不碰。

        重命名用两段式（先换临时名再落终名）：新编号永远不大于旧编号，
        但目录里可能残留同名的杂文件，两段式保证中途不会因为"目标名没腾出来"失败，
        也不会互相覆盖。编号宽度按剩下的张数重算，与下载 / 导入时的命名规则一致。
        """
        ch = self.get_chapter(cid)
        base = self._chapter_abs(ch)
        base_r = base.resolve()
        # 深拷贝：_read_meta 返回的是缓存里的同一个对象，直接改会污染缓存
        meta = json.loads(json.dumps(self._read_meta(ch["dir"])))
        images = list(meta.get("images") or [])

        wanted: list[str] = []
        for f in files or []:
            s = str(f or "")
            if s and s not in wanted:
                wanted.append(s)
        if not wanted:
            raise LibraryError("没有指定要删除的图片")

        known = {str(i.get("file") or "") for i in images if i.get("file")}
        unknown = [f for f in wanted if f not in known]
        if unknown:
            raise LibraryError("这些图片不在该章节里：" + "、".join(unknown[:3]))
        for f in wanted:
            p = (base / f).resolve()
            if base_r not in p.parents:
                raise LibraryError("图片路径不在章节目录内，已取消删除：" + f)

        doomed = set(wanted)
        keep = [i for i in images if str(i.get("file") or "") not in doomed]
        numbered = [i for i in keep if i.get("file")]
        if not numbered:
            raise LibraryError("至少要留一张图片（想清空整章请用「从书库移除」）")

        # 1) 删文件（meta 里的条目也一起摘掉，写在动磁盘之前，顺序无副作用）
        meta["images"] = keep
        for f in wanted:
            p = base / f
            if p.is_file():
                p.unlink()

        # 2) 剩下的重新编号：两段式（旧名 → 临时名 → 终名）
        width = max(3, len(str(len(numbered))))
        renamed: dict[str, str] = {}
        pending: list[tuple[str, str, str]] = []   # (旧名, 临时名, 终名)
        for n, item in enumerate(numbered, 1):
            old = str(item["file"])
            ext = Path(old).suffix
            new = f"images/{n:0{width}d}{ext}"
            renamed[old] = new
            if new == old or not (base / old).is_file():
                continue
            tmp = f"images/__ren_{n}__{ext}"
            (base / old).rename(base / tmp)
            pending.append((old, tmp, new))
        # 终名必须都是空的：它们要么属于刚删掉的图、要么已经让位给更靠前的编号。
        # 「终名还被占着」意味着磁盘状态和 meta 不一致（典型：该删的没删掉），
        # 这时把临时名还原回去再报错收手 —— 绝不能 rename 覆盖掉一张还在用的图，
        # 也不能让 os.rename 抛个 FileExistsError 把章节留在半改完的状态。
        busy = [p[2] for p in pending if (base / p[2]).exists()]
        if busy:
            for old, tmp, _ in pending:
                if (base / tmp).is_file():
                    (base / tmp).rename(base / old)
            raise LibraryError("要写入的文件名还被占用，已取消删除：" + "、".join(busy[:3]))
        for _, tmp, new in pending:
            (base / tmp).rename(base / new)

        # 3) 写回 meta（头图理论上在章节根目录、不在 images/ 里，但万一指进来也要跟着走）
        for n, item in enumerate(numbered, 1):
            item["file"] = renamed[str(item["file"])]
            item["index"] = n
        cover = meta.get("cover")
        if isinstance(cover, dict) and cover.get("file") in renamed:
            cover["file"] = renamed[str(cover["file"])]
        write_meta(base, meta)

        for key in [k for k in self._meta_cache if k[0] == ch["dir"]]:
            self._meta_cache.pop(key, None)
        return {"chapter_id": cid, "removed": len(wanted), "remaining": len(numbered)}

    def _chapter_in_any_work(self, cid: str) -> bool:
        return any(cid in w["chapters"] for w in self.works)

    # ---------- 自定义封面 ----------

    def _cover_still_used(self, rel: str) -> bool:
        for w in self.works:
            if w.get("cover") == rel:
                return True
        for c in self.chapters:
            if c.get("cover") == rel:
                return True
        return False

    def _drop_cover_if_unused(self, rel: str) -> None:
        """删除 _covers 下我们自己复制的那份封面；用户原图不受影响。"""
        if self._cover_still_used(rel):
            return
        try:
            root = self.shelf_root.resolve()
            p = (self.shelf_root / rel).resolve()
            if root in p.parents and p.is_file():
                p.unlink()
        except Exception:
            pass

    def _save_cover_file(self, src: str, name: str) -> str:
        src_path = Path(src)
        if not src_path.is_file():
            # 也接受「相对书架目录」的路径：从章节内容里选封面时，前端手里只有
            # meta 里的相对路径（它在服务端，前端没法拼出绝对路径）。
            # 同时夹一层「必须在书架目录内」，免得相对路径被当成逃出书架的口子。
            alt = self.shelf_root / src
            try:
                root = self.shelf_root.resolve()
                p = alt.resolve()
                inside = root in p.parents
            except Exception:
                p, inside = None, False
            if inside and p is not None and p.is_file():
                src_path = p
            else:
                raise LibraryError("封面文件不存在")
        ext = src_path.suffix.lower()
        if ext not in IMAGE_EXTS:
            raise LibraryError("不支持的图片格式")
        covers_dir = self.shelf_root / "_covers"
        covers_dir.mkdir(parents=True, exist_ok=True)
        target = covers_dir / f"{name}{ext}"
        shutil.copyfile(src_path, target)
        return target.relative_to(self.shelf_root).as_posix()

    def set_work_cover(self, wid: str, src: str) -> str:
        rel = self._save_cover_file(src, f"work_{wid}")
        w = self.get_work(wid)
        w["cover"] = rel
        w["updated_at"] = _now()
        self.save()
        return self._cover_url(rel)

    def set_chapter_cover(self, wid: str, cid: str, src: str) -> str:
        rel = self._save_cover_file(src, f"ch_{cid}")
        w = self.get_work(wid)
        if cid not in w["chapters"]:
            raise LibraryError("该作品里没有这个章节")
        self.get_chapter(cid)["cover"] = rel
        self.save()
        return self._cover_url(rel)

    def save_chapter_cover_data(self, wid: str, cid: str, data_url: str) -> str:
        """把前端 canvas 抓下来的画面（DataURL / base64）存成章节封面。

        与 set_chapter_cover 只差「来源」：那边是磁盘上的文件，这里是内存里的字节。
        落盘位置、命名、返回值都对齐（都不动 updated_at —— 设个封面不该把作品顶到
        书架最前面），前端两条路因此可以共用同一段后续处理。
        """
        raw = (data_url or "").split(",", 1)[-1]
        try:
            data = base64.b64decode(raw or "")
        except Exception:
            raise LibraryError("图像数据解析失败")
        if not data:
            raise LibraryError("未提供图像数据")
        # 顺序有讲究：先把字节写进去，再改索引 —— 写盘失败的话不该留下一条指向空文件的记录
        covers_dir = self.shelf_root / "_covers"
        covers_dir.mkdir(parents=True, exist_ok=True)
        target = covers_dir / f"ch_{cid}.jpg"
        target.write_bytes(data)
        w = self.get_work(wid)
        if cid not in w["chapters"]:
            # 极端情况：写完了才发现章节不在这个作品里。清掉刚写的文件，别留垃圾。
            try:
                target.unlink()
            except Exception:
                pass
            raise LibraryError("该作品里没有这个章节")
        ch = self.get_chapter(cid)
        ch["cover"] = target.relative_to(self.shelf_root).as_posix()
        self.save()
        return self._cover_url(ch["cover"])

    # ---------- 导入 ----------

    def _upsert_from_meta(self, cid: str, rel: str, meta: dict, source: str,
                          opus_id: str | None) -> dict:
        ch = self.chapter_by_id(cid)
        data = {
            "id": cid,
            "source": source,
            "opus_id": opus_id,
            "title": meta.get("title") or cid,
            "author": (meta.get("author") or {}).get("name") or "",
            "dir": rel,
        }
        if ch:
            ch.update({k: v for k, v in data.items() if k != "id"})
            ch.setdefault("added_at", _now())
        else:
            data["cover"] = None
            data["note"] = ""
            data["added_at"] = _now()
            self.chapters.append(self._norm_chapter(data))
            ch = self.chapter_by_id(cid)
        self.save()
        return ch

    def register_chapter(self, cid: str, source: str = "opus",
                         opus_id: str | None = None) -> dict:
        """把已经在 chapters/<cid>/ 里的目录登记进池（单目录模式下载完直接用这个）。"""
        rel = f"chapters/{cid}"
        meta = self._read_meta(rel)
        if not meta:
            raise LibraryError(f"章节目录还没有写完（缺 meta.json）：{rel}")
        return self._upsert_from_meta(cid, rel, meta, source, opus_id)

    def import_opus_dir(self, src_dir: Path, mode: str = "move") -> dict:
        """把下载落点的目录搬（或复制）进书架并登记入池。

        幂等：目标已存在时只补登记，不覆盖已有内容。
        """
        src_dir = Path(src_dir)
        meta_path = src_dir / "meta.json"
        if not meta_path.is_file():
            raise LibraryError(f"不是有效的章节目录（缺 meta.json）：{src_dir}")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            raise LibraryError(f"meta.json 读不出来：{meta_path}")
        opus_id = str(meta.get("opus_id") or "").strip() or None
        cid = opus_chapter_id(opus_id) if opus_id else _new_local_id()
        target = chapter_dir(self.shelf_root, cid)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            if mode == "copy":
                shutil.copytree(src_dir, target)
            else:
                shutil.move(str(src_dir), str(target))
        rel = target.relative_to(self.shelf_root).as_posix()
        self._meta_cache.pop(rel, None)
        return self._upsert_from_meta(cid, rel, meta, "opus", opus_id)

    def import_local_images(
        self,
        title: str,
        author: str,
        paths: list[str],
        source_dir: str | None = None,
    ) -> dict:
        """把一组本地图片复制成一个章节（不改动用户的原图）。"""
        files = [Path(p) for p in paths or [] if p]
        files = [p for p in files if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
        if not files:
            raise LibraryError("没有可用的图片文件")
        files.sort(key=lambda p: natural_key(p.name))

        cid = _new_local_id()
        target = chapter_dir(self.shelf_root, cid)
        images_dir = target / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        width = max(3, len(str(len(files))))

        items: list[dict] = []
        for i, src in enumerate(files, 1):
            ext = src.suffix.lower()
            name = f"{i:0{width}d}{ext}"
            dst = images_dir / name
            shutil.copyfile(src, dst)
            size = dst.stat().st_size
            wh = sniff_image_size(dst)
            items.append(
                {
                    "index": i,
                    "file": f"images/{name}",
                    "url": None,
                    "width": wh[0] if wh else None,
                    "height": wh[1] if wh else None,
                    "size": size,
                }
            )

        meta = build_local_meta(
            title=title or (source_dir and Path(source_dir).name) or "导入章节",
            author=author or "",
            images=items,
            source_dir=source_dir,
            source_paths=[str(p) for p in files],
        )
        write_meta(target, meta)
        rel = target.relative_to(self.shelf_root).as_posix()
        self._meta_cache.pop(rel, None)
        return self._upsert_from_meta(cid, rel, meta, "local", None)

    def import_video_file(self, src: Path, title: str = "", author: str = "",
                          ffmpeg: str | None = None) -> dict:
        """把一个本地视频**复制**成一个视频章节（不改动用户的原文件）。

        与「视频转图集」是并列的两条路：那条抽成一堆图片，这条原样保留一个视频文件。

        封面：有 ffmpeg 就截首帧，但**截帧失败绝不连累入库**（降级成没封面，
        列表里显示占位）；没有 ffmpeg 就直接不截。
        """
        src = Path(src)
        if not is_video(src):
            raise LibraryError(f"不是支持的视频文件：{src.name}")
        if not src.is_file():
            raise LibraryError(f"文件不存在：{src}")

        cid = _new_local_id()
        target = chapter_dir(self.shelf_root, cid)
        target.mkdir(parents=True, exist_ok=True)
        # 保留原扩展名：把 webm 存成 .mp4 会让 Chromium 因为容器不符而拒播
        name = "video" + src.suffix.lower()
        dst = target / name
        shutil.copyfile(src, dst)

        cover_file = None
        if ffmpeg:
            try:
                extract_thumb(ffmpeg, str(src), target / "cover.jpg")
                cover_file = "cover.jpg"
            except (VideoGrabError, OSError):
                cover_file = None

        meta = build_video_meta(
            title=title or src.stem,
            author=author or "",
            video_file=name,
            size=dst.stat().st_size,
            orig_path=str(src),
            cover_file=cover_file,
        )
        write_meta(target, meta)
        rel = target.relative_to(self.shelf_root).as_posix()
        self._meta_cache.pop(rel, None)
        return self._upsert_from_meta(cid, rel, meta, "video", None)

    def scan_pending_imports(self, download_root: Path) -> list[dict]:
        """双目录模式下，暂存目录里还没进书架的已完成章节。"""
        root = Path(download_root)
        out: list[dict] = []
        if not root.is_dir():
            return out
        for meta_file in sorted(root.glob("*/*/meta.json")):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            opus_id = str(meta.get("opus_id") or "").strip()
            if not opus_id or self.has_opus(opus_id):
                continue
            d = meta_file.parent
            images = meta.get("images") or []
            out.append(
                {
                    "opus_id": opus_id,
                    "title": meta.get("title") or "",
                    "author": (meta.get("author") or {}).get("name") or "",
                    "path": str(d),
                    "image_count": len(images),
                    "ok_count": sum(1 for i in images if not i.get("error")),
                }
            )
        return out
