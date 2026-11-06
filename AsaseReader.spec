# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# 一份 spec、两个分支（方案 plan-installer-package.md §3.3）：
#   ASASE_PACKAGE_MODE=onedir → exe + 依赖目录（安装包用，启动快、少误报）
#   其余                      → 原来的绿色单文件 exe
# datas / hiddenimports / icon / console 只定义一次，杜绝两份 spec 漂移。
ONEDIR = os.environ.get("ASASE_PACKAGE_MODE") == "onedir"
VERSION_FILE = Path(os.environ.get("ASASE_VERSION_FILE", "file_version_info.txt"))

hiddenimports = []
hiddenimports += collect_submodules('webview')


# 两个图标各有分工（详见 gui.py 的注释）：
# - icon.ico     交付的集成包（PNG 条目）→ exe 文件图标，PyInstaller 自己会转成 BMP；
# - icon_win.ico 32bpp BMP 条目副本 → 运行时给 System.Drawing.Icon 读（窗口/任务栏）。
ICON = 'src/asase_reader/assets/icon.ico'
ICON_WIN = 'src/asase_reader/assets/icon_win.ico'

a = Analysis(
    ['asase.py'],
    pathex=['src'],
    binaries=[],
    # 两个图标都要作为**运行时文件**随包带上：窗口/任务栏图标是运行时按路径读的
    # （gui._icon_path），源码运行时读包内那一份。
    datas=[('src/asase_reader/assets/gui.html', 'asase_reader/assets'),
           (ICON, 'asase_reader/assets'),
           (ICON_WIN, 'asase_reader/assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 版本资源由 tools/gen_version_info.py 从 __version__ 生成。缺生成物时跳过，
# 保证 `pyinstaller AsaseReader.spec` 这条老命令在没有跑一键脚本时仍能用。
exe_kwargs = dict(
    name='AsaseReader',
    icon=ICON,          # exe 文件图标（资源管理器 / 快捷方式 / 任务栏）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
if VERSION_FILE.is_file():
    exe_kwargs['version'] = str(VERSION_FILE)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True,
                   upx_exclude=[], name='AsaseReader')
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **exe_kwargs)
