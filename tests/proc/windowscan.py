"""The reader of window hazards that tests/proc/test_spawn_sites.py (class Hazards) and
tests/proc/test_entry_points.py share: a helper, not a test module, so importing it runs no tests.

  scan_python(text, rel)    [(rel, function, what, line)] for one Python module's source
  scan_script(text, rel)    [(rel, '<script>', what, line)] for one .ps1, .cmd or .bat file
  process_uses(text)        [line] of every place a Python module reaches a way to start a process
  kitlib_imports(text)      [line] of every module-level kitlib import
  is_python(path)           True for a .py or .pyw file, or a python-shebang file with no extension

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
  A bare name bound to a value holding it (or CREATE_NEW_CONSOLE) is followed: each use of that
  name inside a `creationflags=` is reported too (unless the start names pythonw, for
  DETACHED_PROCESS), so a flag kept in a module constant cannot leak into another start unseen.
- 'AllocConsole', 'AllocConsoleWithOptions', 'AttachConsole': a console made or joined by hand.
- 'WMI Win32_Process Create': a start through WMI, which no hook in this process sees.
- 'Start-Process', 'cmd /c start', 'start ""', '-WindowStyle Hidden': PowerShell and cmd ways to
  start a program in a window of its own, or to hide one after Windows has shown it (Windows
  Terminal shows it anyway).
- 'start in code text': Python code held as text that pythonw runs (`[pythonw, '-c', code]`)
  with a start through subprocess (Popen, run, call, check_call, check_output, reached as
  subprocess.<it>, through `import subprocess as <alias>`, or `from subprocess import <it>`) whose
  `creationflags=` does not visibly hold CREATE_NO_WINDOW (the name, an int literal with its bit,
  or a name the code bound to one of those), unless a kitlib import comes before it in the text.
  Each start is judged on its own: creationflags=0 or a flagged start beside a bare one is a hit.
  pythonw has no console, and a process that never imports kitlib has no hook, so each console
  child it starts opens a window. Code text that is not valid Python (a + chain with a variable
  part, say) is a hit when it names subprocess and calls any of those names: the conservative
  reading.

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

Code text pythonw runs: a string expression (a constant, an f-string's constant parts, or a +
chain of them, joined) that is an item after the first of a list or tuple whose first item's
source text names pythonw, or is the value assigned to a bare name that is such an item (any
assignment to that name in the module), or an item of the list or tuple a `for` over such a name
walks (and the value of a bare name there).

scan_script reads every line that is not a comment ('#' in PowerShell, 'rem' or '::' in cmd)
against SCRIPT_PATTERNS.

Flag names followed: a bare name assigned (anywhere in the module, any scope) a value that holds
DETACHED_PROCESS or CREATE_NEW_CONSOLE by name, as getattr's string, as a bit of an int literal
when the assigned name holds CREATION, CONSOLE or DETACH, or through another followed name.

What it cannot see: code text written to a file and run from there, code text that a console
python runs detached, a name built at run time (getattr with a computed string, a flag value read
from another module), a start inside a string that is not one of the forms above, code in any
other language, a flag kept in an attribute or a dict rather than a bare name, and an int literal
flag bound to a name that does not say CREATION, CONSOLE or DETACH.
"""
import ast
import os
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
CODE_STARTS = ('Popen', 'run', 'call', 'check_call', 'check_output')
CODE_START = re.compile(r'\b(?:%s)\s*\(' % '|'.join(CODE_STARTS))
NO_WINDOW = 0x08000000


def is_python(path):
    """A .py or .pyw file, or a file with no extension whose first line is a python shebang."""
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.py', '.pyw'):
        return True
    if ext:
        return False
    try:
        with open(path, 'rb') as f:
            first = f.readline(200)
    except OSError:
        return False
    return first.startswith(b'#!') and b'python' in first


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


def _string_text(node):
    """The text a string expression holds (a constant, an f-string with each replacement field
    read as the name _, or a chain of + between them, a part that is no string read as _), or None
    when the node is not one."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return ''.join(v.value if isinstance(v, ast.Constant) else '_' for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_text(node.left), _string_text(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else '_') + (right if right is not None else '_')
    return None


def _no_window(value, bound):
    """True when a creationflags value visibly holds CREATE_NO_WINDOW: the name, an int literal
    with its bit, or a name in `bound` (names the code bound to one of those)."""
    for n in ast.walk(value):
        if isinstance(n, ast.Name) and (n.id == 'CREATE_NO_WINDOW' or n.id in bound):
            return True
        if isinstance(n, ast.Attribute) and n.attr == 'CREATE_NO_WINDOW':
            return True
        if isinstance(n, ast.Constant) and type(n.value) is int and n.value & NO_WINDOW:
            return True
    return False


def _code_text_hit(text):
    """True when Python code held as text has a start through subprocess whose creationflags do
    not visibly hold CREATE_NO_WINDOW and that no earlier kitlib import (which installs the
    CreateProcess hook in the process that runs it) covers. Each start is judged on its own."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 'subprocess' in text and bool(CODE_START.search(text))
    mods, funcs, kit = {'subprocess'}, set(), None
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == 'subprocess':
                    mods.add(a.asname or a.name)
                elif a.name.split('.')[0] == 'kitlib':
                    kit = min(kit or (n.lineno, n.col_offset), (n.lineno, n.col_offset))
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            if n.module == 'subprocess':
                for a in n.names:
                    if a.name == '*':
                        funcs.update(CODE_STARTS)
                    elif a.name in CODE_STARTS:
                        funcs.add(a.asname or a.name)
            elif (n.module or '').split('.')[0] == 'kitlib':
                kit = min(kit or (n.lineno, n.col_offset), (n.lineno, n.col_offset))
    assigns = [(t.id, n.value) for n in ast.walk(tree) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)]
    bound, grew = set(), True
    while grew:
        grew = False
        for name, value in assigns:
            if name not in bound and _no_window(value, bound):
                bound.add(name)
                grew = True
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id in mods and f.attr in CODE_STARTS
                or isinstance(f, ast.Name) and f.id in funcs):
            continue
        if kit is not None and kit < (n.lineno, n.col_offset):
            continue
        if not any(k.arg == 'creationflags' and _no_window(k.value, bound) for k in n.keywords):
            return True
    return False


def _flag_labels(value, read_ints, names):
    """The console flags (FLAGS keys) a value holds: by name (a name, an attribute, getattr's
    string), through a name in `names` (name -> labels), or, when read_ints, as an int literal's
    bit."""
    out = set()
    for n in ast.walk(value):
        if isinstance(n, ast.Name):
            out |= names.get(n.id, set()) | ({n.id} & set(FLAGS))
        elif isinstance(n, ast.Attribute) and n.attr in FLAGS:
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in FLAGS:
            out.add(n.value)
        elif read_ints and isinstance(n, ast.Constant) and type(n.value) is int:
            out |= set(name for name, bit in FLAGS.items() if n.value & bit)
    return out


def _flag_names(tree):
    """{bare name: the console flags its value holds} for every name the module binds to one."""
    assigns = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    assigns.append((t.id, node.value, bool(FLAG_TARGET.search(t.id))))
    names, grew = {}, True
    while grew:
        grew = False
        for name, value, read_ints in assigns:
            got = _flag_labels(value, read_ints, names)
            if not got <= names.get(name, set()):
                names[name] = names.get(name, set()) | got
                grew = True
    return names


def _pythonw_code(tree):
    """id() of every expression handed to pythonw in an argv written as a list or tuple whose
    first item's source text names pythonw: the items after it, and for an item that is a bare
    name, the items of a list or tuple a `for` over that name walks, and the value of each
    assignment to such a name in the module."""
    names, out = set(), set()
    for node in ast.walk(tree):
        if (isinstance(node, (ast.List, ast.Tuple)) and node.elts
                and 'pythonw' in ast.unparse(node.elts[0]).lower()):
            for e in node.elts[1:]:
                if isinstance(e, ast.Name):
                    names.add(e.id)
                else:
                    out.add(id(e))
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id in names
                and isinstance(node.iter, (ast.List, ast.Tuple))):
            for e in node.iter.elts:
                if isinstance(e, ast.Name):
                    names.add(e.id)
                else:
                    out.add(id(e))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            out.add(id(node.value))
    return out


class _Scan(ast.NodeVisitor):

    def __init__(self, tree):
        self.where = []
        self.os_names = set()
        self.found = []
        self.docs = _docstrings(tree)
        self.pythonw_ok = set()         # id() of nodes inside the creationflags of a pythonw start
        self.flag_values = set()        # id() of nodes where an int literal is read as flags
        self.in_text = set()            # id() of nodes inside a string expression already read as code
        self.in_flags = set()           # id() of nodes inside a creationflags= value
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
                        self.in_flags |= inside
                        if _names_pythonw(node):
                            self.pythonw_ok |= inside
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if node.value is not None and any(
                        isinstance(t, ast.Name) and FLAG_TARGET.search(t.id) for t in targets):
                    self.flag_values |= set(id(n) for n in ast.walk(node.value))
        self.pythonw_code = _pythonw_code(tree)     # id() of string expressions pythonw runs
        self.flag_names = _flag_names(tree)         # bare name -> the console flags it holds

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
            if node.id not in NAMES and id(node) in self.in_flags:
                for what in sorted(self.flag_names.get(node.id, ())):
                    self.add(what, node)

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
            self.code_text(node)
            if v in NAMES or (v.startswith('ShellExecute') and v.isidentifier()):
                self.add(v, node)
                return
            for what in _string_hits(v):
                self.add(what, node)
        elif type(v) is int and id(node) in self.flag_values:
            for name, bit in FLAGS.items():
                if v & bit:
                    self.add(name, node)

    def code_text(self, node):
        """'start in code text' for a string expression that holds Python code starting a process
        with no window choice; each of its parts is then read only for the other forms."""
        if id(node) in self.in_text or id(node) not in self.pythonw_code:
            return
        text = _string_text(node)
        if not text:
            return
        for sub in ast.walk(node):
            self.in_text.add(id(sub))
        if _code_text_hit(text):
            self.add('start in code text', node)

    def visit_BinOp(self, node):
        self.code_text(node)
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        self.code_text(node)
        self.generic_visit(node)

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
