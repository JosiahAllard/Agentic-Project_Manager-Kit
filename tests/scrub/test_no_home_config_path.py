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
    `kuser.DIR_NAME`) and whose first part comes from the home folder (home, USERPROFILE,
    expanduser, home_dir, Path.home);
  - a PowerShell line, not a comment, naming `.pm-kit` with USERPROFILE, HOME or `~`.
Docstrings and comments may still name the home folder: they describe the move and its leftovers."""
import ast
import re
import unittest

from tests.scrub.test_scrub import read_text, tracked_files

ROOTS = ('kitlib/', 'tools/', 'boot/', 'hosts/', 'installer/')
CONFIG = '.pm-kit'

# path: why it may name the home folder's machine config. Each is a leftover's handling, read or
# guarded, never a place the kit keeps its config. Only files the check finds something in are
# listed (test_every_allowed_file_still_needs_it). The leftover handling that joins a home folder
# held in a short name (`h`) needs no line: kitlib/config/user.py's locate() and home_location(),
# kitlib/config/move.py, kitlib/pmstart/configcheck.py (LEFTOVER), kitlib/machinesetup/links.py.
ALLOWED = {
    'kitlib/config/user.py': 'DISPLAY: the old home config\'s name, for messages about a leftover',
    'kitlib/delegations/load.py': 'delegations_file(): the home folder\'s delegations copy, the '
                                  'move\'s source, which the guard still holds',
    'kitlib/guard/jail.py': 'home_config_dirs(): no write root may be on a leftover home config folder',
    'kitlib/guard/rules.py': 'hub_secret_folders(): guards the leftover home token folder the move '
                             'leaves as its source',
    'kitlib/autotasks/tick.py': 'state_dir(): the auto-tasks state, machine-local; an owner '
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


def _is_config(node):
    """Whether `node` is the config folder's name: the literal or kitlib.config.user.DIR_NAME."""
    if isinstance(node, ast.Constant):
        return node.value == CONFIG
    if isinstance(node, ast.Name):
        return node.id == 'DIR_NAME'
    return (isinstance(node, ast.Attribute) and node.attr == 'DIR_NAME'
            and isinstance(node.value, ast.Name) and node.value.id in CONFIG_NAMES)


def _holds_config(node):
    return any(_is_config(n) for n in ast.walk(node))


def _homeish(src, node):
    return bool(HOMEISH.search(ast.get_source_segment(src, node) or ''))


def _div_parts(node):
    """The operands of a chain of `/` joins, leftmost first."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _div_parts(node.left) + [node.right]
    return [node]


def python_findings(src):
    """[(line, what)] for one Python file's text."""
    tree = ast.parse(src)
    docs = _docstrings(tree)
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            if SPELLED.search(n.value):
                found.append((n.lineno, 'spells the home config folder'))
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod):
            left = n.left
            if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                    and left.value.startswith(('~/', '~\\')) and _holds_config(n.right)):
                found.append((n.lineno, 'formats ~/ with the config folder'))
        elif isinstance(n, ast.JoinedStr):
            head = n.values[0] if n.values else None
            if (isinstance(head, ast.Constant) and isinstance(head.value, str)
                    and head.value.startswith(('~/', '~\\')) and _holds_config(n)):
                found.append((n.lineno, 'formats ~/ with the config folder'))
        elif isinstance(n, ast.Call) and n.args:
            if any(_is_config(a) for a in n.args[1:]) and _homeish(src, n.args[0]):
                found.append((n.lineno, 'joins the config folder to the home folder'))
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            parts = _div_parts(n)
            if len(parts) > 1 and any(_is_config(p) for p in parts[1:]) and _homeish(src, parts[0]):
                found.append((n.lineno, 'joins the config folder to the home folder'))
    return sorted(set(found))         # a chain of `/` joins is one finding, not one per link


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
        if not rel.startswith(ROOTS) or rel in ALLOWED:
            continue
        if rel.endswith('.py'):
            check = python_findings
        elif rel.endswith(('.ps1', '.psm1')):
            check = shell_findings
        else:
            continue
        text = read(rel)
        if text is None:
            continue
        out.extend('%s:%d: %s' % (rel, line, what) for line, what in check(text))
    return out


class NoHomeConfigPathTest(unittest.TestCase):

    def test_no_kit_code_builds_a_home_folder_config_path(self):
        found = findings(tracked_files(), read_text)
        self.assertEqual(found, [], 'kit code names a machine-config path in the home folder; '
                                    'derive it from kitlib.config.user.locate (the PM repository), '
                                    'or add the file to ALLOWED with its reason')

    def test_every_allowed_file_still_needs_it(self):
        stale = [rel for rel in sorted(ALLOWED)
                 if not python_findings(read_text(rel) or '')]
        self.assertEqual(stale, [], 'ALLOWED names a file with nothing found (gone, or its home '
                                    'path removed): drop its line')

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


if __name__ == '__main__':
    unittest.main()
