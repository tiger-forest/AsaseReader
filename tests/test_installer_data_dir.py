"""v2 数据目录与首启引导的后端行为断言（§5.5）。

这些断言刻意检查可观测副作用：磁盘树、config.json 字节、静态服务 root、
返回体、API 调用参数与 library.json 内容。仅检查「函数被调用了」不算数。
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_helpers import assert_tmp_guard, ensure_tmp_env  # noqa: E402
import asase_reader.backend as backend  # noqa: E402
from asase_reader import cli  # noqa: E402
from asase_reader.backend import (  # noqa: E402
    DEFAULT_CONFIG,
    Backend,
    _clear_default_shelf_dir_cache,
    default_shelf_dir,
    get_data_dir,
)

TMP = Path(tempfile.mkdtemp(prefix="asase_installer_"))
ensure_tmp_env()
assert_tmp_guard(TMP, "TMP")
assert_tmp_guard(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
DOC_ROOT = TMP / "Docs"
DOC_ROOT.mkdir(parents=True)
DEFAULT_LIBRARY = DOC_ROOT / "AsaseReaderLibrary"
backend._DEFAULT_SHELF_DIR_CACHE = DEFAULT_LIBRARY

MB = 1024 * 1024
GB = 1024 * MB
FREE = SimpleNamespace(free=4 * GB)
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok    " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


class FakeStaticServer:
    def __init__(self, root: Path):
        self.root = root
        self.calls: list[Path] = []

    def set_root(self, root: Path) -> None:
        self.root = root
        self.calls.append(root)


def fresh_backend(name: str, *, setup_done: bool = True) -> Backend:
    state = TMP / name / "state"
    state.mkdir(parents=True, exist_ok=True)
    b = Backend(state_dir=state, config_path=state / "config.json")
    b.config["setup_done"] = setup_done
    return b


def tree(root: Path) -> list[tuple[str, str]]:
    if not root.exists():
        return []
    return sorted(
        (str(p.relative_to(root)), "d" if p.is_dir() else "f")
        for p in root.rglob("*")
    )


print("== 1. 默认配置 / 默认书库 / 双锚点")
check(DEFAULT_CONFIG["shelf_dir"] == "", "shelf_dir 默认留空")
check(DEFAULT_CONFIG["setup_done"] is False
      and type(DEFAULT_CONFIG["setup_done"]) is bool,
      "setup_done 初始是 Python False")

_clear_default_shelf_dir_cache()
with patch.object(backend, "_DEFAULT_SHELF_DIR_CACHE", None), \
     patch.object(ctypes.windll.shell32, "SHGetKnownFolderPath",
                  side_effect=lambda guid, flags, token, out: (
                      setattr(out._obj, "value", str(DOC_ROOT)) or 0
                  ), create=True), \
     patch.object(ctypes.windll.ole32, "CoTaskMemFree",
                  return_value=None, create=True):
    known = default_shelf_dir()
    check(known == DEFAULT_LIBRARY, f"默认书库来自 Known Folder：{known}")
    check(known.is_absolute(), "默认书库是绝对路径")
    check(not known.exists(), "default_shelf_dir() 永不预建目录")
    fallback_backend = fresh_backend("fallback", setup_done=False)
    fallback_state = fallback_backend.get_state()
    check(fallback_backend.shelf_root() == known,
          "空配置时 shelf_root() 回退默认书库，不回退项目 outputs")
    check(fallback_state["shelf_dir"] == str(known),
          "get_state 的 shelf_dir 也是默认书库的绝对路径")
    fallback_backend.library_state()
    check(not known.exists(), "跑一圈只读状态仍不预建默认书库")
backend._DEFAULT_SHELF_DIR_CACHE = DEFAULT_LIBRARY

with patch.object(sys, "frozen", False, create=True):
    check(get_data_dir() == backend.PROJECT_ROOT / ".asase", "源码模式锚定项目 .asase")
with patch.object(sys, "frozen", True, create=True), \
     patch.dict(os.environ, {"LOCALAPPDATA": str(TMP / "LocalAppData")}):
    frozen_dir = TMP / "LocalAppData" / "AsaseReader"
    check(get_data_dir() == frozen_dir, f"frozen 锚定 LOCALAPPDATA：{get_data_dir()}")
    check(backend.DATA_DIR == frozen_dir, "backend.DATA_DIR 动态求值，没有固化")

print("== 2. 严格路径规则（规则 1~11）")
b = fresh_backend("rules")
normal = TMP / "rules" / "good" / "library"
anchor = Path(tempfile.gettempdir()).anchor
with patch.object(backend, "PROJECT_ROOT", TMP / "rules" / "install"), \
     patch.object(backend, "_DEFAULT_SHELF_DIR_CACHE", DEFAULT_LIBRARY), \
     patch("asase_reader.backend.shutil.disk_usage", return_value=FREE):
    r = b.preview_dir(str(normal))
    check(r["ok"] is True, f"普通子目录可通过：{r.get('error')}")
    check(not normal.exists(), "preview_dir 不 mkdir")
    check(b.preview_dir("relative")["ok"] is False, "相对路径被拒")
    unc = b.preview_dir(r"\\server\share\library")
    check(unc["ok"] is False and "网络路径" in unc["error"], "UNC 被拒")

    with patch.object(ctypes.windll.kernel32, "GetDriveTypeW",
                      return_value=4, create=True):
        remote = b.preview_dir(str(normal))
        check(remote["ok"] is False and "网络驱动器" in remote["error"],
              "DRIVE_REMOTE 被拒")
    with patch.object(ctypes.windll.kernel32, "GetDriveTypeW",
                      return_value=2, create=True):
        removable = b.preview_dir(str(normal))
        check(removable["ok"] is False and "本地固定磁盘" in removable["error"],
              "非 DRIVE_FIXED 被拒")

    root_result = b.preview_dir(anchor)
    check(root_result["ok"] is False and "整个磁盘" in root_result["error"],
          f"盘符根被拒：{root_result.get('error')}")

    home_root = TMP / "rules" / "home"
    home_root.mkdir(parents=True, exist_ok=True)
    with patch.object(Path, "home", return_value=home_root):
        home_result = b.preview_dir(str(home_root))
        check(home_result["ok"] is False and "用户主目录" in home_result["error"],
              "用户目录根被拒")
    docs_result = b.preview_dir(str(DOC_ROOT))
    check(docs_result["ok"] is False and "「文档」目录" in docs_result["error"],
          "「文档」Known Folder 根被拒")

    state_child = TMP / "rules" / "state" / "library"
    state_result = b.preview_dir(str(state_child))
    check(state_result["ok"] is False and "程序数据目录" in state_result["error"],
          "state_dir 子目录被拒")
    global_child = TMP / "rules" / "install" / ".asase" / "library"
    global_result = b.preview_dir(str(global_child))
    check(global_result["ok"] is False and "程序数据目录" in global_result["error"],
          "get_data_dir() 子目录被拒")

    install_child = TMP / "rules" / "install" / "outputs"
    install_result = b.preview_dir(str(install_child))
    check(install_result["ok"] is True, "安装目录及子目录不阻断")
    check(any(w["code"] == "install_dir" for w in install_result["warnings"]),
          "安装目录产生 install_dir 结构化警告")

    probe_target = TMP / "rules" / "probe" / "library"
    probe_result = b._validate_path(str(probe_target), skip_probe=False)
    check(probe_result["ok"] is True, "提交路径的真实可写探针通过")
    check(probe_target.is_dir(), "提交路径会建目标目录")
    check(not list(probe_target.glob(".asase_probe_*.tmp")), "探针临时文件已 finally 删除")

    with patch("asase_reader.backend.shutil.disk_usage",
               return_value=SimpleNamespace(free=99 * MB)):
        blocked = b.preview_dir(str(normal))
        check(blocked["ok"] is False and "100 MB" in blocked["error"],
              "A 档 <100MB 阻断")
    with patch("asase_reader.backend.shutil.disk_usage",
               return_value=SimpleNamespace(free=int(1.5 * GB))):
        warned = b.preview_dir(str(normal))
        check(warned["ok"] is True
              and any(w["code"] == "low_space" for w in warned["warnings"]),
              "A 档 <2GB 黄字但不阻断")
    with patch("asase_reader.backend.shutil.disk_usage", return_value=FREE):
        plenty = b.preview_dir(str(normal))
        check(plenty["ok"] is True
              and not any(w["code"] == "low_space" for w in plenty["warnings"]),
              "A 档空间充足无警告")

print("== 3. 先校验后提交：四条失败断言")
b = fresh_backend("commit_failure")
old_shelf = TMP / "commit_failure" / "old"
b.config["shelf_dir"] = str(old_shelf)
b.config["download_dir"] = ""
b._save_config()
config_bytes = b.config_path.read_bytes()
before_cfg = dict(b.config)
server = FakeStaticServer(old_shelf)
b.static_server = server
with patch.object(b, "_notify_state") as notify:
    bad = b.update_config({"shelf_dir": anchor})
check(bad["ok"] is False, "非法目录保存被拒")
check(dict(b.config) == before_cfg, "失败后内存 config 不变")
check(server.calls == [], "失败后 static_server.set_root 未调用")
check(b.config_path.read_bytes() == config_bytes, "失败后 config.json 字节不变")
check(bad["config"]["shelf_dir"] == str(old_shelf)
      and bad["shelf_dir"] == str(old_shelf), "失败返回体不含新目录")
check(notify.call_count == 0, "失败后没有状态推送")

print("== 4. 未确认零写入 + 非目录保存的作用域")
for label, confirmed in (("unconfirmed", False), ("confirmed", True)):
    shelf_disk = TMP / "zero" / label / "shelf_disk"
    staging_disk = TMP / "zero" / label / "staging_disk"
    shelf_disk.mkdir(parents=True, exist_ok=True)
    staging_disk.mkdir(parents=True, exist_ok=True)
    b = fresh_backend(f"zero_{label}", setup_done=confirmed)
    b.config["shelf_dir"] = str(shelf_disk / "library")
    b.config["download_dir"] = str(staging_disk / "stage")
    b._save_config()
    cfg_before = json.loads(b.config_path.read_text(encoding="utf-8"))
    shelf_before, staging_before = tree(shelf_disk), tree(staging_disk)
    b.get_state()
    b.library_state()
    b.update_config({"concurrency": 1})
    if not confirmed:
        b.add_urls("https://www.bilibili.com/opus/1261150454223470609")
    cfg_after = json.loads(b.config_path.read_text(encoding="utf-8"))
    check(tree(shelf_disk) == shelf_before, f"{label}：书架盘零写入")
    check(tree(staging_disk) == staging_before, f"{label}：暂存盘零写入")
    check(not list(shelf_disk.rglob(".asase_probe_*.tmp")),
          f"{label}：书架盘没有探针文件")
    check(cfg_after["shelf_dir"] == cfg_before["shelf_dir"]
          and cfg_after["download_dir"] == cfg_before["download_dir"],
          f"{label}：config.json 的目录值不变（DATA_DIR 的 config.json 本身允许写）")

print("== 5. setup_done 显式 bool 与幂等")
b = fresh_backend("bool", setup_done=False)
r1 = b.update_config({"setup_done": 1})
check(r1["ok"] is True and type(b.config["setup_done"]) is bool
      and b.config["setup_done"] is True, "setup_done=1 被显式转成 bool True")
r2 = b.update_config({"setup_done": True})
check(r2["ok"] is True and b.config["setup_done"] is True,
      "重复确认保持幂等")

print("== 6. preview_dir 与 update_config 共用校验但探针开关相反")
b = fresh_backend("probe_switch", setup_done=False)
target = TMP / "probe_switch" / "target" / "library"
with patch.object(b, "_validate_path", wraps=b._validate_path) as spy, \
     patch("asase_reader.backend.shutil.disk_usage", return_value=FREE):
    pre = b.preview_dir(str(target))
    check(pre["ok"] is True and not target.exists(),
          "preview_dir 不落盘、不 mkdir")
    saved = b.update_config({"shelf_dir": str(target), "setup_done": True})
    check(saved["ok"] is True, f"首启确认保存成功：{saved.get('error')}")
    modes = [call.kwargs.get("skip_probe", False) for call in spy.call_args_list]
    check(modes[:2] == [True, False],
          f"preview 跳过探针、提交使用默认探针：{modes}")

print("== 7. 空间 B 档：视频开始前 <50MB 拒绝")
b = fresh_backend("space_b", setup_done=True)
b.config["shelf_dir"] = str(TMP / "space_b" / "shelf")
b._video_state()["phase"] = "confirm"
with patch("asase_reader.backend.resolve_ffmpeg", return_value="ffmpeg"), \
     patch("asase_reader.backend.shutil.disk_usage",
           return_value=SimpleNamespace(free=49 * MB)):
    space_b = b.video_import_confirm()
check(space_b["ok"] is False and "50 MB" in space_b["error"],
      "视频开始前 50MB 防线拒绝转换")
check(b._video_state()["phase"] == "confirm", "空间不足时不启动抽帧线程")

print("== 8. get_state 三件值在锁内一次取齐")
b = fresh_backend("snapshot", setup_done=True)
b.config["shelf_dir"] = str(TMP / "snapshot" / "library")
b.config["download_dir"] = str(TMP / "snapshot" / "stage")
inside = []
orig_display = b.shelf_dir_display


def locked_display() -> str:
    inside.append(b.lock._is_owned())
    return orig_display()


b.shelf_dir_display = locked_display
state = b.get_state()
check(inside == [True], "shelf_dir_display 在锁内调用")
check(state["config"] is not b.config, "config 是快照，不是活引用")
check(state["config"]["shelf_dir"] == str(TMP / "snapshot" / "library")
      and state["shelf_dir"] == str(b.shelf_root()),
      "config / shelf_dir 同一时刻且互相一致")
check(state["download_dir"] == str(TMP / "snapshot" / "stage"),
      "download_dir 来自同一次快照")

print("== 9. CLI --out 读配置、回退默认书库、绝不指 DATA_DIR")
state_dir = TMP / "cli" / "state"
state_dir.mkdir(parents=True, exist_ok=True)
my_shelf = TMP / "cli" / "my_library"
(state_dir / "config.json").write_text(
    json.dumps({"shelf_dir": str(my_shelf)}, ensure_ascii=False), encoding="utf-8")
args = cli.build_arg_parser().parse_args(
    ["https://www.bilibili.com/opus/1261150454223470609", "--state-dir", str(state_dir)])
cli._fill_defaults(args)
check(Path(args.out) == my_shelf, "CLI --out 优先读 config 的 shelf_dir")
check(Path(args.out) != get_data_dir()
      and get_data_dir() not in Path(args.out).parents,
      "配置里的 --out 不是 DATA_DIR 或其子目录")

empty_state = TMP / "cli" / "empty_state"
empty_state.mkdir(parents=True, exist_ok=True)
default_cli = TMP / "cli" / "default_library"
args2 = cli.build_arg_parser().parse_args(
    ["https://www.bilibili.com/opus/1261150454223470609", "--state-dir", str(empty_state)])
with patch.object(cli, "default_shelf_dir", return_value=default_cli):
    cli._fill_defaults(args2)
check(Path(args2.out) == default_cli, "无配置时 --out 回退 default_shelf_dir()")
check(Path(args2.out) != get_data_dir(), "回退值不指 DATA_DIR")

explicit = TMP / "cli" / "explicit"
args3 = cli.build_arg_parser().parse_args(
    ["https://www.bilibili.com/opus/1261150454223470609",
     "--state-dir", str(empty_state), "--out", str(explicit)])
cli._fill_defaults(args3)
check(Path(args3.out) == explicit, "显式 --out 不被后置回填覆盖")

print("== 10. 确认后当次会话写库生效（M12）")
b = fresh_backend("m12", setup_done=False)
shelf = TMP / "m12" / "shelf"
shelf.mkdir(parents=True, exist_ok=True)
legacy = shelf / "作者" / "opus_777_旧布局"
legacy.mkdir(parents=True, exist_ok=True)
(legacy / "meta.json").write_text(json.dumps({
    "source": "opus", "opus_id": "777", "title": "旧布局章节",
    "author": {"mid": 1, "name": "作者"}, "images": [],
}, ensure_ascii=False), encoding="utf-8")
b.config["shelf_dir"] = str(shelf)
b.library_state()  # 显式缓存 readonly=True 实例；当前 GUI 时序不会自动访问
with patch("asase_reader.backend.shutil.disk_usage", return_value=FREE):
    confirmed = b.update_config({"shelf_dir": str(shelf), "setup_done": True})
check(confirmed["ok"] is True, f"幂等确认成功：{confirmed.get('error')}")
work = b.create_work("确认后的新作品", "作者")
library_file = shelf / "library.json"
check(library_file.is_file(), "确认后当次会话 create_work 真的落盘")
if library_file.is_file():
    check("确认后的新作品" in library_file.read_text(encoding="utf-8"),
          "library.json 含新作品")
check(not legacy.exists() and (shelf / "chapters" / "opus_777").is_dir(),
      "确认后按写权限重建库，migrate 当次补跑")
check(work.get("id"), "create_work 正常返回作品 id")

_clear_default_shelf_dir_cache()
shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILED:", len(fails))
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
