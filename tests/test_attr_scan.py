"""静态扫描：类里 self.X 的读取，是否在同类的某处被赋值（__init__ 或方法内）。

用于抓「重构改了属性名但漏改引用」这类只在运行时才炸的问题
（1.1.1 的 library.outputs_root -> shelf_root 就是典型）。

判定规则：
- **无基类的类**：self.X 读不到赋值 -> 判定为问题，退出码 1。
- **有基类的类**：属性很可能来自父类（如 BaseHTTPRequestHandler 的 wfile /
  send_error），扫不出来，只打印不计入失败，避免 21 条误报淹没真问题。

注意：以前这个脚本末尾写死 sys.exit(0)，扫出什么都是绿灯 —— 那比没有更危险，
会让人以为"检查过了"。已修正。
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src" / "asase_reader"


def scan(path: Path) -> tuple[list[str], list[str]]:
    """返回 (确定的问题, 继承来的疑似项)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    inherited: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        has_base = bool(node.bases)
        assigned: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.AnnAssign):
                # 两种形态都要认：类里的 `x: int` 和 `self._meta_cache: dict = {}`
                tgt = sub.target
                if isinstance(tgt, ast.Name):
                    assigned.add(tgt.id)
                elif isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name) \
                        and tgt.value.id == "self":
                    assigned.add(tgt.attr)
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        assigned.add(t.id)
                    elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                            and t.value.id == "self":
                        assigned.add(t.attr)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # async def 也算，否则 Backend._worker / _run_job 会被误报
                assigned.add(sub.name)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "self" and isinstance(sub.ctx, ast.Load):
                if sub.attr not in assigned:
                    line = f"{path.name}:{sub.lineno} {node.name}.{sub.attr}"
                    (inherited if has_base else problems).append(line)
    return problems, inherited


def main() -> int:
    allp: list[str] = []
    allm: list[str] = []
    for f in sorted(ROOT.glob("*.py")):
        p, m = scan(f)
        allp += p
        allm += m

    if allp:
        print("确定的问题（无基类的类，self.X 从未被赋值）：")
        for p in allp:
            print("  " + p)
    if allm:
        print(f"\n继承自基类的属性（不判定为失败，共 {len(allm)} 条）：")
        for p in allm[:10]:
            print("  " + p)
        if len(allm) > 10:
            print(f"  ... 另有 {len(allm) - 10} 条")

    print(f"\n确定的问题：{len(allp)}")
    return 1 if allp else 0


if __name__ == "__main__":
    sys.exit(main())
