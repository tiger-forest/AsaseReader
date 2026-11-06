"""Library(readonly=True) 的零修复闸门与 NOTE-010 回归。

每个 readonly 用例都配一个可写反向对照：证明夹具本身确实会触发迁移 / 备份 /
清库，避免「什么都没发生」造成的假绿灯（方案 §5.5 / M10、M11、M13）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
from asase_reader.library import Library  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="asase_readonly_"))
ensure_tmp_env()
assert_tmp_guard(TMP, "TMP")

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok    " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def legacy_shelf(name: str) -> tuple[Path, Path]:
    shelf = TMP / name / "shelf"
    old = shelf / "作者" / "opus_1_旧布局"
    write_json(old / "meta.json", {
        "source": "opus", "opus_id": "1", "title": "旧布局章节",
        "author": {"mid": 1, "name": "作者"}, "images": [],
    })
    return shelf, old


def v1_shelf(name: str) -> Path:
    shelf = TMP / name / "shelf"
    write_json(shelf / "library.json", {
        "works": [{
            "id": "w1", "name": "v1 作品", "author": "作者",
            "chapters": [{"opus_id": "1", "path": "chapters/opus_1",
                          "title": "第一章"}],
        }],
    })
    return shelf


def dangling_shelf(name: str) -> Path:
    shelf = TMP / name / "shelf"
    write_json(shelf / "library.json", {
        "version": 2,
        "chapters": [
            {"id": f"opus_{i}", "source": "opus", "opus_id": str(i),
             "title": f"第{i}章", "dir": f"chapters/opus_{i}"}
            for i in range(1, 4)
        ],
        "works": [],
    })
    return shelf


print("== 1. readonly：旧布局目录零搬动（M10）")
shelf_ro, old_ro = legacy_shelf("migrate_ro")
ro = Library(shelf_ro / "library.json", shelf_ro, readonly=True)
check(old_ro.is_dir(), "readonly load 后旧布局目录原位不动")
check(not (shelf_ro / "chapters" / "opus_1").exists(),
      "readonly load 没有搬进 chapters/")
check(not (shelf_ro / "library.json").exists(),
      "readonly load 没有写 library.json")

shelf_rw, old_rw = legacy_shelf("migrate_rw")
rw = Library(shelf_rw / "library.json", shelf_rw, readonly=False)
check(not old_rw.is_dir() and (shelf_rw / "chapters" / "opus_1").is_dir(),
      "反向对照：可写 load 确实会搬动旧布局")

print("== 2. readonly：v1 格式不写 .bak（M11）")
shelf_v1_ro = v1_shelf("v1_ro")
v1_ro = Library(shelf_v1_ro / "library.json", shelf_v1_ro, readonly=True)
check(len(v1_ro.works) == 1, "readonly 仍纯读解析 v1 结构")
check(not (shelf_v1_ro / "library.json.bak").exists(),
      "readonly 的 _from_v1 跳过 _backup")

shelf_v1_rw = v1_shelf("v1_rw")
Library(shelf_v1_rw / "library.json", shelf_v1_rw, readonly=False)
check((shelf_v1_rw / "library.json.bak").is_file(),
      "反向对照：可写 load 确实会生成 .bak")

print("== 3. readonly：_drop_dangling 不清空内存库（M13）")
shelf_d_ro = dangling_shelf("dangling_ro")
raw_before = (shelf_d_ro / "library.json").read_bytes()
d_ro = Library(shelf_d_ro / "library.json", shelf_d_ro, readonly=True)
check(len(d_ro.chapters) == 3, "readonly 保留 3 条无目录章节")
check((shelf_d_ro / "library.json").read_bytes() == raw_before,
      "readonly 不改写 library.json")

shelf_d_rw = dangling_shelf("dangling_rw")
d_rw = Library(shelf_d_rw / "library.json", shelf_d_rw, readonly=False)
check(len(d_rw.chapters) == 0,
      "反向对照：可写 load 确实会清掉 3 条 dangling 章节")

print("== 4. NOTE-010：library.json 根是数组也能启动")
shelf_array = TMP / "array_root" / "shelf"
write_json(shelf_array / "library.json", ["不是对象"])
try:
    arr = Library(shelf_array / "library.json", shelf_array, readonly=True)
    check(arr.works == [] and arr.chapters == [],
          "根数组按空库处理，不抛 AttributeError")
except Exception as e:
    check(False, f"根数组不应让程序起不来：{type(e).__name__}: {e}")

print("== 5. readonly save() 直接短路")
shelf_save = TMP / "save_ro" / "shelf"
save_lib = Library(shelf_save / "library.json", shelf_save, readonly=True)
save_lib.save()
check(not (shelf_save / "library.json").exists(), "readonly save 不写文件")

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
