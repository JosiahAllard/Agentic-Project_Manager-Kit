"""No kit code builds a machine-config path in the home folder: the machine config lives in the PM
repository the PM-repo pointer names (kitlib.config.user.locate), so a new home-folder path would
bring back a copy a new user has to make and keep by hand.

It reads the tracked Python and PowerShell files of kitlib/, tools/, boot/, hosts/ and installer/
and refuses, outside ALLOWED:
  - a string (not a docstring) that spells the home-folder config folder: `~/.pm-kit`,
    `%USERPROFILE%\\.pm-kit`, `$env:USERPROFILE\\.pm-kit`, `$HOME\\.pm-kit`;
  - a '~/...' format string filled with the config folder's name (`'~/%s' % user.DIR_NAME`, an
    f-string);
  - a call, a `.joinpath` or a `/` join whose later parts hold the config folder (`'.pm-kit'`,
    `user.DIR_NAME`, `kuser.DIR_NAME`, `kitlib.config.user.DIR_NAME`, an import alias of it or of
    kitlib.config.user (`KU.DIR_NAME`), or a name the module assigns from one, as
    `FOLDER = user.DIR_NAME`) and whose first part (a joinpath's receiver) comes from the home
    folder: a name in it says so (home, USERPROFILE, expanduser, home_dir, Path.home, a keyword
    `home=`, a string naming USERPROFILE or HOME), or a name assigned from it, in the same
    function or a scope around it (`h = home_dir(); join(h, DIR_NAME)`, a module's
    `BASE = os.path.expanduser('~')` used in a function), lambdas included;
  - a PowerShell line, not a comment, naming `.pm-kit` with USERPROFILE, HOME or `~`, or with a
    variable an earlier line set from one (`$h = $env:USERPROFILE`).
It reads AST node names, not source text, and follows plain-name assignments only: a home folder
passed through an attribute, a container, a function's return value or another module is not
followed.
ALLOWED exempts one function (or module-level name) of a file, never the whole file.
Docstrings and comments may still name the home folder: they describe the move and its leftovers."""
import ast
import re
import unittest

from tests.scrub.test_scrub import read_text, tracked_files

ROOTS = ('kitlib/', 'tools/', 'boot/', 'hosts/', 'installer/')
CONFIG = '.pm-kit'

# (path, scope): why that one function (or module-level name) may name the home folder's machine
# config. Each is a leftover's handling, read or guarded, never a place the kit keeps its config.
# The scope is the function's qualified name, or the name a module-level line assigns, so new
# home-path code elsewhere in the same file is still refused. Only scopes the check finds
# something in are listed (test_every_allowed_scope_still_needs_it).
ALLOWED = {
    ('kitlib/config/user.py', 'home_location'): 'the home folder\'s config, the move\'s source '
                                                '(kitlib.config.move), never what the kit reads',
    ('kitlib/config/user.py', 'locate'): 'the leftover check: a home-folder copy is reported '
                                         '(Location.leftover), never read',
    ('kitlib/delegations/load.py', 'delegations_file'): 'the home folder\'s delegations copy, the '
                                                        'move\'s source, which the guard still holds',
    ('kitlib/guard/jail.py', 'home_config_dirs'): 'no write root may be on a leftover home config '
                                                  'folder',
    ('kitlib/guard/rules.py', 'hub_secret_folders'): 'guards the leftover home token folder the '
                                                     'move leaves as its source',
    ('kitlib/autotasks/tick.py', 'state_dir'): 'the auto-tasks state, machine-local; an owner '
                                               'question on where it belongs is open',
}

SPELLED = re.compile(r'(?i)(?:~|%USERPROFILE%|\$env:USERPROFILE|\$HOME)[/\\]\.pm-kit(?![\w-])')
HOMEISH = re.compile(r'(?i)home|USERPROFILE|expanduser')
SHELL_HOME = re.compile(r'(?i)USERPROFILE|\$HOME\b|~[/\\]')
SHELL_SET = re.compile(r'^\s*\$(\w+)\s*=(.*)$')
SHELL_VAR = re.compile(r'\$(\w+)')
CONFIG_NAMES = ('user', 'kuser')
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _docstrings(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = n.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _is_config(node, aliases=()):
    """Whether `node` is the config folder's name: the literal, kitlib.config.user.DIR_NAME (as
    `user.`, `kuser.`, a module alias or the full dotted name), or a name in `aliases` (one the module gives it)."""
    if isinstance(node, ast.Constant):
        return node.value == CONFIG
    if isinstance(node, ast.Name):
        return node.id == 'DIR_NAME' or node.id in aliases
    if isinstance(node, ast.Attribute) and node.attr == 'DIR_NAME':
        v = node.value
        return ((isinstance(v, ast.Name) and (v.id in CONFIG_NAMES or ('module', v.id) in aliases))
                or (isinstance(v, ast.Attribute) and v.attr == 'user'))
    return False


def _holds_config(node, aliases=()):
    return any(_is_config(n, aliases) for n in ast.walk(node))


def _target_names(targets):
    """The plain names assigned to: `h` and the parts of `a, b`, never `plan` of `plan.home`."""
    out = []
    for t in targets:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            out.extend(_target_names(t.elts))
    return out


def _config_aliases(tree):
    """The names a module gives the config folder's name, anywhere in it: `FOLDER = user.DIR_NAME`
    and `from kitlib.config.user import DIR_NAME as D`; and, as ('module', name), the names it
    gives kitlib.config.user (`import kitlib.config.user as KU`)."""
    aliases, assigns = set(), []
    for a in ast.walk(tree):
        if isinstance(a, ast.ImportFrom):
            aliases.update(al.asname for al in a.names if al.name == 'DIR_NAME' and al.asname)
            if a.module == 'kitlib.config':
                aliases.update(('module', al.asname) for al in a.names
                               if al.name == 'user' and al.asname)
        elif isinstance(a, ast.Import):
            aliases.update(('module', al.asname) for al in a.names
                           if al.name == 'kitlib.config.user' and al.asname)
        elif isinstance(a, ast.Assign):
            assigns.append(a)
    grew = True
    while grew:
        grew = False
        for a in assigns:
            new = [t for t in _target_names(a.targets) if t not in aliases]
            if new and _is_config(a.value, aliases):
                aliases.update(new)
                grew = True
    return aliases


def _homeish(node, held=()):
    """Whether `node` comes from the home folder, by the names in it: one that says so (home,
    home_dir, Path.home, expanduser, USERPROFILE), a keyword or a string naming it (`home=True`,
    '%USERPROFILE%', '$HOME'), or a name in `held` (one assigned from the home folder). It reads names, never the
    source text, so a file is split once (ast.parse), not once per node."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            if n.id in held or HOMEISH.search(n.id):
                return True
        elif isinstance(n, ast.Attribute):
            if HOMEISH.search(n.attr):
                return True
        elif isinstance(n, ast.keyword):
            if n.arg and HOMEISH.search(n.arg):
                return True
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and HOMEISH.search(n.value):
            return True
    return False


def _div_parts(node):
    """The operands of a chain of `/` joins, leftmost first."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_parts(node.left) + [node.right]
    return [node]


def _joins(n):
    """The (home side, config side) pairs of node `n`: a call's first argument and its other
    arguments; a `.joinpath` call's receiver and its arguments (`Path(h).joinpath(DIR_NAME)`); a
    `/` chain's first part and the rest."""
    if isinstance(n, ast.Call):
        out = [(n.args[0], n.args[1:])] if n.args else []
        if isinstance(n.func, ast.Attribute) and n.func.attr == 'joinpath':
            out.append((n.func.value, n.args))
        return out
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
        parts = _div_parts(n)
        return [(parts[0], parts[1:])]
    return []


def _own_nodes(scope):
    """The nodes of a module, class or function, without those of the functions and classes
    defined in it (their own scopes); a lambda's body is its enclosing scope's."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, SCOPES):
            todo.extend(ast.iter_child_nodes(n))


def _params(scope):
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    a = scope.args
    return ({x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
            | {x.arg for x in (a.vararg, a.kwarg) if x})


def _held_home(nodes, inherited):
    """The names a scope holds from the home folder: those of the scopes around it (`inherited`)
    and those it assigns from the home folder (home_dir(), expanduser, USERPROFILE, or a name
    already held), to a fixed point: `h = home_dir(); q = join(h, 'x')` holds both."""
    held = set(inherited)
    assigns = [n for n in nodes if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
               and n.value is not None]
    grew = True
    while grew:
        grew = False
        for a in assigns:
            targets = a.targets if isinstance(a, ast.Assign) else [a.target]
            new = [t for t in _target_names(targets) if t not in held]
            if new and _homeish(a.value, held):
                held.update(new)
                grew = True
    return held


def _module_name(tree, line):
    """The name a module-level statement on `line` assigns (`DISPLAY = ...`), or '<module>'."""
    for st in tree.body:
        if st.lineno <= line <= st.end_lineno:
            targets = st.targets if isinstance(st, ast.Assign) else (
                [st.target] if isinstance(st, ast.AnnAssign) else [])
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                return targets[0].id
    return '<module>'


def _scope_findings(nodes, held, docs, aliases):
    found = []
    for n in nodes:
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            if SPELLED.search(n.value):
                found.append((n.lineno, 'spells the home config folder'))
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
            left = n.left
            if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                    and left.value.startswith(('~/', '~\\')) and _holds_config(n.right, aliases)):
                found.append((n.lineno, 'formats ~/ with the config folder'))
        elif isinstance(n, ast.JoinedStr):
            head = n.values[0] if n.values else None
            if (isinstance(head, ast.Constant) and isinstance(head.value, str)
                    and head.value.startswith(('~/', '~\\')) and _holds_config(n, aliases)):
                found.append((n.lineno, 'formats ~/ with the config folder'))
        for home, rest in _joins(n):
            if any(_is_config(p, aliases) for p in rest) and _homeish(home, held):
                found.append((n.lineno, 'joins the config folder to the home folder'))
                break
    return found


def python_findings(src):
    """[(line, what, scope)] for one Python file's text. The scope is the qualified name of the
    function or class the line is in, or for a module-level line the name it assigns. A name a
    scope holds from the home folder is held in the functions inside it too (not one a function
    takes as a parameter), and a class body's names are not passed to its methods."""
    tree = ast.parse(src)
    docs = _docstrings(tree)
    aliases = _config_aliases(tree)
    found = set()                     # a chain of `/` joins is one finding, not one per link

    def visit(scope, name, inherited):
        nodes = list(_own_nodes(scope))
        held = _held_home(nodes, set(inherited) - _params(scope))
        for line, what in _scope_findings(nodes, held, docs, aliases):
            found.add((line, what, name or _module_name(tree, line)))
        passed = inherited if isinstance(scope, ast.ClassDef) else held
        for n in nodes:
            if isinstance(n, SCOPES):
                visit(n, (name + '.' if name else '') + n.name, passed)

    visit(tree, None, frozenset())
    return sorted(found)


def shell_findings(text):
    """[(line, what)] for one PowerShell file's text: a line, not a comment, that names the
    config folder with the home folder, or with a variable an earlier line set from it
    (`$h = $env:USERPROFILE`, then `Join-Path $h '.pm-kit'`)."""
    found = []
    held = set()
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('#'):
            continue
        uses = {v.lower() for v in SHELL_VAR.findall(line)} & held
        m = SHELL_SET.match(line)
        if m and (SHELL_HOME.search(m.group(2))
                  or {v.lower() for v in SHELL_VAR.findall(m.group(2))} & held):
            held.add(m.group(1).lower())
        if SPELLED.search(line) or (CONFIG in line and (SHELL_HOME.search(line) or uses)):
            found.append((i, 'names the home config folder'))
    return found


def findings(files, read):
    out = []
    for rel in files:
        if not rel.startswith(ROOTS):
            continue
        text = read(rel)
        if text is None:
            continue
        if rel.endswith('.py'):
            out.extend('%s:%d: %s (in %s)' % (rel, line, what, scope)
                       for line, what, scope in python_findings(text)
                       if (rel, scope) not in ALLOWED)
        elif rel.endswith(('.ps1', '.psm1')):
            out.extend('%s:%d: %s' % (rel, line, what) for line, what in shell_findings(text))
    return out


class NoHomeConfigPathTest(unittest.TestCase):

    def test_no_kit_code_builds_a_home_folder_config_path(self):
        found = findings(tracked_files(), read_text)
        self.assertEqual(found, [], 'kit code names a machine-config path in the home folder; '
                                    'derive it from kitlib.config.user.locate (the PM repository), '
                                    'or add its (file, function) to ALLOWED with its reason')

    def test_every_allowed_scope_still_needs_it(self):
        stale = [key for key in sorted(ALLOWED) if key[1] not in
                 {scope for _, _, scope in python_findings(read_text(key[0]) or '')}]
        self.assertEqual(stale, [], 'ALLOWED names a scope with nothing found (gone, renamed, or '
                                    'its home path removed): drop or fix its line')

    def test_each_form_is_caught(self):
        cases = {
            "p = os.path.join(home, '.pm-kit', 'config.json')\n": 1,
            "p = os.path.join(os.environ['USERPROFILE'], user.DIR_NAME)\n": 1,
            "p = os.path.join(kuser.home_dir(), kuser.DIR_NAME, 'x')\n": 1,
            "p = Path.home() / '.pm-kit' / 'hub'\n": 1,
            "s = '~/.pm-kit/config.json'\n": 1,
            "s = '~/%s/%s' % (user.DIR_NAME, 'config.json')\n": 1,
            "s = f'~/{user.DIR_NAME}/config.json'\n": 1,
            "p = os.path.join(pm, user.DIR_NAME, 'config.json')\n": 0,
            "def f():\n    '''reads ~/.pm-kit no more'''\n": 0,
        }
        for src, want in cases.items():
            self.assertEqual(len(python_findings(src)), want, src)
        self.assertEqual(len(shell_findings("$p = Join-Path $env:USERPROFILE '.pm-kit'\n")), 1)
        self.assertEqual(len(shell_findings("# the old ~/.pm-kit folder\n")), 0)

    def test_a_home_folder_held_in_a_local_name_is_caught(self):
        cases = {
            "h = user.home_dir()\np = os.path.join(h, user.DIR_NAME)\n": 1,
            "def f():\n    h = os.path.expanduser('~')\n    return os.path.join(h, '.pm-kit')\n": 1,
            "h = os.environ['USERPROFILE']\nq = os.path.join(h, 'x')\np = os.path.join(q, DIR_NAME)\n": 1,
            "h = Path.home()\np = Path(h) / '.pm-kit'\n": 1,
            "def f():\n    h = user.home_dir()\ndef g(h):\n    return os.path.join(h, DIR_NAME)\n": 0,
            "pm = locate().path\np = os.path.join(pm, user.DIR_NAME)\n": 0,
            "FOLDER = user.DIR_NAME\ndef f():\n    h = home_dir()\n    return os.path.join(h, FOLDER)\n": 1,
            "plan.home = home = home_dir()\np = os.path.join(plan.pm, DIR_NAME)\n": 0,
        }
        for src, want in cases.items():
            self.assertEqual(len(python_findings(src)), want, src)

    def test_a_joinpath_on_the_home_folder_is_caught(self):
        cases = {
            "p = Path(user.home_dir()).joinpath(user.DIR_NAME)\n": 1,
            "p = Path.home().joinpath('.pm-kit', 'hub')\n": 1,
            "p = Path(pm).joinpath(user.DIR_NAME)\n": 0,
        }
        for src, want in cases.items():
            self.assertEqual(len(python_findings(src)), want, src)

    def test_a_module_name_held_from_the_home_folder_is_caught_in_a_function(self):
        cases = {
            "BASE = os.path.expanduser('~')\ndef f():\n    return os.path.join(BASE, DIR_NAME)\n": 1,
            "BASE = os.path.expanduser('~')\nclass C:\n    def f(self):\n"
            "        return os.path.join(BASE, DIR_NAME)\n": 1,
            "BASE = os.path.expanduser('~')\ndef f(BASE):\n    return os.path.join(BASE, DIR_NAME)\n": 0,
        }
        for src, want in cases.items():
            self.assertEqual(len(python_findings(src)), want, src)

    def test_an_imported_alias_a_dotted_name_and_a_lambda_are_caught(self):
        cases = {
            "from kitlib.config.user import DIR_NAME as D\np = os.path.join(home_dir(), D)\n": 1,
            "p = os.path.join(home_dir(), kitlib.config.user.DIR_NAME)\n": 1,
            "f = lambda: os.path.join(home_dir(), DIR_NAME)\n": 1,
        }
        for src, want in cases.items():
            self.assertEqual(len(python_findings(src)), want, src)

    def test_an_expanded_variable_a_keyword_and_a_module_alias_are_caught(self):
        cases = {
            "p = os.path.join(os.path.expandvars('%USERPROFILE%'), DIR_NAME)\n": 1,
            "p = os.path.join(expandvars('$HOME'), DIR_NAME)\n": 1,
            "def f():\n    h = os.path.expandvars('%USERPROFILE%')\n    return join(h, DIR_NAME)\n": 1,
            "p = os.path.join(paths(home=True), DIR_NAME)\n": 1,
            "import kitlib.config.user as KU\np = os.path.join(home_dir(), KU.DIR_NAME)\n": 1,
            "from kitlib.config import user as KU\np = os.path.join(home_dir(), KU.DIR_NAME)\n": 1,
            "p = os.path.join(os.path.expandvars('%PM_REPO%'), DIR_NAME)\n": 0,
            "p = os.path.join(paths(repo=True), DIR_NAME)\n": 0,
            "import kitlib.config.user as KU\np = os.path.join(home_dir(), KU)\n": 0,
        }
        for src, want in cases.items():
            self.assertEqual(len(python_findings(src)), want, src)

    def test_a_shell_variable_held_from_the_home_folder_is_caught(self):
        self.assertEqual(len(shell_findings("$h = $env:USERPROFILE\n$p = Join-Path $h '.pm-kit'\n")), 1)
        self.assertEqual(len(shell_findings("$h = $PmRepo\n$p = Join-Path $h '.pm-kit'\n")), 0)

    def test_an_allowed_file_is_allowed_its_named_function_only(self):
        rel = 'kitlib/config/user.py'
        text = 'def brand_new():\n    return os.path.join(home_dir(), DIR_NAME)\n'
        self.assertEqual(findings([rel], lambda r: text),
                         ['%s:2: joins the config folder to the home folder (in brand_new)' % rel])


if __name__ == '__main__':
    unittest.main()
