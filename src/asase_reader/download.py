"""下载单篇 opus 的全部图片并归档。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .control import Control, OperationCancelled
from .errors import BiliOpusError
from .fetch import Fetcher
from .models import ImageItem, Opus
from .store import build_meta, image_ext_from_url, write_meta

_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
]


def sniff_ext(data: bytes) -> str | None:
    """校验图片魔数并返回真实扩展名；无法识别返回 None（视为下载到非图片内容）。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return "." + ext
    return None


class DownloadStats:
    def __init__(self) -> None:
        self.total = 0
        self.downloaded = 0
        self.skipped = 0
        self.reused = 0
        self.failed = 0
        self.bytes = 0


def _is_complete(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


async def download_opus(
    fetcher: Fetcher,
    opus: Opus,
    target_dir: Path,
    include_cover: bool = True,
    overwrite: bool = False,
    log=None,
    control: Control | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> tuple[Path, DownloadStats]:
    """下载一篇 opus 到 target_dir。返回 (归档目录, 统计)。

    target_dir 由调用方决定：单目录模式下就是书架里的 chapters/<chapter_id>/，
    双目录模式下是下载暂存目录下的 {作者}/{opus_id}_{标题}/（之后再由导入搬进书架）。

    - 断点续传：已存在且完整的文件直接跳过；
    - 同一篇内重复 URL 只下载一次，后续条目复用已下载文件（保留阅读顺序）；
    - control：协作式暂停/取消令牌，在每张图片之间检查；
    - on_event：结构化进度事件（type: cover/image/done），供 GUI 消费。
    """
    log = log or (lambda *a, **k: None)
    emit = on_event or (lambda e: None)
    stats = DownloadStats()
    target_dir = Path(target_dir)
    images_dir = target_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    width = max(3, len(str(len(opus.images))))
    stats.total = len(opus.images)

    url_to_file: dict[str, str] = {}  # 同篇内 URL 去重 -> 复用文件

    # ---- 头图（cover.jpg，不进主图序列） ----
    cover_file: str | None = None
    if include_cover and opus.cover and opus.cover.url:
        if control:
            await control.checkpoint()
        cpath = target_dir / ("cover" + image_ext_from_url(opus.cover.url))
        # 落盘时用的是 sniff 出来的真实扩展名，可能和 URL 里的后缀不一样
        # （B 站很常见）。所以判断"已下过"必须扫 cover.*，不能只按 URL 后缀拼
        # 一个路径去查——否则每次重跑都会把头图再下一遍。
        existing_cover = (
            None if overwrite
            else next((p for p in sorted(target_dir.glob("cover.*")) if _is_complete(p)), None)
        )
        if existing_cover is not None:
            cpath = existing_cover
            stats.skipped += 1
        else:
            try:
                data = await fetcher.fetch_image(opus.cover.url)
                real_ext = sniff_ext(data)
                if not real_ext:
                    raise BiliOpusError("内容不是有效图片")
                if real_ext != cpath.suffix:
                    cpath = cpath.with_suffix(real_ext)
                cpath.write_bytes(data)
                stats.downloaded += 1
                stats.bytes += len(data)
            except BiliOpusError as e:
                opus.cover.error = str(e)
                log(f"  [warn] 头图下载失败: {e}")
        cover_file = cpath.name if cpath.exists() else None

    # ---- 正文图片 ----
    for img in opus.images:
        if control:
            await control.checkpoint()

        existing = url_to_file.get(img.url)
        if existing:
            img.file = existing
            stats.reused += 1
            emit({"type": "image", "index": img.index, "total": len(opus.images),
                  "ok": True, "reused": True, "name": existing})
            continue

        name = f"{img.index:0{width}d}{image_ext_from_url(img.url)}"
        path = images_dir / name
        if not overwrite and not _is_complete(path):
            # 落盘用的是 sniff 出来的真实扩展名，可能和 URL 后缀不一致（B 站常见，
            # 比如 URL 是 .webp 但内容是 jpg）。兜底按编号找同名的任意图片文件，
            # 否则每次重跑都会把这张图再下一遍。
            alt = next(
                (p for p in sorted(images_dir.glob(path.stem + ".*")) if _is_complete(p)),
                None,
            )
            if alt is not None:
                path = alt
        if not overwrite and _is_complete(path):
            img.file = f"images/{path.name}"
            img.size = path.stat().st_size
            url_to_file[img.url] = img.file
            stats.skipped += 1
            continue

        try:
            data = await fetcher.fetch_image(img.url)
            real_ext = sniff_ext(data)
            if not real_ext:
                raise BiliOpusError("内容不是有效图片（可能已被删除）")
            if real_ext != path.suffix:
                path = path.with_suffix(real_ext)
                name = path.name
            path.write_bytes(data)
            img.file = f"images/{name}"
            img.size = len(data)
            url_to_file[img.url] = img.file
            stats.downloaded += 1
            stats.bytes += len(data)
            log(f"  [{img.index}/{len(opus.images)}] {name} ({len(data) // 1024} KB)")
            emit({"type": "image", "index": img.index, "total": len(opus.images),
                  "ok": True, "reused": False, "name": name, "size": len(data)})
        except BiliOpusError as e:
            img.error = str(e)
            stats.failed += 1
            log(f"  [{img.index}/{len(opus.images)}] 失败: {e}")
            emit({"type": "image", "index": img.index, "total": len(opus.images),
                  "ok": False, "error": str(e)})

    # 清理空目录残留的旧图（overwrite 时）：仅当本篇下载全部成功后校验
    meta = build_meta(opus, include_cover, cover_file)
    write_meta(target_dir, meta)
    emit({"type": "done", "stats": {
        "total": stats.total, "downloaded": stats.downloaded,
        "skipped": stats.skipped, "reused": stats.reused,
        "failed": stats.failed, "bytes": stats.bytes,
    }, "dir": str(target_dir)})
    return target_dir, stats


def opus_is_complete(target_dir: Path, opus: Opus, include_cover: bool) -> bool:
    """判断某篇是否已在 target_dir 完整下载（用于批量时跳过）。"""
    d = Path(target_dir)
    if not (d / "meta.json").is_file():
        return False
    meta_path = d / "meta.json"
    try:
        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    imgs = meta.get("images") or []
    if len(imgs) != len(opus.images):
        return False
    for item in imgs:
        if item.get("error"):
            return False
        f = d / (item.get("file") or "")
        if not f.is_file() or f.stat().st_size == 0:
            return False
    if include_cover and meta.get("cover"):
        cf = d / (meta["cover"].get("file") or "")
        if not cf.is_file() or cf.stat().st_size == 0:
            return False
    return True
