#!/usr/bin/env python3
"""安装包双轨构建的静态与生成物断言（方案 §7.1）。

自检里**不跑 PyInstaller**（一次 2~5 分钟）；真打包留到 CP8 人工验收。
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"PASS  {msg}")
    else:
        fails.append(msg)
        print(f"FAIL  {msg}")


spec = (ROOT / "AsaseReader.spec").read_text(encoding="utf-8")
gen = (ROOT / "tools" / "gen_version_info.py").read_text(encoding="utf-8")
init = (ROOT / "src" / "asase_reader" / "__init__.py").read_text(encoding="utf-8")

print("--- spec：一份定义、两个分支 ---")
check("ASASE_PACKAGE_MODE" in spec, "spec 读 ASASE_PACKAGE_MODE 开关")
check('ONEDIR = os.environ.get("ASASE_PACKAGE_MODE") == "onedir"' in spec,
      "onedir 开关口径正确")
check("exclude_binaries=True" in spec, "onedir 分支有 exclude_binaries=True")
check(re.search(r"\bCOLLECT\(", spec) is not None, "onedir 分支有 COLLECT")
check(spec.count("EXE(") == 2, f"EXE 恰好两个分支（实际 {spec.count('EXE(')}）")
check(spec.count("datas=[") == 1, "datas 只定义一次（防两份 spec 漂移）")
check(spec.count("hiddenimports += collect_submodules") == 1,
      "hiddenimports 只收集一次")
check(spec.count("console=False") == 1, "console=False 只定义一次")
check(any(ln.strip().startswith("icon=ICON") for ln in spec.splitlines()),
      "icon=ICON 仍是非注释行（test_app_icon 的存量口径）")
check("VERSION_FILE" in spec and "is_file()" in spec,
      "版本资源有存在性守卫：缺生成物时老命令仍能打包")
check("'version'" in spec or '"version"' in spec, "spec 引用生成的版本资源")

print("\n--- 版本号唯一来源 ---")
ver = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.M)
check(ver is not None, "能从 __init__.py 读到 __version__")
check("__version__" in gen and "read_version" in gen,
      "gen_version_info.py 从 __init__.py 读版本")
check("dynamic" in (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
      "pyproject.toml 仍是 dynamic version")
check("2.0.0" not in gen, "生成脚本里没有硬编码当前版本号")
check("2.1.0" not in gen, "生成脚本里没有硬编码目标版本号")

print("\n--- 真跑生成脚本：篡改源版本号，产物必须跟着变 ---")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    fake_src = tmp / "__init__.py"
    fake_src.write_text('__version__ = "9.8.7"\n', encoding="utf-8")
    out = tmp / "out"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "gen_version_info.py"),
         "--source", str(fake_src), "--out-dir", str(out)],
        capture_output=True, text=True, encoding="utf-8",
    )
    check(proc.returncode == 0,
          f"gen_version_info.py 正常退出（rc={proc.returncode} {proc.stderr[:120]}）")
    info = out / "file_version_info.txt"
    macros = out / "installer" / "version.iss"
    check(info.is_file(), "生成 file_version_info.txt")
    check(macros.is_file(), "生成 installer/version.iss")
    if info.is_file():
        txt = info.read_text(encoding="utf-8")
        check("filevers=(9, 8, 7, 0)" in txt, "版本资源 tuple 跟随 9.8.7")
        check("9.8.7.0" in txt, "版本资源字符串跟随 9.8.7")
        check("浅濑凛" in txt, "版本资源带版权署名浅濑凛")
        check("Asase Reader" in txt, "版本资源带产品名")
    if macros.is_file():
        txt = macros.read_text(encoding="utf-8")
        check('MyAppVersion "9.8.7"' in txt, "iss 版本宏跟随 9.8.7")
        check("浅濑凛" in txt, "iss 宏带版权署名")

print("\n--- .iss：向导 / 卸载数据边界 / WebView2 ---")
iss_path = ROOT / "installer" / "AsaseReader.iss"
check(iss_path.is_file(), "installer/AsaseReader.iss 存在")
if iss_path.is_file():
    iss = iss_path.read_text(encoding="utf-8-sig")
    check('#include "version.iss"' in iss, "引入生成的版本宏")
    check("PrivilegesRequired=lowest" in iss, "免管理员 A 路线")
    check("PrivilegesRequired=admin" not in iss, "没有要求管理员权限")
    check("DefaultDirName={localappdata}\\Programs\\AsaseReader" in iss,
          "默认装到 %LOCALAPPDATA%\\Programs\\AsaseReader")
    check(re.search(r"^AppVersion=\{#MyAppVersion\}", iss, re.M) is not None,
          "AppVersion 用宏而非字面量")
    check(re.search(r"^VersionInfoVersion=\{#MyAppVersion4\}", iss, re.M) is not None,
          "版本资源用宏而非字面量")
    check('Name: "chinesesimplified"' in iss and "ChineseSimplified.isl" in iss,
          "向导仅简体中文")
    check('MessagesFile: "ChineseSimplified.isl"' in iss,
          "中文语言文件走项目内相对路径，不依赖 Inno 安装目录")
    lang = ROOT / "installer" / "ChineseSimplified.isl"
    check(lang.is_file() and "[LangOptions]" in lang.read_text(encoding="utf-8-sig"),
          "简体中文语言文件随仓库入库且格式合法")
    check('Name: "english"' not in iss.lower(), "没有英文向导（用户拍板不做双语）")

    desktop_tasks = [ln for ln in iss.splitlines()
                     if ln.strip().startswith('Name: "desktopicon"')]
    check(len(desktop_tasks) == 1, "desktopicon 任务只定义一次")
    check(bool(desktop_tasks) and "unchecked" not in desktop_tasks[0],
          "桌面快捷方式默认勾选（没有 unchecked）")
    check("{group}" in iss and "{autodesktop}" in iss, "开始菜单 + 桌面快捷方式都建")
    check("postinstall" in iss, "有「装完立即启动」勾选项")
    check('Source: "..\\dist\\onedir\\AsaseReader\\*"' in iss,
          "只收 onedir 产物，不带源码 / 夹具 / 缓存")
    # 以下三条来自 CP8 首次真机编译的报错，静态断言原来漏了
    check("Flags: ignoreversion recursesubdirs createallsubdirs" in iss,
          "[Files] 用 Inno 合法的 recursesubdirs")
    check("recursedirs" not in iss, "没有非法的 recursedirs flag（真机编译会直接中止）")
    code_head = iss.split("[Code]")[1].split("function")[0]
    check("// " in code_head and "  ; " not in code_head,
          "[Code] const 段用 Pascal 注释，不混用 .iss 分号注释")

    # 硬约束：卸载段内绝不出现数据目录或任何书库路径
    m = re.search(r"\[UninstallDelete\](.*?)(?=\r?\n\[|\Z)", iss, re.S)
    check(m is not None, "有明确的 [UninstallDelete] 段（意图可见、便于断言）")
    if m:
        seg = m.group(1)
        check("{localappdata}\\AsaseReader" not in seg, "卸载不删数据目录")
        check("AsaseReaderLibrary" not in seg, "卸载不删默认书库")
        check("shelf" not in seg.lower(), "卸载不碰书库路径")

    check("function NextButtonClick" in iss and "wpSelectDir" in iss,
          "向导里拦截安装路径选择")
    check("{localappdata}\\AsaseReader" in iss, "硬拒把程序装到数据目录（有具体判据）")
    check("RegQueryStringValue" in iss and "EdgeUpdate\\Clients" in iss,
          "WebView2 运行时检测")
    check("DownloadTemporaryFile" in iss and "go.microsoft.com" in iss,
          "缺运行时时下载微软官方在线安装器")
    check("microsoft.com" in iss.lower() and "http://" not in iss.lower(),
          "只走微软官方 HTTPS 域名")
    check("function PrepareToInstall" in iss and "Result := '';" in iss,
          "WebView2 引导失败不阻断安装")

print("\n--- build_release.py：一键出包链路 ---")
build_src = (ROOT / "tools" / "build_release.py").read_text(encoding="utf-8")
check("def newest_source_mtime" in build_src, "新鲜度基准来自 src/** 与 gui.html")
check("SHA256SUMS" in build_src and "hashlib.sha256" in build_src,
      "生成 SHA256 校验值")
check("BUILD_INFO" in build_src, "生成 BUILD_INFO 溯源信息")
check("source_newest_mtime" in build_src and "fresh=" in build_src,
      "BUILD_INFO 记源码最新 mtime 并逐件标 fresh")
check("产物比源码旧，禁止发版" in build_src, "新鲜度不达标时退出非 0")
check("ASASE_PACKAGE_MODE" in build_src, "onedir 分支显式设环境变量")
check("DIST / 'onefile'" in build_src and "DIST / 'onedir'" in build_src,
      "两轨 distpath 隔离（不混在 dist/ 根下）")
check("ISCC" in build_src and "找不到 Inno Setup" in build_src,
      "ISCC 缺位时明确失败，不假绿灯")
check("SELFTEST" in build_src and "selftest.py" in build_src, "出包前跑完整自检")
check("gen_version_info" in build_src, "出包前生成版本资源")
check("AsaseReaderSetup-" in build_src and "-portable.exe" in build_src,
      "安装包与绿色版资产命名")
check('add_argument("--snapshot"' in build_src,
      "CP5 开工：--snapshot 已注册为命令（原 CP4 反向断言随需求变更转正）")

proc = subprocess.run(
    [sys.executable, str(ROOT / "tools" / "build_release.py"), "--check"],
    capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
)
check(proc.returncode == 0,
      f"--check 正常退出（rc={proc.returncode} {proc.stderr[:160]}）")
check("CHECK OK" in proc.stdout, "--check 输出自检结论")
check("plan:" in proc.stdout, "--check 打印完整链路")
check("ASASE_ISCC" in build_src,
      "支持环境变量覆盖 ISCC 路径（装到非系统盘也能出包）")
check("_registry_iscc" in build_src and "import winreg" in build_src,
      "从注册表读 Inno Setup 安装位置，不硬编码本机盘符")
check("return _registry_iscc()" in build_src, "注册表兜底真的被调用")
check("onedir_prev_" in build_src and ".rename(" in build_src,
      "重跑 --build 时把旧 onedir 产物挪走而非删除（真机踩到批量删除拦截）")

print()
if fails:
    print(f"FAILED {len(fails)}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
