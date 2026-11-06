"""解析 opus 页面：从 HTML 内嵌的 __INITIAL_STATE__ 中提取图片信息。

只提取图片（para_type == 2 的段落 + 头图相册），正文文字一律丢弃。
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from .errors import ParseError
from .models import ImageItem, Opus

OPUS_PATH_RE = re.compile(r"/opus/(\d+)")
_BARE_ID_RE = re.compile(r"^\d{15,25}$")


def extract_opus_id(value: str) -> str:
    """从 URL、纯数字 ID 或一段文本中提取 opus ID。"""
    v = value.strip()
    if not v:
        raise ParseError("空的专栏链接")
    if _BARE_ID_RE.match(v):
        return v
    m = OPUS_PATH_RE.search(v)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{15,25})\b", v)
    if m:
        return m.group(1)
    raise ParseError(f"无法从 {value!r} 中识别出专栏 ID")


def opus_url(opus_id: str) -> str:
    return f"https://www.bilibili.com/opus/{opus_id}"


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return "." + ext if re.fullmatch(r"[a-z0-9]{2,5}", ext) else ".jpg"


def find_preview_ratio(detail: dict) -> int | None:
    """在 detail.modules 里找 module_paywall，取出付费墙放出的百分比。

    返回 None 表示**没有付费墙**（当前身份能看到完整内容）；
    返回 0 表示有付费墙但拿不到比例（仍按预览版处理）。

    这是判断「当前身份是否只有预览权限」的唯一可靠依据 —— 不要试图靠
    检查图片内容（比如全黑图）来猜，作者本来就可能在漫画里画黑页。
    """
    for module in detail.get("modules") or []:
        if not isinstance(module, dict):
            continue
        pw = module.get("module_paywall")
        if not isinstance(pw, dict) or not pw:
            continue
        ratio = pw.get("preview_ratio")
        if isinstance(ratio, bool):
            return 0
        if isinstance(ratio, (int, float)) and 0 < ratio < 100:
            return int(ratio)
        return 0
    return None


def parse_opus_html(html: str, source_url: str = "") -> Opus:
    """解析 opus 页面 HTML，返回只含图片的 Opus 对象。"""
    marker = html.find("__INITIAL_STATE__")
    if marker < 0:
        hint = ""
        if "请求被拦截" in html or "412" in html:
            hint = "（疑似触发风控拦截页）"
        elif "登录" in html:
            hint = "（页面要求登录或内容不可见）"
        raise ParseError("页面中未找到 __INITIAL_STATE__" + hint)

    brace = html.find("{", marker)
    if brace < 0:
        raise ParseError("页面内嵌数据格式异常")
    try:
        state, _ = json.JSONDecoder().raw_decode(html[brace:])
    except json.JSONDecodeError as e:
        raise ParseError(f"内嵌 JSON 解析失败: {e}") from e

    detail = state.get("detail") or {}
    opus_id = str(detail.get("id_str") or state.get("id") or "").strip()
    if not opus_id:
        raise ParseError("内嵌数据中缺少专栏 ID，可能内容不存在或需要登录")

    # modules 是一个列表，每个元素仅在自身类型的 module_* 键上有值
    title = ""
    author_name = ""
    author_mid = None
    published_at = None
    published_text = ""
    cover: ImageItem | None = None
    images: list[ImageItem] = []

    for module in detail.get("modules") or []:
        m_title = module.get("module_title")
        if m_title and not title:
            title = (m_title.get("text") or "").strip()

        m_author = module.get("module_author")
        if m_author:
            author_name = (m_author.get("name") or "").strip()
            author_mid = m_author.get("mid")
            published_text = (m_author.get("pub_time") or "").strip()
            pub_ts = str(m_author.get("pub_ts") or "").strip()
            if pub_ts.isdigit():
                published_at = int(pub_ts)

        m_top = module.get("module_top")
        if m_top and cover is None:
            pics = ((m_top.get("display") or {}).get("album") or {}).get("pics") or []
            if pics:
                p = pics[0]
                cover = ImageItem(
                    index=0,
                    url=_normalize_url(p.get("url", "")),
                    width=p.get("width"),
                    height=p.get("height"),
                )

        m_content = module.get("module_content")
        if m_content:
            for para in m_content.get("paragraphs") or []:
                if para.get("para_type") != 2:  # 只取图片段，文字段直接丢弃
                    continue
                pic = para.get("pic") or {}
                for p in pic.get("pics") or []:
                    url = _normalize_url(p.get("url", ""))
                    if not url:
                        continue
                    images.append(
                        ImageItem(
                            index=len(images) + 1,
                            url=url,
                            width=p.get("width"),
                            height=p.get("height"),
                        )
                    )

    if not title:
        basic_title = (detail.get("basic") or {}).get("title") or ""
        title = re.sub(r"\s*-\s*哔哩哔哩$", "", basic_title).strip() or f"opus_{opus_id}"

    opus = Opus(
        opus_id=opus_id,
        source_url=source_url or opus_url(opus_id),
        title=title,
        author_name=author_name,
        author_mid=author_mid,
        published_at=published_at,
        published_text=published_text,
        cover=cover,
        images=images,
        preview_ratio=find_preview_ratio(detail),
    )
    return opus
