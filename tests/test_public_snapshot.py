"""公开快照（CP5）：白名单、扫描器、长期镜像仓库的可观测断言。

不依赖真实 GitHub，也不在主仓库里建 remote；只做静态检查 + 在临时目录真跑扫描器。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
SCAN_SRC = (ROOT / "tools" / "scan_public_snapshot.py").read_text(encoding="utf-8")
SCAN = ROOT / "tools" / "scan_public_snapshot.py"
LICENSE = (ROOT / "LICENSE").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT / "tools"))
import build_release as br  # noqa: E402  P0-1 白名单不变量要真调函数

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)


def make_clean(base: Path) -> None:
    for rel in (
        "LICENSE", "README.md", "CHANGELOG.md", "pyproject.toml",
        "AsaseReader.spec", "asase.py", "src/asase_reader/__init__.py",
        "tools/build_release.py", "tools/scan_public_snapshot.py",
        "installer/AsaseReader.iss", "tests/selftest.py",
    ):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("clean placeholder\n", encoding="utf-8")


def run_scan(base: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCAN), str(base)],
                          capture_output=True, text=True, encoding="utf-8")


# ---- 1. MIT 许可与项目元数据 ----
check("MIT License" in LICENSE, "LICENSE 是 MIT 文本")
check("Copyright (c) 2026 浅濑凛 (Asase Rin)" in LICENSE, "LICENSE 版权署名正确")
check('license = { text = "MIT" }' in PYPROJECT, "pyproject 声明 MIT")
check("Homepage = \"https://github.com/tiger-forest/AsaseReader\"" in PYPROJECT,
      "pyproject Homepage 指向已拍板的公开仓库")

# ---- 2. 主仓库里不再有本机专属路径（可公开性改造）----
for rel in ("launch_gui.bat", "tests/selftest.py", "tests/helpers.js", "README.md"):
    text = (ROOT / rel).read_text(encoding="utf-8")
    check("thinkbook" not in text, f"{rel} 不含本机用户名")
    check("G:" + "\\Dev" not in text and "G:" + "/Dev" not in text,
          f"{rel} 不含本机项目盘路径")
check("ASASE_PYTHON" in (ROOT / "launch_gui.bat").read_text(encoding="utf-8"),
      "launch_gui.bat 支持环境变量覆盖解释器")
check("os.environ" in (ROOT / "tests" / "selftest.py").read_text(encoding="utf-8"),
      "selftest 支持 NODE_PATH 环境变量")

# ---- 3. 白名单与镜像仓库契约 ----
check(".workbuddy" not in BUILD.split("SNAPSHOT_ALLOW_TOP = frozenset({")[1].split("})")[0],
      "白名单不含 .workbuddy/")
check("PROGRESS.md" not in BUILD.split("SNAPSHOT_ALLOW_TOP = frozenset({")[1].split("})")[0],
      "白名单不含 PROGRESS.md")
for name in ("LICENSE", "src", "tests", "tools", "installer", "urls.example.txt"):
    check(f'"{name}"' in BUILD.split("SNAPSHOT_ALLOW_TOP = frozenset({")[1].split("})")[0],
          f"白名单含 {name}")
check("version.iss" in BUILD, "生成物 version.iss 被排除")
check('add_argument("--snapshot"' in BUILD, "--snapshot 已注册")
check('"git", "init", "-b", "main"' in BUILD, "首次建立 main 分支镜像仓库")
check('if top.name == ".git":' in BUILD, "镜像同步跳过 .git（保留仓库）")
check("_wanted_files" in BUILD and "_sync_worktree(SNAPSHOT)" in BUILD,
      "差异同步：只删多余文件，不再整棵挪走工作树")
check("snapshot_prev_" not in BUILD.split("def snapshot()")[1].split("def main()")[0],
      "--snapshot 不再产生 snapshot_prev_* 工作树备份")
check("_git([\"commit\"" in BUILD, "脚本自动 commit")
check('["remote", "get-url", "origin"]' in BUILD,
      "记录 origin 以便 .git 丢失恢复")
check("git\", \"clone" in BUILD, ".git 丢失时从 origin 恢复")
check("SNAPSHOT_AUTHOR = \"AsaseRin\"" in BUILD, "快照提交作者为 AsaseRin")
check("79520005+tiger-forest@users.noreply.github.com" in BUILD,
      "快照提交邮箱为已拍板的 noreply")
check("BUILD_INFO" in BUILD and "请先 --build" in BUILD,
      "版本一致性检查会阻断不一致")
check("str(SCAN), str(SNAPSHOT)" in BUILD, "生成快照后强制调用扫描器")
check("SHA256SUMS.release.txt" in BUILD, "生成 Release 专用两行 sums")
check("for p in (portable, setup)" in BUILD,
      "两行 sums 只含 portable 与 setup，不含根目录同件")

# ---- 3.1 P0 三项加固（2026-11-06 20:18 批准；真跑，不靠字符串存在）----
check(not br.whitelist_gaps(), "当前主仓库白名单覆盖全部入库顶层项")
with tempfile.TemporaryDirectory() as td:
    mini = Path(td)
    subprocess.run(["git", "init", "-q"], cwd=mini, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=mini, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"],
                   cwd=mini, check=True)
    (mini / "README.md").write_text("x", encoding="utf-8")
    (mini / "docs").mkdir()
    (mini / "docs" / "a.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=mini, check=True)
    check(br.whitelist_gaps(mini) == ["docs"],
          "新增顶层目录会被白名单不变量抓到（不是静默漏）")
    check(br.main_repo_dirty(mini), "有未提交改动 → 脏检查为真")
    subprocess.run(["git", "commit", "-q", "-m",
                    "chore(snapshot): sync v9.9.9 (from deadbeef)"],
                   cwd=mini, check=True)
    check(not br.main_repo_dirty(mini), "提交后工作区干净")
    check(br.mirror_version(mini) == "9.9.9", "镜像最近提交版本被解析出来")

    bi = mini / "BUILD_INFO.txt"
    real = datetime.fromtimestamp(br.newest_source_mtime()).isoformat()
    bi.write_text(f"version=2.1.0\nsource_newest_mtime={real}\n",
                  encoding="utf-8")
    check(br.source_drift(bi) is None, "BUILD_INFO 与源码 mtime 一致 → 不报警")
    bi.write_text("version=2.1.0\nsource_newest_mtime=2000-01-01T00:00:00\n",
                  encoding="utf-8")
    check(br.source_drift(bi) is not None,
          "源码被改过但没重跑 --build → 报警")
    bi.unlink()
    check(br.source_drift(bi) is None,
          "没有 BUILD_INFO 时跳过（公开仓库 clone 场景）")
check("gaps = whitelist_gaps()" in BUILD
      and "if main_repo_dirty():" in BUILD
      and "if drift:" in BUILD
      and "if mirror and mirror != version:" in BUILD,
      "三项检查都接进了 --snapshot")

# ---- 3.2 P1/P2 加固（2026-11-06 20:38 批准）----
check("SNAPSHOT_ORIGIN_FALLBACK" in BUILD
      and "https://github.com/tiger-forest/AsaseReader.git" in BUILD
      and "origin = _saved_origin()" in BUILD,
      "origin.txt 丢失时有入库常量兜底")
with tempfile.TemporaryDirectory() as td:
    saved_origin_file = br.ORIGIN_FILE
    br.ORIGIN_FILE = Path(td) / "missing.txt"
    check(br._saved_origin()
          == "https://github.com/tiger-forest/AsaseReader.git",
          "origin.txt 真的丢了也能返回兜底常量")
    br.ORIGIN_FILE = saved_origin_file
with tempfile.TemporaryDirectory() as td:
    bdir = Path(td)
    for i in range(4):
        d = bdir / f"snapshot_prev_{1000 + i}"
        d.mkdir()
        (d / "a.txt").write_text("x", encoding="utf-8")
    for i in range(3):
        d = bdir / f"onedir_prev_{2000 + i}"
        d.mkdir()
        (d / "b.exe").write_text("x", encoding="utf-8")
    removed = br.prune_backups(bdir, keep=2)
    check(len(list(bdir.glob("snapshot_prev_*"))) == 2,
          "snapshot 备份只留最近 2 份")
    check(len(list(bdir.glob("onedir_prev_*"))) == 2,
          "onedir 备份只留最近 2 份")
    check(len(removed) == 3, "返回被清理列表，便于观测")
check("prune_backups()" in BUILD and "BACKUP_KEEP" in BUILD,
      "备份清理已接入 --build / --snapshot")

with tempfile.TemporaryDirectory() as td:
    empty = Path(td)
    try:
        br.write_release_sums("9.9.9", empty)
        check(False, "缺产物时 write_release_sums 必须抛 SystemExit")
    except SystemExit as exc:
        check("拒绝生成 Release 两行 sums" in str(exc),
              "错误信息明确指向 --build")
    except Exception as exc:  # 崩溃也要变成明确 FAIL，不许测试自己炸掉
        check(False, f"缺产物时应抛 SystemExit，实际 {type(exc).__name__}: {exc}")
with tempfile.TemporaryDirectory() as td:
    rel = Path(td)
    (rel / "AsaseReader-9.9.9-portable.exe").write_bytes(b"a")
    (rel / "AsaseReaderSetup-9.9.9.exe").write_bytes(b"b")
    br.write_release_sums("9.9.9", rel)
    lines = (rel / "SHA256SUMS.release.txt").read_text(
        encoding="utf-8").splitlines()
    check(len(lines) == 2, "有两件产物时生成两行 sums")

# 差异同步真跑：多余文件被删、.git 不被碰、新文件被复制
with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as sd:
    dest, srcroot = Path(td), Path(sd)
    (dest / ".git").mkdir()
    (dest / ".git" / "config").write_text("git-internal", encoding="utf-8")
    (dest / "extra.txt").write_text("stale", encoding="utf-8")
    (dest / "src").mkdir()
    (dest / "src" / "old.py").write_text("stale", encoding="utf-8")
    (srcroot / "src").mkdir()
    (srcroot / "src" / "new.py").write_text("new", encoding="utf-8")
    wanted = {Path("src/new.py"): srcroot / "src" / "new.py"}
    copied, removed = br._sync_worktree(dest, wanted)
    check((copied, removed) == (1, 2), f"复制 1 / 删除 2，实际 {copied}/{removed}")
    git_cfg = dest / ".git" / "config"
    check(git_cfg.is_file()
          and git_cfg.read_text(encoding="utf-8") == "git-internal",
          ".git 内部文件不被碰")
    new_file = dest / "src" / "new.py"
    check(new_file.is_file()
          and new_file.read_text(encoding="utf-8") == "new",
          "新文件被复制")
    check(not (dest / "extra.txt").exists()
          and not (dest / "src" / "old.py").exists(), "多余文件真的被删")

# ---- 4. 扫描规则本身 ----
check("HISTORY_DOCS" in SCAN_SRC, "CHANGELOG 历史文档放行机制存在")
rules_block = SCAN_SRC.split("PATH_RULES =")[1].split("HISTORY_DOCS")[0]
check("bilibili" not in rules_block, "扫描规则不拦真实 opus 链接")
check("SESS" + "DATA" not in SCAN_SRC, "扫描器自身不含完整凭据字面量")
check("C:" + r"[\\/]Users" in SCAN_SRC, "本机用户目录规则存在")

# ---- 5. 临时目录真跑扫描器 ----
with tempfile.TemporaryDirectory() as td:
    base = Path(td)

    clean = base / "clean"
    make_clean(clean)
    r = run_scan(clean)
    check(r.returncode == 0, f"干净样本 rc=0（实际 {r.returncode}：{r.stdout[:160]}）")
    check("SCAN OK" in r.stdout, "干净样本输出 SCAN OK")

    # M11：白名单把 .workbuddy/ 放回来
    bad_dir = base / "bad_dir"
    make_clean(bad_dir)
    (bad_dir / ".workbuddy").mkdir()
    (bad_dir / ".workbuddy" / "MEMORY.md").write_text("x", encoding="utf-8")
    r = run_scan(bad_dir)
    check(r.returncode != 0, "含 .workbuddy/ 的样本 rc!=0")
    check("FAIL:" in r.stdout, "含 .workbuddy/ 的样本有真实 FAIL 输出")

    # 不该出现的独立文件
    bad_file = base / "bad_file"
    make_clean(bad_file)
    (bad_file / "PROGRESS.md").write_text("x", encoding="utf-8")
    r = run_scan(bad_file)
    check(r.returncode != 0 and "不该出现的文件" in r.stdout,
          "PROGRESS.md 被扫描器拦下")

    # M12：扫描器对敏感样本返回 0 的反向验证（这里验证它确实会红）
    bad_path = base / "bad_path"
    make_clean(bad_path)
    (bad_path / "README.md").write_text(
        "old = " + "G:" + "\\Dev\\BiliPassageDownloader\\outputs\n", encoding="utf-8")
    r = run_scan(bad_path)
    check(r.returncode != 0 and "本机项目路径" in r.stdout,
          "含本机项目路径的样本 rc!=0 且有真实 FAIL")

    bad_user = base / "bad_user"
    make_clean(bad_user)
    (bad_user / "README.md").write_text(
        "venv = " + "C:/Users/" + "someone/AppData/python.exe\n", encoding="utf-8")
    r = run_scan(bad_user)
    check(r.returncode != 0 and "本机用户目录" in r.stdout,
          "含本机用户目录的样本被拦下")

    bad_cred = base / "bad_cred"
    make_clean(bad_cred)
    (bad_cred / "README.md").write_text(
        "cookie: " + "SESS" + "DATA=abcdef123456; other\n", encoding="utf-8")
    r = run_scan(bad_cred)
    check(r.returncode != 0 and "登录凭据字段" in r.stdout,
          "含凭据字面量的样本被拦下")

    # 历史文档放行：CHANGELOG 里的本机路径不算命中，但凭据仍拦
    hist = base / "hist"
    make_clean(hist)
    (hist / "CHANGELOG.md").write_text(
        "迁移自 " + "G:" + "\\Dev\\old\\outputs\n", encoding="utf-8")
    r = run_scan(hist)
    check(r.returncode == 0, "CHANGELOG 历史本机路径按拍板放行")
    (hist / "CHANGELOG.md").write_text(
        "cookie: " + "SESS" + "DATA=abcdef123456\n", encoding="utf-8")
    r = run_scan(hist)
    check(r.returncode != 0, "CHANGELOG 里的凭据字面量不放行")

    # 白名单放行：真实 opus 链接与通用程序路径不误报
    allowed = base / "allowed"
    make_clean(allowed)
    (allowed / "urls.example.txt").write_text(
        "https://www.bilibili.com/opus/1261150454223470609\n", encoding="utf-8")
    (allowed / "README.md").write_text(
        "node at C:/Program Files/nodejs/node.exe\n", encoding="utf-8")
    r = run_scan(allowed)
    check(r.returncode == 0, f"真实 opus 链接与通用路径不误报（{r.stdout[:160]}）")

    # 缺必需文件
    missing = base / "missing"
    make_clean(missing)
    (missing / "LICENSE").unlink()
    r = run_scan(missing)
    check(r.returncode != 0 and "缺必需文件" in r.stdout, "缺 LICENSE 被拦下")

print()
if fails:
    print(f"FAILED {len(fails)}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
