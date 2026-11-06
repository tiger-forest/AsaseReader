#!/usr/bin/env python3
"""公开快照敏感信息扫描（方案 plan-installer-package.md §5.1）。

用法::

    python tools/scan_public_snapshot.py <snapshot_dir>

命中即**退出码非 0**，由 ``build_release.py --snapshot`` 强制调用。
规则里的敏感字面量用拼接构造，避免扫描器自己命中自己。

拍板放行的两类（不算命中）：
- 真实 opus id / ``bilibili.com/opus`` 链接（夹具白名单，见 README）。
- ``CHANGELOG.md`` 里的历史本机路径（历史文字一字不改）；但其中的
  凭据字面量**一律不放行**。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

EXCLUDED_DIRS = frozenset({
    ".git", ".workbuddy", "outputs", ".asase", ".state", ".cache",
    "build", "dist", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".idea", ".vscode",
})
EXCLUDED_FILES = frozenset({
    "PROGRESS.md", "urls.txt", "AsaseReader.exe", "file_version_info.txt",
    "version.iss", ".DS_Store",
})
EXCLUDED_SUFFIXES = (".bak", ".log", ".pyc", ".pyo", ".exe", ".dll", ".pyd")

REQUIRED = (
    "LICENSE", "README.md", "CHANGELOG.md", "pyproject.toml",
    "AsaseReader.spec", "asase.py", "src/asase_reader/__init__.py",
    "tools/build_release.py", "tools/scan_public_snapshot.py",
    "installer/AsaseReader.iss", "tests/selftest.py",
)

PATH_RULES = [
    ("本机项目路径", re.compile(r"G:[\\/]Dev[\\/]", re.I)),
    ("本机用户目录", re.compile(r"C:[\\/]Users[\\/][^\\/\s'\"`<>]+", re.I)),
    ("本机 venv 路径", re.compile(r"\.workbuddy[\\/]binaries[\\/]python", re.I)),
]
# 凭据字面量只认「字段名 = 长 token」。按方案 §5.1，合成测试桩
# （fake / unit_test / ... 之类）、JS 对象字段（sessdata: false）、夹具里的
# 解析正则（DedeUserID=(.+?)）属于「脱敏会破坏夹具语义」的保留项，不算命中。
CRED_TOKEN = r"([A-Za-z0-9%_\-]{8,})"
CRED_TEST_MARKERS = ("fake", "test", "secret", "dummy", "example", "placeholder")
CRED_RULES = [
    ("登录凭据字段 A", re.compile("SESS" + r"DATA\s*=\s*" + CRED_TOKEN, re.I)),
    ("登录凭据字段 B", re.compile("bili" + r"_jct\s*=\s*" + CRED_TOKEN, re.I)),
    ("登录凭据字段 C", re.compile("Dede" + r"UserID\s*=\s*" + CRED_TOKEN, re.I)),
]
HISTORY_DOCS = frozenset({"CHANGELOG.md"})


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        # .git 是镜像仓库自身，允许存在；其余「不该出现的目录」由主循环判 FAIL，
        # 绝不能在这里静默跳过 —— 否则把 .workbuddy/ 加回白名单也扫不出来（M11）。
        if rel.parts[0] == ".git":
            continue
        yield p, rel


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: scan_public_snapshot.py <snapshot_dir>")
        return 2
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"FAIL: 快照目录不存在：{root}")
        return 1

    fails: list[str] = []
    for rel in REQUIRED:
        if not (root / rel).is_file():
            fails.append(f"缺必需文件：{rel}")

    files = list(iter_files(root))
    for path, rel in files:
        name = path.name
        if name in EXCLUDED_FILES or name.endswith(EXCLUDED_SUFFIXES):
            fails.append(f"不该出现的文件：{rel}")
            continue
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            fails.append(f"不该出现的目录：{rel}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 二进制资源（图标 / 夹具占位图）不扫内容
        is_history = rel.parts[0] in HISTORY_DOCS
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, rule in PATH_RULES:
                if is_history and label.startswith("本机"):
                    continue
                hit = rule.search(line)
                if hit:
                    fails.append(f"{rel}:{lineno} {label}: {hit.group(0)[:60]}")
            for label, rule in CRED_RULES:
                hit = rule.search(line)
                if hit and not any(mark in hit.group(1).lower()
                                   for mark in CRED_TEST_MARKERS):
                    fails.append(f"{rel}:{lineno} {label}: {hit.group(0)[:40]}")

    if fails:
        print(f"FAILED {len(fails)}")
        for item in fails:
            print("FAIL:", item)
        return 1
    print(f"SCAN OK ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
