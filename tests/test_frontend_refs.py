"""前端内部函数引用检查（抓「改了函数名但漏改调用点」）。

背景：JS 的函数引用是运行时才解析的。把一个内部函数 `pickFolder` 改名成
`pickFolderXXX` 而不改调用它的地方，`node --check` 语法检查、jsdom 的 DOM
结构测试全都照常通过——只有用户真的点那个按钮时才会 ReferenceError。
变异测试已实测确认这类问题现有自检抓不到，故补这一项。

做法：从 gui.html 里抽出所有函数定义名，再抽出所有调用点
（含 HTML 内联 `onclick="..."` 里的引用），两边比对，断链即失败。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "asase_reader" / "assets" / "gui.html"

# JS 关键字：正则会把 `if (`、`catch (` 也当成"调用"，必须排除
KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "try", "catch", "finally", "throw", "return", "break", "continue",
    "function", "async", "await", "new", "delete", "typeof", "instanceof",
    "in", "of", "void", "yield", "super", "this", "with", "var", "let",
    "const", "class", "extends", "import", "export", "get", "set", "static",
}

# 浏览器 / JS 内置全局：不是本项目定义的函数
BUILTINS = {
    "Object", "Array", "String", "Number", "Boolean", "Math", "JSON", "Date",
    "RegExp", "Error", "TypeError", "RangeError", "SyntaxError", "Map", "Set",
    "WeakMap", "WeakSet", "Promise", "Symbol", "BigInt", "Intl", "Reflect",
    "Proxy", "Function", "ArrayBuffer", "Uint8Array", "DataView",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "setTimeout",
    "setInterval", "clearTimeout", "clearInterval", "requestAnimationFrame",
    "cancelAnimationFrame", "queueMicrotask", "fetch", "alert", "confirm",
    "prompt", "atob", "btoa", "structuredClone", "eval",
    # 全局对象：会被当成 Name( 形式调用（如 console.log 不是，但 JSON.parse 是属性
    # 调用；这里保险起见列出可能出现裸名调用形态的）
    "console", "document", "window", "localStorage", "sessionStorage",
    "location", "navigator", "history", "screen", "performance", "crypto",
    "Blob", "URL", "FormData", "Headers", "Request", "Response", "Image",
    "FileReader", "Event", "CustomEvent", "MutationObserver",
}


def strip_noise(src: str) -> str:
    """去掉注释与字符串字面量，避免把文本里的 "xxx()" 当成调用。"""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"//[^\n]*", " ", src)
    src = re.sub(r"'(?:[^'\\\n]|\\.)*'", '""', src)
    src = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', src)
    src = re.sub(r"`(?:[^`\\]|\\.)*`", '""', src, flags=re.S)
    return src


def defined_names(src: str) -> set[str]:
    """收集所有「可被当作函数调用」的名字：函数定义 + 变量声明。

    变量也要算：像 `let _markReady = null; ... _markReady()` 这种把 resolve
    存进变量再调用的写法，只看函数定义会误报。
    """
    names: set[str] = set()
    pats = [
        r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",                 # function foo / async function foo
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b",  # const foo = function
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>",  # const foo = () =>
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?[A-Za-z_$][\w$]*\s*=>",  # const foo = a =>
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)",                     # const foo = <任意值>
        # 对象字面量里的方法简写：`return { refresh() { ... } }` —— 它确实是定义，
        # 但调用点扫描会把 `refresh(` 当成一次调用，不收进来就是误报。
        # 用 `{` / `,` 开头定位，不依赖行首锚点（findall 没开 MULTILINE）。
        r"[\{,]\s*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
    ]
    for p in pats:
        names.update(re.findall(p, src, flags=re.S))
    return names


def local_names(src: str) -> set[str]:
    """收集函数参数名（箭头函数 + 普通 function 声明），避免把形参当断链。"""
    out: set[str] = set()
    chunks: list[str] = []
    for m in re.finditer(r"\(([^()]*)\)\s*=>", src, flags=re.S):
        chunks.append(m.group(1))
    # 普通函数声明的形参同样要收：function mkChip(text, cls, onRemove) 里的
    # onRemove 在别的行被调用，光看箭头函数参数会漏
    for m in re.finditer(r"function\s+[A-Za-z_$][\w$]*\s*\(([^)]*)\)", src, flags=re.S):
        chunks.append(m.group(1))
    for chunk in chunks:
        for part in chunk.split(","):
            part = part.strip().strip("{}[]").split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", part):
                out.add(part)
    return out


def call_names(src: str) -> set[str]:
    """收集所有裸名调用点（形如 foo(），属性调用 obj.foo( 不算。"""
    return set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", src))


def inline_handler_refs(html_body: str) -> set[str]:
    """HTML 内联事件里的引用：既匹配 `onclick="foo()"` 也匹配 `onclick="foo"`。"""
    out: set[str] = set()
    for m in re.finditer(r"on[a-z]+\s*=\s*\"([^\"]*)\"", html_body):
        code = m.group(1).strip()
        out.update(re.findall(r"([A-Za-z_$][\w$]*)\s*\(", code))
        if re.fullmatch(r"[A-Za-z_$][\w$]*", code):
            out.add(code)
    return out


def main() -> int:
    if not HTML.is_file():
        print(f"FAIL 找不到 {HTML}")
        return 1
    raw = HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", raw, flags=re.S)
    if not scripts:
        print("FAIL gui.html 里没有 <script>，结构异常")
        return 1
    js = "\n".join(scripts)
    js_clean = strip_noise(js)

    defined = defined_names(js_clean)
    calls = call_names(js_clean) | inline_handler_refs(raw)

    # 只关心"既不是本项目定义的函数、也不是内置/关键字"的调用
    unknown = {
        c for c in calls
        if c not in defined
        and c not in BUILTINS
        and c not in KEYWORDS
        and c not in local_names(js_clean)
    }

    print(f"函数定义：{len(defined)} 个")
    print(f"调用点：{len(calls)} 个")
    if unknown:
        print("\nFAIL 以下名字被调用，但在 gui.html 里找不到定义：")
        for n in sorted(unknown):
            print("  " + n)
        print(f"\n共 {len(unknown)} 处断链（改了函数名却漏改调用点，运行时才会炸）")
        return 1
    print("OK 所有调用点都能找到对应定义")
    return 0


if __name__ == "__main__":
    sys.exit(main())
