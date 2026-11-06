"""公开快照（CP5）：白名单、扫描器、长期镜像仓库的可观测断言。

不依赖真实 GitHub，也不在主仓库里建 remote；只做静态检查 + 在临时目录真跑扫描器。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
SCAN_SRC = (ROOT / "tools" / "scan_public_snapshot.py").read_text(encoding="utf-8")
SCAN = ROOT / "tools" / "scan_public_snapshot.py"
LICENSE = (ROOT / "LICENSE").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

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
check('if item.name == ".git":' in BUILD, "镜像同步保留 .git（删除同步）")
check("snapshot_prev_" in BUILD, "旧工作树挪走而非删")
check("_git([\"commit\"" in BUILD, "脚本自动 commit")
check('["remote", "get-url", "origin"]' in BUILD,
      "记录 origin 以便 .git 丢失恢复")
check("git\", \"clone" in BUILD, ".git 丢失时从 origin 恢复")
check("SNAPSHOT_AUTHOR = \"AsaseRin\"" in BUILD, "快照提交作者为 AsaseRin")
check("79520005+tiger-forest@users.noreply.github.com" in BUILD,
      "快照提交邮箱为已拍板的 noreply")
check("BUILD_INFO" in BUILD and "请先 --build" in BUILD,
      "三处版本一致性检查会阻断不一致")
check("str(SCAN), str(SNAPSHOT)" in BUILD, "生成快照后强制调用扫描器")
check("SHA256SUMS.release.txt" in BUILD, "生成 Release 专用两行 sums")
check("for p in (portable, setup)" in BUILD,
      "两行 sums 只含 portable 与 setup，不含根目录同件")

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
