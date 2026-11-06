"""本地静态文件服务：把 GUI 页面与 outputs 下的图片放进同一个 localhost 源。

作用：
- 前端 <img> 直接以 /outputs/... 相对路径加载图片，同源无 CORS 问题；
- 配合原生 loading="lazy" 与预加载实现阅读器懒加载。

约定：构造时的 root 就是「书架目录」（可以是项目根下的 outputs，也可以是用户
自定义的任意绝对路径）；URL 前缀固定为 /outputs/，切换书架目录只需 set_root()。
书架是唯一的内容库，下载暂存目录不需要通过 http 暴露。

安全：只绑定 127.0.0.1；只允许 GET；路径解析后必须仍在书架目录内。

Range：支持 `bytes=` 单段切片（HTTP 206），浏览器的 <video> 靠它拖进度条。
解析不了或多段的 Range 一律**退回 200 全量**（RFC 7233：无法理解的 Range 应当忽略，
而不是报错），只有语法合法但越界的才回 416。
"""

from __future__ import annotations

import http.server
import mimetypes
import re
import shutil
import socket
import threading
from pathlib import Path
from urllib.parse import unquote

# mimetypes 在 Windows 上读注册表，换台机器可能缺项；这些后缀一律硬编码兜底。
# 视频那几个必须与 videograb.VIDEO_EXTS 覆盖同一批（test_staticserver_range.py 守着）。
_EXTRA_TYPES = {
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    # mimetypes 在有的机器上把 .ico 认成 vnd.microsoft.icon，有的根本不认 ——
    # 一律钉成 x-icon（浏览器/Chromium 都吃这个）。
    ".ico": "image/x-icon",
}

_CHUNK = 64 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_UNSATISFIABLE = "unsatisfiable"


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "BiliOpusGUI/0.1"

    def log_message(self, *args) -> None:  # 静默
        pass

    def do_GET(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        root: Path = self.server.root
        gui_html: Path = self.server.gui_html

        if path in ("/", "/index.html"):
            self._serve(gui_html)
            return
        if path == "/favicon.ico":
            # 页面图标（浏览器标签页）。没配就照旧 404 —— 图标缺失不该影响别的资源
            icon_file: Path | None = getattr(self.server, "icon_file", None)
            if icon_file is not None and icon_file.is_file():
                self._serve(icon_file)
            else:
                self.send_error(404)
            return
        if path.startswith("/outputs/"):
            rel = path[len("/outputs/"):]
            # root 就是书架目录本身（可以是任意绝对路径），/outputs/ 只是 URL 前缀
            base = root.resolve()
            try:
                target = (base / rel).resolve()
            except Exception:
                self.send_error(404)
                return
            if base not in target.parents or not target.is_file():
                self.send_error(404)
                return
            self._serve(target)
            return
        self.send_error(404)

    def _resolve_range(self, size: int):
        """把 Range 头解析成要发送的字节区间。

        返回三种结果：
        - None：没有 Range，或 Range 无效 / 多段 —— 按 200 全量处理；
        - _UNSATISFIABLE：语法合法但越界（或空文件）—— 回 416；
        - (start, end)：闭区间，已夹在 [0, size-1] 内。
        """
        raw = (self.headers.get("Range") or "").strip()
        if not raw:
            return None
        if "," in raw:  # 多段 Range 不做，退回全量（浏览器基本不发）
            return None
        m = _RANGE_RE.match(raw)
        if not m:
            return None
        s, e = m.group(1), m.group(2)
        if not s and not e:  # "bytes=-"
            return None
        if not s:  # "bytes=-500"：最后 500 字节
            n = int(e)
            if n <= 0:
                return _UNSATISFIABLE
            start, end = max(0, size - n), size - 1
        else:
            start = int(s)
            end = int(e) if e else size - 1
        if size <= 0 or start >= size or start > end:
            return _UNSATISFIABLE
        return start, min(end, size - 1)

    def _serve(self, f: Path) -> None:
        ctype = mimetypes.guess_type(f.name)[0] or _EXTRA_TYPES.get(f.suffix.lower())
        if not ctype:
            ctype = "application/octet-stream"
        try:
            size = f.stat().st_size
            rng = self._resolve_range(size)
            if rng == _UNSATISFIABLE:
                self.send_response(416)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            if rng is None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                # 封面会被反复覆盖（换一张、从视频里截一帧），路径却始终不变 ——
                # 这里不禁强缓存的话，改完封面得重启程序才看得到新图。
                # 其余资源（正文图片）一小时内不会变，缓存照旧：视频章节大、
                # 图片多，每次都回源是白白的开销。
                if f.parent.name == "_covers":
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                else:
                    self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                with open(f, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
                return
            start, end = rng
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                self._copy_exact(fh, end - start + 1)
        except (FileNotFoundError, ConnectionError, BrokenPipeError):
            pass

    def _copy_exact(self, fh, total: int) -> None:
        """只写 total 个字节：多一个字节都会让 <video> 认为响应坏了。"""
        left = total
        while left > 0:
            chunk = fh.read(min(_CHUNK, left))
            if not chunk:
                return
            self.wfile.write(chunk)
            left -= len(chunk)


class StaticServer:
    def __init__(self, root: Path, gui_html: Path,
                 icon_file: Path | None = None) -> None:
        self.root = Path(root)
        self.gui_html = Path(gui_html)
        self.icon_file = Path(icon_file) if icon_file is not None else None
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self.port: int = 0
        self._lock = threading.Lock()

    def set_root(self, root: Path) -> None:
        """运行时切换书架目录（前端 URL 前缀 /outputs/ 不变）。"""
        with self._lock:
            self.root = Path(root)
            if self._httpd is not None:
                self._httpd.root = self.root  # type: ignore[attr-defined]

    def start(self, port: int = 0) -> str:
        """起服务，返回 base URL。

        port 给非 0 时先试这个端口（界面服务要固定端口，否则 localStorage 每次
        换源、界面设置全丢）；被占（OSError）就退回随机端口 —— 起不来比端口漂更糟。
        """
        try:
            self._httpd = http.server.ThreadingHTTPServer(
                ("127.0.0.1", port), _Handler
            )
        except OSError:
            self._httpd = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), _Handler
            )
        self._httpd.root = self.root  # type: ignore[attr-defined]
        self._httpd.gui_html = self.gui_html  # type: ignore[attr-defined]
        self._httpd.icon_file = self.icon_file  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
        return self.base

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
