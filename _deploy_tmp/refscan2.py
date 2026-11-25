"""精确扫描:顶层符号的 文件内引用 / 跨文件引用,识别装饰器注册的处理器。"""
import ast
from pathlib import Path

ROOT = Path(r'C:\Users\Wren-GS\Desktop\exam\01-OCR流水线\服务')
app_dir = ROOT / 'app'


def load(py):
    return ast.parse(py.read_text(encoding='utf-8'))


class RefCounter(ast.NodeVisitor):
    def __init__(self, name, def_node):
        self.name = name
        self.def_node = def_node
        self.count = 0

    def visit_FunctionDef(self, node):
        if node is not self.def_node:
            self.count += 0  # 同名嵌套定义不算引用
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Name(self, node):
        if node.id == self.name:
            self.count += 1
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr == self.name:
            self.count += 1
        self.generic_visit(node)


app_pys = sorted(app_dir.rglob('*.py'))
tool_pys = sorted((ROOT / 'tools').rglob('*.py'))
test_pys = sorted((ROOT / 'tests').rglob('*.py'))
fe = ROOT / 'frontend' / 'index.html'
fe_src = fe.read_text(encoding='utf-8') if fe.exists() else ''

trees = {p: load(p) for p in app_pys + tool_pys + test_pys}

print(f"{'symbol':<30} {'file':<30} {'intra':>5} {'app':>4} {'tools':>5} {'tests':>5} {'fe':>3}  note")
for py in app_pys:
    tree = trees[py]
    rel = str(py.relative_to(ROOT))
    for node in tree.body:
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
            decorated = bool(node.decorator_list)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            decorated = False
        else:
            continue
        for name in names:
            if name.startswith('__'):
                continue
            rc = RefCounter(name, node)
            rc.visit(tree)
            intra = rc.count
            a = t = s = 0
            for other in app_pys:
                if other == py:
                    continue
                rc2 = RefCounter(name, None)
                rc2.visit(trees[other])
                a += rc2.count
            for other in tool_pys:
                rc2 = RefCounter(name, None)
                rc2.visit(trees[other])
                t += rc2.count
            for other in test_pys:
                rc2 = RefCounter(name, None)
                rc2.visit(trees[other])
                s += rc2.count
            f = fe_src.count(name)
            if a == 0 and t == 0 and s == 0 and f == 0 and (intra == 0 or decorated):
                note = 'REGISTERED(handler)' if decorated else 'NO-REF-ANYWHERE'
                print(f"{name:<30} {rel:<30} {intra:>5} {a:>4} {t:>5} {s:>5} {f:>3}  {note}")
