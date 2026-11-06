"""前端调用的每个 api().xxx() 都必须在 GuiApi 上存在，且只能是可调用的公开方法。

一旦漏了，运行时的表现就是「xxx is not a function」，而且只在点到那个按钮时才炸。
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader.gui import GuiApi  # noqa: E402
from asase_reader.backend import Backend  # noqa: E402

html = (ROOT / "src/asase_reader/assets/gui.html").read_text(encoding="utf-8")

# "xxx" 来自就绪闸门那段注释里的示例（"任何 api().xxx() 都会抛"），不是真实调用
used = sorted(n for n in set(re.findall(r"api\(\)\.(\w+)", html)) if n != "xxx")
tmp = Path(tempfile.mkdtemp(prefix="asase_api_surface_"))
ensure_tmp_env()
assert_tmp_guard(tmp, "state_dir")
b = Backend(state_dir=tmp / "state")
api = GuiApi(b)

fails = []
print("前端调用的 API 共", len(used), "个")
for name in used:
    attr = getattr(api, name, None)
    if attr is None:
        print(f"  FAIL  {name}: GuiApi 上没有")
        fails.append(name)
    elif not callable(attr):
        print(f"  FAIL  {name}: 不是可调用方法")
        fails.append(name)
    else:
        print(f"  ok    {name}")

# 反向：GuiApi 上的公开属性必须全是方法（否则会被 pywebview 反射遍历，启动卡死）
extra = []
for name in dir(api):
    if name.startswith("_"):
        continue
    attr = getattr(api, name)
    if not callable(attr):
        extra.append(name)
if extra:
    print("  FAIL  GuiApi 上有非方法的公开属性（会被 pywebview 反射遍历）:", extra)
    fails.extend(extra)
else:
    print("  ok    GuiApi 上没有非方法的公开属性")

print()
shutil.rmtree(tmp, ignore_errors=True)
print("FAILED:", len(fails))
sys.exit(1 if fails else 0)
