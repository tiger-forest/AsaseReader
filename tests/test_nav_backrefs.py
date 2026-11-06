"""返回记账静态检查：进作品页 / 编辑页的调用点必须带来源参数。

背景（v1.8.1 修过的真 bug）：作品页右上角「编辑作品」调 openEdit 时漏传了
workBackTo，表现是「从所有作品页点进来，保存后却被送回书架」。

这类 bug 语法检查抓不到、jsdom 的结构测试也抓不到 —— 少传一个参数不会报错，
只有用户真的从那个入口走一遍才会撞上。所以补一条静态扫描：数参数个数。

唯一允许不带来源的是阅读器「← 返回」回作品页那处（它刻意沿用上次的来源，
见 gui.html 里 btnReaderBack 的注释）。白名单用"第一个参数必须是 currentWorkId"
钉死，避免日后有人拿它当挡箭牌乱加单参调用。

openEdit 与 openChapterEdit 没有例外：它们每次都该显式拿到来源。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "asase_reader" / "assets" / "gui.html"

ALLOW_SINGLE_ARG = {"openWork": "currentWorkId"}


def strip_comments(src: str) -> str:
    """去掉注释再扫：注释里举例写的 openWork(id) 不该被当成调用点。

    块注释按换行数补回等量换行 —— 直接替换成空格会让后面统计的行号整体偏移，
    报告里指错位置，比不报还糟（第一次跑就偏了 60 行）。
    """
    def repl(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")

    src = re.sub(r"/\*.*?\*/", repl, src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def split_top_level(s: str) -> list[str]:
    """按顶层的逗号切参数（括号里的逗号不算，避免 `f(a, b)` 被切成两段）。"""
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def calls_with_args(src: str, name: str) -> list[tuple[int, list[str]]]:
    """找出所有 name(...) 调用点：逐个括号配对，返回 (行号, 参数列表)。"""
    out: list[tuple[int, list[str]]] = []
    for m in re.finditer(r"(?<![.\w$])" + name + r"\s*\(", src):
        i = m.end() - 1          # 指向 '('
        depth = 0
        j = i
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        args = split_top_level(src[i + 1:j])
        line = src.count("\n", 0, m.start()) + 1
        out.append((line, args))
    return out


def main() -> int:
    if not HTML.is_file():
        print(f"FAIL 找不到 {HTML}")
        return 1
    src = strip_comments(HTML.read_text(encoding="utf-8"))

    bad: list[str] = []
    total = 0
    for name, allow_first in ALLOW_SINGLE_ARG.items():
        for line, args in calls_with_args(src, name):
            total += 1
            if len(args) >= 2:
                continue
            if args and args[0] == allow_first:
                continue
            bad.append(f"第 {line} 行 {name}({', '.join(args) or '无参'})：没带来源参数")

    # openEdit 没有例外：它每次都该显式拿到来源
    # openChapterEdit 同理（v1.16 的章节编辑页）：不带来源就会把从编辑作品页进来的人送回书架
    for name in ("openEdit", "openChapterEdit"):
        for line, args in calls_with_args(src, name):
            total += 1
            if len(args) < 2:
                bad.append(f"第 {line} 行 {name}({', '.join(args) or '无参'})：没带来源参数")

    print(f"进页调用点：{total} 处（openWork / openEdit，含函数定义处）")
    if bad:
        print("\nFAIL 以下调用点没传来源参数 —— 会重现 v1.8.1 那个"
              "「从所有作品页进来却被送回书架」的 bug：")
        for b in bad:
            print("  " + b)
        print(f"\n共 {len(bad)} 处。修法：把来源（\"shelf\" / \"allworks\"）作为"
              "第二个参数传进去。")
        return 1
    print("OK 所有进页调用点都带上了来源参数")
    return 0


if __name__ == "__main__":
    sys.exit(main())
