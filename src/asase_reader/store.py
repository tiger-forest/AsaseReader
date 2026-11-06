"""落盘：目录命名、文件名、meta.json。

两套目录布局并存：
- 下载落点（双目录模式下）：``{download_dir}/{作者}/{opus_id}_{标题}/`` —— 见 opus_dir；
- 书架里的章节：``{shelf_dir}/chapters/<chapter_id>/`` —— 见 chapter_dir，扁平、
  与作者/标题/所属作品解耦（章节会被重新归类，路径里带作品信息迟早会被打脸）。

两种来源（B 站下载 / 本地图片导入）写出的 meta.json 是同一套结构，下游的阅读、
封面、编辑逻辑因此不需要区分来源。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from pathvalidate import sanitize_filename

from .models import Opus


def safe_component(name: str, max_len: int = 60) -> str:
    """生成可用的路径片段：去掉非法字符与空白，截断长度。"""
    cleaned = sanitize_filename(name.strip(), replacement_text="_")
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_len]


def image_ext_from_url(url: str) -> str:
    path = url.split("?", 1)[0]
    if "." in path:
        ext = path.rsplit(".", 1)[-1].lower()
        if 2 <= len(ext) <= 5 and ext.isalnum():
            return "." + ext
    return ".jpg"


def opus_dir(root: Path, opus: Opus) -> Path:
    """下载落点（仅双目录模式使用）：{root}/{作者}/{opus_id}_{标题}/。"""
    author = safe_component(opus.author_name or "未知作者")
    name = safe_component(f"{opus.opus_id}_{opus.title}")
    return root / author / name


def chapter_dir(root: Path, chapter_id: str) -> Path:
    """书架里一个章节的存放位置：扁平布局，不掺作者/标题/所属作品。"""
    return Path(root) / "chapters" / safe_component(chapter_id, max_len=80)


def natural_key(name: str) -> list:
    """自然排序键：让 2.jpg 排在 10.jpg 前面（纯字符串排序会反过来）。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(name))]


def sniff_image_size(path: Path) -> tuple[int, int] | None:
    """只读文件头拿宽高，读不出来返回 None。

    不引第三方依赖：手动解析 JPEG / PNG / GIF / WebP 的头信息。宽高用于阅读器
    判断长图（高宽比 > 1.8），读不出来时前端按普通图处理，不影响阅读。
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(64 * 1024)
    except OSError:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data[:3] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_size(data)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # 无长度字段的标记：SOI/EOI/TEM/RSTn
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        if seglen < 2:
            return None
        # SOF0~SOF15 里排除 DHT(0xC4) / JPG(0xC8) / DAC(0xCC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (
                int.from_bytes(data[i + 7 : i + 9], "big"),
                int.from_bytes(data[i + 5 : i + 7], "big"),
            )
        i += 2 + seglen
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    fourcc = data[12:16]
    if fourcc == b"VP8X" and len(data) >= 30:
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    if fourcc == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if fourcc == b"VP8 ":
        i = data.find(b"\x9d\x01\x2a", 20, 80)
        if i != -1 and len(data) >= i + 11:
            return (
                int.from_bytes(data[i + 3 : i + 5], "little") & 0x3FFF,
                int.from_bytes(data[i + 5 : i + 7], "little") & 0x3FFF,
            )
    return None


def build_meta(opus: Opus, include_cover: bool, cover_file: str | None) -> dict:
    return {
        "source": "opus",
        "opus_id": opus.opus_id,
        "source_url": opus.source_url,
        "title": opus.title,
        "author": {"mid": opus.author_mid, "name": opus.author_name},
        "published_at": opus.published_at,
        "published_text": opus.published_text,
        "downloaded_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "include_cover": include_cover,
        "cover": (
            {
                "file": cover_file,
                "url": opus.cover.url,
                "width": opus.cover.width,
                "height": opus.cover.height,
            }
            if cover_file
            else None
        ),
        "images": [img.to_meta() for img in opus.images],
    }


def build_video_meta(
    title: str,
    author: str,
    video_file: str,
    size: int,
    orig_path: str | None = None,
    cover_file: str | None = None,
) -> dict:
    """视频章节的 meta.json。

    刻意**不存 duration / width / height**：那三个要 ffprobe 才拿得到，而播放时
    `<video>` 自己就知道，为一个用不上的字段引一个新依赖不值。
    `images` 恒为空 —— 视频章节没有图片流，`chapter_media` 才是它的内容入口。
    """
    return {
        "source": "video",
        "opus_id": None,
        "source_url": None,
        "title": title or "",
        "author": {"mid": None, "name": author or ""},
        "published_at": None,
        "published_text": "",
        "imported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "include_cover": False,
        "cover": {"file": cover_file} if cover_file else None,
        "images": [],
        "video": {
            "file": video_file,
            "size": size,
            "orig_path": orig_path,
        },
    }


def build_local_meta(
    title: str,
    author: str,
    images: list[dict],
    source_dir: str | None = None,
    source_paths: list[str] | None = None,
) -> dict:
    """本地图片导入生成的 meta.json，结构与下载章节保持一致。

    images 元素形如 {index, file, width, height, size}；保留原始路径只是留痕，
    供日后排查，不会被程序回读。
    """
    return {
        "source": "local",
        "opus_id": None,
        "source_url": None,
        "title": title or "",
        "author": {"mid": None, "name": author or ""},
        "published_at": None,
        "published_text": "",
        "imported_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "include_cover": False,
        "cover": None,
        "images": images,
        "source_dir": source_dir,
        "source_paths": list(source_paths or []),
    }


def write_meta(dir_path: Path, meta: dict) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / "meta.json"
    tmp = dir_path / "meta.json.tmp"
    tmp.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(target)
