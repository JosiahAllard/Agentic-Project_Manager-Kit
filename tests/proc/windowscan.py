"""The reader of window hazards that tests/proc/test_spawn_sites.py (class Hazards) and
tests/proc/test_entry_points.py share: a helper, not a test module, so importing it runs no tests.

  scan_python(text, rel)    [(rel, function, what, line)] for one Python module's source
  scan_script(text, rel)    [(rel, '<script>', what, line)] for one .ps1, .cmd or .bat file
  process_uses(text)        [line] of every place a Python module reaches a way to start a process

A window hazard is a construct that can open a console window whatever kitlib.proc.console's
CreateProcess hook does: the hook wraps _winapi.CreateProcess, the one door subprocess and
multiprocessing go through, so what goes round that door, or asks Windows for a window on purpose,
is out of its reach. What scan_python finds (`what` is the label on the left):

- 'os.system', 'os.spawn*', 'os.exec*' (the name as written: 'os.spawnv', 'os.execl'...), and
  'from os import <it>': the C runtime starts these with CreateProcess itself, past the hook.
- 'os.startfile', 'ShellExecute*' (any name that starts so): the shell starts the program, past
  the hook, with a window of the program's choosing.
- 'CreateProcessW', 'CreateProcessA', 'CreateProcessAsUserW', 'CreateProcessAsUserA',
  'CreateProcessWithLogonW', 'CreateProcessWithTokenW': kernel32/advapi32 reached through ctypes,
  past the hook.
- 'CREATE_NEW_CONSOLE': a console window asked for.
- 'DETACHED_PROCESS': a console program started so opens a window of its own as soon as it starts
  anything that needs a console. Not found where the start's argv visibly names pythonw (below).
- 'AllocConsole', 'AllocConsoleWithOptions', 'AttachConsole': a console made or joined by hand.
- 'WMI Win32_Process Create': a start through WMI, which no hook in this process sees.
- 'Start-Process', 'cmd /c start', 'start ""', '-WindowStyle Hidden': PowerShell and cmd ways to
  start a program in a window of its own, or to hide one after Windows has shown it (Windows
  Terminal shows it anyway).

How each is seen in Python source: a name or attribute (load, not the left side of an
assignment), an imported name, or a string constant that is exactly that name (getattr(subprocess,
'DETACHED_PROCESS')). CREATE_NEW_CONSOLE and DETACHED_PROCESS are also seen as an int literal
holding their bit (0x10, 0x8) inside a `creationflags=` keyword or on the right of an assignment
whose target's name holds CREATION, CONSOLE or DETACH. WMI: a string constant that names
Win32_Process (not Win32_ProcessStartup) and Create. The PowerShell and cmd forms: a string
constant (not a docstring) matching SCRIPT_PATTERNS, or a list or tuple of string constants with
'Start-Process', '/c' then 'start', or '-WindowStyle' then 'Hidden' among its items. A
DETACHED_PROCESS inside the `creationflags=` of a call whose first argument is a list or tuple
whose first item's source text names pythonw is not reported: pythonw is a GUI program that never
gets a console.

scan_script reads every line that is not a comment ('#' in PowerShell, 'rem' or '::' in cmd)
against SCRIPT_PATTERNS.

What it cannot see: a name built at run time (getattr with a computed string, a flag value read
from elsewhere), a start inside a string that is not one of the forms above, code in any other
language, and a DETACHED_PROCESS value handed through a variable whose name does not say so.
"""
import ast
import re

OS_BYPASS = ('system', 'startfile')
OS_PREFIXES = ('spawn', 'exec')
WIN_STARTS = ('CreateProcessW', 'CreateProcessA', 'CreateProcessAsUserW', 'CreateProcessAsUserA',
              'CreateProcessWithLogonW', 'CreateProcessWithTokenW')
CONSOLES = ('AllocConsole', 'AllocConsoleWithOptions', 'AttachConsole')
FLAGS = {'CREATE_NEW_CONSOLE': 0x10, 'DETACHED_PROCESS': 0x8}
NAMES = WIN_STARTS + CONSOLES + tuple(FLAGS)
FLAG_TARGET = re.compile(r'CREATION|CONSOLE|DETACH', re.I)
WMI = re.compile(r'\bWin32_Process\b(?!Startup)')
WMI_CREATE = re.compile(r'\bCreate\b')
SCRIPT_PATTERNS = (
    ('Start-Process', re.compile(r'\bStart-Process\b', re.I)),
    ('cmd /c start', re.compile(r'\bcmd(?:\.exe)?["\']?\s+/[ck]\s+start\b', re.I)),
    ('start ""', re.compile(r'(?<![\w-])start\s+""', re.I)),
    ('-WindowStyle Hidden', re.compile(r'-w\w*\s+[\'"]?hidden\b', re.I)),
)
PROCESS_MODULES = ('subprocess', 'multiprocessing', 'concurrent.futures', 'asyncio')


def _docstrings(tree):
    """id() of every docstring node: the first string statement of a module, class or function."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _names_pythonw(call):
    """True when the call's first argument is a list or tuple whose first item names pythonw."""
    if not call.args or not isinstance(call.args[0], (ast.List, ast.Tuple)) or not call.args[0].elts:
        return False
    return 'pythonw' in ast.unparse(call.args[0].elts[0]).lower()


def _string_hits(text):
    hits = [what for what, rx in SCRIPT_PATTERNS if rx.search(text)]
    if WMI.search(text) and WMI_CREATE.search(text):
        hits.append('WMI Win32_Process Create')
    return hits


def _sequence_hits(items):
    """The PowerShell and cmd forms in an argv written as a list of strings."""
    low = [s.lower() for s in items]
    hits = []
    if 'start-process' in low:
        hits.append('Start-Process')
    pairs = list(zip(low, low[1:]))
    if ('/c', 'start') in pairs or ('/k', 'start') in pairs:
        hits.append('cmd /c start')
    if any(a.startswith('-w') and a[1:] in 'windowstyle' and b == 'hidden' for a, b in pairs):
        hits.append('-WindowStyle Hidden')
    return hits


class _Scan(ast.NodeVisitor):

    def __init__(self, tree):
        self.where = []
        self.os_names = set()
        self.found = []
        self.docs = _docstrings(tree)
        self.pythonw_ok = set()         # id() of nodes inside the creationflags of a pythonw start
        self.flag_values = set()        # id() of nodes where an int literal is read as flags
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == 'os':
                        self.os_names.add(a.asname or 'os')
            elif isinstance(node, ast.Call):
                for k in node.keywords:
                    if k.arg == 'creationflags':
                        inside = set(id(n) for n in ast.walk(k.value))
                        self.flag_values |= inside
                        if _names_pythonw(node):
                            self.pythonw_ok |= inside
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if node.value is not None and any(
                        isinstance(t, ast.Name) and FLAG_TARGET.search(t.id) for t in targets):
                    self.flag_values |= set(id(n) for n in ast.walk(node.value))

    def add(self, what, node):
        if what == 'DETACHED_PROCESS' and id(node) in self.pythonw_ok:
            return
        self.found.append(('.'.join(self.where) or '<module>', what, node.lineno))

    def scoped(self, node):
        self.where.append(node.name)
        self.generic_visit(node)
        self.where.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = scoped

    def visit_ImportFrom(self, node):
        for a in node.names:
            if node.module == 'os' and (a.name in OS_BYPASS or a.name.startswith(OS_PREFIXES)):
                self.add('from os import %s' % a.name, node)
            elif a.name in NAMES or a.name.startswith('ShellExecute'):
                self.add(a.name, node)

    def name_hit(self, name, node):
        if name in NAMES:
            self.add(name, node)
        elif name.startswith('ShellExecute'):
            self.add(name, node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.name_hit(node.id, node)

    def visit_Attribute(self, node):
        if (isinstance(node.value, ast.Name) and node.value.id in self.os_names
                and (node.attr in OS_BYPASS or node.attr.startswith(OS_PREFIXES))):
            self.add('os.%s' % node.attr, node)
        elif isinstance(node.ctx, ast.Load):
            self.name_hit(node.attr, node)
        self.generic_visit(node)

    def visit_Constant(self, node):
        v = node.value
        if isinstance(v, str):
            if id(node) in self.docs:
                return
            if v in NAMES or (v.startswith('ShellExecute') and v.isidentifier()):
                self.add(v, node)
                return
            for what in _string_hits(v):
                self.add(what, node)
        elif type(v) is int and id(node) in self.flag_values:
            for name, bit in FLAGS.items():
                if v & bit:
                    self.add(name, node)

    def visit_List(self, node):
        items = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if len(items) > 1:
            for what in _sequence_hits(items):
                self.add(what, node)
        self.generic_visit(node)

    visit_Tuple = visit_List


def scan_python(text, rel):
    """[(rel, function, what, line)] for one Python module's source."""
    tree = ast.parse(text, rel)
    scan = _Scan(tree)
    scan.visit(tree)
    return [(rel, func, what, line) for func, what, line in scan.found]


def scan_script(text, rel):
    """[(rel, '<script>', what, line)] for one .ps1, .cmd or .bat file."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip().lower()
        if s.startswith(('#', 'rem ', '::')) or s == 'rem':
            continue
        found.extend((rel, '<script>', what, n) for what, rx in SCRIPT_PATTERNS if rx.search(line))
    return found


def process_uses(text, rel='<source>'):
    """[line] of every place the module reaches a way to start a process: an attribute of
    subprocess or multiprocessing (any: PIPE too, a conservative reading), an import from them,
    `import multiprocessing`, os.system/startfile/popen/spawn*/exec*, and asyncio's
    create_subprocess_* or ProcessPoolExecutor by name. `import subprocess` alone is no use."""
    tree = ast.parse(text, rel)
    mods, os_names, lines = {}, set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in ('subprocess', 'multiprocessing') or a.name.startswith('multiprocessing.'):
                    mods[a.asname or a.name.split('.')[0]] = a.name
                    if a.name.startswith('multiprocessing'):
                        lines.append(node.lineno)
                elif a.name == 'os':
                    os_names.add(a.asname or 'os')
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ''
            if mod in ('subprocess',) or mod.startswith('multiprocessing'):
                lines.append(node.lineno)
            elif any(a.name in ('ProcessPoolExecutor', 'create_subprocess_exec',
                                'create_subprocess_shell') for a in node.names):
                lines.append(node.lineno)
            elif mod == 'os' and any(a.name in OS_BYPASS + ('popen',) or a.name.startswith(OS_PREFIXES)
                                     for a in node.names):
                lines.append(node.lineno)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in mods:
                lines.append(node.lineno)
            elif node.value.id in os_names and (node.attr in OS_BYPASS + ('popen',)
                                                or node.attr.startswith(OS_PREFIXES)):
                lines.append(node.lineno)
            elif node.attr in ('ProcessPoolExecutor', 'create_subprocess_exec',
                               'create_subprocess_shell'):
                lines.append(node.lineno)
    return sorted(set(lines))


def kitlib_imports(text, rel='<source>'):
    """[line] of every module-level `import kitlib...` / `from kitlib... import` (in an if, try or
    with at module level too; never inside a function or class)."""
    tree = ast.parse(text, rel)
    lines = []

    def walk(body):
        for node in body:
            if isinstance(node, ast.Import) and any(
                    a.name == 'kitlib' or a.name.startswith('kitlib.') for a in node.names):
                lines.append(node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and (
                    node.module == 'kitlib' or (node.module or '').startswith('kitlib.')):
                lines.append(node.lineno)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            else:
                for field in ('body', 'orelse', 'finalbody', 'handlers'):
                    sub = getattr(node, field, None)
                    if isinstance(sub, list):
                        walk([s for s in sub if isinstance(s, ast.AST)])
    walk(tree.body)
    return sorted(lines)
