"""静态服务的 HTTP 206 Range 支持（视频拖进度条的前置改造）。

这个测的是**共用文件服务**，它同时服务图片和视频，所以除了 Range 本身的四种结果，
还必须守住两条零回归：不带 Range 的请求行为完全不变、图片照旧 200 全量。

覆盖：
- 无 Range → 200 + Accept-Ranges（图片走这条）
- 有效切片 → 206 + Content-Range，且**实际字节数 == Content-Length**（多一个字节
  浏览器就认为响应坏了）
- 三种写法：bytes=0-499 / bytes=1000-（到尾） / bytes=-100（尾部 N 字节）
- 越界与 start > end → 416 + `Content-Range: bytes */{size}`
- 无效 / 多段 Range → 退回 200 全量（RFC 7233：无法理解的 Range 应当忽略，不是报错）
- 视频后缀的 MIME 兜底，且与 videograb.VIDEO_EXTS 覆盖同一批
- 路径穿越防护不受影响

跑法（也由 tests/selftest.py 统一调用）：
    python tests/test_staticserver_range.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from asase_reader.staticserver import StaticServer, _EXTRA_TYPES  # noqa: E402
from asase_reader.videograb import VIDEO_EXTS  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def get(base: str, rel: str, rng: str | None = None):
    """发一个 GET，返回 (code, headers, body)；非 2xx 也照样返回不抛。"""
    req = urllib.request.Request(base + rel)
    if rng:
        req.add_header("Range", rng)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="asase_range_"))
    gui_html = ROOT / "src" / "asase_reader" / "assets" / "gui.html"

    # 1KB 的可预测内容：下标即字节值，切片对不对一眼能看出来
    payload = bytes(range(256)) * 4
    check(len(payload) == 1024, "测试样本 1024 字节")
    (tmp / "video.mp4").write_bytes(payload)
    (tmp / "001.jpg").write_bytes(b"\xff\xd8\xff\x00JPG")
    (tmp / "empty.bin").write_bytes(b"")

    server = StaticServer(root=tmp, gui_html=gui_html)
    base = server.start()
    try:
        print("== 1. 无 Range：仍是 200 全量（图片走这条）")
        code, h, body = get(base, "/outputs/001.jpg")
        check(code == 200, f"无 Range → 200（实际 {code}）")
        check(h.get("Accept-Ranges") == "bytes", "带 Accept-Ranges: bytes 声明")
        check(body == b"\xff\xd8\xff\x00JPG", "图片内容完整一致")
        check(h.get("Content-Length") == "7", "Content-Length 正确")

        print("== 2. 有效切片 → 206")
        code, h, body = get(base, "/outputs/video.mp4", "bytes=0-499")
        check(code == 206, f"bytes=0-499 → 206（实际 {code}）")
        check(h.get("Content-Range") == "bytes 0-499/1024",
              f"Content-Range 正确（实际 {h.get('Content-Range')}）")
        check(h.get("Content-Length") == "500", "Content-Length = 500")
        check(len(body) == 500, f"实际字节数 == Content-Length（实际 {len(body)}）")
        check(body == payload[:500], "切片内容与源文件一致")

        code, h, body = get(base, "/outputs/video.mp4", "bytes=1000-")
        check(code == 206 and body == payload[1000:],
              f"bytes=1000- 到文件尾（实际 {len(body)} 字节）")
        check(h.get("Content-Range") == "bytes 1000-1023/1024", "到尾的 Content-Range")

        code, h, body = get(base, "/outputs/video.mp4", "bytes=-100")
        check(code == 206 and body == payload[-100:],
              "bytes=-100 取尾部 100 字节")
        check(h.get("Content-Range") == "bytes 924-1023/1024", "尾部的 Content-Range")

        code, h, body = get(base, "/outputs/video.mp4", "bytes=0-99999")
        check(code == 206 and len(body) == 1024, "end 超出文件大小时夹到末尾")

        print("== 3. 越界 / start>end → 416")
        for rng in ("bytes=99999999-", "bytes=1024-", "bytes=10-5"):
            code, h, _ = get(base, "/outputs/video.mp4", rng)
            check(code == 416, f"{rng} → 416（实际 {code}）")
            check(h.get("Content-Range") == "bytes */1024",
                  f"{rng} 的 Content-Range: bytes */1024（实际 {h.get('Content-Range')}）")

        print("== 4. 无效 / 多段 Range → 退回 200 全量（不报错）")
        for rng in ("bytes=abc", "bytes=", "0-100", "items=0-5",
                    "bytes=0-99,200-299"):
            code, h, body = get(base, "/outputs/video.mp4", rng)
            check(code == 200 and len(body) == 1024,
                  f"Range: {rng!r} 忽略并回全量（实际 {code}）")

        code, _, _ = get(base, "/outputs/empty.bin", "bytes=0-10")
        check(code == 416, f"空文件带 Range 也是 416（实际 {code}）")

        print("== 5. 视频 MIME 兜底")
        for ext, want in ((".mp4", "video/mp4"), (".m4v", "video/mp4"),
                          (".webm", "video/webm"), (".mov", "video/quicktime")):
            (tmp / f"clip{ext}").write_bytes(b"\x00" * 16)
            _, h, _ = get(base, f"/outputs/clip{ext}")
            check((h.get("Content-Type") or "").startswith("video/"),
                  f"{ext} 的 Content-Type 是视频类型（实际 {h.get('Content-Type')}）")
            check(_EXTRA_TYPES.get(ext) == want, f"_EXTRA_TYPES[{ext}] = {want}")

        missing = [e for e in VIDEO_EXTS if e not in _EXTRA_TYPES]
        check(not missing, f"VIDEO_EXTS 每个后缀都有 MIME 兜底（缺 {missing}）")

        print("== 6. 穿越防护不受影响")
        blocked = False
        try:
            urllib.request.urlopen(base + "/outputs/..%2Fpyproject.toml")
        except urllib.error.HTTPError as e:
            blocked = e.code == 404
        except Exception:
            blocked = True
        check(blocked, "路径穿越仍被拦下（404）")
    finally:
        server.stop()

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("FAILED:", len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
