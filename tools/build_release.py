#!/usr/bin/env python3
"""浅濑阅读器一键出包（方案 plan-installer-package.md §3 / §5.4 / §10 CP4）。

链路：自检 → 版本资源 → PyInstaller 两轨 → 安装包 → SHA256 → BUILD_INFO → 新鲜度硬校验。

用法::

    python tools/build_release.py --check     # 只体检 + 打印计划，不写文件、不打包
    python tools/build_release.py --build     # 真出包（ISCC 缺位会明确失败，不假绿灯）
    python tools/build_release.py --snapshot  # 生成/同步本地公开镜像仓库（CP5）

``--snapshot`` 只产出**本地**镜像仓库，供用户用 GitHub Desktop 发布；
**推 GitHub 与发 Release 不在本脚本**，本仓库永不设 remote。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from gen_version_info import read_version  # noqa: E402  唯一来源，不重复解析

SPEC = ROOT / "AsaseReader.spec"
ISS = ROOT / "installer" / "AsaseReader.iss"
GEN = ROOT / "tools" / "gen_version_info.py"
SELFTEST = ROOT / "tests" / "selftest.py"
SRC = ROOT / "src"
GUI_HTML = ROOT / "src" / "asase_reader" / "assets" / "gui.html"
PYINSTALLER = Path(sys.executable).with_name("pyinstaller.exe")
DIST = ROOT / "dist"
RELEASE = DIST / "release"

# ---- 公开快照（CP5，方案 §5.2 长期镜像仓库）----
SNAPSHOT_ROOT = ROOT / "build" / "public_snapshot"
SNAPSHOT = SNAPSHOT_ROOT / "AsaseReader"
ORIGIN_FILE = SNAPSHOT_ROOT / "origin.txt"
# `.git` 与 origin.txt 都在被忽略的 build/ 里，清 build 会一起没；
# 常量兜底跟着代码入库，保证「镜像仓库丢了还能 clone 回来」。
SNAPSHOT_ORIGIN_FALLBACK = "https://github.com/tiger-forest/AsaseReader.git"
BACKUP_KEEP = 2  # snapshot_prev_* / onedir_prev_* 各保留最近几份
SCAN = ROOT / "tools" / "scan_public_snapshot.py"
SNAPSHOT_AUTHOR = "AsaseRin"
SNAPSHOT_EMAIL = "79520005+tiger-forest@users.noreply.github.com"

# 白名单：只认顶层这些名字，其余（.workbuddy/ / PROGRESS.md / 产物 / 数据）一律不带
SNAPSHOT_ALLOW_TOP = frozenset({
    ".gitattributes", ".gitignore", "AsaseReader.spec", "CHANGELOG.md",
    "README.md", "LICENSE", "asase.py", "launch_gui.bat",
    "pyproject.toml", "urls.example.txt",
    "src", "tests", "tools", "installer",
})
# 明确拍板「不公开」的入库顶层项。白名单 + 本集合必须覆盖 git ls-files 的
# 全部顶层名，否则新增文件会被静默漏出公开快照（P0-1）。
SNAPSHOT_KNOWN_EXCLUDED = frozenset({".workbuddy", "PROGRESS.md"})
SNAPSHOT_SKIP_NAMES = frozenset({
    "node_modules", "__pycache__", ".pytest_cache", ".git",
})
SNAPSHOT_SKIP_SUFFIX = (".pyc", ".pyo", ".bak", ".log", ".exe", ".dll", ".pyd")
SNAPSHOT_SKIP_FILES = frozenset({"version.iss"})

ISCC_CANDIDATES = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Inno Setup 6" / "ISCC.exe",
)


def newest_source_mtime() -> float:
    """src/** 与 gui.html 的最新修改时间 —— 新鲜度纪律只认这个，不认提交信息。"""
    files = [p for p in SRC.rglob("*")
             if p.is_file() and "__pycache__" not in p.parts]
    files.append(GUI_HTML)
    return max(p.stat().st_mtime for p in files)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registry_iscc() -> Path | None:
    """从卸载注册表读 InstallLocation —— 支持装到非系统盘（本机是 F 盘）。

    不把本机盘符硬编码进仓库；找不到就返回 None，由调用方明确报错。
    """
    try:
        import winreg
    except ImportError:
        return None
    bases = (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, base in bases:
        try:
            root = winreg.OpenKey(hive, base)
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
                except OSError:
                    continue
                try:
                    name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                    if "Inno Setup" not in name:
                        continue
                    loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
                    candidate = Path(loc) / "ISCC.exe"
                    if candidate.is_file():
                        return candidate
                except OSError:
                    continue
                finally:
                    winreg.CloseKey(sub)
        finally:
            winreg.CloseKey(root)
    return None


def find_iscc() -> Path | None:
    override = os.environ.get("ASASE_ISCC")
    if override:
        path = Path(override)
        if path.is_file():
            return path
    found = shutil.which("ISCC.exe")
    if found:
        return Path(found)
    for candidate in ISCC_CANDIDATES:
        if candidate.is_file():
            return candidate
    return _registry_iscc()


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True, encoding="utf-8").strip()
    except Exception:
        return "unknown"


def whitelist_gaps(root: Path = ROOT) -> list[str]:
    """P0-1：入库的顶层名必须要么在白名单、要么在明确排除集。

    否则以后新增 docs/、新的根文件等会被 ``_copy_whitelisted`` 静默跳过，
    而扫描器只看快照里已有的文件，照样 SCAN OK —— 线上悄悄缺文件。
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "ls-files"],
            text=True, encoding="utf-8")
    except Exception:
        return []
    tops = {ln.split("/", 1)[0] for ln in out.splitlines() if ln.strip()}
    return sorted(tops - SNAPSHOT_ALLOW_TOP - SNAPSHOT_KNOWN_EXCLUDED)


def main_repo_dirty(root: Path = ROOT) -> bool:
    """P0-2：快照复制的是工作树，提交信息却写 HEAD；有未提交改动就必须拦。

    参数可注入，测试能在临时 git 仓库里真跑，而不是只检查源码里有这行字。
    """
    cp = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                        capture_output=True, text=True, encoding="utf-8")
    return cp.returncode == 0 and bool(cp.stdout.strip())


def mirror_version(snapshot_dir: Path = SNAPSHOT) -> str | None:
    """P0-3：镜像最近一次快照提交里记录的版本号。"""
    if not (snapshot_dir / ".git").is_dir():
        return None
    msg = _git(["log", "-1", "--format=%s"], snapshot_dir, check=False)
    m = re.search(r"v(\d+\.\d+\.\d+)", msg)
    return m.group(1) if m else None


def source_drift(build_info: Path = RELEASE / "BUILD_INFO.txt") -> str | None:
    """P0-3：源码在 --build 之后被改过 → 线上源码会与 Release 的 exe 脱节。

    一致返回 None，不一致返回可直接展示的原因。
    """
    if not build_info.is_file():
        return None
    recorded = next((ln.split("=", 1)[1] for ln in
                     build_info.read_text(encoding="utf-8").splitlines()
                     if ln.startswith("source_newest_mtime=")), None)
    if not recorded:
        return None
    actual = datetime.fromtimestamp(newest_source_mtime()).isoformat()
    if recorded != actual:
        return "源码在 --build 之后被改过：请先重跑 --build，否则线上源码与 Release 里的 exe 不是同一份代码。"
    return None


def run(cmd: list[Path | str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(str(x) for x in cmd), flush=True)
    rc = subprocess.run([str(x) for x in cmd], cwd=str(ROOT), env=env).returncode
    if rc != 0:
        raise SystemExit(f"命令失败（rc={rc}）：{cmd[0]}")


def check() -> int:
    """只体检：不写任何文件、不跑打包。"""
    for path in (SPEC, ISS, GEN, SELFTEST, PYINSTALLER):
        if not path.exists():
            raise SystemExit(f"缺文件：{path}")
    version = read_version(ROOT / "src" / "asase_reader" / "__init__.py")
    iscc = find_iscc()
    print(f"version={version}")
    print("plan: selftest -> gen_version_info -> pyinstaller onefile -> "
          "pyinstaller onedir -> ISCC -> sha256 -> BUILD_INFO")
    print(f"distpath: {DIST / 'onefile'} | {DIST / 'onedir'}")
    print(f"snapshot: {SNAPSHOT}（长期镜像仓库，--snapshot 生成）")
    print(f"iscc: {iscc if iscc else 'MISSING（CP8 编译安装包会卡住，需装 Inno Setup 6）'}")
    print("CHECK OK")
    return 0


def build(skip_selftest: bool) -> int:
    version = read_version(ROOT / "src" / "asase_reader" / "__init__.py")

    # 1. 自检必须全绿才允许出包（失败由 subprocess 的非 0 退出码直接中断）
    if not skip_selftest:
        run([sys.executable, SELFTEST])

    # 2. 版本号唯一来源 → 版本资源 + .iss 宏
    run([sys.executable, GEN, "--out-dir", ROOT])

    # 3. 两轨打包，distpath 隔离，避免 dist/AsaseReader.exe 与 dist/AsaseReader/ 混淆
    # COLLECT 重跑时会先删整个 onedir 目录（130+ 文件），部分环境会拦批量删除
    # 导致 --build 直接失败。改成把旧产物**挪走**，保证出包可重复执行。
    onedir_target = DIST / "onedir" / "AsaseReader"
    if onedir_target.exists():
        backup = ROOT / "build" / f"onedir_prev_{int(time.time())}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        onedir_target.rename(backup)
        print(f"+ 旧 onedir 产物挪到 {backup}")
        prune_backups()
    for mode in ("onefile", "onedir"):
        env = dict(os.environ)
        if mode == "onedir":
            env["ASASE_PACKAGE_MODE"] = "onedir"
        else:
            env.pop("ASASE_PACKAGE_MODE", None)
        env.setdefault("NODE_PATH", str(ROOT / ".cache" / "node_modules"))
        run([PYINSTALLER, SPEC, "--noconfirm", "--clean",
             "--distpath", DIST / mode,
             "--workpath", ROOT / "build" / mode], env=env)

    RELEASE.mkdir(parents=True, exist_ok=True)
    green_src = DIST / "onefile" / "AsaseReader.exe"
    if not green_src.is_file():
        raise SystemExit(f"onefile 产物缺失：{green_src}")
    portable = RELEASE / f"AsaseReader-{version}-portable.exe"
    shutil.copy2(green_src, ROOT / "AsaseReader.exe")   # 根目录绿色版照旧
    shutil.copy2(green_src, portable)

    # 4. 安装包。ISCC 缺位必须明确失败，绝不能跳过还写「已打包」
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("找不到 Inno Setup 6 的 ISCC.exe：安装包未生成。"
                         "装好 ISCC 后重跑 --build（本脚本不会假装成功）。")
    run([iscc, ISS])
    compiled = DIST / "installer" / f"AsaseReaderSetup-{version}.exe"
    if not compiled.is_file():
        raise SystemExit(f"ISCC 没产出安装包：{compiled}")
    setup = RELEASE / f"AsaseReaderSetup-{version}.exe"
    shutil.copy2(compiled, setup)

    # 5. 校验值 + 溯源信息
    artifacts = (ROOT / "AsaseReader.exe", portable, setup)
    (RELEASE / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(p)}  {p.name}\n" for p in artifacts), encoding="utf-8")

    src_mtime = newest_source_mtime()
    lines = [
        f"built_at={datetime.now().isoformat(timespec='seconds')}",
        f"git_head={git_head()}",
        f"version={version}",
        f"source_newest_mtime={datetime.fromtimestamp(src_mtime).isoformat()}",
    ]
    for path in artifacts:
        fresh = path.stat().st_mtime >= src_mtime
        lines.append(
            f"{path.name}: size={path.stat().st_size} "
            f"mtime={datetime.fromtimestamp(path.stat().st_mtime).isoformat()} "
            f"sha256={sha256(path)} fresh={fresh}")
    (RELEASE / "BUILD_INFO.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 6. 三件产物的 mtime 都必须晚于源码；有一条不满足就禁止发版
    stale = [p.name for p in artifacts if p.stat().st_mtime < src_mtime]
    if stale:
        raise SystemExit(f"产物比源码旧，禁止发版：{stale}")
    write_release_sums(version)
    print(f"BUILD OK -> {RELEASE}")
    return 0


def write_release_sums(version: str, release_dir: Path = RELEASE) -> None:
    """Release 专用两行校验值：本机 sums 有三行（含根目录同件），不直接上传。

    缺产物必须**硬失败**：以前只 WARN，会让下载者拿到旧 sums 配新包。
    """
    portable = release_dir / f"AsaseReader-{version}-portable.exe"
    setup = release_dir / f"AsaseReaderSetup-{version}.exe"
    if not (portable.is_file() and setup.is_file()):
        raise SystemExit(
            f"缺 {version} 的 portable / setup 产物，拒绝生成 Release 两行 sums；"
            "请先 --build（上传旧 sums 会让下载者校验失败）。")
    out = release_dir / "SHA256SUMS.release.txt"
    out.write_text(
        "".join(f"{sha256(p)}  {p.name}\n" for p in (portable, setup)),
        encoding="utf-8")
    print(f"+ 写出 {out}")


def _saved_origin() -> str:
    """origin.txt 丢了就用入库常量兜底，保证镜像仓库能从 GitHub clone 回来。"""
    if ORIGIN_FILE.is_file():
        text = ORIGIN_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    return SNAPSHOT_ORIGIN_FALLBACK


def prune_backups(build_dir: Path = ROOT / "build",
                  keep: int = BACKUP_KEEP) -> list[str]:
    """P1：两类 *_prev_<ts> 备份只留最近 keep 份，避免无限堆积。

    逐文件 unlink + 空目录 rmdir，不走 rmtree（大批量删除会被环境保护拦下）；
    清理失败只警告，绝不阻断出包 / 同步主流程。返回被清理的目录名，便于测试观测。
    """
    removed: list[str] = []
    if not build_dir.is_dir():
        return removed
    for prefix in ("snapshot_prev_", "onedir_prev_"):
        dirs = sorted((p for p in build_dir.glob(prefix + "*") if p.is_dir()),
                      key=lambda p: p.name)
        for old in dirs[:-keep] if keep > 0 else dirs:
            try:
                for item in sorted(old.rglob("*"),
                                   key=lambda p: len(p.parts), reverse=True):
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        item.rmdir()
                old.rmdir()
                removed.append(old.name)
                print(f"+ 清理旧备份 {old.name}")
            except OSError as exc:
                print(f"WARN: 清理备份 {old.name} 失败（不影响主流程）：{exc}")
    return removed


def _skip_rel(rel: Path) -> bool:
    if any(part in SNAPSHOT_SKIP_NAMES for part in rel.parts):
        return True
    if rel.name in SNAPSHOT_SKIP_FILES:
        return True
    return rel.name.endswith(SNAPSHOT_SKIP_SUFFIX)


def _wanted_files() -> dict[Path, Path]:
    """白名单过滤后的「相对路径 → 源文件」映射，供差异同步使用。"""
    wanted: dict[Path, Path] = {}
    for item in sorted(ROOT.iterdir()):
        if item.name not in SNAPSHOT_ALLOW_TOP:
            continue
        if item.is_dir():
            for p in sorted(item.rglob("*")):
                rel = p.relative_to(ROOT)
                if _skip_rel(rel) or not p.is_file():
                    continue
                wanted[rel] = p
        elif item.is_file():
            wanted[item.relative_to(ROOT)] = item
    return wanted


def _sync_worktree(dest: Path,
                   wanted: dict[Path, Path] | None = None) -> tuple[int, int]:
    """镜像同步：只删「主仓库已不存在」的文件，再覆盖复制。

    以前整棵工作树挪走再复制，会不断产生 snapshot_prev_* 备份；而项目目录内
    每个文件的删除约 0.25s（安全审计），清理一批旧备份要 5 分钟。
    镜像仓库自己有 .git，旧内容都在历史里，根本不需要备份 —— 改为差异同步后
    正常发版删除 0 个文件，零额外开销，且仍满足「删除同步」。
    返回 (复制数, 删除数)。
    """
    wanted = wanted if wanted is not None else _wanted_files()
    removed = 0
    for top in sorted(dest.iterdir()):
        if top.name == ".git":        # 跳过 git 内部成千上万的对象文件
            continue
        candidates = (sorted(top.rglob("*"), key=lambda q: len(q.parts),
                             reverse=True) if top.is_dir() else [top])
        for p in candidates:
            if not p.is_file():
                continue
            if p.relative_to(dest) in wanted:
                continue
            p.unlink()
            removed += 1
    for rel, src in wanted.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return len(wanted), removed


def _git(args: list[str], cwd: Path, *, check: bool = True) -> str:
    cp = subprocess.run(["git", *args], cwd=str(cwd), text=True,
                        encoding="utf-8", capture_output=True)
    if check and cp.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{cp.stderr.strip()}")
    return cp.stdout.strip()


def snapshot() -> int:
    """生成本地公开镜像仓库；保留同一个 .git，供 GitHub Desktop 持续 Push。"""
    # P0-1：新增顶层文件必须显式拍板（进白名单或明确排除），不能静默漏
    gaps = whitelist_gaps()
    if gaps:
        raise SystemExit(
            "白名单漏了入库的顶层项（新增文件必须显式拍板公开或排除）："
            + ", ".join(gaps))
    # P0-2：先提交主仓库，快照的「来源 hash」才有意义
    if main_repo_dirty():
        raise SystemExit(
            "主仓库有未提交改动：请先 commit 再 --snapshot。"
            "快照复制的是工作树，带未提交内容会让来源 hash 指错提交。")

    version = read_version(ROOT / "src" / "asase_reader" / "__init__.py")
    info = RELEASE / "BUILD_INFO.txt"
    if info.is_file():
        lines = info.read_text(encoding="utf-8").splitlines()
        built = next((ln.split("=", 1)[1] for ln in lines
                      if ln.startswith("version=")), None)
        if built and built != version:
            raise SystemExit(
                f"版本不一致：源码 {version} / BUILD_INFO {built}；请先 --build")
    # P0-3a：改了 src/** 却没重跑 --build → 线上源码与 Release 的 exe 脱节
    drift = source_drift()
    if drift:
        raise SystemExit(drift)
    # P0-3b：三处版本同源（源码 / BUILD_INFO / 镜像最近提交）
    mirror = mirror_version()
    if mirror and mirror != version:
        raise SystemExit(
            f"版本不一致：镜像最近提交 {mirror} / 源码 {version}；"
            "请先 --build 再 --snapshot")

    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    had_git = (SNAPSHOT / ".git").is_dir()

    # .git 丢失：能从记录的 origin 恢复就 clone，否则把孤儿目录挪走重建
    if SNAPSHOT.exists() and not had_git:
        origin = _saved_origin()
        backup = ROOT / "build" / f"snapshot_orphan_{stamp}"
        shutil.move(str(SNAPSHOT), str(backup))
        print(f"+ 无 .git 的旧快照挪到 {backup}")
        if origin:
            run(["git", "clone", origin, str(SNAPSHOT)])
            had_git = True
            print(f"+ 已从 {origin} 恢复镜像仓库")

    if not SNAPSHOT.exists():
        SNAPSHOT.mkdir(parents=True)
    if had_git:
        count, removed = _sync_worktree(SNAPSHOT)
        print(f"+ 差异同步：{count} 文件，删除 {removed} 个多余文件（保留 .git）")
    else:
        run(["git", "init", "-b", "main", str(SNAPSHOT)])
        _git(["config", "user.name", SNAPSHOT_AUTHOR], SNAPSHOT)
        _git(["config", "user.email", SNAPSHOT_EMAIL], SNAPSHOT)
        count, removed = _sync_worktree(SNAPSHOT)
        print(f"+ 首次建立镜像仓库（main），{count} 文件")
    prune_backups()  # 只管 --build 的 onedir 备份与历史遗留备份

    run([sys.executable, str(SCAN), str(SNAPSHOT)])  # 命中即非 0，直接中断
    write_release_sums(version)

    _git(["add", "-A"], SNAPSHOT)
    head = git_head()
    if _git(["status", "--porcelain"], SNAPSHOT):
        msg = (f"chore(snapshot): sync v{version} (from {head[:7]})" if had_git
               else f"chore(snapshot): initial public snapshot v{version} (from {head[:7]})")
        _git(["commit", "-m", msg], SNAPSHOT)
        print(f"+ 提交：{msg}")
    else:
        print("SNAPSHOT OK（工作树无变化，无需新提交）")

    origin = _git(["remote", "get-url", "origin"], SNAPSHOT, check=False)
    if origin:
        ORIGIN_FILE.write_text(origin + "\n", encoding="utf-8")
    print(f"snapshot files={count} head={_git(['rev-parse', '--short', 'HEAD'], SNAPSHOT)}")
    print(f"NEXT: GitHub Desktop → Add Local Repository → {SNAPSHOT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--build", action="store_true")
    group.add_argument("--snapshot", action="store_true")
    ap.add_argument("--skip-selftest", action="store_true")
    args = ap.parse_args()
    if args.check:
        return check()
    if args.snapshot:
        return snapshot()
    return build(args.skip_selftest)


if __name__ == "__main__":
    raise SystemExit(main())
