from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ImageItem:
    """一张图片。index 为 1 起的序号，遵循专栏原文顺序。"""

    index: int
    url: str
    width: int | None = None
    height: int | None = None
    file: str | None = None
    size: int | None = None
    error: str | None = None

    def to_meta(self) -> dict:
        data = {
            "index": self.index,
            "file": self.file,
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "size": self.size,
        }
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class Opus:
    """一篇专栏解析后的结果。只保留图片，正文文本一律丢弃。"""

    opus_id: str
    source_url: str
    title: str
    author_name: str = ""
    author_mid: int | None = None
    published_at: int | None = None
    published_text: str = ""
    cover: ImageItem | None = None
    images: list[ImageItem] = field(default_factory=list)
    # 付费墙（充电专属）：preview_ratio 是服务端放出的百分比，
    # 非 None 表示当前身份只能看到预览版（完整内容是它的前缀）。
    preview_ratio: int | None = None

    @property
    def paywalled(self) -> bool:
        """当前身份是否只拿到了预览版。"""
        return self.preview_ratio is not None

    def estimated_total(self) -> int | None:
        """按 preview_ratio 反推完整图片数（只是估算，用于提示"少了多少"）。"""
        if self.preview_ratio is None or not self.images:
            return None
        r = self.preview_ratio
        if not 0 < r < 100:
            return None
        return round(len(self.images) * 100 / r)
