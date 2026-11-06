"""测试环境守卫与 tmp 注入辅助。

守卫规则（§5.5）：
1. 测试中 LOCALAPPDATA / DATA_DIR 必须指向 tmp。
2. 测试中创建的 Backend 其 shelf_root / shelf_dir 必须指向 tmp。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def is_in_tmp(p: Path | str | None) -> bool:
    if not p:
        return False
    try:
        rp = Path(p).resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()
        return tmp_root in rp.parents or "tmp" in str(rp).lower() or "temp" in str(rp).lower()
    except Exception:
        return False


def assert_tmp_guard(path: Path | str | None, name: str = "path") -> None:
    assert is_in_tmp(path), f"守卫失败：{name} ({path}) 必须指向 tmp 目录，不得使用默认宿主或项目根目录！"


def ensure_tmp_env() -> Path:
    """确保 os.environ['LOCALAPPDATA'] 指向测试临时目录，并返回该路径。"""
    localappdata = os.environ.get("LOCALAPPDATA")
    if not is_in_tmp(localappdata):
        tmp_appdata = Path(tempfile.mkdtemp(prefix="asase_test_appdata_"))
        os.environ["LOCALAPPDATA"] = str(tmp_appdata)
        return tmp_appdata
    return Path(localappdata)
