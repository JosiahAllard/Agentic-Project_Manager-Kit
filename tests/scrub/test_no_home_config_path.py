"""No kit code builds a machine-config path in the home folder: the machine config lives in the PM
repository the PM-repo pointer names (kitlib.config.user.locate), so a new home-folder path would
bring back a copy a new user has to make and keep by hand.

It reads the tracked Python and PowerShell files of kitlib/, tools/, boot/, hosts/ and installer/
and refuses, outside ALLOWED:
  - a string (not a docstring) that spells the home-folder config folder: `~/.pm-kit`,
    `%USERPROFILE%\\.pm-kit`, `$env:USERPROFILE\\.pm-kit`, `$HOME\\.pm-kit`;
  - a '~/...' format string filled with the config folder's name (`'~/%s' % user.DIR_NAME`, an
    f-string);
  - a call or a `/` join whose arguments hold the config folder (`'.pm-kit'`, `user.DIR_NAME`,
    `kuser.DIR_NAME`, or a name the module assigns from one, as `FOLDER = user.DIR_NAME`) and whose
    first part comes from the home folder (home, USERPROFILE, expanduser, home_dir, Path.home), in
    its own text or through a local name assigned from it (`h = home_dir(); join(h, DIR_NAME)`);
  - a PowerShell line, not a comment, naming `.pm-kit` with USERPROFILE, HOME or `~`.
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
    ('kitlib/config/user.py', 'DISPLAY'): 'the old home config\'s name, for messages about a '
                                          'leftover',
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
CONFIG_NAMES = ('user', 'kuser')


def _docstrings(tree):
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = n.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _is_config(node, aliases=()):
    """Whether `node` is the config folder's name: the literal, kitlib.config.user.DIR_NAME, or a
    name in `aliases` (one the module assigns from those, as `FOLDER = user.DIR_NAME`)."""
    if isinstance(node, ast.Constant):
        return node.value == CONFIG
    if isinstance(node, ast.Name):
        return node.id == 'DIR_NAME' or node.id in aliases
    return (isinstance(node, ast.Attribute) and node.attr == 'DIR_NAME'
            and isinstance(node.value, ast.Name) and node.value.id in CONFIG_NAMES)


def _holds_config(node, aliases=()):
    return any(_is_config(n, aliases) for n in ast.walk(node))


def _config_aliases(tree):
    """The names a module assigns the config folder's name to, anywhere in it."""
    aliases = set()
    grew = True
    while grew:
        grew = False
        for a in ast.walk(tree):
            if isinstance(a, ast.Assign) and _is_config(a.value, aliases):
                for t in a.targets:
                    if isinstance(t, ast.Name) and t.id not in aliases:
                        aliases.add(t.id)
                        grew = True
    return aliases


def _homeish(src, node, held=()):
    """Whether `node` comes from the home folder: its source names it, or it uses a name `held`
    (a local assigned from the home folder)."""
    if HOMEISH.search(ast.get_source_segment(src, node) or ''):
        return True
    return any(isinstance(n, ast.Name) and n.id in held for n in ast.walk(node))


def _div_parts(node):
    """The operands of a chain of `/` joins, leftmost first."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_parts(node.left) + [node.right]
    return [node]


def _own_nodes(scope):
    """The nodes of a module or function body, without those of the functions and classes
    defined in it."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(n))


def _target_names(targets):
    """The plain names assigned to: `h` and the parts of `a, b`, never `plan` of `plan.home`."""
    out = []
    for t in targets:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            out.extend(_target_names(t.elts))
    return out


def _held_home(src, scope):
    """The names a scope assigns from the home folder (home_dir(), expanduser, USERPROFILE, or a
    name already held), to a fixed point: `h = home_dir(); q = join(h, 'x')` holds both."""
    held = set()
    assigns = [n for n in _own_nodes(scope) if isinstance(n, (ast.Assign, ast.AnnAssign,
                                                                ast.NamedExpr))]
    grew = True
    while grew:
        grew = False
        for a in assigns:
            if a.value is None or not _homeish(src, a.value, held):
                continue
            targets = a.targets if isinstance(a, ast.Assign) else [a.target]
            for n in _target_names(targets):
                if n not in held:
                    held.add(n)
                    grew = True
    return held


def _scopes(tree):
    """[(scope node, its name)]: the module, then every function and class by qualified name."""
    out = [(tree, None)]

    def visit(node, prefix):
        for n in ast.iter_child_nodes(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + n.name
                out.append((n, name))
                visit(n, name + '.')
            else:
                visit(n, prefix)
    visit(tree, '')
    return out


def _module_name(tree, line):
    """The name a module-level statement on `line` assigns (`DISPLAY = ...`), or '<module>'."""
    for st in tree.body:
        if st.lineno <= line <= st.end_lineno:
            targets = st.targets if isinstance(st, ast.Assign) else (
                [st.target] if isinstance(st, ast.AnnAssign) else [])
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                return targets[0].id
    return '<module>'


def _scope_findings(src, scope, docs, aliases):
    held = _held_home(src, scope)
    found = []
    for n in _own_nodes(scope):
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
        elif isinstance(n, ast.Call) and n.args:
            if any(_is_config(a, aliases) for a in n.args[1:]) and _homeish(src, n.args[0], held):
                found.append((n.lineno, 'joins the config folder to the home folder'))
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            parts = _div_parts(n)
            if (len(parts) > 1 and any(_is_config(p, aliases) for p in parts[1:])
                    and _homeish(src, parts[0], held)):
                found.append((n.lineno, 'joins the config folder to the home folder'))
    return found


def python_findings(src):
    """[(line, what, scope)] for one Python file's text. The scope is the qualified name of the
    function or class the line is in, or for a module-level line the name it assigns."""
    tree = ast.parse(src)
    docs = _docstrings(tree)
    aliases = _config_aliases(tree)
    found = set()                     # a chain of `/` joins is one finding, not one per link
    for scope, name in _scopes(tree):
        for line, what in _scope_findings(src, scope, docs, aliases):
            found.add((line, what, name or _module_name(tree, line)))
    return sorted(found)


def shell_findings(text):
    """[(line, what)] for one PowerShell file's text: a line, not a comment, that names the
    config folder with the home folder."""
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith('#'):
            continue
        if SPELLED.search(line) or (CONFIG in line and SHELL_HOME.search(line)):
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

    def test_an_allowed_file_is_allowed_its_named_function_only(self):
        rel = 'kitlib/config/user.py'
        text = 'def brand_new():\n    return os.path.join(home_dir(), DIR_NAME)\n'
        self.assertEqual(findings([rel], lambda r: text),
                         ['%s:2: joins the config folder to the home folder (in brand_new)' % rel])


if __name__ == '__main__':
    unittest.main()
