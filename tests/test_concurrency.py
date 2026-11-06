"""并发红线专项测试：上限 2 必须在「代码 / GUI / README」三处一致，且每个入口都夹住。

为什么单独写这一个文件（2026-10-25，v1.14.1）：

README 与三份记忆都写「并发 1~2」，但 cli.py / backend.py 各自写了一个
`max(1, min(x, 4))` 的字面量，GUI 输入框也是 `max="4"`。`git log -S` 查出
这个 4 从 v1.0.1 基线入库那天就在，**从未经过任何决策** —— 不是有意放宽，
是写代码时随手留下的数字，而红线文档是另一套来源。

更糟的是当时**没有任何一处会在配置落盘前夹取**：config.json 里存 3，
GUI 就显示 3、下载也真的按 3 跑 —— 红线可以被实际突破，不只是文档不符。

而 test_cli.py 第 129 行的注释当时明明白白写着「夹在 1~4（红线是 1~2）」，
却断言了**代码的行为**而不是红线的要求 —— 测试在替不一致背书，所以四个月没人发现。

本文件守三件事：
1. clamp_concurrency() 本身的行为（含脏输入，非数字不放行）。
2. Backend 的两道闸：读盘配置会被夹、update_config 保存前会被夹。
3. 三个可见面必须同源：MAX_CONCURRENCY == GUI 的 max == README 的红线数字，
   且源码里不允许再出现绕过 helper 的硬编码夹取。

不发任何网络请求，不写用户目录。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import os  # noqa: E402
from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader.backend import MAX_CONCURRENCY, Backend, clamp_concurrency  # noqa: E402

fails: list[str] = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def backend_with_config(cfg: dict) -> Backend:
    """造一个 Backend，磁盘上的 config.json 是给定的（测试完后不留痕）。"""
    ensure_tmp_env()
    td = Path(tempfile.mkdtemp(prefix="asase_conc_test_"))
    full_cfg = {"setup_done": True, **cfg}
    if not full_cfg.get("shelf_dir") or full_cfg.get("shelf_dir") == "outputs":
        full_cfg["shelf_dir"] = str(td / "outputs")
    (td / "config.json").write_text(json.dumps(full_cfg, ensure_ascii=False), encoding="utf-8")
    assert_tmp_guard(td, "td")
    assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
    b = Backend(state_dir=td)
    assert_tmp_guard(b.shelf_root(), "shelf_root")
    return b


# ------------------------------------------------------------------ 1. 夹取函数

print("== 1. clamp_concurrency：任意输入都落在红线的 1~2")

check(clamp_concurrency(1) == 1, "1 保持 1（下边界不被抬到默认）")
check(clamp_concurrency(2) == 2, "2 保持 2（默认值不被改动）")
check(clamp_concurrency(3) == MAX_CONCURRENCY, f"3 被夹到 {MAX_CONCURRENCY}（红线之外）")
check(clamp_concurrency(4) == MAX_CONCURRENCY, f"4 被夹到 {MAX_CONCURRENCY}（旧代码的宽松上限）")
check(clamp_concurrency(100) == MAX_CONCURRENCY, "100 被夹住")
check(clamp_concurrency(0) == 1, "0 被抬到 1（不能出现并发 0 = 卡死）")
check(clamp_concurrency(-5) == 1, "负数被抬到 1")
check(clamp_concurrency("3") == MAX_CONCURRENCY, "字符串 '3' 也按数字处理")
check(clamp_concurrency("abc") == 2, "非数字回落到 2（而不是直接放行）")
check(clamp_concurrency(None) == 2, "None 回落到 2")
check(clamp_concurrency(2.9) == 2, "浮点向下取整后仍在区间内")
check(1 <= clamp_concurrency(999) <= MAX_CONCURRENCY, "返回值永远落在 1~MAX_CONCURRENCY")


# ------------------------------------------------- 2. 读盘：旧配置/手改配置会被纠偏

print("")
print("== 2. 加载配置：磁盘上的越界值必须被夹回（这条路径以前完全没闸）")

b = backend_with_config({"concurrency": 4, "shelf_dir": "outputs"})
check(b.config["concurrency"] == MAX_CONCURRENCY,
      f"旧配置里的 4 读进来就是 {MAX_CONCURRENCY}（不是等下载时才夹）")

b = backend_with_config({"concurrency": 99})
check(b.config["concurrency"] == MAX_CONCURRENCY, "手改成 99 也会被夹回")

b = backend_with_config({"concurrency": 3})
check(b.config["concurrency"] == MAX_CONCURRENCY, "3 也会被夹回（这正是以前能真跑 3 的场景）")

b = backend_with_config({"concurrency": 2})
check(b.config["concurrency"] == 2, "本来合规的 2 不受影响")

b = backend_with_config({"concurrency": "oops"})
check(b.config["concurrency"] == 2, "脏值回落到 2")

b = backend_with_config({})
check(b.config["concurrency"] == 2, "没有这个键时用默认值 2")


# -------------------------------------------------- 3. 写入：保存前就夹平，界面不说谎

print("")
print("== 3. update_config：界面显示的值必须等于真正生效的值")

td = Path(tempfile.mkdtemp(prefix="asase_conc_test_"))
# 书架不能在 state 目录里面（2.0.0 目录校验规则 7），用兄弟目录
state_dir = td / "state"
shelf_dir = td / "bookshelf"
state_dir.mkdir(exist_ok=True)
shelf_dir.mkdir(exist_ok=True)
b = Backend(state_dir=state_dir)

res = b.update_config({"concurrency": 4, "shelf_dir": str(shelf_dir)})
check(res["config"]["concurrency"] == MAX_CONCURRENCY,
      f"保存 concurrency=4 后 config 里是 {MAX_CONCURRENCY}")
check(b.config["concurrency"] == MAX_CONCURRENCY, "后端状态里也是夹后的值")

res = b.update_config({"concurrency": 99, "shelf_dir": str(shelf_dir)})
check(res["config"]["concurrency"] == MAX_CONCURRENCY, "保存 99 也会被夹")

res = b.update_config({"concurrency": 1, "shelf_dir": str(shelf_dir)})
check(res["config"]["concurrency"] == 1, "保存 1（合规下界）应原样保留")

# 旁路调用 API 也可能传越界值：发请求的人未必看得见 GUI 的 max 属性
res = b.update_config({"concurrency": 3, "shelf_dir": str(shelf_dir)})
check(res["config"]["concurrency"] == MAX_CONCURRENCY, "直接调 API 传 3 也夹回")


# -------------------------------------------- 4. 三个可见面必须同源（防再次漂移）

print("")
print("== 4. 单一来源：常量 == GUI 上限 == README 红线")

gui_html = (ROOT / "src" / "asase_reader" / "assets" / "gui.html").read_text(encoding="utf-8")
m = re.search(r'id="cfg_concurrency"[^>]*max="(\d+)"', gui_html)
check(m is not None, "GUI 的并发输入框存在且有 max 属性")
if m:
    check(int(m.group(1)) == MAX_CONCURRENCY,
          f"GUI 的 max={m.group(1)} 与 MAX_CONCURRENCY={MAX_CONCURRENCY} 一致")

readme = (ROOT / "README.md").read_text(encoding="utf-8")
# 「并发 1~2」与「并发上限 2」两种写法都认：前者是历史表述，后者避开
# Markdown 把成对 `~` 解析成删除线（2026-11-06 公开 README 精简后改用后者）。
m = re.search(r"并发(?:上限\s*|1~\s*)(\d+)", readme)
check(m is not None, "README 里能找到并发上限的红线表述")
if m:
    check(int(m.group(1)) == MAX_CONCURRENCY,
          f"README 写的上限 {m.group(1)} 与 MAX_CONCURRENCY={MAX_CONCURRENCY} 一致")

# 源码里不许再出现绕过 clamp_concurrency 的硬编码夹取。
# 曾经 cli.py / backend.py 各自有一份 min(x, 4)，改一处漏一处就是这么来的。
print("")
print("== 5. 源码里不许再有散落的并发夹取字面量")
src_dir = ROOT / "src" / "asase_reader"
stray: list[str] = []
for py in sorted(src_dir.glob("*.py")):
    text = py.read_text(encoding="utf-8")
    if py.name == "backend.py":
        # backend.py 里 clamp_concurrency 的定义用到 min，属唯一允许处
        text = text.replace("return max(1, min(n, MAX_CONCURRENCY))", "")
    for hit in re.finditer(r"(max|min)\s*\([^()\n]*concurrency[^()\n]*\)", text, re.I):
        line = text[: hit.start()].count("\n") + 1
        stray.append(f"{py.name}:{line}  {hit.group(0)}")
check(not stray, "除 clamp_concurrency 外没有别处在夹取并发"
                 + ("" if not stray else "（发现：" + "; ".join(stray) + "）"))

print()
print("FAILED:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
