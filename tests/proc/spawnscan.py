"""The reader of process starts that tests/proc/test_spawn_sites.py and
tests/hubsite/test_package.py share: a helper, not a test module, so importing it runs no tests.

  scan_source(text, rel)    [(rel, function, what, line)] for one module's source

What it finds and what it cannot see: tests/proc/test_spawn_sites.py, its docstring.
"""
import ast

SPAWNERS = ('run', 'Popen', 'call', 'check_call', 'check_output', 'getoutput', 'getstatusoutput')
OS_STARTS = ('system', 'startfile', 'popen')
ASYNC_STARTS = ('create_subprocess_exec', 'create_subprocess_shell')
POOLS = ('ProcessPoolExecutor',)


class _Scan(ast.NodeVisitor):
    """Collects (function, what, line) for one module."""

    def __init__(self):
        self.where = []                 # the enclosing classes and functions
        self.modules = set()            # the names `subprocess` is imported under
        self.os_names = set()
        self.called = set()             # id() of the Attribute nodes that are a call's function
        self.found = []

    def add(self, what, node):
        self.found.append(('.'.join(self.where) or '<module>', what, node.lineno))

    def scoped(self, node):
        self.where.append(node.name)
        self.generic_visit(node)
        self.where.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = scoped

    def visit_Import(self, node):
        for a in node.names:
            top = a.name.split('.')[0]
            if a.name == 'subprocess':
                self.modules.add(a.asname or 'subprocess')
            elif a.name == 'os':
                self.os_names.add(a.asname or 'os')
            elif top == 'multiprocessing':
                self.add('import multiprocessing', node)

    def visit_ImportFrom(self, node):
        mod = node.module or ''
        for a in node.names:
            if mod == 'subprocess' and a.name in SPAWNERS + ('*',):
                self.add('from subprocess import %s' % a.name, node)
            elif mod == 'os' and (a.name in OS_STARTS or a.name.startswith('spawn')):
                self.add('from os import %s' % a.name, node)
            elif mod.split('.')[0] == 'multiprocessing':
                self.add('import multiprocessing', node)
            elif a.name in POOLS + ASYNC_STARTS:
                self.add('from %s import %s' % (mod, a.name), node)

    def spawner(self, node):
        """'subprocess.run' when node is <a subprocess name>.<spawner>, else None."""
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in self.modules and node.attr in SPAWNERS):
            return 'subprocess.%s' % node.attr
        return None

    def visit_Call(self, node):
        what = self.spawner(node.func)
        if what:
            self.called.add(id(node.func))
            if not any(k.arg == 'creationflags' for k in node.keywords):
                self.add(what, node)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        what = self.spawner(node)
        if what and id(node) not in self.called:
            self.add('ref %s' % what, node)
        elif (isinstance(node.value, ast.Name) and node.value.id in self.os_names
              and (node.attr in OS_STARTS or node.attr.startswith('spawn'))):
            self.add('os.%s' % node.attr, node)
        elif node.attr in ASYNC_STARTS + POOLS:
            self.add(node.attr, node)
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in ASYNC_STARTS + POOLS:
            self.add(node.id, node)


def scan_source(text, rel):
    """[(rel, function, what, line)] for one module's source."""
    scan = _Scan()
    tree = ast.parse(text, rel)
    for node in ast.walk(tree):         # the imports first: a function may use a later import
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == 'subprocess':
                    scan.modules.add(a.asname or 'subprocess')
                elif a.name == 'os':
                    scan.os_names.add(a.asname or 'os')
    scan.visit(tree)
    return [(rel, func, what, line) for func, what, line in scan.found]
