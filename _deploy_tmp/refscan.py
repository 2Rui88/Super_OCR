"""扫描 app/ 内定义的顶层符号在活链路(app/)与周边(tools/, tests/)的引用次数。"""
import ast
import sys
from pathlib import Path

ROOT = Path(r'C:\Users\Wren-GS\Desktop\exam\01-OCR流水线\服务')
app_dir = ROOT / 'app'

# 收集 app 各模块的顶层定义
defs = {}  # name -> 定义所在文件
for py in app_dir.rglob('*.py'):
    tree = ast.parse(py.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.setdefault(node.name, []).append(py.relative_to(ROOT))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    defs.setdefault(t.id, []).append(py.relative_to(ROOT))

# 各范围内引用计数(不含定义自身文件中的定义行)
def refs_in(files, name):
    n = 0
    for py in files:
        try:
            src = py.read_text(encoding='utf-8')
        except Exception:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name:
                n += 1
            elif isinstance(node, ast.Attribute) and node.attr == name:
                n += 1
    return n

app_pys = sorted(app_dir.rglob('*.py'))
tool_pys = sorted((ROOT / 'tools').rglob('*.py'))
test_pys = sorted((ROOT / 'tests').rglob('*.py'))
frontend = ROOT / 'frontend' / 'index.html'
fe_src = frontend.read_text(encoding='utf-8') if frontend.exists() else ''

print(f"{'symbol':<32} {'defined_in':<28} {'app':>4} {'tools':>6} {'tests':>6} {'frontend':>8}")
for name, where in sorted(defs.items()):
    if name.startswith('__'):
        continue
    a = refs_in([p for p in app_pys if p.relative_to(ROOT) not in where], name)
    t = refs_in(tool_pys, name)
    s = refs_in(test_pys, name)
    f = fe_src.count(name)
    # app 计数排除定义文件后为 0 且 tools/tests 也低 → 死代码候选
    flag = ' <-- DEAD?' if a == 0 else ''
    print(f"{name:<32} {str(where[0]):<28} {a:>4} {t:>6} {s:>6} {f:>8}{flag}")
