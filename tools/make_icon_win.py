"""由交付的 PNG 生成 .NET（WinForms / System.Drawing）读得动的 ICO。

为什么需要这一道转换：
交付的 `assets/icon.ico` 是 7 尺寸集成包，但**每个条目都是 PNG 压缩的**
（Vista 格式）。PyInstaller 读得动（它嵌入 exe 时会转成 BMP 条目，所以 exe 图标
是对的），`System.Drawing.Icon` 却读不动 —— 实测两种表现：

- `Graphics.DrawIcon` 抛 `请求的范围扩展超过了数组的结尾`（`Icon.BmpFrame()` 里）；
- 多尺寸那份不抛，但画出来是**彩色噪点**。

窗口 / 任务栏图标走的就是 `System.Drawing.Icon`（pywebview 的 winforms 分支：
`self.Icon = Icon(_state['icon'])`），所以必须给它 **BMP 条目**的 ICO。

怎么转（踩过的两条死路写在后面，别再试）：
按尺寸把 PNG 画进一张 `Format32bppArgb` 的位图，`LockBits` 取出 BGRA 像素，
自己写 BITMAPINFOHEADER（高度写 2 倍：图像 + AND 掩码）+ 逐行**倒序**的像素
+ 全零掩码，最后拼成多尺寸 ICO。

- 死路一：`Icon.FromHandle(Bitmap.GetHicon())` 再 `Save()` —— 存出来是 **4bpp
  （16 色）**，画质被压坏（256 那档只有 40KB，32bpp 应该 270KB）。
- 死路二：直接用交付的 PNG 条目 ICO —— 就是上面那个噪点问题。

用法：
    python tools/make_icon_win.py            # 生成 assets/icon_win.ico
    python tools/make_icon_win.py --check    # 只校验现有产物

只依赖 pythonnet（clr）+ System.Drawing，不需要 Pillow。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "asase_reader" / "assets"
OUT = ASSETS / "icon_win.ico"

# 与交付包里的尺寸对齐：任务栏 16/24/32，Alt-Tab 与资源管理器大图 64/128/256
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _dib(png: Path, size: int) -> bytes:
    """把一张 PNG 变成 ICO 用的 BMP 条目（BITMAPINFOHEADER + BGRA + AND 掩码）。"""
    import clr

    clr.AddReference("System.Drawing")
    from System import Array, Byte
    from System.Drawing import Bitmap, Graphics, Rectangle
    from System.Drawing.Imaging import ImageLockMode, PixelFormat
    from System.Runtime.InteropServices import Marshal

    src = Bitmap(str(png))
    if src.Width != size or src.Height != size:
        raise SystemExit(f"{png.name} 是 {src.Width}x{src.Height}，应为 {size}x{size}")

    # 统一画进 32bpp 位图：PNG 可能没有 alpha 通道，那样 LockBits 出来是 24bpp
    bmp = Bitmap(size, size, PixelFormat.Format32bppArgb)
    g = Graphics.FromImage(bmp)
    g.DrawImage(src, Rectangle(0, 0, size, size))
    g.Dispose()

    dat = bmp.LockBits(Rectangle(0, 0, size, size), ImageLockMode.ReadOnly,
                       PixelFormat.Format32bppArgb)
    try:
        stride = abs(dat.Stride)
        nbytes = stride * size
        # Marshal.Copy 的托管侧必须是真正的 byte[]，传 ctypes 数组会访问违规
        arr = Array.CreateInstance(Byte, nbytes)
        Marshal.Copy(dat.Scan0, arr, 0, nbytes)
        raw = bytes(arr)
    finally:
        bmp.UnlockBits(dat)

    # LockBits 是**自上而下**，ICO 的 DIB 要**自下而上**
    rows = [raw[i * stride:(i + 1) * stride][:size * 4] for i in range(size)]
    pixels = b"".join(reversed(rows))

    mask_row = ((size + 31) // 32) * 4          # 1bpp，按 4 字节对齐
    mask = b"\x00" * (mask_row * size)          # 有 alpha 就全零即可

    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(pixels) + len(mask), 0, 0, 0, 0)
    return header + pixels + mask


def build() -> Path:
    import clr  # noqa: F401  提前失败，别等到写了一半

    frames = [(s, _dib(ASSETS / f"icon_{s}.png", s)) for s in SIZES]
    for s, payload in frames:
        print(f"  {s}x{s}  32bpp  {len(payload) // 1024}KB")

    n = len(frames)
    out = bytearray(struct.pack("<HHH", 0, 1, n))
    offset = 6 + 16 * n
    for s, payload in frames:
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                           len(payload), offset)
        offset += len(payload)
    for _s, payload in frames:
        out += payload
    OUT.write_bytes(bytes(out))
    print(f"已生成 {OUT.name}（{len(out) // 1024}KB，{n} 个 32bpp BMP 条目）")
    return OUT


def check(path: Path = OUT) -> bool:
    """产物校验：条目都是 32bpp BMP，且 .NET 能读。"""
    import clr

    clr.AddReference("System.Drawing")
    from System.Drawing import Icon

    b = path.read_bytes()
    _res, typ, count = struct.unpack("<HHH", b[:6])
    kinds = []
    for i in range(count):
        off = 6 + i * 16
        w, h, _cc, _r, _pl, bpp, nbytes, ioff = struct.unpack(
            "<BBBBHHII", b[off:off + 16])
        payload = b[ioff:ioff + nbytes]
        is_png = payload[:8] == b"\x89PNG\r\n\x1a\n"
        kinds.append((w or 256, bpp, "PNG" if is_png else "BMP"))
    print(f"  {path.name}: type={typ} 条目 {count} 个 {kinds}")
    ic = Icon(str(path))
    print(f"  System.Drawing.Icon 读到了：{ic.Width}x{ic.Height}")
    return count > 0 and all(k[1] == 32 and k[2] == "BMP" for k in kinds)


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if check() else 1)
    build()
    ok = check()
    print("校验通过" if ok else "校验失败：还有 PNG 条目")
    sys.exit(0 if ok else 1)
