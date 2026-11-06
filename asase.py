"""Asase Reader 打包/开发双用启动器。

- 开发运行: python asase.py --gui
- PyInstaller 打包入口: pyinstaller ... asase.py
"""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).parent / "src"))

from asase_reader.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
