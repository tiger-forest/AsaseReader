#!/usr/bin/env python3
"""生成 PyInstaller 版本资源与 Inno Setup 版本宏。

版本号**唯一来源**是 ``src/asase_reader/__init__.py`` 的 ``__version__``；
本脚本只读取它，自身绝不内置版本号。产出物不入库，由
``tools/build_release.py`` 在打包前调用。

用法::

    python tools/gen_version_info.py                    # 默认源 + 项目根
    python tools/gen_version_info.py --source P --out-dir D   # 测试夹具可注入

方案见 ``.workbuddy/memory/plan-installer-package.md`` §4.5 / §5.3。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "src" / "asase_reader" / "__init__.py"

PUBLISHER = "浅濑凛 (Asase Rin)"
PRODUCT = "Asase Reader"
DESCRIPTION = "浅濑阅读器 Asase Reader"
COPYRIGHT_YEAR = 2026  # 与 LICENSE 的 Copyright 行保持一致，换年需同步改 LICENSE

VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
NUMERIC_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")


def read_version(source: Path) -> str:
    match = VERSION_RE.search(source.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"找不到 __version__：{source}")
    version = match.group(1)
    if not NUMERIC_RE.match(version):
        raise SystemExit(f"版本号不是纯数字点分格式：{version}")
    return version


def four_tuple(version: str) -> tuple[int, ...]:
    parts = [int(x) for x in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def render_version_info(version: str) -> str:
    """生成 PyInstaller ``version=`` 可读的 VSVersionInfo 文本。"""
    parts = four_tuple(version)
    tuple_text = ", ".join(str(x) for x in parts)
    dotted = ".".join(str(x) for x in parts)
    copyright_text = f"Copyright (c) {COPYRIGHT_YEAR} {PUBLISHER}"
    return f'''# -*- coding: utf-8 -*-
# 由 tools/gen_version_info.py 生成，勿手工编辑。
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({tuple_text}),
    prodvers=({tuple_text}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', '{PUBLISHER}'),
        StringStruct('FileDescription', '{DESCRIPTION}'),
        StringStruct('FileVersion', '{dotted}'),
        StringStruct('InternalName', 'AsaseReader'),
        StringStruct('LegalCopyright', '{copyright_text}'),
        StringStruct('OriginalFilename', 'AsaseReader.exe'),
        StringStruct('ProductName', '{PRODUCT}'),
        StringStruct('ProductVersion', '{dotted}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
'''


def render_iss_macros(version: str) -> str:
    """生成 installer/version.iss，由 AsaseReader.iss 用 #include 引入。"""
    dotted = ".".join(str(x) for x in four_tuple(version))
    copyright_text = f"Copyright (c) {COPYRIGHT_YEAR} {PUBLISHER}"
    return f'''; 由 tools/gen_version_info.py 生成，勿手工编辑。
#define MyAppVersion "{version}"
#define MyAppVersion4 "{dotted}"
#define MyAppPublisher "{PUBLISHER}"
#define MyAppCopyright "{copyright_text}"
#define MyAppProduct "{PRODUCT}"
'''


def generate(source: Path, out_dir: Path) -> tuple[Path, Path]:
    version = read_version(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = out_dir / "file_version_info.txt"
    iss_dir = out_dir / "installer"
    iss_dir.mkdir(parents=True, exist_ok=True)
    macros = iss_dir / "version.iss"
    info.write_text(render_version_info(version), encoding="utf-8")
    macros.write_text(render_iss_macros(version), encoding="utf-8")
    return info, macros


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--out-dir", type=Path, default=ROOT)
    args = ap.parse_args()
    info, macros = generate(args.source, args.out_dir)
    print(f"version: {read_version(args.source)}")
    print(f"wrote: {info}")
    print(f"wrote: {macros}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
